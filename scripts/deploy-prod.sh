#!/usr/bin/env bash
# SeedAI 生产部署脚本(基于 docker-compose 全套编排)
#
# 为什么用 docker compose 全套而不是裸跑:
#   - 数据栈 redis/mysql/chroma 由 compose 自动拉起(本次线上 500 的根因就是
#     Redis 没起,AI 核心 /generate 直接 500)。
#   - 前端用 nginx 托管生产 build 产物(不再是用 `vite dev` 当生产用)。
#   - 后端 uvicorn 以 production 模式运行(无 --reload)。
#   - 已含 v0.3.1 的 SSE 错误帧修复: 上游 5xx 会显示明确提示而非 Internal Server Error。
#
# 用法:
#   ./scripts/deploy-prod.sh            # 默认: 拉最新代码 + 构建并起全套
#   ./scripts/deploy-prod.sh data       # 仅起数据栈(redis/mysql/chroma),不动前后端
#   ./scripts/deploy-prod.sh down       # 停掉全部 compose 管理的容器(volume 数据保留)
#   ./scripts/deploy-prod.sh logs       # 跟踪查看各服务日志
#   ./scripts/deploy-prod.sh status     # 查看服务状态与健康端口
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ---------- 0. 前置检查 ----------
command -v docker >/dev/null 2>&1 || { echo "✗ 未检测到 docker,请先安装 Docker Engine"; exit 1; }
if ! docker compose version >/dev/null 2>&1; then
  echo "✗ docker compose 插件不可用(需 Docker 20.10+ 自带 compose v2)"
  exit 1
fi
if [ "${1:-all}" != "down" ]; then
  [ -f .env ] || { echo "✗ 缺少 .env(从 .env.example 复制并填写真实密钥/连接串)"; exit 1; }
fi

MODE="${1:-all}"

# ---------- 1. 拉最新代码(含 v0.3.1 修复) ----------
if [ "$MODE" != "data" ] && [ "$MODE" != "down" ] && [ "$MODE" != "logs" ] && [ "$MODE" != "status" ]; then
  echo "==> 拉取最新代码 (含 v0.3.1 SSE 错误帧修复)"
  git pull --ff-only
fi

# ---------- 2. 起栈 ----------
case "$MODE" in
  data)
    echo "==> 仅起数据栈 redis/mysql/chroma"
    docker compose up -d redis mysql chroma
    ;;
  down)
    echo "==> 停止全部 compose 容器(volume 数据保留)"
    docker compose down
    exit 0
    ;;
  logs)
    docker compose logs -f --tail=150
    exit 0
    ;;
  status)
    docker compose ps
    echo "--- 端口探测 ---"
    curl -s -o /dev/null -w "  business /health        = %{http_code}\n" "http://localhost:7101/health" || true
    curl -s -o /dev/null -w "  business /api/chat(无登录)= %{http_code}\n" "http://localhost:7101/api/chat?conversation_id=1&messages=x" || true
    exit 0
    ;;
  *)
    echo "==> 构建并起全套(数据栈 + 业务 + AI + 前端)"
    echo "    ⚠ 若 7100/7101/7102 被裸跑的 vite/uvicorn 占用,请先停掉避免端口冲突:"
    echo "       pkill -f 'vite' ; pkill -f 'uvicorn app.main:app'"
    docker compose up -d --build
    ;;
esac

# ---------- 3. 等待业务就绪(数据栈 mysql 初始化较慢) ----------
echo "==> 等待业务 /health 就绪(最多 ~90s)..."
for i in $(seq 1 45); do
  if curl -fsS -o /dev/null "http://localhost:7101/health" 2>/dev/null; then
    echo "    business 已就绪 (${i}x2s)"; break
  fi
  sleep 2
done

# ---------- 4. 验证 ----------
echo "==> 验证端点"
curl -s -o /dev/null -w "  business /health              = %{http_code}\n" "http://localhost:7101/health"
curl -s -o /dev/null -w "  business /api/chat(无登录)    = %{http_code}\n" "http://localhost:7101/api/chat?conversation_id=1&messages=x"
echo "    · /api/chat(无登录) 返回 401 且 body 含 'Missing authentication' => 旧代码仍在"
echo "    · 返回 200 且为 SSE error 帧(code=AUTH_REQUIRED)        => v0.3.1 已生效"
echo "  AI 核心 /generate 连通性探测(会入队一条测试任务,可忽略):"
curl -s -o /dev/null -w "  ai /generate                  = %{http_code}\n" \
  -X POST "http://localhost:7102/generate" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hi"}]}' || true
echo "    · /generate 返回 200/202 => Redis 已连通; 仍 500 => 数据栈未就绪,稍候重跑 ./deploy-prod.sh status"

echo ""
echo "==> 部署完成。访问 http://<your-domain>:7100"
echo "    查看日志: ./scripts/deploy-prod.sh logs"
echo "    仅重启数据栈: ./scripts/deploy-prod.sh data"
