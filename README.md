# novnc-docker

ブラウザから ROS 2 Jazzy デスクトップ環境にアクセスできる、マルチユーザー対応のセッション管理システムです。  
Docker + noVNC + Nginx + mDNS (Avahi) を組み合わせて、`http://<名前>.local` という URL でセッションに接続できます。

## アーキテクチャ

```mermaid
flowchart TB
    Browser["ブラウザ\nhttp://alice.local"]

    subgraph infra["compose.infra.yaml（共有インフラ）"]
        Nginx["Nginx\nリバースプロキシ\nWebSocket 対応"]
        Avahi["Avahi\nmDNS 公開\nalice.local → ホスト IP"]
    end

    subgraph session["compose.session.yaml（セッションコンテナ）"]
        noVNC["noVNC\nポート 6080 + SESSION_ID"]
        Xvnc["Xvnc\nポート 5900 + SESSION_ID"]
        Desktop["Fluxbox + lxterminal"]
    end

    Browser -->|"mDNS 名前解決"| Avahi
    Browser -->|"HTTP / WebSocket"| Nginx
    Nginx -->|"プロキシ"| noVNC
    noVNC -->|"VNC"| Xvnc
    Xvnc --> Desktop
```

### コンポーネント

| コンポーネント | 役割 |
|---|---|
| `compose.infra.yaml` | 共有インフラ（Nginx・Avahi）。全セッションで 1 つだけ起動 |
| `compose.session.yaml` | ROS 2 セッション。ユーザーごとに独立したスタックとして起動 |
| `Dockerfile` | ROS 2 Jazzy + TigerVNC + noVNC + Fluxbox の環境イメージ |
| `entrypoint.sh` | SESSION_ID に基づきポートを割り当てて VNC/noVNC を起動 |
| `orchestrator.py` | セッションの起動・停止・Nginx 設定・mDNS 登録を自動化 |

## 前提条件

- Docker（Compose プラグイン含む）
- Python 3.12 以上
- LAN 内の他マシンから接続する場合は mDNS（Bonjour/Avahi）が利用可能な環境

## インストール

```bash
git clone https://github.com/atomon/novnc-docker
cd novnc-docker
```

## 使い方

### セッションの起動

```bash
python orchestrator.py start alice
```

起動後、同一 LAN 内のブラウザから `http://alice.local` でアクセスできます。  
VNC パスワードは不要です（`SecurityTypes None`）。

### セッションの停止

```bash
python orchestrator.py stop alice
```

コンテナの削除・Nginx 設定の削除・mDNS の登録解除を一括で行います。

## ポート割り当て

SESSION_ID は 10 から自動採番されます（最大 100 セッション）。

| 用途 | ポート |
|---|---|
| VNC | `5900 + SESSION_ID` |
| noVNC (WebSocket) | `6080 + SESSION_ID` |

同一ホスト上でセッションが増えても衝突しません。

## 環境変数

`compose.session.yaml` で参照される変数（`orchestrator.py` が自動で設定します）。

| 変数 | 説明 |
|---|---|
| `SESSION_ID` | セッションの識別番号（10〜109） |
| `CONTAINER_NAME` | コンテナ名兼 mDNS ホスト名 |
| `ROS_DOMAIN_ID` | ROS 2 のドメイン ID（デフォルト: 0） |

## ベースイメージ

[osrf/ros:jazzy-desktop](https://hub.docker.com/r/osrf/ros/) をベースに以下を追加しています。

- `tigervnc-standalone-server`
- `fluxbox`（ウィンドウマネージャ）
- `lxterminal`
- `novnc` / `websockify`

## ライセンス

[LICENSE](./LICENSE) を参照してください。
