@echo off
REM Run this script as Administrator to start the local bare-domain reverse proxy.
REM Prerequisite: nginx installed and nginx.exe in PATH (e.g. `winget install -e --id nginx`).
where nginx >nul 2>nul || (
    echo nginx not found in PATH. Install it first: winget install -e --id nginx
    pause
    exit /b 1
)
cd /d %~dp0
nginx -c "%~dp0nginx-local-dev.conf" -p "%~dp0"
echo nginx started. Open http://seedai.huzhen.net.cn/ and http://seedapi.huzhen.net.cn/
echo To stop: nginx -c "%~dp0nginx-local-dev.conf" -s stop
pause
