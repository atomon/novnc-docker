"""Multi-user Session Manager using Docker Compose and mDNS Aliases."""

import argparse
import os
import socket
import subprocess

INFRA_COMPOSE_FILE = "compose.infra.yaml"
SESSION_COMPOSE_FILE = "compose.session.yaml"
NGINX_CONTAINER = "nginx_proxy"
MDNS_CONTAINER = "avahi_mdns"
INFRA_VERSION = "1"

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


def get_free_session_id() -> int:
    """未使用のSESSION_IDを採番

    Returns:
        利用可能なSESSION_ID

    Raises:
        RuntimeError: SESSION_IDが上限（109）に達した場合
    """
    # DISPLAY番号とポートの衝突を避けるため、10番以降を使用
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
    for i in range(10, 110):
        if i not in used:
            return i
    raise RuntimeError("利用可能なSESSION_IDが上限に達しました。")


def ensure_infra() -> None:
    """インフラコンテナ（Nginx・Avahi）が正常稼働していなければ起動。バージョン不一致は警告して再起動"""
    try:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                '{{index .Config.Labels "novnc.infra.version"}}\t{{.State.Running}}',
                NGINX_CONTAINER,
                MDNS_CONTAINER,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = [line.strip() for line in result.stdout.strip().splitlines() if line]
        if len(lines) == 2 and all(
            ver == INFRA_VERSION and running == "true"
            for ver, running in (line.split("\t") for line in lines)
        ):
            return
        print(
            "[!] Warning: 既存のインフラコンテナが検出されましたが、バージョンが一致しません。再起動します。"
        )
    except subprocess.CalledProcessError:
        pass
    subprocess.run(
        ["docker", "compose", "-f", INFRA_COMPOSE_FILE, "up", "--wait"],
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
    port = 6080 + session_id
    conf_name = f"session_{session_id}_{container_name}.conf"
    conf_content = f"""
server {{
    listen 80;
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


def publish_mdns_alias(container_name: str) -> None:
    """AvahiコンテナでmDNSエイリアスをLAN内に公開

    Args:
        container_name: 公開するホスト名（.localサフィックスなし）
    """
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
    """AvahiコンテナのmDNS公開プロセスを停止

    Args:
        container_name: 停止するホスト名（.localサフィックスなし）
    """
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
        ["docker", "compose", "-f", SESSION_COMPOSE_FILE, "-p", container_name, *args],
        check=True,
        **kwargs,
    )


def start_session(container_name: str, session_type: str = "ros2") -> None:
    """セッションを起動（インフラ確認・コンテナ起動・Nginx設定・mDNS登録）

    Args:
        container_name: 起動するセッションのコンテナ名
        session_type: セッション種別（"ros2" または "linux"）
    """
    ensure_infra()

    session_id = get_free_session_id()
    config = SESSION_TYPES[session_type]
    env = {
        **os.environ,
        "CONTAINER_NAME": container_name,
        "SESSION_ID": str(session_id),
        "DOCKERFILE": config["dockerfile"],
        "IMAGE_NAME": config["image"],
    }

    print(f"[*] セッション起動中: {container_name} (ID: {session_id})...")
    # プロジェクト名をコンテナ名に指定し、独立したComposeスタックとして起動
    session_compose(container_name, "up", "-d", env=env)
    apply_nginx_conf(session_id, container_name)
    publish_mdns_alias(container_name)
    print(f"[*] 準備完了: http://{container_name}.local")


def stop_session(container_name: str) -> None:
    """セッションを完全停止してリソースをクリーンアップ

    Args:
        container_name: 停止するセッションのコンテナ名
    """
    print(f"[*] セッション停止中: {container_name}...")
    unpublish_mdns_alias(container_name)
    session_compose(container_name, "down", stderr=subprocess.DEVNULL)
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
    print("[*] クリーンアップ完了。")


def main() -> None:
    """CLIエントリーポイント"""
    parser = argparse.ArgumentParser(description="Multi-user Session Manager")
    parser.add_argument("action", choices=["start", "stop"])
    parser.add_argument("container_name")
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


if __name__ == "__main__":
    main()
