@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%GATEWAY_KEY%"=="" echo [warn] GATEWAY_KEY not set - gateway runs WITHOUT auth
python scripts\gateway.py --port 8080 %*