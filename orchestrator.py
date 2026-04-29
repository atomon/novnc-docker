"""Multi-user Session Manager using Docker Compose and mDNS Aliases."""

import argparse
import json
import os
import platform
import socket
import subprocess

INFRA_COMPOSE_FILE = "compose.infra.yaml"
INFRA_PROJECT = "novnc_infra"
SESSION_COMPOSE_FILE = "compose.session.yaml"
NGINX_CONTAINER = "nginx_proxy"
MDNS_CONTAINER = "avahi_mdns"
INFRA_VERSION = "1"
NGINX_PORT = 80
NOVNC_BASE_PORT = 6080
SESSION_ID_MIN = 10

IS_MACOS = platform.system() == "Darwin"
COMPOSE_PROFILE = "macos" if IS_MACOS else "linux"

SESSION_TYPES: dict[str, dict[str, str]] = {
    "ros2": {"dockerfile": "Dockerfile.ros2", "image": "ros2_novnc_desktop:jazzy"},
    "linux": {"dockerfile": "Dockerfile.linux", "image": "linux_novnc_desktop:latest"},
}


def get_host_ip() -> str:
    """ホストマシンのプライマリIPアドレスを取得

    Returns:
        ホストのIPアドレス。取得失敗時は "127.0.0.1"
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _get_session_id_from_label(container_name: str) -> int | None:
    """コンテナラベルから SESSION_ID を取得

    Args:
        container_name: 検査対象のコンテナ名

    Returns:
        SESSION_ID。取得失敗時は None
    """
    try:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "novnc.session.id"}}',
                container_name,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        val = result.stdout.strip()
        return int(val) if val.isdigit() else None
    except (subprocess.CalledProcessError, ValueError):
        return None


def session_url(container_name: str, session_id: int | None = None) -> str:
    """セッションのアクセス URL を生成

    Args:
        container_name: セッションのコンテナ名
        session_id: セッションID。None の場合はラベルから取得

    Returns:
        macOS: localhost の noVNC 直接 URL、Linux: mDNS ホスト名 URL
    """
    if IS_MACOS:
        if session_id is None:
            session_id = _get_session_id_from_label(container_name) or SESSION_ID_MIN
        port = NOVNC_BASE_PORT + session_id
        return f"http://localhost:{port}/vnc.html?autoconnect=true"
    host = f"{container_name}.local"
    return f"http://{host}" if NGINX_PORT == 80 else f"http://{host}:{NGINX_PORT}"


def get_free_session_id() -> int:
    """未使用のSESSION_IDを採番

    Returns:
        利用可能なSESSION_ID

    Raises:
        RuntimeError: SESSION_IDが上限（109）に達した場合
    """
    if IS_MACOS:
        res = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                "label=novnc.session.id",
                "--format",
                '{{.Label "novnc.session.id"}}',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        used = {int(x) for x in res.stdout.splitlines() if x.strip().isdigit()}
    else:
        try:
            res = subprocess.run(
                [
                    "docker",
                    "exec",
                    NGINX_CONTAINER,
                    "sh",
                    "-c",
                    "ls /etc/nginx/conf.d/session_*.conf",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            used = {
                int(line.split("/")[-1].split("_")[1])
                for line in res.stdout.strip().splitlines()
                if line
            }
        except subprocess.CalledProcessError:
            used = set()
    for i in range(SESSION_ID_MIN, SESSION_ID_MIN + 100):
        if i not in used:
            return i
    raise RuntimeError("利用可能なSESSION_IDが上限に達しました。")


def ensure_infra() -> None:
    """インフラコンテナ（Nginx・Avahi）が正常稼働していなければ起動。バージョン不一致はエラー"""
    if IS_MACOS:
        return
    try:
        result = subprocess.run(
            ["docker", "inspect", NGINX_CONTAINER, MDNS_CONTAINER],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        pass  # コンテナが存在しない → compose up へ
    else:
        containers: list[dict] = json.loads(result.stdout)
        if len(containers) == 2 and all(
            c["State"]["Running"]
            and c.get("Config", {}).get("Labels", {}).get("novnc.infra.version")
            == INFRA_VERSION
            for c in containers
        ):
            return
        raise RuntimeError(
            f"既存のインフラコンテナのバージョンが一致しません。先に停止してください: "
            f"docker compose -f {INFRA_COMPOSE_FILE} down"
        )
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            INFRA_COMPOSE_FILE,
            "-p",
            INFRA_PROJECT,
            "up",
            "--wait",
        ],
        env={**os.environ, "INFRA_VERSION": INFRA_VERSION},
        check=True,
    )


def nginx_reload() -> None:
    """Nginxの設定をリロード"""
    subprocess.run(
        ["docker", "exec", NGINX_CONTAINER, "nginx", "-s", "reload"], check=True
    )


def apply_nginx_conf(session_id: int, container_name: str) -> None:
    """NginxにHTTPリバースプロキシ設定を注入してリロード

    Args:
        session_id: セッションの識別番号
        container_name: コンテナ名（server_nameに使用）
    """
    if IS_MACOS:
        return
    port = NOVNC_BASE_PORT + session_id
    conf_name = f"session_{session_id}_{container_name}.conf"
    conf_content = f"""
server {{
    listen {NGINX_PORT};
    server_name {container_name}.local;

    location = / {{
        return 301 http://$host/vnc.html?autoconnect=true;
    }}

    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }}
}}
"""
    subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            NGINX_CONTAINER,
            "sh",
            "-c",
            f"cat > /etc/nginx/conf.d/{conf_name}",
        ],
        input=conf_content.encode(),
        check=True,
    )
    nginx_reload()


def remove_nginx_conf(container_name: str) -> None:
    """Nginxのセッション設定を削除してリロード（Linux のみ）

    Args:
        container_name: 削除対象のコンテナ名
    """
    if IS_MACOS:
        return
    subprocess.run(
        [
            "docker",
            "exec",
            NGINX_CONTAINER,
            "sh",
            "-c",
            f"rm -f /etc/nginx/conf.d/session_*_{container_name}.conf",
        ],
        check=True,
    )
    nginx_reload()


def publish_mdns_alias(container_name: str) -> None:
    """AvahiコンテナでmDNSエイリアスをLAN内に公開（Linux のみ）

    Args:
        container_name: 公開するホスト名（.localサフィックスなし）
    """
    if IS_MACOS:
        return
    ip = get_host_ip()
    alias = f"{container_name}.local"
    # avahi-publish-addressをデタッチドモードで実行（エイリアス維持のため）
    subprocess.run(
        [
            "docker",
            "exec",
            "-d",
            MDNS_CONTAINER,
            "avahi-publish-address",
            "-R",
            alias,
            ip,
        ],
        check=True,
    )
    print(f"[*] mDNS公開完了: {alias} -> {ip}")


def unpublish_mdns_alias(container_name: str) -> None:
    """AvahiコンテナのmDNS公開プロセスを停止（Linux のみ）

    Args:
        container_name: 停止するホスト名（.localサフィックスなし）
    """
    if IS_MACOS:
        return
    alias = f"{container_name}.local"
    subprocess.run(
        [
            "docker",
            "exec",
            MDNS_CONTAINER,
            "pkill",
            "-f",
            f"avahi-publish-address -R {alias}",
        ],
        stderr=subprocess.DEVNULL,
    )


def session_compose(container_name: str, *args: str, **kwargs) -> None:
    """セッションコンテナに対してdocker composeコマンドを実行

    Args:
        container_name: Composeプロジェクト名兼コンテナ名
        *args: docker composeに渡すサブコマンドと引数
        **kwargs: subprocess.runに渡す追加引数
    """
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            SESSION_COMPOSE_FILE,
            "-p",
            container_name,
            "--profile",
            COMPOSE_PROFILE,
            *args,
        ],
        check=True,
        **kwargs,
    )


def is_session_running(container_name: str) -> bool:
    """セッションコンテナの稼働状態を確認

    Args:
        container_name: 確認対象のコンテナ名

    Returns:
        コンテナが存在かつ running 状態であれば True
    """
    try:
        result = subprocess.run(
            ["docker", "inspect", container_name],
            capture_output=True,
            text=True,
            check=True,
        )
        containers: list[dict] = json.loads(result.stdout)
        return bool(containers) and containers[0]["State"]["Running"]
    except subprocess.CalledProcessError:
        return False


def list_sessions() -> None:
    """起動中のセッション一覧を表示"""
    subprocess.run(["docker", "compose", "ls"], check=False)


def start_session(container_name: str, session_type: str = "ros2") -> None:
    """セッションを起動（インフラ確認・コンテナ起動・Nginx設定・mDNS登録）

    Args:
        container_name: 起動するセッションのコンテナ名
        session_type: セッション種別（"ros2" または "linux"）
    """
    if is_session_running(container_name):
        print(
            f"[*] {container_name} はすでに起動しています。: {session_url(container_name)}"
        )
        return

    ensure_infra()

    session_id = get_free_session_id()
    config = SESSION_TYPES[session_type]
    env = {
        **os.environ,
        "CONTAINER_NAME": container_name,
        "SESSION_ID": str(session_id),
        "DOCKERFILE": config["dockerfile"],
        "IMAGE_NAME": config["image"],
        "NOVNC_PORT": str(NOVNC_BASE_PORT + session_id),
    }

    print(f"[*] セッション起動中: {container_name} (ID: {session_id})...")
    # プロジェクト名をコンテナ名に指定し、独立したComposeスタックとして起動
    session_compose(container_name, "up", "-d", env=env)
    apply_nginx_conf(session_id, container_name)
    publish_mdns_alias(container_name)
    print(f"[*] 準備完了: {session_url(container_name, session_id)}")


def stop_session(container_name: str) -> None:
    """セッションを完全停止してリソースをクリーンアップ

    Args:
        container_name: 停止するセッションのコンテナ名
    """
    print(f"[*] セッション停止中: {container_name}...")
    unpublish_mdns_alias(container_name)
    session_compose(container_name, "down", stderr=subprocess.DEVNULL)
    remove_nginx_conf(container_name)
    print("[*] クリーンアップ完了。")


def main() -> None:
    """CLIエントリーポイント"""
    parser = argparse.ArgumentParser(description="Multi-user Session Manager")
    parser.add_argument("action", choices=["start", "stop", "list"])
    parser.add_argument("container_name", nargs="?")
    parser.add_argument(
        "--type",
        choices=list(SESSION_TYPES),
        default="ros2",
        dest="session_type",
    )
    args = parser.parse_args()

    if args.action == "start":
        start_session(args.container_name, args.session_type)
    elif args.action == "stop":
        stop_session(args.container_name)
    elif args.action == "list":
        list_sessions()


if __name__ == "__main__":
    main()
