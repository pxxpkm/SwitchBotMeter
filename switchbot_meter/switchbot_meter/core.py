"""Core: config, psychrometrics, BLE parse, history store, alerts."""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import sys
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import threading

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

# One BLE scan at a time — Windows/WinRT rejects overlapping scanners.
_ble_lock = threading.Lock()


class _BleLoop:
    """Long-lived asyncio loop on a daemon thread (avoids asyncio.run per scan)."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="sbm-ble-loop", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=8)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()

    def run(self, coro, timeout: float | None = None):
        self.start()
        if self._loop is None:
            raise RuntimeError("BLE event loop failed to start")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout)

    def stop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)


_ble_loop = _BleLoop()


def app_dir() -> Path:
    """Directory for user-writable files (config, data).

    When frozen by PyInstaller, use the folder containing the .exe so settings
    survive upgrades and are easy to find. Source tree uses this package dir.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    """Read-only bundled resources (PyInstaller _MEIPASS or source dir)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
DEFAULT_CONFIG_PATH = APP_DIR / "config.json"

SWITCHBOT_COMPANY_ID = 0x0969
SWITCHBOT_COMPANY_IDS = {0x0969, 0x0059}  # 0x0969 SwitchBot; 0x0059 older Nordic
# Service-data Byte0 bit[6:0] device types that carry TH readings
METER_DEVICE_TYPES = {
    0x54,  # 'T' Meter / WoSensorTH
    0x74,  # 't' Meter pairing
    0x69,  # 'i' Meter Plus
    0x49,  # 'I' Meter Plus (alt)
    0x77,  # 'w' Indoor/Outdoor Meter
}
METER_TYPE_LABELS = {
    0x54: "Meter",
    0x74: "Meter（配對模式）",
    0x69: "Meter Plus",
    0x49: "Meter Plus",
    0x77: "Outdoor Meter",
}
# Known SwitchBot types that are not thermometers (do not parse as TH)
NON_METER_DEVICE_TYPES = {
    0x48, 0x42,  # Bot / Button
    0x4C, 0x6C, 0x50, 0x70,  # Hub
    0x46, 0x66,  # Fan
    0x4D, 0x6D,  # Hub Mini
}

METRIC_LABELS = {
    "temperature_c": "溫度 (°C)",
    "humidity_pct": "濕度 (%)",
    "abs_humidity": "絕對濕度 (g/m³)",
    "dew_point_c": "露點 (°C)",
    "vpd_kpa": "VPD (kPa)",
    "battery_pct": "電量 (%)",
    "rssi": "RSSI (dBm)",
}

OPS = {
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: abs(a - b) < 1e-9,
}


# ── Psychrometrics (match SwitchBot app fields) ──────────────────────────────

def dew_point_c(temp_c: float, rh: float) -> float:
    """Magnus formula dew point (°C)."""
    rh = max(0.01, min(100.0, rh))
    a, b = 17.62, 243.12
    gamma = (a * temp_c) / (b + temp_c) + math.log(rh / 100.0)
    return (b * gamma) / (a - gamma)


def absolute_humidity_gm3(temp_c: float, rh: float) -> float:
    """Absolute humidity in g/m³."""
    rh = max(0.0, min(100.0, rh))
    # Saturation vapor pressure (hPa), Buck-ish
    es = 6.112 * math.exp((17.62 * temp_c) / (243.12 + temp_c))
    e = es * (rh / 100.0)
    return 216.7 * e / (273.15 + temp_c)


def vpd_kpa(temp_c: float, rh: float) -> float:
    """Vapor pressure deficit (kPa)."""
    rh = max(0.0, min(100.0, rh))
    # Tetens
    svp = 0.6108 * math.exp((17.27 * temp_c) / (temp_c + 237.3))
    avp = svp * (rh / 100.0)
    return max(0.0, svp - avp)


# ── Reading model ────────────────────────────────────────────────────────────

@dataclass
class Reading:
    temperature_c: float
    humidity_pct: float
    battery_pct: int | None
    rssi: int | None
    address: str
    at: float
    abs_humidity: float = 0.0
    dew_point_c: float = 0.0
    vpd_kpa: float = 0.0

    def with_derived(self) -> "Reading":
        self.abs_humidity = absolute_humidity_gm3(self.temperature_c, self.humidity_pct)
        self.dew_point_c = dew_point_c(self.temperature_c, self.humidity_pct)
        self.vpd_kpa = vpd_kpa(self.temperature_c, self.humidity_pct)
        return self

    def metric(self, name: str) -> float | None:
        if name == "temperature_c":
            return self.temperature_c
        if name == "humidity_pct":
            return float(self.humidity_pct)
        if name == "abs_humidity":
            return self.abs_humidity
        if name == "dew_point_c":
            return self.dew_point_c
        if name == "vpd_kpa":
            return self.vpd_kpa
        if name == "battery_pct":
            return float(self.battery_pct) if self.battery_pct is not None else None
        if name == "rssi":
            return float(self.rssi) if self.rssi is not None else None
        return None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["at_iso"] = datetime.fromtimestamp(self.at).isoformat(timespec="seconds")
        return d


# ── Config ───────────────────────────────────────────────────────────────────

def default_config() -> dict[str, Any]:
    return {
        "device": {
            "name": "Meter Plus",
            "mac": "",
            "scan_seconds": 4.0,
            "interval_seconds": 20.0,
        },
        "display": {
            "opacity": 0.90,
            "always_on_top": True,
            "compact": False,
            "show_abs_humidity": True,
            "show_dew_point": True,
            "show_vpd": True,
            "show_battery": True,
            # Temporarily clear always-on-top while another app is fullscreen (games).
            "drop_topmost_when_fullscreen": True,
            "width": 280,
            "height": 0,
            "position": {"x": 80, "y": 80},
        },
        "alerts": {
            "enabled": True,
            "cooldown_minutes": 15,
            "rearm_margin": 0.5,
            "windows_toast": True,
            "sound": True,
            # Custom alert WAV (app-level audio; works even if Windows "system sounds" are off).
            # Empty → use built-in sounds/alert.wav (auto-generated on first use).
            "sound_file": "",
            # When another app is fullscreen (games), skip Windows toast popups.
            "quiet_when_fullscreen": True,
            # Also mute alert sound during fullscreen (default: still play sound).
            "quiet_sound_when_fullscreen": False,
            "rules": [
                {
                    "id": "hot_humid",
                    "enabled": True,
                    "label": "高溫高濕",
                    "logic": "and",
                    "message": "室溫偏高且潮濕，注意降溫通風、補充水分",
                    "conditions": [
                        {"metric": "temperature_c", "op": ">=", "value": 30.0},
                        {"metric": "humidity_pct", "op": ">=", "value": 60.0},
                    ],
                },
                {
                    "id": "temp_high",
                    "enabled": True,
                    "label": "高溫",
                    "logic": "and",
                    "message": "室溫偏高，建議開冷氣或加強通風",
                    "conditions": [
                        {"metric": "temperature_c", "op": ">=", "value": 30.0},
                    ],
                },
                {
                    "id": "temp_very_high",
                    "enabled": True,
                    "label": "酷熱",
                    "logic": "and",
                    "message": "室溫過高，請注意中暑風險",
                    "conditions": [
                        {"metric": "temperature_c", "op": ">=", "value": 33.0},
                    ],
                },
                {
                    "id": "stuffy",
                    "enabled": True,
                    "label": "悶熱",
                    "logic": "and",
                    "message": "偏熱又偏濕，體感悶熱，建議除濕或開冷氣",
                    "conditions": [
                        {"metric": "temperature_c", "op": ">=", "value": 28.0},
                        {"metric": "humidity_pct", "op": ">=", "value": 70.0},
                    ],
                },
                {
                    "id": "temp_low",
                    "enabled": True,
                    "label": "低溫",
                    "logic": "and",
                    "message": "室溫偏低，注意保暖",
                    "conditions": [
                        {"metric": "temperature_c", "op": "<=", "value": 18.0},
                    ],
                },
                {
                    "id": "hum_high_mold",
                    "enabled": True,
                    "label": "高濕易霉",
                    "logic": "and",
                    "message": "濕度偏高，易發霉，建議除濕",
                    "conditions": [
                        {"metric": "humidity_pct", "op": ">=", "value": 75.0},
                    ],
                },
                {
                    "id": "hum_low_dry",
                    "enabled": True,
                    "label": "過乾",
                    "logic": "and",
                    "message": "空氣偏乾，可考慮加濕",
                    "conditions": [
                        {"metric": "humidity_pct", "op": "<=", "value": 35.0},
                    ],
                },
                {
                    "id": "dew_high",
                    "enabled": False,
                    "label": "露點偏高",
                    "logic": "and",
                    "message": "露點偏高，牆面／玻璃較易結露",
                    "conditions": [
                        {"metric": "dew_point_c", "op": ">=", "value": 24.0},
                    ],
                },
                {
                    "id": "vpd_high",
                    "enabled": False,
                    "label": "VPD 偏高",
                    "logic": "and",
                    "message": "VPD 偏高（植物蒸散偏快），可留意加濕或遮蔭",
                    "conditions": [
                        {"metric": "vpd_kpa", "op": ">=", "value": 1.5},
                    ],
                },
                {
                    "id": "vpd_low",
                    "enabled": False,
                    "label": "VPD 偏低",
                    "logic": "and",
                    "message": "VPD 偏低（偏潮濕悶），通風／除濕較佳",
                    "conditions": [
                        {"metric": "vpd_kpa", "op": "<=", "value": 0.4},
                    ],
                },
                {
                    "id": "comfort_out",
                    "enabled": False,
                    "label": "舒適區外",
                    "logic": "or",
                    "message": "已離開常見舒適區間（約 20–28°C、40–70% 濕度）",
                    "conditions": [
                        {"metric": "temperature_c", "op": "<", "value": 20.0},
                        {"metric": "temperature_c", "op": ">", "value": 28.0},
                        {"metric": "humidity_pct", "op": "<", "value": 40.0},
                        {"metric": "humidity_pct", "op": ">", "value": 70.0},
                    ],
                },
                {
                    "id": "battery_low",
                    "enabled": True,
                    "label": "電量低",
                    "logic": "and",
                    "message": "溫度計電量偏低，請盡快換電池",
                    "conditions": [
                        {"metric": "battery_pct", "op": "<=", "value": 15.0},
                    ],
                },
            ],
        },
        "history": {
            "enabled": True,
            "db_path": "data/history.db",
            "keep_days": 365,
        },
        # Current-user Run key; no admin. Re-applied on launch if True (path may move).
        "start_with_windows": False,
    }


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or (app_dir() / "config.json")
    base = default_config()
    if not path.exists():
        # Prefer bundled default next to resources, then write user copy
        bundled = bundle_dir() / "config.json"
        if bundled.exists() and bundled != path:
            try:
                with open(bundled, "r", encoding="utf-8") as f:
                    base = _deep_merge(base, json.load(f))
            except Exception:
                pass
        save_config(base, path)
        return base
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return _deep_merge(base, data)


def save_config(cfg: dict[str, Any], path: Path | None = None) -> None:
    path = path or (app_dir() / "config.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = deepcopy(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ── Windows startup (HKCU Run) ───────────────────────────────────────────────

STARTUP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE_NAME = "SwitchBotMeter"


def startup_launch_command() -> str:
    """Quoted command that starts this app (frozen exe, or pythonw + app.py)."""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    py = Path(sys.executable).resolve()
    pythonw = py.with_name("pythonw.exe")
    if pythonw.is_file():
        py = pythonw
    script = Path(__file__).resolve().parent / "app.py"
    return f'"{py}" "{script}"'


def windows_startup_command(name: str = STARTUP_VALUE_NAME) -> str | None:
    """Current HKCU Run value, or None if missing / not Windows."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY) as key:
            value, _typ = winreg.QueryValueEx(key, name)
        text = str(value or "").strip()
        return text or None
    except OSError:
        return None
    except Exception:
        return None


def is_windows_startup_enabled(name: str = STARTUP_VALUE_NAME) -> bool:
    return windows_startup_command(name) is not None


def set_windows_startup(
    enabled: bool,
    name: str = STARTUP_VALUE_NAME,
    command: str | None = None,
) -> None:
    """Add or remove this app from the current user's Windows startup list."""
    if sys.platform != "win32":
        if enabled:
            raise RuntimeError("僅支援 Windows 開機啟動")
        return
    import winreg

    if enabled:
        cmd = (command if command is not None else startup_launch_command()).strip()
        if not cmd:
            raise RuntimeError("無法組出啟動命令")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, cmd)
        return
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        return
    except OSError as e:
        # ERROR_FILE_NOT_FOUND — value already gone
        if getattr(e, "winerror", None) == 2:
            return
        raise


# ── BLE ──────────────────────────────────────────────────────────────────────

def _norm_uuid(u: str) -> str:
    return u.lower().replace("-", "")


def _service_payload(adv: AdvertisementData) -> bytes | None:
    """Return SwitchBot service-data bytes only (0xFD3D / legacy CBA2).

    Windows/Bleak often expands 16-bit UUID 0xFD3D to
    ``0000fd3d-0000-1000-8000-00805f9b34fb``, so ``endswith('fd3d')`` misses it.
    """
    if not adv.service_data:
        return None
    for key, data in adv.service_data.items():
        k = _norm_uuid(str(key))
        if "fd3d" in k or "cba20d00" in k or "00000d00" in k:
            return bytes(data)
    return None


def _decode_th3(temp_data: bytes) -> tuple[float, float, bool] | None:
    """3-byte TH block used by both service data[3:6] and manufacturer data[8:11].

    Temperature integers are always °C. Bit 7 of the humidity byte is the
    *display* unit on the device screen, not the wire encoding.
    """
    if len(temp_data) < 3:
        return None
    temp = (temp_data[1] & 0x7F) + (temp_data[0] & 0x0F) / 10.0
    if not (temp_data[1] & 0x80):
        temp = -temp
    humidity = temp_data[2] & 0x7F
    display_f = bool(temp_data[2] & 0x80)
    if not (0 <= humidity <= 99):
        return None
    if not (-40.0 <= temp <= 85.0):
        return None
    return temp, float(humidity), display_f


def _strip_company_id(raw: bytes) -> bytes:
    if len(raw) >= 2 and raw[0] == 0x69 and raw[1] == 0x09:
        return raw[2:]
    return raw


def parse_meter_service_data(data: bytes) -> dict | None:
    """Parse WoSensorTH / Meter Plus *service data only* (tests + fallback)."""
    if len(data) < 6:
        return None
    dtype = data[0] & 0x7F
    if dtype in NON_METER_DEVICE_TYPES:
        return None
    th = _decode_th3(data[3:6])
    if th is None:
        return None
    temp, humidity, display_f = th
    battery: int | None = None
    if len(data) >= 3:
        b = data[2] & 0x7F
        if 0 <= b <= 100:
            battery = b
    return {
        "device_type": dtype,
        "battery_pct": battery,
        "temperature_c": temp,
        "humidity_pct": humidity,
        "display_f": display_f,
    }


def parse_meter_manufacturer_data(data: bytes) -> dict | None:
    """TH from manufacturer payload. Battery is *not* in this block — do not invent it."""
    raw = _strip_company_id(bytes(data))
    if len(raw) < 11:
        return None
    th = _decode_th3(raw[8:11])
    if th is None:
        return None
    temp, humidity, display_f = th
    return {
        "device_type": None,
        "battery_pct": None,
        "temperature_c": temp,
        "humidity_pct": humidity,
        "display_f": display_f,
    }


def parse_advertisement(adv: AdvertisementData) -> dict | None:
    """Merge service + manufacturer data the way pySwitchbot does.

    - Temperature / humidity: manufacturer data[8:11] if present, else service[3:6]
    - Battery: **only** service data byte 2. Manufacturer bytes are MAC / flags,
      not battery (reading them produced fake 11% / 0% values).
    """
    svc = _service_payload(adv)
    mfg_blob: bytes | None = None
    mfg = getattr(adv, "manufacturer_data", None) or {}
    for cid in SWITCHBOT_COMPANY_IDS:
        blob = mfg.get(cid)
        if blob:
            mfg_blob = _strip_company_id(bytes(blob))
            break

    dtype: int | None = None
    battery: int | None = None
    th: tuple[float, float, bool] | None = None

    if svc:
        dtype = svc[0] & 0x7F
        if dtype in NON_METER_DEVICE_TYPES:
            return None
        if len(svc) >= 3:
            b = svc[2] & 0x7F
            if 0 <= b <= 100:
                battery = b
        if len(svc) >= 6:
            th = _decode_th3(svc[3:6])

    if mfg_blob and len(mfg_blob) >= 11:
        th_m = _decode_th3(mfg_blob[8:11])
        if th_m:
            th = th_m

    if th is None:
        return None
    temp, humidity, display_f = th
    return {
        "device_type": dtype,
        "battery_pct": battery,
        "temperature_c": temp,
        "humidity_pct": humidity,
        "display_f": display_f,
    }


def advertisement_is_switchbot(adv: AdvertisementData, name: str = "") -> bool:
    if _service_payload(adv) is not None:
        return True
    mfg = getattr(adv, "manufacturer_data", None) or {}
    if any(cid in mfg for cid in SWITCHBOT_COMPANY_IDS):
        return True
    n = (name or "").lower()
    return any(k in n for k in ("meter", "wosensor", "switchbot", "溫濕", "温湿"))


def reading_from_advertisement(device: BLEDevice, adv: AdvertisementData) -> Reading | None:
    parsed = parse_advertisement(adv)
    if not parsed:
        return None
    bat = parsed.get("battery_pct")
    return Reading(
        temperature_c=parsed["temperature_c"],
        humidity_pct=parsed["humidity_pct"],
        battery_pct=int(bat) if bat is not None else None,
        rssi=adv.rssi,
        address=device.address.upper(),
        at=time.time(),
    ).with_derived()


@dataclass
class DiscoveredMeter:
    address: str
    name: str
    rssi: int | None
    device_type: int | None
    device_label: str
    temperature_c: float | None
    humidity_pct: float | None
    battery_pct: int | None


def _meter_label(dtype: int | None, name: str) -> str:
    if dtype in METER_TYPE_LABELS:
        return METER_TYPE_LABELS[dtype]
    if name:
        return name
    return "SwitchBot"


async def scan_once(mac: str, duration: float) -> Reading | None:
    best: Reading | None = None
    target = (mac or "").upper().strip()
    if not target:
        return None

    def on_detect(device: BLEDevice, adv: AdvertisementData) -> None:
        nonlocal best
        if device.address.upper() != target:
            return
        reading = reading_from_advertisement(device, adv)
        if not reading:
            return
        # Battery only appears on service-data packets; keep last known
        if reading.battery_pct is None and best is not None:
            reading.battery_pct = best.battery_pct
        # Prefer a packet that has battery; otherwise strongest RSSI
        if best is None:
            best = reading
            return
        new_has_bat = reading.battery_pct is not None
        old_has_bat = best.battery_pct is not None
        if new_has_bat and not old_has_bat:
            best = reading
            return
        if old_has_bat and not new_has_bat:
            if reading.rssi is not None and (best.rssi is None or reading.rssi > best.rssi):
                reading.battery_pct = best.battery_pct
                best = reading
            return
        if (reading.rssi or -999) >= (best.rssi or -999):
            if reading.battery_pct is None:
                reading.battery_pct = best.battery_pct
            best = reading

    async with BleakScanner(detection_callback=on_detect):
        await asyncio.sleep(duration)
    return best


def scan_once_sync(mac: str, duration: float) -> Reading | None:
    with _ble_lock:
        return _ble_loop.run(scan_once(mac, duration), timeout=max(8.0, float(duration) + 20.0))


async def discover_meters(duration: float = 8.0) -> list[DiscoveredMeter]:
    """Active scan: collect nearby SwitchBot meters (TH if the packet has it)."""
    found: dict[str, DiscoveredMeter] = {}

    def on_detect(device: BLEDevice, adv: AdvertisementData) -> None:
        addr = device.address.upper()
        name = (device.name or getattr(adv, "local_name", None) or "") or ""
        parsed = parse_advertisement(adv)
        if parsed is None and not advertisement_is_switchbot(adv, name):
            return
        dtype = int(parsed["device_type"]) if parsed else None
        if dtype is not None and dtype in NON_METER_DEVICE_TYPES:
            return
        rssi = adv.rssi
        prev = found.get(addr)
        # Prefer a result that actually has a reading; then stronger RSSI
        if prev is not None:
            prev_has = prev.temperature_c is not None
            new_has = parsed is not None
            if prev_has and not new_has:
                if rssi is not None and (prev.rssi is None or rssi > prev.rssi):
                    found[addr] = DiscoveredMeter(
                        address=addr,
                        name=name or prev.name,
                        rssi=rssi,
                        device_type=prev.device_type,
                        device_label=prev.device_label,
                        temperature_c=prev.temperature_c,
                        humidity_pct=prev.humidity_pct,
                        battery_pct=prev.battery_pct,
                    )
                return
            if prev_has and new_has and rssi is not None and prev.rssi is not None and rssi < prev.rssi:
                return
        label = _meter_label(dtype, name)
        found[addr] = DiscoveredMeter(
            address=addr,
            name=name or label,
            rssi=rssi,
            device_type=dtype,
            device_label=label,
            temperature_c=parsed["temperature_c"] if parsed else None,
            humidity_pct=parsed["humidity_pct"] if parsed else None,
            battery_pct=int(parsed["battery_pct"]) if parsed and parsed.get("battery_pct") is not None else None,
        )

    async with BleakScanner(detection_callback=on_detect):
        await asyncio.sleep(duration)

    def sort_key(m: DiscoveredMeter) -> tuple:
        return (
            0 if m.temperature_c is not None else 1,
            -(m.rssi if m.rssi is not None else -999),
            m.address,
        )

    return sorted(found.values(), key=sort_key)


def discover_meters_sync(duration: float = 8.0) -> list[DiscoveredMeter]:
    with _ble_lock:
        return _ble_loop.run(
            discover_meters(duration), timeout=max(12.0, float(duration) + 20.0)
        )


async def ble_radio_state() -> str:
    """'ok' | 'off' | 'missing' | 'unknown' — Windows radio via WinRT when available."""
    try:
        from winrt.windows.devices.radios import Radio, RadioKind, RadioState

        radios = await Radio.get_radios_async()
        bt = [r for r in radios if r.kind == RadioKind.BLUETOOTH]
        if not bt:
            return "missing"
        if any(r.state == RadioState.ON for r in bt):
            return "ok"
        return "off"
    except Exception:
        return "unknown"


def ble_radio_state_sync() -> str:
    try:
        return _ble_loop.run(ble_radio_state(), timeout=8.0)
    except Exception:
        return "unknown"


# ── History (SQLite) ─────────────────────────────────────────────────────────

class HistoryStore:
    def __init__(self, db_path: Path, keep_days: int = 365) -> None:
        self.db_path = db_path
        self.keep_days = keep_days
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    ts_iso TEXT NOT NULL,
                    mac TEXT,
                    temperature_c REAL,
                    humidity_pct REAL,
                    abs_humidity REAL,
                    dew_point_c REAL,
                    vpd_kpa REAL,
                    battery_pct INTEGER,
                    rssi INTEGER
                )
                """
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts)")

    def add(self, r: Reading) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO readings
                (ts, ts_iso, mac, temperature_c, humidity_pct, abs_humidity,
                 dew_point_c, vpd_kpa, battery_pct, rssi)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r.at,
                    datetime.fromtimestamp(r.at).isoformat(timespec="seconds"),
                    r.address,
                    r.temperature_c,
                    r.humidity_pct,
                    r.abs_humidity,
                    r.dew_point_c,
                    r.vpd_kpa,
                    r.battery_pct,
                    r.rssi,
                ),
            )

    def purge_old(self) -> int:
        cutoff = time.time() - self.keep_days * 86400
        with self._conn() as c:
            cur = c.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
            return cur.rowcount

    def _where(
        self, since: float | None, until: float | None
    ) -> tuple[str, list[Any]]:
        sql = " WHERE 1=1"
        params: list[Any] = []
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        if until is not None:
            sql += " AND ts <= ?"
            params.append(until)
        return sql, params

    def count(self, since: float | None = None, until: float | None = None) -> int:
        where, params = self._where(since, until)
        with self._conn() as c:
            row = c.execute(f"SELECT COUNT(*) FROM readings{where}", params).fetchone()
        return int(row[0]) if row else 0

    def query(
        self,
        since: float | None = None,
        until: float | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        where, params = self._where(since, until)
        sql = f"SELECT * FROM readings{where} ORDER BY ts ASC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def query_series(
        self,
        since: float | None = None,
        until: float | None = None,
        max_points: int = 2000,
    ) -> list[dict[str, Any]]:
        """Evenly sampled points across the window (for charts).

        Avoids ``ORDER BY ts ASC LIMIT N`` which keeps only the *oldest* N rows
        and drops the recent end of week/month views.
        """
        max_points = max(2, int(max_points))
        n = self.count(since, until)
        if n <= max_points:
            return self.query(since=since, until=until, limit=max(n, 0) or 1) if n else []

        t0 = float(since) if since is not None else 0.0
        t1 = float(until) if until is not None else time.time()
        if since is None or until is None:
            with self._conn() as c:
                where, params = self._where(since, until)
                ext = c.execute(
                    f"SELECT MIN(ts), MAX(ts) FROM readings{where}", params
                ).fetchone()
            if not ext or ext[0] is None:
                return []
            if since is None:
                t0 = float(ext[0])
            if until is None:
                t1 = float(ext[1])
        span = max(1.0, t1 - t0)
        bucket = span / float(max_points)
        where, params = self._where(since, until)
        sql = f"""
            SELECT
              MIN(ts) AS ts,
              MIN(ts_iso) AS ts_iso,
              MAX(mac) AS mac,
              AVG(temperature_c) AS temperature_c,
              AVG(humidity_pct) AS humidity_pct,
              AVG(abs_humidity) AS abs_humidity,
              AVG(dew_point_c) AS dew_point_c,
              AVG(vpd_kpa) AS vpd_kpa,
              AVG(battery_pct) AS battery_pct,
              AVG(rssi) AS rssi
            FROM readings
            {where}
            GROUP BY CAST((ts - ?) / ? AS INTEGER)
            ORDER BY ts ASC
        """
        params.extend([t0, bucket])
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def stats(self, since: float | None = None) -> dict[str, Any]:
        where, params = self._where(since, None)
        sql = f"""
            SELECT
              COUNT(*) AS count,
              MIN(temperature_c) AS temp_min,
              MAX(temperature_c) AS temp_max,
              AVG(temperature_c) AS temp_avg,
              MIN(humidity_pct) AS hum_min,
              MAX(humidity_pct) AS hum_max,
              AVG(humidity_pct) AS hum_avg
            FROM readings
            {where}
        """
        with self._conn() as c:
            row = c.execute(sql, params).fetchone()
        if not row or not row["count"]:
            return {}
        return {
            "count": int(row["count"]),
            "temp_min": row["temp_min"],
            "temp_max": row["temp_max"],
            "temp_avg": row["temp_avg"],
            "hum_min": row["hum_min"],
            "hum_max": row["hum_max"],
            "hum_avg": row["hum_avg"],
        }

    def export_csv(self, path: Path, since: float | None = None) -> int:
        rows = self.query(since=since, limit=1000000)
        path.parent.mkdir(parents=True, exist_ok=True)
        headers = [
            "ts_iso",
            "mac",
            "temperature_c",
            "humidity_pct",
            "abs_humidity",
            "dew_point_c",
            "vpd_kpa",
            "battery_pct",
            "rssi",
        ]
        import csv

        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({h: "" if r.get(h) is None else r.get(h) for h in headers})
        return len(rows)

    def import_csv(self, path: Path, mac: str = "") -> int:
        raw = Path(path).read_text(encoding="utf-8-sig")
        parsed = parse_history_csv(raw)
        n = 0
        for item in parsed:
            ts = float(item["ts"])
            if self._has_near(ts, mac):
                continue
            temp = float(item["temperature_c"])
            hum = float(item["humidity_pct"])
            r = Reading(
                temperature_c=temp,
                humidity_pct=hum,
                battery_pct=item.get("battery_pct"),
                rssi=None,
                address=str(item.get("mac") or mac or ""),
                at=ts,
            ).with_derived()
            self.add(r)
            n += 1
        return n

    def _has_near(self, ts: float, mac: str, slop: float = 0.6) -> bool:
        with self._conn() as c:
            if mac:
                row = c.execute(
                    "SELECT 1 FROM readings WHERE mac=? AND ts BETWEEN ? AND ? LIMIT 1",
                    (mac, ts - slop, ts + slop),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT 1 FROM readings WHERE ts BETWEEN ? AND ? LIMIT 1",
                    (ts - slop, ts + slop),
                ).fetchone()
        return row is not None


def parse_history_csv(text: str) -> list[dict[str, Any]]:
    """Parse this app's CSV or a SwitchBot App export into reading dicts."""
    import csv
    import io

    text = (text or "").lstrip("\ufeff")
    if not text.strip():
        return []
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return []

    def key_of(name: str) -> str:
        n = name.strip().lower().replace(" ", "").replace("_", "")
        n = n.replace("（", "(").replace("）", ")")
        return n

    fields = {key_of(h): h for h in reader.fieldnames if h}

    def pick(*needles: str) -> str | None:
        for needle in needles:
            for k, orig in fields.items():
                if needle in k:
                    return orig
        return None

    time_col = pick("tsiso", "time", "date", "時間", "时间", "日時", "日時")
    temp_col = pick("temperaturec", "temperature", "temp", "溫度", "温度")
    hum_col = pick("humiditypct", "humidity", "humid", "濕度", "湿度")
    mac_col = pick("mac")
    bat_col = pick("batterypct", "battery")
    if not time_col or not temp_col or not hum_col:
        return []

    out: list[dict[str, Any]] = []
    for row in reader:
        try:
            ts = _parse_csv_time(str(row.get(time_col) or ""))
            temp = float(str(row.get(temp_col, "")).replace("℃", "").replace("°C", "").strip())
            hum = float(str(row.get(hum_col, "")).replace("%", "").strip())
        except (TypeError, ValueError):
            continue
        if not (-40.0 <= temp <= 85.0) or not (0.0 <= hum <= 100.0):
            continue
        bat = None
        if bat_col:
            try:
                bat = int(float(str(row.get(bat_col) or "").strip()))
            except (TypeError, ValueError):
                bat = None
        mac = str(row.get(mac_col) or "").strip() if mac_col else ""
        out.append(
            {
                "ts": ts,
                "temperature_c": temp,
                "humidity_pct": hum,
                "battery_pct": bat,
                "mac": mac,
            }
        )
    return out


def _parse_csv_time(value: str) -> float:
    value = value.strip()
    if not value:
        raise ValueError("empty time")
    try:
        return float(value)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            return datetime.strptime(value.replace("Z", ""), fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "")).timestamp()
    except ValueError as exc:
        raise ValueError(f"unparsed time: {value}") from exc


# ── Alert rules (multi-condition) ────────────────────────────────────────────

def rule_conditions(rule: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize a rule to a list of conditions (legacy single-metric supported)."""
    conds = rule.get("conditions")
    if isinstance(conds, list) and conds:
        out: list[dict[str, Any]] = []
        for c in conds:
            if not isinstance(c, dict):
                continue
            metric = c.get("metric")
            if not metric:
                continue
            try:
                val = float(c.get("value", 0))
            except (TypeError, ValueError):
                continue
            out.append(
                {
                    "metric": str(metric),
                    "op": str(c.get("op", ">=")),
                    "value": val,
                }
            )
        if out:
            return out
    # Legacy flat fields
    if rule.get("metric"):
        try:
            val = float(rule.get("value", 0))
        except (TypeError, ValueError):
            val = 0.0
        return [
            {
                "metric": str(rule.get("metric")),
                "op": str(rule.get("op", ">=")),
                "value": val,
            }
        ]
    return []


def rule_logic(rule: dict[str, Any]) -> str:
    logic = str(rule.get("logic", "and")).lower().strip()
    return "or" if logic == "or" else "and"


def format_condition(cond: dict[str, Any], *, with_value: float | None = None) -> str:
    metric = str(cond.get("metric", ""))
    name = METRIC_LABELS.get(metric, metric)
    op = str(cond.get("op", ">="))
    thr = float(cond.get("value", 0))
    if with_value is None:
        return f"{name} {op} {thr:g}"
    return f"{name} {with_value:.1f} {op} {thr:g}"


def format_rule_summary(rule: dict[str, Any]) -> str:
    conds = rule_conditions(rule)
    if not conds:
        return "（無條件）"
    joiner = " 或 " if rule_logic(rule) == "or" else " 且 "
    return joiner.join(format_condition(c) for c in conds)


def _cond_matches(reading: Reading, cond: dict[str, Any]) -> bool | None:
    """True/False if evaluable; None if metric missing or op unknown."""
    metric = str(cond.get("metric", ""))
    op = str(cond.get("op", ">="))
    cmp = OPS.get(op)
    if not cmp:
        return None
    val = reading.metric(metric)
    if val is None:
        return None
    try:
        thr = float(cond.get("value", 0))
    except (TypeError, ValueError):
        return None
    return bool(cmp(val, thr))


def _cond_cleared(reading: Reading, cond: dict[str, Any], margin: float) -> bool:
    """Whether this condition has clearly left the active band (for re-arm)."""
    metric = str(cond.get("metric", ""))
    op = str(cond.get("op", ">="))
    val = reading.metric(metric)
    if val is None:
        return False
    try:
        thr = float(cond.get("value", 0))
    except (TypeError, ValueError):
        return False
    if op in (">=", ">"):
        return val <= thr - margin
    if op in ("<=", "<"):
        return val >= thr + margin
    # equality / unknown: cleared when not matching
    m = _cond_matches(reading, cond)
    return m is False


def rule_is_active(reading: Reading, rule: dict[str, Any]) -> bool:
    """Whether all/any conditions currently match (AND/OR)."""
    conds = rule_conditions(rule)
    if not conds:
        return False
    results: list[bool] = []
    for c in conds:
        m = _cond_matches(reading, c)
        if m is None:
            return False
        results.append(m)
    if rule_logic(rule) == "or":
        return any(results)
    return all(results)


def rule_is_cleared(reading: Reading, rule: dict[str, Any], margin: float) -> bool:
    """Re-arm when the compound rule is safely inactive (with margin)."""
    conds = rule_conditions(rule)
    if not conds:
        return True
    if rule_is_active(reading, rule):
        return False
    cleared = [_cond_cleared(reading, c, margin) for c in conds]
    if rule_logic(rule) == "or":
        # OR: only re-arm when every branch is clearly clear
        return all(cleared)
    # AND: re-arm when any condition is clearly clear (AND cannot hold)
    return any(cleared)


def format_rule_message(rule: dict[str, Any], reading: Reading) -> str:
    """Toast / alert body: custom 通知文字 first, then live readings."""
    custom = str(rule.get("message") or "").strip()
    label = str(rule.get("label") or rule.get("id") or "警報")
    conds = rule_conditions(rule)
    parts: list[str] = []
    for c in conds:
        val = reading.metric(str(c.get("metric", "")))
        if val is None:
            parts.append(format_condition(c))
        else:
            parts.append(format_condition(c, with_value=val))
    joiner = " 或 " if rule_logic(rule) == "or" else "；"
    detail = joiner.join(parts) if parts else ""
    # Prefer user-written 通知文字 as the main body (Windows toast text line).
    if custom and detail:
        return f"{custom}\n{detail}"
    if custom:
        return custom
    if detail:
        return detail
    return label


# ── Alert engine ─────────────────────────────────────────────────────────────

class AlertEngine:
    def __init__(self, alerts_cfg: dict[str, Any]) -> None:
        self.cfg = alerts_cfg
        # rule_id -> last fire time
        self._last_fire: dict[str, float] = {}
        # rule_id -> armed
        self._armed: dict[str, bool] = {}

    def update_config(self, alerts_cfg: dict[str, Any]) -> None:
        self.cfg = alerts_cfg

    def evaluate(self, reading: Reading) -> list[dict[str, Any]]:
        if not self.cfg.get("enabled", True):
            return []
        cooldown = float(self.cfg.get("cooldown_minutes", 15)) * 60.0
        margin = float(self.cfg.get("rearm_margin", 0.5))
        now = time.time()
        fired: list[dict[str, Any]] = []

        for rule in self.cfg.get("rules", []):
            rid = str(rule.get("id") or rule.get("label") or id(rule))
            if not rule.get("enabled", True):
                continue
            conds = rule_conditions(rule)
            if not conds:
                continue
            label = str(rule.get("label") or rid)
            active = rule_is_active(reading, rule)

            if rid not in self._armed:
                self._armed[rid] = True

            if not active:
                if rule_is_cleared(reading, rule, margin):
                    self._armed[rid] = True
                continue

            if not self._armed.get(rid, True):
                continue
            last = self._last_fire.get(rid, 0.0)
            if now - last < cooldown:
                continue

            self._last_fire[rid] = now
            self._armed[rid] = False
            fired.append(
                {
                    "id": rid,
                    "label": label,
                    "conditions": conds,
                    "logic": rule_logic(rule),
                    "message": format_rule_message(rule, reading),
                }
            )
        return fired


def resolve_sound_path(path_str: str | None) -> Path | None:
    """Resolve alert sound path; relative paths are under app_dir()."""
    if not path_str or not str(path_str).strip():
        return None
    p = Path(str(path_str).strip())
    if not p.is_absolute():
        p = app_dir() / p
    return p if p.is_file() else None


def ensure_default_alert_wav() -> Path:
    """Write a short two-tone WAV if missing (no external assets required)."""
    path = app_dir() / "sounds" / "alert.wav"
    if path.is_file() and path.stat().st_size > 44:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    import math
    import struct
    import wave

    rate = 22050
    # two rising tones
    segments = [(880.0, 0.14), (0.0, 0.04), (1175.0, 0.22)]
    samples: list[int] = []
    for freq, dur in segments:
        n = int(rate * dur)
        for i in range(n):
            if freq <= 0:
                samples.append(0)
                continue
            t = i / rate
            # soft attack/release to avoid click
            env = 1.0
            attack = min(0.02, dur / 3)
            release = min(0.04, dur / 3)
            if t < attack:
                env = t / attack
            elif t > dur - release:
                env = max(0.0, (dur - t) / release)
            val = 0.35 * env * math.sin(2 * math.pi * freq * t)
            samples.append(int(max(-1.0, min(1.0, val)) * 32767))

    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"".join(struct.pack("<h", s) for s in samples))
    return path


def play_alert_sound(sound_file: str | None = None) -> None:
    """Play alert audio via app volume (not Windows notification system sounds).

    Custom .wav preferred. Empty path uses generated default chime. Falls back to
    winsound.Beep if file playback fails.
    """
    import threading

    def _play() -> None:
        try:
            import winsound

            path = resolve_sound_path(sound_file)
            if path is None:
                path = ensure_default_alert_wav()
            if path is not None and path.is_file():
                # SND_NODEFAULT: don't fall back to system default if file fails
                flags = winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
                winsound.PlaySound(str(path), flags)
                return
            # Last resort PC beep (also independent of toast system-sound scheme)
            winsound.Beep(880, 160)
            winsound.Beep(1175, 220)
        except Exception:
            try:
                import winsound

                winsound.Beep(1000, 250)
            except Exception:
                pass

    threading.Thread(target=_play, daemon=True).start()


# Stable AUMID (no spaces) — must match registry registration so Windows
# Settings → Notifications lists this app.
TOAST_AUMID = "SwitchBot.Meter.Dashboard"
TOAST_DISPLAY_NAME = "SwitchBot Meter"
_toast_registered = False


def ensure_toast_app_registered() -> str:
    """Register desktop toast app so it appears in Windows notification settings.

    Writes:
      HKCU\\Software\\Classes\\AppUserModelId\\<AUMID>
      HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Notifications\\Settings\\<AUMID>
    and sets the process AppUserModelID.
    """
    global _toast_registered
    aumid = TOAST_AUMID
    display = TOAST_DISPLAY_NAME
    icon = _ensure_toast_icon()

    try:
        import winreg

        # 1) App identity (name + icon in Action Center / Settings)
        base = rf"Software\Classes\AppUserModelId\{aumid}"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, base) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display)
            if icon:
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))
            winreg.SetValueEx(key, "IconBackgroundColor", 0, winreg.REG_SZ, "FF1B222C")

        # 2) Allow notifications for this AUMID
        nbase = rf"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\{aumid}"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, nbase) as key:
            winreg.SetValueEx(key, "Enabled", 0, winreg.REG_DWORD, 1)
            # 1 = show in action center
            try:
                winreg.SetValueEx(key, "ShowInActionCenter", 0, winreg.REG_DWORD, 1)
            except Exception:
                pass
    except Exception:
        pass

    # 3) Process-level AUMID (helps Windows attribute toasts correctly)
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(aumid)
    except Exception:
        pass

    _toast_registered = True
    return aumid


def _ensure_toast_icon() -> Path | None:
    """Write a tiny PNG icon used by the toast app registration."""
    path = app_dir() / "sounds" / "toast_icon.png"
    try:
        if path.is_file() and path.stat().st_size > 50:
            return path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        # 32x32 solid accent-ish PNG (no PIL dependency)
        import struct
        import zlib

        w = h = 32
        # BGR-ish teal #4fc3f7
        r, g, b = 0x4F, 0xC3, 0xF7
        raw = bytearray()
        for y in range(h):
            raw.append(0)  # filter None
            for x in range(w):
                # simple thermometer-ish: white stick in center
                cx, cy = abs(x - 16), abs(y - 16)
                if cx <= 3 and 4 <= y <= 22:
                    raw.extend((0xE8, 0xEE, 0xF6, 0xFF))  # RGBA
                elif (x - 16) ** 2 + (y - 24) ** 2 <= 36:
                    raw.extend((0xFF, 0x8A, 0x80, 0xFF))  # bulb
                else:
                    raw.extend((r, g, b, 0xFF))
        compressed = zlib.compress(bytes(raw), 9)

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # 8-bit RGBA
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
        path.write_bytes(png)
        return path.resolve()
    except Exception:
        return None


def is_foreground_fullscreen() -> bool:
    """True if the foreground window covers (almost) an entire monitor.

    Used to avoid toast popups interrupting games / video fullscreen.
    Returns False for desktop, taskbar, and our own process windows.
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", ctypes.c_ulong),
            ]

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False

        # Ignore desktop / shell chrome
        buf = ctypes.create_unicode_buffer(256)
        if user32.GetClassNameW(hwnd, buf, 256):
            cls = buf.value
            if cls in (
                "Progman",
                "WorkerW",
                "Shell_TrayWnd",
                "Shell_SecondaryTrayWnd",
                "NotifyIconOverflowWindow",
                "Windows.UI.Core.CoreWindow",  # some system UI
            ):
                return False

        # Ignore our own process (HUD / settings)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == kernel32.GetCurrentProcessId():
            return False

        wr = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(wr)):
            return False
        ww = max(0, wr.right - wr.left)
        wh = max(0, wr.bottom - wr.top)
        if ww < 200 or wh < 200:
            return False

        MONITOR_DEFAULTTONEAREST = 2
        hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return False
        mr = mi.rcMonitor
        mw = max(1, mr.right - mr.left)
        mh = max(1, mr.bottom - mr.top)

        # True fullscreen / borderless: edges flush with the whole monitor
        # (maximized apps usually leave the taskbar, so they won't match).
        edge_slop = 4
        covers = (
            wr.left <= mr.left + edge_slop
            and wr.top <= mr.top + edge_slop
            and wr.right >= mr.right - edge_slop
            and wr.bottom >= mr.bottom - edge_slop
        )
        if covers:
            return True
        # Near-fullscreen (≥98% area) without OS chrome class — borderless games
        area_ratio = (ww * wh) / float(mw * mh)
        return area_ratio >= 0.98
    except Exception:
        return False


def notify_alert(
    title: str,
    body: str,
    *,
    toast: bool = True,
    sound: bool = True,
    sound_file: str = "",
    quiet_when_fullscreen: bool = True,
    quiet_sound_when_fullscreen: bool = False,
) -> None:
    """Show toast and/or play alert sound; optionally quiet during fullscreen games."""
    fullscreen = False
    if quiet_when_fullscreen or quiet_sound_when_fullscreen:
        fullscreen = is_foreground_fullscreen()

    do_toast = toast and not (quiet_when_fullscreen and fullscreen)
    do_sound = sound and not (quiet_sound_when_fullscreen and fullscreen)

    if do_toast:
        send_windows_toast(title, body, sound=False)
    if do_sound:
        play_alert_sound(sound_file)


def _xml_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def send_windows_toast(title: str, body: str, sound: bool = False) -> None:
    """Windows toast with title + body text.

    Registers AUMID first so the app shows up under
    Settings → System → Notifications as "SwitchBot Meter".
    Toast audio stays silent by default — use play_alert_sound.
    """
    if not _toast_registered:
        ensure_toast_app_registered()

    title = (title or "Meter 警報").strip() or "Meter 警報"
    body = (body or "").strip() or "（無詳細說明）"
    aumid = TOAST_AUMID

    audio_xml = (
        '<audio src="ms-winsoundevent:Notification.Default" loop="false"/>'
        if sound
        else '<audio silent="true"/>'
    )
    body_xml = "\n".join(
        f'            <text hint-style="body">{_xml_escape(line)}</text>'
        for line in body.splitlines()
        if line.strip()
    )
    if not body_xml:
        body_xml = f'            <text hint-style="body">{_xml_escape(body)}</text>'

    icon = _ensure_toast_icon()
    icon_xml = ""
    if icon is not None:
        # file:/// URI for local icon
        uri = icon.as_uri()
        icon_xml = f'      <image placement="appLogoOverride" src="{_xml_escape(uri)}" hint-crop="circle"/>'

    toast_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<toast duration="long">
  <visual>
    <binding template="ToastGeneric">
{icon_xml}
      <text hint-style="title">{_xml_escape(title)}</text>
{body_xml}
    </binding>
  </visual>
  {audio_xml}
</toast>
"""
    tag = f"sbm_{int(time.time() * 1000) % 10_000_000}"
    group = "SwitchBotMeterAlerts"

    if _toast_via_winrt(toast_xml, aumid, tag, group):
        return
    try:
        from winotify import Notification, audio

        toast = Notification(
            app_id=aumid,
            title=title,
            msg=body.replace("\n", " · "),
            duration="long",
        )
        if sound:
            toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception:
        pass


def _toast_via_winrt(toast_xml: str, aumid: str, tag: str, group: str) -> bool:
    """Show a toast through WinRT (no PowerShell)."""
    try:
        from winrt.windows.data.xml.dom import XmlDocument
        from winrt.windows.ui.notifications import ToastNotification, ToastNotificationManager

        doc = XmlDocument()
        doc.load_xml(toast_xml)
        toast = ToastNotification(doc)
        try:
            toast.tag = tag
            toast.group = group
        except Exception:
            pass
        try:
            notifier = ToastNotificationManager.create_toast_notifier_with_id(aumid)
        except Exception:
            notifier = ToastNotificationManager.create_toast_notifier(aumid)
        notifier.show(toast)
        return True
    except Exception:
        return False


_instance_mutex = None


def acquire_single_instance(name: str = "Local\\SwitchBotMeter.SingleInstance") -> bool:
    """True if this process now owns the instance mutex (keep handle alive)."""
    global _instance_mutex
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return True
        _instance_mutex = handle
        return int(kernel32.GetLastError()) != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def period_since(period: str) -> float:
    now = time.time()
    if period == "hour":
        return now - 3600
    if period == "day":
        return now - 86400
    if period == "week":
        return now - 7 * 86400
    if period == "month":
        return now - 30 * 86400
    return now - 3600
