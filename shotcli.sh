#!/bin/sh
# Запуск CLI на Ubuntu: ./shotcli.sh   или   python3 -m shotcli
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then
  exec python3 -m shotcli "$@"
fi
if command -v python >/dev/null 2>&1; then
  exec python -m shotcli "$@"
fi
echo "Нужен python3. На Ubuntu: sudo apt install python3" >&2
exit 1
