#!/bin/bash
set -euo pipefail

# SESSION_IDのバリデーション
if [ -z "$SESSION_ID" ]; then
    echo "Error: SESSION_ID is required."
    exit 1
fi

# CONTAINER_NAMEのバリデーション
if [ -z "$CONTAINER_NAME" ]; then
    echo "Error: CONTAINER_NAME is required."
    exit 1
fi

VNC_PORT=$((5900 + SESSION_ID))
NOVNC_PORT=$((6080 + SESSION_ID))
DISPLAY_NUM=":${SESSION_ID}"

# 古いロックファイルの削除
rm -f /tmp/.X${SESSION_ID}-lock /tmp/.X11-unix/X${SESSION_ID}

# Xvncの起動
Xvnc $DISPLAY_NUM -geometry 1920x1080 -depth 24 -SecurityTypes None -desktop "${CONTAINER_NAME}" &

# Xvncのソケットファイルが生成されるまで待機（最大10秒）
for i in $(seq 1 10); do
    [ -S "/tmp/.X11-unix/X${SESSION_ID}" ] && break
    sleep 1
done

export DISPLAY=$DISPLAY_NUM

# ウィンドウマネージャとターミナルの起動
lxterminal &
fluxbox &

# noVNCプロキシのパスを特定
NOVNC_EXEC=$(command -v novnc_proxy)

if [ -z "$NOVNC_EXEC" ]; then
    echo "Error: novnc_proxy not found. Please check if 'novnc' package is installed."
    exit 1
fi

echo "Starting noVNC: $NOVNC_EXEC --vnc localhost:$VNC_PORT --listen $NOVNC_PORT"
exec "$NOVNC_EXEC" --vnc localhost:$VNC_PORT --listen $NOVNC_PORT
