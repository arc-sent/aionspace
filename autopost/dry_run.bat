@echo off
rem Dry run: Claude prepares the posts, they are printed here and saved to out\, NOTHING is sent to Telegram.
chcp 65001 >nul
cd /d "%~dp0"
python aion_daily.py run --dry-run --force > last_output.txt 2>&1
type last_output.txt
pause
