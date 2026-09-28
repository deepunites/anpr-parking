@echo off
chcp 65001 >nul
setlocal EnableExtensions
title ANPR Parking - остановка
cd /d "%~dp0"
echo Останавливаю ANPR Parking - данные сохраняются...
docker compose -f deploy\local\compose.yml stop
if errorlevel 1 goto fail
echo.
echo Остановлено. Запустить снова: START.bat
pause
exit /b 0
:fail
echo [!] Не удалось остановить - запущен ли Docker Desktop?
pause
exit /b 1
