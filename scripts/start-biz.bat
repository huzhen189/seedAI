@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo === 启动业务服务 (端口 7101) ===
C:\Users\zhenhu\.workbuddy\binaries\python\envs\seedai-biz\Scripts\python.exe -m uvicorn app.main:app --app-dir backend\business --host 0.0.0.0 --port 7101
pause
