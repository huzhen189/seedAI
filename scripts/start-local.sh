#!/usr/env bash
# 本地开发一键启动(无需 docker): 杀掉占用端口的旧进程 -> 清 pycache -> 起重业务服务。
#
# ⚠️ 约定变更(2026-07-18):AI 后端不再由本脚本统一启动,改由你用 cmd 命令行自行启动,
# 以便实时查看日志。业务服务仍由本脚本启动(连 .env 里的统一云库)。
#
# AI 后端手动启动命令(另开一个终端执行):
#   cd backend/ai_service
#   /c/Users/zhenhu/.workbuddy/binaries/python/envs/default/Scripts/python.exe app/main.py
#   # 该 __main__ 块会把端口锁定为 7102 并打印日志到控制台;Ctrl+C 即可停止。
#
# 或等价地(显式 uvicorn):
#   /c/Users/zhenhu/.workbuddy/binaries/python/envs/default/Scripts/python.exe \
#     -m uvicorn app.main:app --app-dir backend/ai_service --host 127.0.0.1 --port 7102 --log-level info
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY_BIZ="/c/Users/zhenhu/.workbuddy/binaries/python/envs/seedai-biz/Scripts/python.exe"
PY_AI="/c/Users/zhenhu/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

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

echo "==> 起重业务服务 (7101) ..."
nohup "$PY_BIZ" -m uvicorn app.main:app --app-dir backend/business --host 0.0.0.0 --port 7101 --log-level info > /tmp/business_7101.log 2>&1 &
echo "    业务 PID=$!"

sleep 10
echo "==> 健康检查 ..."
curl -s -o /dev/null -w "    业务 /health = %{http_code}\n" --max-time 6 http://127.0.0.1:7101/health

echo ""
echo "==> AI 后端请自行启动(见脚本顶部说明),例如:"
echo "    cd backend/ai_service && $PY_AI app/main.py"
echo "==> 完成。业务日志: /tmp/business_7101.log"
