#!/usr/bin/env python3
"""
bt-proximity: Умная блокировка и разблокировка Linux (Wayland / X11) по Bluetooth / BLE (iPhone, Android, Smart Watch).
"""

import os
import sys
import time
import json
import struct
import argparse
import subprocess
import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

APP_NAME = "bt-proximity"
CONFIG_DIR = os.path.expanduser("~/.config/bt-proximity")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
SERVICE_NAME = "bt-proximity.service"
USER_SYSTEMD_DIR = os.path.expanduser("~/.config/systemd/user")
USER_SERVICE_PATH = os.path.join(USER_SYSTEMD_DIR, SERVICE_NAME)

DEFAULT_CONFIG = {
    "device_mac": "",
    "device_name": "",
    "distance_profile": "very_near",
    "rssi_lock_threshold": -62,
    "rssi_unlock_threshold": -55,
    "away_timeout": 3.5,
    "required_near_hits": 2,
    "required_away_hits": 3,
    "manual_lock_requires_password": True
}

PROFILES = {
    "very_near": {
        "name": "Очень близко (~1-1.5 м, сразу при отходе от стола)",
        "lock": -62,
        "unlock": -55,
        "timeout": 3.0
    },
    "near": {
        "name": "Близко (~2-2.5 м, зона рабочего кабинета)",
        "lock": -68,
        "unlock": -60,
        "timeout": 4.0
    },
    "medium": {
        "name": "Средне (~3.5-5 м, в пределах комнаты)",
        "lock": -76,
        "unlock": -68,
        "timeout": 5.0
    },
    "far": {
        "name": "Далеко (~7-10 м, при выходе в другую комнату)",
        "lock": -84,
        "unlock": -76,
        "timeout": 6.0
    }
}


def log(msg):
    print(msg, flush=True)


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                res = DEFAULT_CONFIG.copy()
                res.update(cfg)
                return res
        except Exception as e:
            log(f"[!] Ошибка чтения {CONFIG_FILE}: {e}")
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)
    log(f"[✓] Конфигурация сохранена в {CONFIG_FILE}")


def mac_to_path(mac):
    clean = mac.replace(":", "_").upper()
    return f"/org/bluez/hci0/dev_{clean}"


def get_current_session():
    user = os.environ.get("USER", "bambycha")
    try:
        out = subprocess.check_output(
            ["loginctl", "list-sessions", "--no-legend"],
            universal_newlines=True
        )
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == user:
                return parts[0]
    except Exception:
        pass
    return "c2"


def wake_display():
    """Включение дисплея под Wayland (Mutter / Gala) и X11"""
    try:
        subprocess.run([
            "busctl", "--user", "set-property",
            "org.gnome.Mutter.DisplayConfig",
            "/org/gnome/Mutter/DisplayConfig",
            "org.gnome.Mutter.DisplayConfig",
            "PowerSaveMode", "i", "0"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    # Запасной вариант для X11
    try:
        subprocess.run(["xset", "dpms", "force", "on"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def lock_screen(session_id, rssi):
    log(f"[{time.strftime('%H:%M:%S')}] >>> iPhone ДАЛЕКО (RSSI: {rssi} dBm). Блокировка экрана...")
    try:
        subprocess.run(["loginctl", "lock-session", session_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    try:
        subprocess.run([
            "busctl", "--user", "call",
            "org.gnome.ScreenSaver",
            "/org/gnome/ScreenSaver",
            "org.gnome.ScreenSaver",
            "Lock"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def unlock_screen(session_id, rssi):
    log(f"[{time.strftime('%H:%M:%S')}] >>> iPhone РЯДОМ (RSSI: {rssi} dBm). Разблокировка экрана...")
    wake_display()
    try:
        subprocess.run(["loginctl", "activate", session_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    try:
        subprocess.run(["loginctl", "unlock-session", session_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    try:
        subprocess.run([
            "busctl", "--user", "call",
            "org.gnome.ScreenSaver",
            "/org/gnome/ScreenSaver",
            "org.gnome.ScreenSaver",
            "SetActive", "b", "false"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class ProximityDaemon:
    def __init__(self, cfg, is_monitor_only=False):
        self.cfg = cfg
        self.is_monitor_only = is_monitor_only
        self.device_mac = cfg["device_mac"].upper()
        self.device_path = mac_to_path(self.device_mac)
        self.adapter_path = "/org/bluez/hci0"
        self.rssi_lock = cfg.get("rssi_lock_threshold", -58)
        self.rssi_unlock = cfg.get("rssi_unlock_threshold", -52)
        self.away_timeout = float(cfg.get("away_timeout", 4.0))
        self.req_near_hits = int(cfg.get("required_near_hits", 2))
        self.req_away_hits = int(cfg.get("required_away_hits", 3))
        self.manual_lock_requires_pwd = cfg.get("manual_lock_requires_password", True)

        self.last_seen_time = time.time()
        self.last_ble_rssi = None
        self.last_bt_rssi = None
        self.smooth_rssi = None
        self.last_connected = False
        self.probe_in_progress = False
        self.last_probe_time = 0.0

        self.state = "unlocked"
        self.near_hits = 0
        self.away_hits = 0
        self.locked_by_proximity = False
        self.manual_lock_detected = False

        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        self.session_bus = dbus.SessionBus()

        self.adapter_obj = self.bus.get_object("org.bluez", self.adapter_path)
        self.device_obj = self.bus.get_object("org.bluez", self.device_path)
        self.adapter = dbus.Interface(self.adapter_obj, "org.bluez.Adapter1")
        self.dev_iface = dbus.Interface(self.device_obj, "org.bluez.Device1")

    def is_screensaver_active(self):
        try:
            ss = self.session_bus.get_object("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver")
            props = dbus.Interface(ss, "org.gnome.ScreenSaver")
            return bool(props.GetActive())
        except Exception:
            pass
        try:
            session_id = get_current_session()
            out = subprocess.check_output(
                ["loginctl", "show-session", session_id, "-p", "LockedHint"],
                universal_newlines=True
            )
            return "LockedHint=yes" in out
        except Exception:
            pass
        return False

    def start_discovery(self):
        try:
            self.adapter.SetDiscoveryFilter({
                "Transport": dbus.String("auto"),
                "DuplicateData": dbus.Boolean(True)
            })
            self.adapter.StartDiscovery()
        except Exception as e:
            if "AlreadyDiscovering" not in str(e):
                log(f"[!] StartDiscovery error: {e}")

    def on_property_changed(self, interface, changed_props, invalidated_props, path):
        if path == self.device_path:
            if "Connected" in changed_props:
                self.last_connected = bool(changed_props["Connected"])
                if self.last_connected:
                    self.last_seen_time = time.time()
                    self.away_hits = 0
            if "RSSI" in changed_props:
                self.last_ble_rssi = int(changed_props["RSSI"])
                self.last_seen_time = time.time()

    def get_hardware_rssi(self):
        """Чтение реального аппаратного RSSI активного соединения (LE или Classic) через HCI"""
        try:
            out = subprocess.check_output(["hcitool", "con"], text=True, stderr=subprocess.DEVNULL)
            handle = None
            for line in out.splitlines():
                if self.device_mac.upper() in line.upper() and "handle" in line:
                    parts = line.split()
                    if "handle" in parts:
                        idx = parts.index("handle")
                        handle = int(parts[idx + 1])
                        break
            if handle is None:
                return None

            h_low = f"0x{handle & 0xff:02x}"
            h_high = f"0x{(handle >> 8) & 0xff:02x}"

            cmd_out = subprocess.check_output(
                ["hcitool", "cmd", "0x05", "0x0005", h_low, h_high],
                text=True, stderr=subprocess.DEVNULL
            )
            lines = cmd_out.strip().splitlines()
            for i, l in enumerate(lines):
                if "> HCI Event:" in l and i + 1 < len(lines):
                    bytes_list = lines[i + 1].strip().split()
                    if len(bytes_list) >= 7 and bytes_list[3] == "00":
                        return struct.unpack("b", bytes.fromhex(bytes_list[6]))[0]
        except Exception:
            return None
        return None

    def trigger_probe(self):
        """Асинхронный опрос спящего iPhone через D-Bus Connect (не блокирует главный цикл)"""
        if self.probe_in_progress or self.last_connected:
            return
        if time.time() - self.last_probe_time < 2.5:
            return

        self.probe_in_progress = True
        self.last_probe_time = time.time()

        def on_connect_success():
            self.probe_in_progress = False
            self.last_connected = True
            self.last_seen_time = time.time()
            self.away_hits = 0

        def on_connect_error(err):
            self.probe_in_progress = False

        try:
            self.dev_iface.Connect(
                reply_handler=on_connect_success,
                error_handler=on_connect_error,
                timeout=2.5
            )
        except Exception:
            self.probe_in_progress = False

    def check_tick(self):
        now = time.time()

        # Следим, чтобы discovery не отключился
        try:
            props = dbus.Interface(self.adapter_obj, "org.freedesktop.DBus.Properties")
            if not props.Get("org.bluez.Adapter1", "Discovering"):
                self.start_discovery()
        except Exception:
            pass

        # Читаем актуальное состояние подключения из BlueZ
        try:
            dev_props = dbus.Interface(self.device_obj, "org.freedesktop.DBus.Properties")
            self.last_connected = bool(dev_props.Get("org.bluez.Device1", "Connected"))
        except Exception:
            self.last_connected = False

        time_since_seen = now - self.last_seen_time
        is_close = False
        is_far = False
        status_desc = ""
        sig_info = ""

        if self.last_connected:
            self.last_seen_time = now
            time_since_seen = 0.0
            hw_rssi = self.get_hardware_rssi()

            if hw_rssi is not None:
                # Экспоненциальное сглаживание для подавления радиошума
                if self.smooth_rssi is None:
                    self.smooth_rssi = float(hw_rssi)
                else:
                    self.smooth_rssi = 0.6 * hw_rssi + 0.4 * self.smooth_rssi
                cur_rssi = int(round(self.smooth_rssi))
                self.last_rssi = cur_rssi

                sig_info = f"RSSI: {cur_rssi} dBm (raw: {hw_rssi})"
                if cur_rssi < self.rssi_lock:
                    is_far = True
                    status_desc = "ОТОШЁЛ (ДАЛЕКО)"
                elif cur_rssi >= self.rssi_unlock:
                    is_close = True
                    status_desc = "У СТОЛА (БЛИЗКО)"
                else:
                    status_desc = "СРЕДНЯЯ ЗОНА"
            else:
                # Если дескриптор соединения еще инициализируется
                is_close = True
                status_desc = "ПОДКЛЮЧЕН (BT)"
                sig_info = "Инициализация канала"
        else:
            self.smooth_rssi = None
            # Не подключен: проверяем свежие BLE-пакеты (если экран iPhone активен)
            if time_since_seen <= self.away_timeout and self.last_ble_rssi is not None:
                cur_rssi = self.last_ble_rssi
                self.last_rssi = cur_rssi
                sig_info = f"BLE RSSI: {cur_rssi} dBm"
                if cur_rssi >= self.rssi_unlock:
                    is_close = True
                    status_desc = "РЯДОМ (BLE)"
                elif cur_rssi < self.rssi_lock:
                    is_far = True
                    status_desc = "ДАЛЕКО (BLE)"
                else:
                    status_desc = "СРЕДНЯЯ ЗОНА (BLE)"
            else:
                # Нет свежих BLE-пакетов (черный экран iPhone в глубоком сне или отошёл)
                self.trigger_probe()
                if self.probe_in_progress:
                    status_desc = "ОПРОС (Probe...)"
                    sig_info = "Поиск спящего iPhone"
                else:
                    status_desc = "НЕТ СВЯЗИ"
                    sig_info = "Вне зоны действия"
                is_far = True

        if is_far:
            self.away_hits += 1
            self.near_hits = 0
        else:
            self.away_hits = 0
            if is_close:
                self.near_hits += 1
            else:
                self.near_hits = 0

        # Отслеживание ручной блокировки экрана (с кнопки Super+L или из меню)
        screensaver_active = self.is_screensaver_active()
        if screensaver_active:
            if not self.locked_by_proximity and not self.manual_lock_detected:
                self.manual_lock_detected = True
                self.state = "locked"
                log(f"[{time.strftime('%H:%M:%S')}] [!] Зафиксирована ручная блокировка (Super+L/кнопка).")
        else:
            if self.manual_lock_detected or (self.state == "locked" and not self.locked_by_proximity):
                self.manual_lock_detected = False
                self.state = "unlocked"
                self.locked_by_proximity = False
                log(f"[{time.strftime('%H:%M:%S')}] [✓] Экран разблокирован паролем. Слежение снова активно.")

        # Статус для вывода в консоль / журнал
        if self.is_monitor_only:
            mode_prefix = "[MONITOR]"
        elif self.manual_lock_detected and self.manual_lock_requires_pwd:
            mode_prefix = "[MANUAL_LOCK]"
        else:
            mode_prefix = f"[{self.state.upper()}]"

        extra_info = " | ТРЕБУЕТСЯ ПАРОЛЬ" if (self.manual_lock_detected and self.manual_lock_requires_pwd) else ""
        log(f"[{time.strftime('%H:%M:%S')}] {mode_prefix:<13} {status_desc:<24} | {sig_info:<22} | Молчит: {time_since_seen:.1f}с{extra_info}")

        if not self.is_monitor_only:
            session_id = get_current_session()
            if self.state == "unlocked":
                if is_far and self.away_hits >= self.req_away_hits:
                    self.locked_by_proximity = True
                    cur_val = self.last_ble_rssi or self.last_bt_rssi or "--"
                    lock_screen(session_id, cur_val)
                    self.state = "locked"
            elif self.state == "locked":
                # Если была ручная блокировка с кнопки и включена опция "требовать пароль"
                if self.manual_lock_detected and self.manual_lock_requires_pwd:
                    pass  # Ничего не делаем, ждем пока пользователь введет пароль руками
                else:
                    if is_close and self.near_hits >= self.req_near_hits:
                        cur_val = self.last_ble_rssi or self.last_bt_rssi or "--"
                        unlock_screen(session_id, cur_val)
                        self.state = "unlocked"
                        self.locked_by_proximity = False
                        self.manual_lock_detected = False

        return True

    def run(self):
        self.start_discovery()
        self.bus.add_signal_receiver(
            self.on_property_changed,
            signal_name="PropertiesChanged",
            dbus_interface="org.freedesktop.DBus.Properties",
            path_keyword="path"
        )

        header = "РЕЖИМ ТЕСТА / МОНИТОРИНГА (без блокировки)" if self.is_monitor_only else "ФОНОВЫЙ ДЕМОН АВТОБЛОКИРОВКИ"
        log("=" * 68)
        log(f"  bt-proximity: {header}")
        log(f"  Устройство: {self.cfg.get('device_name', 'Device')} ({self.device_mac})")
        log(f"  Порог блокировки: RSSI < {self.rssi_lock} dBm (тишина > {self.away_timeout}с)")
        log(f"  Порог разблокировки: RSSI >= {self.rssi_unlock} dBm")
        log("=" * 68)

        GLib.timeout_add(1000, self.check_tick)
        loop = GLib.MainLoop()
        try:
            loop.run()
        except KeyboardInterrupt:
            log("\nОстановка...")
        finally:
            try:
                self.adapter.StopDiscovery()
            except:
                pass


def scan_devices():
    """Сканирование Bluetooth: сначала сопряженные, затем остальные с именами"""
    devices = []
    seen = set()

    # 1. Сначала сопряженные (Paired)
    try:
        out = subprocess.check_output(["bluetoothctl", "devices", "Paired"], universal_newlines=True)
        for line in out.splitlines():
            parts = line.split(" ", 2)
            if len(parts) >= 3 and parts[0] == "Device":
                mac, name = parts[1], parts[2]
                devices.append({"mac": mac, "name": f"{name} [Сопряжено]", "paired": True})
                seen.add(mac.upper())
    except Exception:
        pass

    # 2. Остальные устройства с нормальными именами (не MAC-адрес вместо имени)
    try:
        out = subprocess.check_output(["bluetoothctl", "devices"], universal_newlines=True)
        for line in out.splitlines():
            parts = line.split(" ", 2)
            if len(parts) >= 3 and parts[0] == "Device":
                mac, name = parts[1], parts[2]
                if mac.upper() not in seen and not name.replace("-", ":").upper() == mac.upper():
                    devices.append({"mac": mac, "name": name, "paired": False})
                    seen.add(mac.upper())
    except Exception:
        pass

    return devices


def interactive_config():
    """Интерактивный мастер настройки"""
    print("\n" + "=" * 60)
    print("      Мастер настройки bt-proximity")
    print("=" * 60)

    cfg = load_config()

    print("\n1. ВЫБОР УСТРОЙСТВА (iPhone / Смартфон / Браслет):")
    devices = scan_devices()

    selected_mac = cfg.get("device_mac", "")
    selected_name = cfg.get("device_name", "")

    if devices:
        print("\nНайденные сопряженные устройства:")
        for idx, dev in enumerate(devices, 1):
            cur_marker = " (текущее)" if dev["mac"].upper() == selected_mac.upper() else ""
            print(f"  [{idx}] {dev['name']} ({dev['mac']}){cur_marker}")
        print(f"  [{len(devices) + 1}] Ввести MAC-адрес вручную")

        prompt_text = f"\nВыберите номер [1-{len(devices) + 1}] (по умолчанию 1 - {devices[0]['name']}): "
        choice = input(prompt_text).strip() or "1"
        if choice.isdigit():
            c_int = int(choice)
            if 1 <= c_int <= len(devices):
                selected_mac = devices[c_int - 1]["mac"]
                selected_name = devices[c_int - 1]["name"]
            elif c_int == len(devices) + 1:
                selected_mac = input("Введите MAC (формат XX:XX:XX:XX:XX:XX): ").strip()
                selected_name = input("Введите имя устройства (например, iPhone): ").strip() or "Bluetooth Device"
    else:
        print("  Сопряженные устройства не найдены. Введите адрес вручную.")
        selected_mac = input(f"MAC-адрес [{selected_mac}]: ").strip() or selected_mac
        selected_name = input(f"Имя устройства [{selected_name}]: ").strip() or selected_name

    cfg["device_mac"] = selected_mac
    cfg["device_name"] = selected_name

    print("\n2. НАСТРОЙКА ДАЛЬНОСТИ СРАБАТЫВАНИЯ:")
    print("  [1] Очень близко (~1-1.5 м, блокировка сразу при отходе от стола) [РЕКОМЕНДУЕТСЯ]")
    print("  [2] Близко       (~2-2.5 м, зона рабочего места)")
    print("  [3] Средне       (~3.5-5 м, в пределах одной комнаты)")
    print("  [4] Далеко       (~7-10 м, при выходе в соседнюю комнату)")
    print("  [5] Ручная калибровка (ввести свои пороги RSSI)")

    dist_choice = input("\nВыберите профиль [1-5] (по умолчанию 1): ").strip() or "1"
    if dist_choice == "1":
        cfg["distance_profile"] = "very_near"
        cfg["rssi_lock_threshold"] = -62
        cfg["rssi_unlock_threshold"] = -55
        cfg["away_timeout"] = 3.0
    elif dist_choice == "2":
        cfg["distance_profile"] = "near"
        cfg["rssi_lock_threshold"] = -68
        cfg["rssi_unlock_threshold"] = -60
        cfg["away_timeout"] = 4.0
    elif dist_choice == "3":
        cfg["distance_profile"] = "medium"
        cfg["rssi_lock_threshold"] = -76
        cfg["rssi_unlock_threshold"] = -68
        cfg["away_timeout"] = 5.0
    elif dist_choice == "4":
        cfg["distance_profile"] = "far"
        cfg["rssi_lock_threshold"] = -84
        cfg["rssi_unlock_threshold"] = -76
        cfg["away_timeout"] = 6.0
    elif dist_choice == "5":
        cfg["distance_profile"] = "custom"
        lock_val = input(f"Порог блокировки RSSI (в dBm, напр. -62) [{cfg.get('rssi_lock_threshold', -62)}]: ").strip()
        if lock_val:
            cfg["rssi_lock_threshold"] = int(lock_val)
        unlock_val = input(f"Порог разблокировки RSSI (в dBm, напр. -55) [{cfg.get('rssi_unlock_threshold', -55)}]: ").strip()
        if unlock_val:
            cfg["rssi_unlock_threshold"] = int(unlock_val)

    print("\n3. ПОВЕДЕНИЕ ПРИ РУЧНОЙ БЛОКИРОВКЕ (Super+L / кнопка питания / меню):")
    print("  [1] Требовать пароль (телефон сам НЕ разблокирует при ручной блокировке) [РЕКОМЕНДУЕТСЯ]")
    print("  [2] Разблокировать телефоном в любом случае (даже после Super+L)")
    pwd_choice = input("\nВыберите [1-2] (по умолчанию 1): ").strip() or "1"
    cfg["manual_lock_requires_password"] = (pwd_choice == "1")

    save_config(cfg)
    print("\n[✓] Настройка завершена!")

    # Если сервис активен, перезапустим его
    res = subprocess.run(["systemctl", "--user", "is-active", SERVICE_NAME], stdout=subprocess.PIPE, text=True)
    if res.stdout.strip() == "active":
        print("[+] Перезапуск службы bt-proximity с новыми параметрами...")
        subprocess.run(["systemctl", "--user", "restart", SERVICE_NAME])
        print("[✓] Служба перезапущена.")


def install_service():
    """Установка и включение systemd user сервиса"""
    os.makedirs(USER_SYSTEMD_DIR, exist_ok=True)
    exec_path = os.path.realpath(__file__)

    service_content = f"""[Unit]
Description=Bluetooth Proximity Auto Lock/Unlock Daemon
After=bluetooth.target

[Service]
Type=simple
ExecStart={exec_path} run
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""
    with open(USER_SERVICE_PATH, "w", encoding="utf-8") as f:
        f.write(service_content)

    subprocess.run(["systemctl", "--user", "daemon-reload"])
    subprocess.run(["systemctl", "--user", "enable", "--now", SERVICE_NAME])
    log(f"[✓] Служба systemd успешно установлена и запущена: {USER_SERVICE_PATH}")
    log("[✓] Автозапуск включен. Служба работает в фоне.")


def uninstall_service():
    """Полное удаление bt-proximity (службы, бинарника и файлов программы)"""
    log("=============================================================")
    log("       Полное удаление bt-proximity                          ")
    log("=============================================================")

    # 1. Остановка и отключение службы systemd
    log("[1/4] Остановка и отключение службы systemd...")
    subprocess.run(["systemctl", "--user", "disable", "--now", SERVICE_NAME], stderr=subprocess.DEVNULL)
    if os.path.exists(USER_SERVICE_PATH):
        try:
            os.remove(USER_SERVICE_PATH)
            subprocess.run(["systemctl", "--user", "daemon-reload"])
            log(f"  [✓] Файл службы удален: {USER_SERVICE_PATH}")
        except Exception as e:
            log(f"  [!] Ошибка удаления службы: {e}")
    else:
        log("  [✓] Служба systemd уже отключена")

    # 2. Удаление симлинка из ~/.local/bin
    log("[2/4] Удаление команды bt-proximity...")
    bin_path = os.path.expanduser("~/.local/bin/bt-proximity")
    if os.path.exists(bin_path) or os.path.islink(bin_path):
        try:
            os.remove(bin_path)
            log(f"  [✓] Команда удалена: {bin_path}")
        except Exception as e:
            log(f"  [!] Ошибка удаления {bin_path}: {e}")

    # 3. Удаление конфигурации
    log("[3/4] Удаление конфигурации...")
    if os.path.exists(CONFIG_DIR):
        try:
            import shutil
            shutil.rmtree(CONFIG_DIR)
            log(f"  [✓] Папка настроек удалена: {CONFIG_DIR}")
        except Exception as e:
            log(f"  [!] Ошибка удаления {CONFIG_DIR}: {e}")

    # 4. Удаление файлов программы
    install_dir = os.path.expanduser("~/.local/share/bt-proximity")
    log("[4/4] Удаление файлов приложения...")
    if os.path.exists(install_dir):
        try:
            import shutil
            # Удаляем файлы внутри, сам каталог удалится при выходе
            shutil.rmtree(install_dir)
            log(f"  [✓] Каталог приложения удален: {install_dir}")
        except Exception as e:
            log(f"  [!] Ошибка удаления {install_dir}: {e}")

    log("=============================================================")
    log("  [✓] bt-proximity полностью удален с компьютера!")
    log("  (Копия установщика сохранена в ваших заметках Obsidian)")
    log("=============================================================")



def show_status():
    """Показать текущий статус сервиса и устройства"""
    cfg = load_config()
    res = subprocess.run(["systemctl", "--user", "is-active", SERVICE_NAME], stdout=subprocess.PIPE, text=True)
    svc_status = res.stdout.strip()

    print("=" * 60)
    print("      Статус bt-proximity")
    print("=" * 60)
    print(f"  Служба в фоне (systemd):  {svc_status.upper()}")
    print(f"  Устройство:              {cfg.get('device_name')} ({cfg.get('device_mac')})")
    print(f"  Профиль дальности:       {cfg.get('distance_profile')}")
    print(f"  Порог блокировки:        {cfg.get('rssi_lock_threshold')} dBm")
    print(f"  Порог разблокировки:     {cfg.get('rssi_unlock_threshold')} dBm")
    print(f"  Таймаут отсутствия:      {cfg.get('away_timeout')} сек")
    pwd_req = "ДА (пароль обязателен)" if cfg.get("manual_lock_requires_password", True) else "НЕТ (телефон разблокирует)"
    print(f"  Блокировка с кнопки:     {pwd_req}")
    print("=" * 60)

    # Проверим, доступен ли телефон прямо сейчас
    try:
        bus = dbus.SystemBus()
        dev_path = mac_to_path(cfg["device_mac"])
        dev_obj = bus.get_object("org.bluez", dev_path)
        props = dbus.Interface(dev_obj, "org.freedesktop.DBus.Properties")
        all_p = props.GetAll("org.bluez.Device1")
        connected = "ДА" if all_p.get("Connected", 0) else "НЕТ"
        rssi = all_p.get("RSSI", "нет данных")
        print(f"  Текущая связь с телефоном: Подключен: {connected} | RSSI: {rssi} dBm")
    except Exception:
        print("  Текущая связь с телефоном: Устройство вне зоны или выключено")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="bt-proximity: Блокировка и разблокировка Linux по Bluetooth/BLE")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "monitor", "test", "status", "config", "scan", "install", "uninstall", "start", "stop", "restart", "pause", "resume"],
                        help="Команда для выполнения")

    args = parser.parse_args()

    if args.command == "config":
        interactive_config()
    elif args.command == "scan":
        devices = scan_devices()
        print("\nНайдено устройств:")
        for d in devices:
            print(f"  {d['mac']}  ->  {d['name']}")
    elif args.command in ["monitor", "test"]:
        cfg = load_config()
        daemon = ProximityDaemon(cfg, is_monitor_only=True)
        daemon.run()
    elif args.command == "status":
        show_status()
    elif args.command in ["start", "resume"]:
        subprocess.run(["systemctl", "--user", "start", SERVICE_NAME])
        print("[✓] Служба bt-proximity запущена.")
    elif args.command in ["stop", "pause"]:
        subprocess.run(["systemctl", "--user", "stop", SERVICE_NAME])
        print("[✓] Служба bt-proximity временно остановлена. Экран не будет блокироваться по Bluetooth.")
    elif args.command == "restart":
        subprocess.run(["systemctl", "--user", "restart", SERVICE_NAME])
        print("[✓] Служба bt-proximity перезапущена.")
    elif args.command == "install":
        install_service()
    elif args.command == "uninstall":
        uninstall_service()
    elif args.command == "run":
        cfg = load_config()
        daemon = ProximityDaemon(cfg, is_monitor_only=False)
        daemon.run()


if __name__ == "__main__":
    main()
