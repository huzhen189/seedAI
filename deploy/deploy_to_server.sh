#!/usr/bin/env bash
# SeedAI 一键上云部署脚本(CVM 复用外部 MySQL/Redis/Chroma)
#
# 前置(本机):
#   - 已配置好到 CVM 的 SSH 访问(密钥或密码)
#   - 改下面 REMOTE_HOST / REMOTE_USER / REMOTE_DIR
#
# 用法:
#   bash deploy/deploy_to_server.sh            # 全量: 同步代码 + 重建 + 起服务
#   bash deploy/deploy_to_server.sh init       # 同步后跑 reset_all 初始化(建表+超管)
#   bash deploy/deploy_to_server.sh logs       # 跟日志
#   bash deploy/deploy_to_server.sh status     # 健康检查
#
# 注意: 脚本走 rsync 同步(已忽略 node_modules/.git/种子数据);首次需确保远端装好 docker。

set -euo pipefail

# ---------- 配置区(按需修改) ----------
REMOTE_HOST="1.12.219.195"      # CVM 公网 IP
REMOTE_PORT="22"
REMOTE_USER="root"              # 或你的 sudo 用户
REMOTE_DIR="/opt/seedai"        # 远端项目目录
SSH_KEY=""                      # 如有专门 key 填路径, 如 "$HOME/.ssh/id_rsa"; 留空用 agent/默认
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# ---------------------------------------

SSH_OPTS=(-p "$REMOTE_PORT" -o StrictHostKeyChecking=accept-new -o BatchMode=yes)
if [[ -n "$SSH_KEY" ]]; then SSH_OPTS+=(-i "$SSH_KEY"); fi
SSH="ssh ${SSH_OPTS[*]} ${REMOTE_USER}@${REMOTE_HOST}"
RSYNC="rsync -az --delete --exclude node_modules --exclude .git --exclude __pycache__ --exclude '*.pyc' --exclude seedai.db --exclude artifacts --exclude logs ${SSH_OPTS[*]}"

cmd="${1:-full}"

case "$cmd" in
  full|init)
    echo ">>> [1/4] 同步代码到 ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}"
    $RSYNC "${LOCAL_DIR}/" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"

    echo ">>> [2/4] 远端装 docker(若未装)并启动 compose"
    $SSH bash -s <<'REMOTE'
      set -e
      if ! command -v docker >/dev/null; then
        echo "安装 docker..."
        curl -fsSL https://get.daocloud.io/docker | sh || curl -fsSL https://get.docker.com | sh
        systemctl enable docker && systemctl start docker
      fi
      if ! docker compose version >/dev/null 2>&1; then
        echo "安装 docker-compose-plugin..."
        apt-get update && apt-get install -y docker-compose-plugin || true
      fi
      cd /opt/seedai
      docker compose -f docker-compose.prod.yml --env-file .env.production pull || true
      docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
REMOTE

    echo ">>> [3/4] 等后端 healthcheck"
    sleep 20
    $SSH "curl -fsS http://127.0.0.1:7101/ready && echo ' BACKEND READY' || echo ' BACKEND NOT READY'"

    if [[ "$cmd" == "init" ]]; then
      echo ">>> [4/4] 初始化数据库(建表 + 超管 huzhen/huzhen189)"
      $SSH "cd /opt/seedai && docker compose -f docker-compose.prod.yml exec -T backend python scripts/reset_all.py"
    else
      echo ">>> [4/4] 跳过数据库初始化(已有数据)。如需重建加 init 参数。"
    fi
    echo ">>> 完成。访问 http://${REMOTE_HOST}:7100"
    ;;
  logs)
    $SSH "cd /opt/seedai && docker compose -f docker-compose.prod.yml logs -f --tail=100"
    ;;
  status)
    $SSH "cd /opt/seedai && docker compose -f docker-compose.prod.yml ps && echo '---' && curl -fsS http://127.0.0.1:7101/ready && echo ' READY' || echo ' NOT READY'"
    ;;
  *)
    echo "用法: $0 [full|init|logs|status]"; exit 1;;
esac
