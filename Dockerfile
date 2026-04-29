FROM osrf/ros:jazzy-desktop

# インタラクティブプロンプトの無効化
ENV DEBIAN_FRONTEND=noninteractive

# 描画スタックおよびnoVNC関連ツールのAPT一括インストール
# ※Ubuntu 24.04 (Noble) 標準パッケージを利用
RUN apt-get update && apt-get install -y \
    tigervnc-standalone-server \
    fluxbox \
    lxterminal \
    novnc \
    websockify \
    net-tools \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# エントリーポイントの配置と権限付与
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ROS 2 Jazzy環境変数の自動ロード設定
RUN echo "source /opt/ros/jazzy/setup.bash" >> /root/.bashrc

ENTRYPOINT ["/entrypoint.sh"]
