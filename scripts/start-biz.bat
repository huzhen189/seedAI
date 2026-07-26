@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo === 启动 SeedAI 单进程后端 (端口 7101, 业务服务 + AI 核心合并) ===
C:\Users\zhenhu\.workbuddy\binaries\python\envs\seedai-biz\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 7101
pause
