@echo off
chcp 65001 >nul
setlocal EnableExtensions
title ANPR Parking - запуск
cd /d "%~dp0"
set "COMPOSE=docker compose -f deploy\local\compose.yml"
if not defined PARKING_HTTP_PORT set "PARKING_HTTP_PORT=8080"
set "URL=http://localhost:%PARKING_HTTP_PORT%"

echo ============================================================
echo   ANPR Parking - запуск локального стенда
echo ============================================================
echo.

rem --- 1. Docker Desktop установлен?
where docker >nul 2>nul
if not errorlevel 1 goto docker_installed
echo [!] Не найден Docker. Установите Docker Desktop для Windows:
echo     https://www.docker.com/products/docker-desktop/
echo     После установки перезагрузите компьютер и снова запустите START.bat
start "" "https://www.docker.com/products/docker-desktop/"
goto fail

:docker_installed
rem --- 2. Docker запущен? Если нет - запускаем Docker Desktop и ждём до 4 минут
docker info >nul 2>nul
if not errorlevel 1 goto docker_ready
echo Запускаю Docker Desktop...
if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
set /a TRIES=0
:wait_docker
timeout /t 5 /nobreak >nul
docker info >nul 2>nul
if not errorlevel 1 goto docker_ready
set /a TRIES+=1
if %TRIES% lss 48 goto wait_docker
echo [!] Docker не запустился за 4 минуты. Откройте Docker Desktop вручную,
echo     дождитесь надписи "Engine running" и снова запустите START.bat
goto fail

:docker_ready
docker compose version >nul 2>nul
if not errorlevel 1 goto compose_ok
echo [!] Нужен Docker Compose v2 - обновите Docker Desktop.
goto fail

:compose_ok
rem --- 3. Сборка образов: первый раз 5-15 минут, дальше - секунды
echo Сборка образов - в первый раз это 5-15 минут, нужен интернет...
%COMPOSE% build
if errorlevel 1 goto fail_build

rem --- 4. Запуск: секреты, база, миграции, демо-данные, приложение
echo.
echo Запуск сервисов...
%COMPOSE% up -d
if errorlevel 1 goto fail_up

rem --- 5. Ждём, пока веб ответит
echo Ожидание готовности %URL% ...
set "PROBE=curl"
where curl >nul 2>nul
if errorlevel 1 set "PROBE=powershell"
set /a TRIES=0
:wait_web
if "%PROBE%"=="curl" (
  curl -fsS -o nul "%URL%/api/v1/plans" >nul 2>nul
) else (
  powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 '%URL%/api/v1/plans' | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
)
if not errorlevel 1 goto web_ready
set /a TRIES+=1
if %TRIES% geq 90 goto fail_web
timeout /t 2 /nobreak >nul
goto wait_web

:web_ready
rem --- 6. Логины и ключи камер - в LOGINS.txt рядом с этим файлом
%COMPOSE% exec -T api cat /state/credentials.txt > LOGINS.txt 2>nul
echo.
type LOGINS.txt
echo.
echo ============================================================
echo   Готово: %URL%
echo   Логины и ключи камер сохранены в LOGINS.txt
echo   Стенд работает в фоне - это окно можно закрыть.
echo   Остановить: STOP.bat    Журналы: LOGS.bat
echo ============================================================
start "" "%URL%"
pause
exit /b 0

:fail_build
echo.
echo [!] Сборка не удалась. Проверьте интернет и свободное место на диске,
echo     затем снова запустите START.bat
goto fail

:fail_up
echo.
echo [!] Сервисы не запустились. Последние строки журнала настройки:
%COMPOSE% logs --tail=60 init
echo.
echo Если занят порт %PARKING_HTTP_PORT%, запустите с другим портом:
echo     set PARKING_HTTP_PORT=8090
echo     START.bat
goto fail

:fail_web
echo.
echo [!] Веб не ответил за 3 минуты. Журнал API:
%COMPOSE% logs --tail=60 api
goto fail

:fail
echo.
pause
exit /b 1
