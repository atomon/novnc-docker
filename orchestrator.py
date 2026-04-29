"""Multi-user ROS 2 Session Manager using Docker Compose and mDNS Aliases.

This script manages shared infrastructure (Nginx/mDNS) and individual ROS 2 sessions.
It dynamically assigns SESSION_IDs and registers .local hostnames for each session.
"""

import argparse
import os
import subprocess
import socket
from typing import List

# 設定定数
INFRA_COMPOSE_FILE = "compose.infra.yaml"
SESSION_COMPOSE_FILE = "compose.session.yaml"
NGINX_CONTAINER = "nginx_proxy"
MDNS_CONTAINER = "avahi_mdns"


def get_host_ip() -> str:
    """ホストマシンの物理IPアドレスを取得する。

    Returns:
        str: ホストのプライマリIPアドレス。
    """
    try:
        # ホストネットワークモードなので、物理ネットワークのIPを取得
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_used_session_ids() -> List[int]:
    """Nginxの設定ファイルから使用中のSESSION_IDを一覧取得する。

    Returns:
        List[int]: 使用中のIDリスト。
    """
    used_ids: List[int] = []
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
        for line in res.stdout.strip().split("\n"):
            if not line:
                continue
            filename = line.split("/")[-1]
            session_id = int(filename.split("_")[1])
            used_ids.append(session_id)
    except subprocess.CalledProcessError:
        pass
    return used_ids


# DISPLAY番号とポートの衝突を避けるため、10番以降を使用する
def get_free_session_id() -> int:
    used_ids = set(get_used_session_ids())
    for i in range(10, 110):
        if i not in used_ids:
            return i
    raise RuntimeError("利用可能なSESSION_IDが上限に達しました。")


def apply_nginx_conf(session_id: int, container_name: str) -> None:
    """Injects Nginx configuration for HTTP proxy.

    Args:
        session_id: Allocated unique ID for the session.
        container_name: Name of the target container.
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
    write_cmd = [
        "docker",
        "exec",
        "-i",
        "nginx_proxy",
        "sh",
        "-c",
        f"cat > /etc/nginx/conf.d/{conf_name}",
    ]
    subprocess.run(write_cmd, input=conf_content.encode("utf-8"), check=True)
    subprocess.run(
        ["docker", "exec", "nginx_proxy", "nginx", "-s", "reload"], check=True
    )


def publish_mdns_alias(container_name: str):
    """Avahiコンテナ内でエイリアス（.local）をLAN内に公開する。"""
    ip = get_host_ip()
    alias = f"{container_name}.local"
    # avahi-publish-addressをデタッチドモードで実行（エイリアス維持のため）
    cmd = [
        "docker",
        "exec",
        "-d",
        MDNS_CONTAINER,
        "avahi-publish-address",
        "-R",
        alias,
        ip,
    ]
    subprocess.run(cmd, check=True)
    print(f"[*] mDNS公開完了: {alias} -> {ip}")


def unpublish_mdns_alias(container_name: str):
    """終了時に該当するmDNSエイリアスの公開プロセスを停止する。"""
    alias = f"{container_name}.local"
    cmd = [
        "docker",
        "exec",
        MDNS_CONTAINER,
        "pkill",
        "-f",
        f"avahi-publish-address -R {alias}",
    ]
    subprocess.run(cmd, stderr=subprocess.DEVNULL)


def start_session(container_name: str):
    """セッションの開始（インフラ確認、コンテナ起動、Nginx、mDNS）"""
    # インフラが死んでいる場合は起動
    subprocess.run(
        ["docker", "compose", "-f", INFRA_COMPOSE_FILE, "up", "--wait"],
        check=True,
    )

    session_id = get_free_session_id()

    # 実行環境変数の準備
    env = os.environ.copy()
    env["CONTAINER_NAME"] = container_name
    env["SESSION_ID"] = str(session_id)

    print(f"[*] セッション起動中: {container_name} (ID: {session_id})...")

    # プロジェクト名をコンテナ名に指定し、独立したComposeスタックとして起動
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            SESSION_COMPOSE_FILE,
            "-p",
            container_name,
            "up",
            "-d",
        ],
        env=env,
        check=True,
    )

    # ルーティングとmDNSの設定
    apply_nginx_conf(session_id, container_name)
    publish_mdns_alias(container_name)

    print(f"[*] 準備完了: http://{container_name}.local")


def stop_session(container_name: str):
    """セッションの完全停止とクリーンアップ。"""
    env = os.environ.copy()
    env["CONTAINER_NAME"] = container_name
    env["SESSION_ID"] = "0"

    print(f"[*] セッション停止中: {container_name}...")

    # mDNS解除
    unpublish_mdns_alias(container_name)

    # Composeスタック削除
    subprocess.run(
        ["docker", "compose", "-f", SESSION_COMPOSE_FILE, "-p", container_name, "down"],
        env=env,
        check=True,
        stderr=subprocess.DEVNULL,
    )

    # Nginx設定削除
    rm_cmd = [
        "docker",
        "exec",
        NGINX_CONTAINER,
        "sh",
        "-c",
        f"rm -f /etc/nginx/conf.d/session_*_{container_name}.conf",
    ]
    subprocess.run(rm_cmd, check=True)
    subprocess.run(
        ["docker", "exec", NGINX_CONTAINER, "nginx", "-s", "reload"], check=True
    )

    print("[*] クリーンアップ完了。")


def main():
    parser = argparse.ArgumentParser(description="Multi-user ROS 2 Session Manager")
    parser.add_argument("action", choices=["start", "stop"], help="Action to perform")
    parser.add_argument(
        "container_name", type=str, help="Name of the session container"
    )

    args = parser.parse_args()

    if args.action == "start":
        start_session(args.container_name)
    elif args.action == "stop":
        stop_session(args.container_name)


if __name__ == "__main__":
    main()
