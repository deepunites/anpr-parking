@echo off
chcp 65001 >nul
setlocal EnableExtensions
title ANPR Parking - сброс данных
cd /d "%~dp0"
echo ============================================================
echo   ВНИМАНИЕ: будут удалены ВСЕ данные стенда без возможности
echo   восстановления - сессии, платежи, клиенты, пользователи,
echo   ключи шифрования номеров и ключи камер.
echo ============================================================
echo.
set "ANSWER="
set /p "ANSWER=Чтобы удалить, введите YES и нажмите Enter: "
if /i not "%ANSWER%"=="YES" goto cancel
docker compose -f deploy\local\compose.yml down -v
if errorlevel 1 goto fail
if exist LOGINS.txt del LOGINS.txt
echo.
echo Данные удалены. START.bat создаст стенд заново с новыми паролями.
pause
exit /b 0
:cancel
echo Отменено - ничего не удалено.
pause
exit /b 0
:fail
echo [!] Не удалось - запущен ли Docker Desktop?
pause
exit /b 1
