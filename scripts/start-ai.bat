@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo === 启动 AI 核心服务 (端口 7102) ===
C:\Users\zhenhu\.workbuddy\binaries\python\envs\default\Scripts\python.exe -m uvicorn app.main:app --app-dir backend\ai_service --host 127.0.0.1 --port 7102
pause
