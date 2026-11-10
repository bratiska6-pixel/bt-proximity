#!/usr/bin/env bash
# ==============================================================================
# Деинсталлятор bt-proximity
# ==============================================================================
set -e

SERVICE_NAME="bt-proximity.service"
USER_SERVICE_PATH="$HOME/.config/systemd/user/$SERVICE_NAME"
INSTALL_DIR="$HOME/.local/share/bt-proximity"
CONFIG_DIR="$HOME/.config/bt-proximity"
BIN_FILE="$HOME/.local/bin/bt-proximity"

echo "================================================================="
echo "           Полное удаление bt-proximity                          "
echo "================================================================="

# 1. Остановка и отключение службы systemd
echo "[1/4] Остановка и отключение службы systemd..."
systemctl --user disable --now "$SERVICE_NAME" 2>/dev/null || true
if [ -f "$USER_SERVICE_PATH" ]; then
    rm -f "$USER_SERVICE_PATH"
    systemctl --user daemon-reload
    echo "  [✓] Файл службы $USER_SERVICE_PATH удален."
fi

# 2. Удаление команды bt-proximity
echo "[2/4] Удаление команды bt-proximity..."
if [ -L "$BIN_FILE" ] || [ -f "$BIN_FILE" ]; then
    rm -f "$BIN_FILE"
    echo "  [✓] Команда $BIN_FILE удалена."
fi

# 3. Удаление настроек
echo "[3/4] Удаление настроек..."
if [ -d "$CONFIG_DIR" ]; then
    rm -rf "$CONFIG_DIR"
    echo "  [✓] Папка настроек $CONFIG_DIR удалена."
fi

# 4. Удаление файлов программы
echo "[4/4] Удаление файлов приложения..."
if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    echo "  [✓] Папка приложения $INSTALL_DIR удалена."
fi

echo "================================================================="
echo "   [✓] bt-proximity полностью удален с компьютера!"
echo "   (Копия установщика сохранена в ваших заметках Obsidian)"
echo "================================================================="

