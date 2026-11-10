#!/usr/bin/env bash
# ==============================================================================
# Установщик bt-proximity: автоматическая блокировка/разблокировка Linux по Bluetooth
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.local/share/bt-proximity"
BIN_DIR="$HOME/.local/bin"

echo "================================================================="
echo "       Установка bt-proximity (Bluetooth Proximity Lock)         "
echo "================================================================="

# 1. Проверка и установка зависимостей
echo "[1/4] Проверка системных зависимостей..."
MISSING_PKGS=""

if ! python3 -c "import dbus" 2>/dev/null; then
    MISSING_PKGS="$MISSING_PKGS python3-dbus"
fi

if ! python3 -c "from gi.repository import GLib" 2>/dev/null; then
    MISSING_PKGS="$MISSING_PKGS python3-gi"
fi

if ! which bluetoothctl >/dev/null 2>&1; then
    MISSING_PKGS="$MISSING_PKGS bluez"
fi

if [ -n "$MISSING_PKGS" ]; then
    echo "[!] Не хватает библиотек:$MISSING_PKGS"
    if which apt-get >/dev/null 2>&1; then
        echo "[+] Установка пакетов через apt (Debian / Ubuntu / elementary)..."
        sudo apt-get update && sudo apt-get install -y python3-dbus python3-gi bluez
    elif which dnf >/dev/null 2>&1; then
        echo "[+] Установка пакетов через dnf (Fedora / RHEL)..."
        sudo dnf install -y python3-dbus python3-gobject bluez
    elif which pacman >/dev/null 2>&1; then
        echo "[+] Установка пакетов через pacman (Arch / Manjaro)..."
        sudo pacman -Sy --noconfirm python-dbus python-gobject bluez bluez-utils
    elif which zypper >/dev/null 2>&1; then
        echo "[+] Установка пакетов через zypper (openSUSE)..."
        sudo zypper install -y python3-dbus-python python3-gobject bluez
    else
        echo "[!] Неизвестный пакетный менеджер. Установите вручную: python3 dbus, gobject (gi), bluez."
    fi
else
    echo "[✓] Все зависимости уже установлены."
fi

# 2. Копирование файлов программы
echo "[2/4] Установка файлов программы..."
mkdir -p "$INSTALL_DIR" "$BIN_DIR"
cp -f "$SCRIPT_DIR/bt_proximity.py" "$INSTALL_DIR/bt_proximity.py"
chmod +x "$INSTALL_DIR/bt_proximity.py"

# Симлинк в ~/.local/bin
ln -sf "$INSTALL_DIR/bt_proximity.py" "$BIN_DIR/bt-proximity"

# Добавление ~/.local/bin в PATH если нет
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    export PATH="$HOME/.local/bin:$PATH"
fi

# 3. Настройка конфигурации
echo "[3/4] Первичная настройка..."
CONFIG_DIR="$HOME/.config/bt-proximity"
mkdir -p "$CONFIG_DIR"

if [[ "$1" == "--yes" || "$1" == "-y" ]] && [ -f "$SCRIPT_DIR/config.json" ]; then
    cp -f "$SCRIPT_DIR/config.json" "$CONFIG_DIR/config.json"
    echo "  [✓] Использован готовый конфиг из пакета (флаг --yes)."
else
    if [ -f "$SCRIPT_DIR/config.json" ] && [ ! -f "$CONFIG_DIR/config.json" ]; then
        cp -f "$SCRIPT_DIR/config.json" "$CONFIG_DIR/config.json"
    fi
    "$INSTALL_DIR/bt_proximity.py" config
fi

# 4. Установка фонового сервиса systemd
echo "[4/4] Запуск фоновой службы в systemd..."
"$INSTALL_DIR/bt_proximity.py" install

echo "================================================================="
echo "   [✓] Установка успешно завершена!"
echo "================================================================="
echo "Управление приложением:"
echo "  bt-proximity status    - проверить статус и уровень сигнала"
echo "  bt-proximity config    - изменить дальность или устройство"
echo "  bt-proximity monitor   - живой тест дальности без блокировки"
echo "  bt-proximity scan      - найти устройства вокруг"
echo "  bt-proximity uninstall - удалить службу"
echo "================================================================="
