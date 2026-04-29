#!/bin/bash
set -e

# SESSION_IDのバリデーション
if [ -z "$SESSION_ID" ]; then
    echo "Error: SESSION_ID is required."
    exit 1
fi

VNC_PORT=$((5900 + SESSION_ID))
NOVNC_PORT=$((6080 + SESSION_ID))
DISPLAY_NUM=":${SESSION_ID}"

# 古いロックファイルの削除
rm -f /tmp/.X${SESSION_ID}-lock /tmp/.X11-unix/X${SESSION_ID}

# Xvncの起動
Xvnc $DISPLAY_NUM -geometry 1920x1080 -depth 24 -SecurityTypes None -desktop "${CONTAINER_NAME}" &
sleep 2

export DISPLAY=$DISPLAY_NUM

# ウィンドウマネージャとターミナルの起動
lxterminal &
fluxbox &

# noVNCプロキシのパスを動的に特定
if [ -f "/usr/bin/novnc_proxy" ]; then
    NOVNC_EXEC="/usr/bin/novnc_proxy"
elif [ -f "/usr/share/novnc/utils/novnc_proxy" ]; then
    NOVNC_EXEC="/usr/share/novnc/utils/novnc_proxy"
else
    # パッケージ内を検索
    NOVNC_EXEC=$(which novnc_proxy || find /usr -name novnc_proxy | head -n 1)
fi

if [ -z "$NOVNC_EXEC" ]; then
    echo "Error: novnc_proxy not found. Please check if 'novnc' package is installed."
    exit 1
fi

echo "Starting noVNC: $NOVNC_EXEC --vnc localhost:$VNC_PORT --listen $NOVNC_PORT"
exec $NOVNC_EXEC --vnc localhost:$VNC_PORT --listen $NOVNC_PORT
