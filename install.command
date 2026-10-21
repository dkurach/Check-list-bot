#!/bin/bash
# Установка бота на macOS: двойной щелчок в Finder.
cd "$(dirname "$0")" || exit 1
echo "=== Установка бота ==="

if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
    echo "Устанавливаю uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh || exit 1
fi
export PATH="$HOME/.local/bin:$PATH"

if uv sync --no-dev && uv run --no-dev python -m checklist_bot setup; then
    read -r -p "Нажмите Enter, чтобы закрыть окно"
else
    echo
    echo "Установка не удалась. Пришлите разработчику текст ошибки выше."
    read -r -p "Нажмите Enter, чтобы закрыть окно"
    exit 1
fi
