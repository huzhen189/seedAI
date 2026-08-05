@echo off
chcp 65001 >nul
REM SeedAI 前后端统一启停包装(git-bash / cmd 均可直接调用)
REM 用法:
REM   .\scripts\dev.bat start
REM   .\scripts\dev.bat stop
REM   .\scripts\dev.bat restart
REM   .\scripts\dev.bat stop  -BackendPort 7101 -FrontendPort 7100
powershell -ExecutionPolicy Bypass -File "%~dp0dev.ps1" %*
