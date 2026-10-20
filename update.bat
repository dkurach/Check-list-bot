@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
echo === Обновление бота ===

if exist .git (
    git pull --ff-only
    if errorlevel 1 goto error
) else (
    echo Папка установлена из архива: распакуйте новую версию поверх этой папки, файл .env и папку data не трогайте.
)

uv sync --no-dev
if errorlevel 1 goto error
uv run --no-dev python -m checklist_bot restart
if errorlevel 1 goto error
pause
exit /b 0

:error
echo.
echo Обновление не удалось. Пришлите разработчику текст ошибки выше.
pause
exit /b 1
