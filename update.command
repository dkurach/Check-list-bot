#!/bin/bash
# Обновление бота на macOS: двойной щелчок в Finder.
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:$PATH"
echo "=== Обновление бота ==="

fail() {
    echo
    echo "Обновление не удалось. Пришлите разработчику текст ошибки выше."
    read -r -p "Нажмите Enter, чтобы закрыть окно"
    exit 1
}

if [ -d .git ]; then
    git pull --ff-only || fail
else
    echo "Папка установлена из архива: распакуйте новую версию поверх этой папки, файл .env и папку data не трогайте."
fi

uv sync --no-dev || fail
uv run --no-dev python -m checklist_bot restart || fail
read -r -p "Нажмите Enter, чтобы закрыть окно"
