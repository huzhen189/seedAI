@echo off
chcp 65001 >nul
cd /d "%~dp0.."
C:\Users\zhenhu\.workbuddy\binaries\python\envs\seedai-biz\Scripts\python.exe scripts\reset_all.py %*
pause
