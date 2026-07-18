#!/usr/bin/env bash
# 本地开发一键启动(无需 docker): 杀掉占用端口的旧进程 -> 清 pycache -> 起重 AI + 业务
# 业务连 .env 里那套统一云库(MySQL/Redis/Chroma), AI 同理。
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY_BIZ="/c/Users/zhenhu/.workbuddy/binaries/python/envs/seedai-biz/Scripts/python.exe"
PY_AI="/c/Users/zhenhu/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

echo "==> 释放 7101/7102 端口(按端口循环强杀) ..."
for port in 7101 7102; do
  for i in $(seq 1 6); do
    pid=$(netstat -ano 2>/dev/null | grep ":$port " | grep LISTENING | awk '{print $NF}' | head -1)
    [ -z "$pid" ] && break
    taskkill //PID "$pid" //F >/dev/null 2>&1 || true
    sleep 1
  done
done

echo "==> 清空 backend pycache ..."
find backend -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

echo "==> 起重 AI 服务 (7102) ..."
nohup "$PY_AI" -m uvicorn app.main:app --app-dir backend/ai_service --host 127.0.0.1 --port 7102 --log-level warning > /tmp/ai_service_7102.log 2>&1 &
echo "    AI PID=$!"

echo "==> 起重业务服务 (7101) ..."
nohup "$PY_BIZ" -m uvicorn app.main:app --app-dir backend/business --host 0.0.0.0 --port 7101 --log-level info > /tmp/business_7101.log 2>&1 &
echo "    业务 PID=$!"

sleep 12
echo "==> 健康检查 ..."
curl -s -o /dev/null -w "    业务 /health = %{http_code}\n" --max-time 6 http://127.0.0.1:7101/health
curl -s -o /dev/null -w "    AI   /health = %{http_code}\n" --max-time 6 http://127.0.0.1:7102/health
echo "==> 完成。日志: /tmp/business_7101.log  /tmp/ai_service_7102.log"
