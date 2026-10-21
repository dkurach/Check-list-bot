@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка бота ===

where uv >nul 2>nul
if errorlevel 1 (
    echo Устанавливаю uv...
    powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
)
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

uv sync --no-dev
if errorlevel 1 goto error
uv run --no-dev python -m checklist_bot setup
if errorlevel 1 goto error
pause
exit /b 0

:error
echo.
echo Установка не удалась. Пришлите разработчику текст ошибки выше.
pause
exit /b 1
