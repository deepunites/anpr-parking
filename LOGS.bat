@echo off
chcp 65001 >nul
setlocal EnableExtensions
title ANPR Parking - журналы
cd /d "%~dp0"
echo Журналы всех сервисов. Выход - Ctrl+C.
echo.
docker compose -f deploy\local\compose.yml ps
echo.
docker compose -f deploy\local\compose.yml logs -f --tail=100
pause
