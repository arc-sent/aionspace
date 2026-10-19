@echo off
chcp 65001 >nul
cd /d "%~dp0"
python aion_daily.py chat-id > last_output.txt 2>&1
python aion_daily.py test-telegram >> last_output.txt 2>&1
type last_output.txt
pause
