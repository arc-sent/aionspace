@echo off
rem Run now: Claude prepares the posts and the bot receives them (even if today's posts were already sent).
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -NoExit -Command "& '%~dp0autostart.ps1' run -NoSleep -Force"
