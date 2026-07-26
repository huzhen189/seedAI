#!/usr/env bash
# 本地开发一键启动(无需 docker): 杀掉占用端口的旧进程 -> 清 pycache -> 起单进程后端。
#
# 2026-07-26 架构变更: 业务服务 + AI 核心已合并为【单进程 FastAPI】(backend/app,
# uvicorn app.main:app,监听 7101)。不再有独立的 7102 AI 服务,旧 start-ai.bat 已删除。
# 前端仍在 :7100,通过 vite proxy 把 /api 打到 127.0.0.1:7101(反向代理无需改动)。
#
# 用法: ./scripts/start-local.sh
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY_BIZ="/c/Users/zhenhu/.workbuddy/binaries/python/envs/seedai-biz/Scripts/python.exe"

echo "==> 释放 7101 端口(按端口循环强杀) ..."
for port in 7101; do
  for i in $(seq 1 6); do
    pid=$(netstat -ano 2>/dev/null | grep ":$port " | grep LISTENING | awk '{print $NF}' | head -1)
    [ -z "$pid" ] && break
    cmd.exe /c "taskkill /PID $pid /F" >/dev/null 2>&1 || true
    sleep 1
  done
done

echo "==> 清空 backend pycache ..."
find backend -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

echo "==> 起重单进程后端 (7101) ..."
nohup "$PY_BIZ" -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 7101 --log-level info > /tmp/seedai_7101.log 2>&1 &
echo "    后端 PID=$!"

sleep 10
echo "==> 健康检查 ..."
curl -s -o /dev/null -w "    后端 /health = %{http_code}\n" --max-time 6 http://127.0.0.1:7101/health

echo ""
echo "==> 完成。后端日志: /tmp/seedai_7101.log"
echo "==> 前端另开终端: cd frontend && npm run dev (默认 :7100,/api 已代理到 7101)"
