"""
SwitchBot Meter Plus — Windows dashboard
- Semi-transparent always-on HUD (App 風格資訊卡)
- Flexible multi-rule alerts (config.json)
- Local history DB + chart + CSV export
- Derived: absolute humidity, dew point, VPD

Run:  python app.py
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from collections.abc import Callable
from typing import Any

from core import (
    APP_DIR,
    METRIC_LABELS,
    OPS,
    AlertEngine,
    DiscoveredMeter,
    HistoryStore,
    Reading,
    acquire_single_instance,
    ble_radio_state_sync,
    discover_meters_sync,
    ensure_toast_app_registered,
    format_condition,
    format_rule_message,
    format_rule_summary,
    load_config,
    notify_alert,
    period_since,
    play_alert_sound,
    rule_conditions,
    rule_is_active,
    rule_logic,
    save_config,
    scan_once_sync,
    set_windows_startup,
)

# Simulated environments for alert preview (name, °C, %RH, battery%)
ALERT_SCENARIOS: list[tuple[str, float, float, int]] = [
    ("舒適室內", 24.0, 50.0, 80),
    ("高溫高濕", 31.0, 65.0, 80),
    ("酷熱乾燥感", 34.0, 45.0, 80),
    ("悶熱（熱+很濕）", 28.5, 75.0, 80),
    ("低溫", 16.0, 48.0, 80),
    ("高濕易霉", 26.0, 78.0, 80),
    ("過乾", 25.0, 30.0, 80),
    ("露點偏高", 29.0, 85.0, 80),
    ("電量低（其餘正常）", 24.0, 50.0, 10),
    ("全面極端", 35.0, 90.0, 8),
]

# Palette inspired by SwitchBot app screenshots
BG = "#151a22"
BG2 = "#1e2632"
BG3 = "#283140"
FG = "#eef3f9"
FG_DIM = "#8e9aab"
ACCENT = "#4fc3f7"
TEMP = "#ff8a80"
HUM = "#4dd0e1"
OK = "#66bb6a"
WARN = "#ffb74d"
ERR = "#ef5350"
LINE = "#3a4656"
COLD = "#64b5f6"
HOT = "#ff7043"
NEUTRAL = "#90a4ae"

# Comfort level → color (used for metric values)
LEVEL_COLOR = {
    "ideal": OK,
    "ok": ACCENT,
    "cool": COLD,
    "warm": WARN,
    "hot": HOT,
    "cold": COLD,
    "dry": WARN,
    "humid": WARN,
    "bad": ERR,
    "unknown": FG_DIM,
}


def _pick_cjk_font() -> str:
    """Prefer a real CJK UI font so Chinese is not sparse/fallback-glyph mess."""
    try:
        import tkinter.font as tkfont

        root = tk._default_root  # type: ignore[attr-defined]
        families = set(tkfont.families(root)) if root else set()
    except Exception:
        families = set()
    for name in (
        "Microsoft JhengHei UI",
        "Microsoft JhengHei",
        "微軟正黑體",
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "微软雅黑",
        "Noto Sans CJK TC",
        "Source Han Sans TC",
        "Segoe UI",
    ):
        if not families or name in families:
            return name
    return "Segoe UI"


# Design reference size — fonts scale relative to this window size
FONT_BASE_W = 320
FONT_BASE_H = 210
FONT_BASE_SIZES = {
    "title": 10,
    "label": 10,  # labels a bit larger + bold = less sparse CJK
    "value": 22,
    "value_sm": 13,
    "chip": 9,
    "status": 9,
    "btn": 12,
    "tip": 9,
}
# Per-role minimum point size (CJK needs a floor)
FONT_MIN_SIZES = {
    "title": 8,
    "label": 8,
    "value": 14,
    "value_sm": 10,
    "chip": 8,
    "status": 8,
    "btn": 10,
    "tip": 8,
}


def _font_scale(width: int, height: int) -> float:
    """Logical UI scale from window size (≈1.0 at design size)."""
    w = max(1, int(width))
    h = max(1, int(height))
    sw = w / float(FONT_BASE_W)
    sh = h / float(FONT_BASE_H)
    # Width-weighted: horizontal space matters more for CJK metric cards
    s = 0.62 * sw + 0.38 * sh
    return max(0.72, min(1.9, s))


def _fonts_scaled(width: int = FONT_BASE_W, height: int = FONT_BASE_H, family: str | None = None) -> dict[str, tuple]:
    f = family or _pick_cjk_font()
    s = _font_scale(width, height)

    def sz(role: str) -> int:
        base = FONT_BASE_SIZES[role]
        lo = FONT_MIN_SIZES[role]
        return max(lo, int(round(base * s)))

    # All roles bold — CJK in regular weight looks thin/sparse on dark HUD.
    def fb(role: str) -> tuple:
        return (f, sz(role), "bold")

    return {
        "title": fb("title"),
        "label": fb("label"),
        "value": fb("value"),
        "value_sm": fb("value_sm"),
        "chip": fb("chip"),
        "status": fb("status"),
        "btn": fb("btn"),
        "tip": fb("tip"),
    }


def _fonts() -> dict[str, tuple]:
    return _fonts_scaled()


def _radio_hint(radio: str) -> str:
    if radio == "off":
        return "（藍牙已關閉）"
    if radio == "missing":
        return "（找不到 BLE 適配器）"
    return ""


def _monitor_work_areas() -> list[tuple[int, int, int, int, bool]]:
    """Work areas as (left, top, right, bottom, is_primary)."""
    try:
        import ctypes
        from ctypes import wintypes

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

        user32 = ctypes.windll.user32
        MONITORINFOF_PRIMARY = 1
        areas: list[tuple[int, int, int, int, bool]] = []

        MonitorEnumProc = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(RECT),
            wintypes.LPARAM,
        )
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.EnumDisplayMonitors.argtypes = [
            wintypes.HDC,
            ctypes.c_void_p,
            MonitorEnumProc,
            wintypes.LPARAM,
        ]
        user32.EnumDisplayMonitors.restype = wintypes.BOOL

        def _cb(hmon, _hdc, _lprect, _lp):
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r = mi.rcWork
                areas.append(
                    (
                        int(r.left),
                        int(r.top),
                        int(r.right),
                        int(r.bottom),
                        bool(mi.dwFlags & MONITORINFOF_PRIMARY),
                    )
                )
            return 1

        cb = MonitorEnumProc(_cb)
        user32.EnumDisplayMonitors(None, None, cb, 0)
        if areas:
            return areas
    except Exception:
        pass
    try:
        import ctypes

        user32 = ctypes.windll.user32
        w, h = int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
        if w > 0 and h > 0:
            return [(0, 0, w, h, True)]
    except Exception:
        pass
    return []


def _overlap_area(
    ax1: int, ay1: int, ax2: int, ay2: int, bx1: int, by1: int, bx2: int, by2: int
) -> int:
    iw = max(0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0, min(ay2, by2) - max(ay1, by1))
    return iw * ih


def clamp_window_pos(x: int, y: int, w: int, h: int) -> tuple[int, int]:
    """Keep the window intersecting some monitor work area; else primary + 80,80."""
    areas = _monitor_work_areas()
    if not areas:
        return x, y
    min_visible = max(80 * 40, int(w * h * 0.15))
    wx2, wy2 = x + w, y + h
    if any(_overlap_area(x, y, wx2, wy2, a[0], a[1], a[2], a[3]) >= min_visible for a in areas):
        return x, y
    primary = next((a for a in areas if a[4]), areas[0])
    nx = primary[0] + 80
    ny = primary[1] + 80
    if nx + w > primary[2]:
        nx = max(primary[0], primary[2] - w)
    if ny + h > primary[3]:
        ny = max(primary[1], primary[3] - h)
    return nx, ny


def assess_temperature(t: float) -> tuple[str, str, str]:
    """Return (level, short_chip, long_tip)."""
    if t < 16:
        return "cold", "過冷", f"溫度 {t:.1f}°C，明顯偏低，注意保暖。"
    if t < 18:
        return "cool", "偏涼", f"溫度 {t:.1f}°C，略涼，體感偏冷。"
    if t <= 26:
        return "ideal", "舒適", f"溫度 {t:.1f}°C，處於常見舒適區間（約 18–26°C）。"
    if t <= 28:
        return "warm", "偏暖", f"溫度 {t:.1f}°C，略暖，可加強通風。"
    if t <= 30:
        return "hot", "偏高", f"溫度 {t:.1f}°C，偏高，建議降溫。"
    if t <= 33:
        return "hot", "高溫", f"溫度 {t:.1f}°C，高溫，注意中暑風險。"
    return "bad", "酷熱", f"溫度 {t:.1f}°C，酷熱，請盡快降溫並補水。"


def assess_humidity(h: float) -> tuple[str, str, str]:
    if h < 30:
        return "dry", "過乾", f"濕度 {h:.0f}%，過低，皮膚／呼吸道易乾燥，可考慮加濕。"
    if h < 40:
        return "dry", "偏乾", f"濕度 {h:.0f}%，略乾，略低於理想 40–60%。"
    if h <= 60:
        return "ideal", "舒適", f"濕度 {h:.0f}%，處於理想區間（約 40–60%）。"
    if h <= 70:
        return "humid", "偏濕", f"濕度 {h:.0f}%，略高，體感可能偏悶。"
    if h <= 75:
        return "humid", "偏高", f"濕度 {h:.0f}%，偏高，建議除濕。"
    return "bad", "過濕", f"濕度 {h:.0f}%，過高，易發霉，建議除濕通風。"


def assess_abs_humidity(a: float) -> tuple[str, str, str]:
    if a < 5:
        return "dry", "偏低", f"絕對濕度 {a:.2f} g/m³，空氣含水量偏低。"
    if a <= 12:
        return "ideal", "適中", f"絕對濕度 {a:.2f} g/m³，室內含水量大致適中。"
    if a <= 16:
        return "humid", "偏高", f"絕對濕度 {a:.2f} g/m³，含水量偏高，體感較悶。"
    return "bad", "過高", f"絕對濕度 {a:.2f} g/m³，含水量很高，悶熱／潮濕感明顯。"


def assess_dew_point(d: float) -> tuple[str, str, str]:
    if d < 10:
        return "cool", "乾燥感", f"露點 {d:.1f}°C，空氣偏乾爽。"
    if d <= 16:
        return "ideal", "舒適", f"露點 {d:.1f}°C，多數人感覺舒適。"
    if d <= 20:
        return "humid", "偏悶", f"露點 {d:.1f}°C，開始有潮濕悶熱感。"
    if d <= 24:
        return "hot", "悶熱", f"露點 {d:.1f}°C，悶熱，牆面／玻璃較易結露。"
    return "bad", "極悶", f"露點 {d:.1f}°C，非常潮濕悶熱，結露風險高。"


def assess_vpd(v: float) -> tuple[str, str, str]:
    if v < 0.4:
        return "humid", "偏低", f"VPD {v:.2f} kPa，偏低（偏濕悶），通風／除濕較佳。植物蒸散偏慢。"
    if v <= 1.2:
        return "ideal", "適中", f"VPD {v:.2f} kPa，多數室內／一般植物可接受的範圍。"
    if v <= 1.5:
        return "warm", "偏高", f"VPD {v:.2f} kPa，略高，空氣偏乾、蒸散偏快。"
    return "bad", "過高", f"VPD {v:.2f} kPa，偏高，乾燥壓力大，可考慮加濕。"


def assess_battery(b: int) -> tuple[str, str, str]:
    if b <= 15:
        return "bad", "電量低", f"電量 {b}%，偏低，請盡快更換電池。"
    if b <= 30:
        return "warm", "注意", f"電量 {b}%，開始偏低，可準備換電池。"
    return "ideal", "充足", f"電量 {b}%，電力充足。"


class HoverTip:
    """Lightweight tooltip for metric cards (Chinese status text)."""

    def __init__(self, widget: tk.Misc, font: tuple) -> None:
        self.widget = widget
        self.font = font
        self.text = ""
        self._tip: tk.Toplevel | None = None
        self._after: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def set_text(self, text: str) -> None:
        self.text = text or ""

    def _schedule(self, _event=None) -> None:
        self._cancel()
        try:
            self._after = self.widget.after(350, self._show)
        except Exception:
            pass

    def _cancel(self) -> None:
        if self._after:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _show(self) -> None:
        self._after = None
        if not self.text:
            return
        self._hide()
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except Exception:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_attributes("-topmost", True)
        try:
            tip.wm_attributes("-alpha", 0.96)
        except Exception:
            pass
        frm = tk.Frame(tip, bg=BG3, highlightthickness=1, highlightbackground=LINE)
        frm.pack()
        tk.Label(
            frm,
            text=self.text,
            justify="left",
            bg=BG3,
            fg=FG,
            font=self.font,
            padx=10,
            pady=8,
            wraplength=280,
        ).pack()
        tip.geometry(f"+{x}+{y}")
        self._tip = tip

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


class BleWorker(threading.Thread):
    def __init__(
        self,
        out_q: queue.Queue,
        stop: threading.Event,
        get_cfg: Callable[[], dict[str, Any]],
        force: threading.Event | None = None,
    ) -> None:
        super().__init__(daemon=True)
        self.out_q = out_q
        self.stop = stop
        self.get_cfg = get_cfg
        self.force = force or threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            forced = self.force.is_set()
            self.force.clear()
            cfg = self.get_cfg()
            dev = cfg.get("device", {})
            mac = str(dev.get("mac", "") or "").strip()
            scan_s = float(dev.get("scan_seconds", 4))
            # Manual refresh uses a longer window — helps weak BLE reception.
            if forced:
                scan_s = max(scan_s, 8.0)
                self.out_q.put(("status", "手動掃描中…（加長聆聽）"))
            idle_s = float(dev.get("interval_seconds", 20))
            if not mac:
                self.out_q.put(("status", "尚未設定 MAC，請右鍵 → 設定 → 掃描附近裝置"))
            else:
                try:
                    reading = scan_once_sync(mac, scan_s)
                    if reading:
                        self.out_q.put(("reading", reading))
                    else:
                        radio = ble_radio_state_sync()
                        self.out_q.put(("miss", (time.time(), radio)))
                except Exception as e:
                    radio = ble_radio_state_sync()
                    self.out_q.put(("error", (str(e), radio)))
            end = time.time() + idle_s
            while time.time() < end and not self.stop.is_set():
                if self.force.is_set():
                    break
                time.sleep(0.15)


class TrayIcon:
    """System tray via pystray — runs in its own thread, never touches Tk's WNDPROC.

    Previous ctypes trays (extra HWND / PeekMessage / subclassing Tk) closed or
    buried the frameless HUD. Left-click default is 「顯示看板」, never 「結束」.
    """

    def __init__(self, app: "MeterApp") -> None:
        from PIL import Image, ImageDraw
        from pystray import Icon, Menu, MenuItem

        self.app = app
        self._icon = None
        img = Image.new("RGBA", (64, 64), (79, 195, 247, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle((28, 8, 36, 40), fill=(232, 238, 246, 255))
        draw.ellipse((16, 28, 48, 60), fill=(255, 138, 128, 255))

        def ui(fn) -> None:
            try:
                app.root.after(0, fn)
            except Exception:
                pass

        self._icon = Icon(
            "SwitchBotMeter",
            img,
            "SwitchBot Meter",
            menu=Menu(
                MenuItem("顯示看板", lambda *_: ui(app._show_hud), default=True),
                MenuItem(
                    "釘選置頂",
                    lambda *_: ui(app._toggle_pin),
                    checked=lambda _: app._pin_wanted(),
                ),
                MenuItem("設定 / 警報規則…", lambda *_: ui(app._open_settings)),
                MenuItem("歷史曲線 / 匯出…", lambda *_: ui(app._open_history)),
                Menu.SEPARATOR,
                MenuItem("結束", lambda *_: ui(app.quit)),
            ),
        )
        threading.Thread(target=self._icon.run, name="sbm-tray", daemon=True).start()

    def remove(self) -> None:
        icon = self._icon
        self._icon = None
        if icon is None:
            return
        try:
            icon.stop()
        except Exception:
            pass


class MeterApp:
    MIN_W = 240
    MIN_H = 168  # keep bottom status strip always visible
    DEFAULT_W = 300

    def __init__(self) -> None:
        from core import app_dir

        self.cfg = load_config()
        # Register Windows toast app (appears in Settings → Notifications)
        try:
            ensure_toast_app_registered()
        except Exception:
            pass
        if self.cfg.get("start_with_windows"):
            try:
                set_windows_startup(True)
            except Exception:
                pass
        hist_cfg = self.cfg.get("history", {})
        db_rel = hist_cfg.get("db_path", "data/history.db")
        db = Path(db_rel)
        if not db.is_absolute():
            db = app_dir() / db
        self.history = HistoryStore(db, int(hist_cfg.get("keep_days", 365)))
        self.alerts = AlertEngine(self.cfg.get("alerts", {}))
        self.q: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.force_scan = threading.Event()
        self.last: Reading | None = None
        self.settings_win: tk.Toplevel | None = None
        self.history_win: tk.Toplevel | None = None

        self.root = tk.Tk()
        self.root.title(self.cfg["device"].get("name", "Meter Plus"))
        self.root.configure(bg=BG)
        self.root.overrideredirect(True)
        disp = self.cfg.get("display", {})
        top = bool(disp.get("always_on_top", True))
        self.root.attributes("-topmost", top)
        self._topmost_applied = top
        self.root.attributes("-alpha", float(disp.get("opacity", 0.9)))
        pos = disp.get("position", {"x": 80, "y": 80})
        self.root.geometry(f"+{int(pos.get('x', 80))}+{int(pos.get('y', 80))}")

        self._drag = (0, 0)
        self._dragging = False
        self._resize_origin: tuple[int, int, int, int] | None = None
        # Last applied topmost state (avoid re-applying every tick → flicker/jump)
        self._topmost_applied: bool | None = None
        self._build_hud()
        self._build_menu()
        self._clamp_hud(persist=True)
        # Geometry is not always realized until mapped
        self.root.after(200, lambda: self._clamp_hud(persist=True))
        # Tray after first map so Tk HWND exists; never before
        self.tray = None
        self.root.after(300, self._install_tray)

        self.worker = BleWorker(self.q, self.stop, lambda: self.cfg, self.force_scan)
        self.worker.start()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.after(200, self._poll)
        self.root.after(2500, self._game_mode_tick)
        try:
            self.history.purge_old()
        except Exception:
            pass

    # ── HUD ──────────────────────────────────────────────────────────────────

    def _build_hud(self) -> None:
        # Pure card: no title bar. Settings / history / quit via right-click menu.
        self.root.update_idletasks()
        self._font_family = _pick_cjk_font()
        self._font_targets: list[tuple[tk.Misc, str]] = []
        self._hover_tips: list[HoverTip] = []
        self._title_bar: tk.Frame | None = None
        self.lbl_name: tk.Label | None = None  # name only in window title / menu context
        self._scale_after: str | None = None
        self._last_font_key: tuple[int, ...] | None = None
        self._hud_action_btns: list[tk.Label] = []

        self.fonts = _fonts_scaled(FONT_BASE_W, FONT_BASE_H, self._font_family)
        F = self.fonts

        self.shell = tk.Frame(self.root, bg=BG, highlightthickness=1, highlightbackground=LINE)
        self.shell.pack(fill="both", expand=True)

        def reg(widget: tk.Misc, role: str) -> tk.Misc:
            self._font_targets.append((widget, role))
            try:
                widget.configure(font=self.fonts[role])  # type: ignore[arg-type]
            except Exception:
                pass
            return widget

        # Metric widgets + hover tips
        self.metric_chips: dict[str, tk.Label] = {}
        self.metric_tips: dict[str, HoverTip] = {}
        self.metric_cards: dict[str, tk.Frame] = {}
        self._chip_visible = True

        def make_card(
            parent: tk.Frame,
            key: str,
            title: str,
            placeholder: str,
            *,
            big: bool = False,
            bg: str = BG2,
        ) -> tk.Label:
            card = tk.Frame(parent, bg=bg, padx=10, pady=6)
            card.pack(side="left", fill="both", expand=True, padx=3)
            self.metric_cards[key] = card
            title_lbl = tk.Label(card, text=title, fg=FG_DIM, bg=bg, anchor="w")
            reg(title_lbl, "label")
            title_lbl.pack(anchor="w")
            val_role = "value" if big else "value_sm"
            val = tk.Label(card, text=placeholder, fg=FG, bg=bg, anchor="w")
            reg(val, val_role)
            val.pack(anchor="w", pady=(1, 0))
            chip = tk.Label(card, text="—", fg=FG_DIM, bg=bg, anchor="w")
            reg(chip, "chip")
            chip.pack(anchor="w", pady=(1, 0))
            self.metric_chips[key] = chip
            tip = HoverTip(card, F["tip"])
            tip.set_text(f"{title}：等待讀數…")
            self.metric_tips[key] = tip
            self._hover_tips.append(tip)
            for w in (val, chip, title_lbl):
                w.bind("<Enter>", lambda e, t=tip: t._schedule(), add="+")
                w.bind("<Leave>", lambda e, t=tip: t._hide(), add="+")
            return val

        # ── Bottom strip FIRST (side=bottom) so status is never clipped when flat ──
        bottom = tk.Frame(self.shell, bg=BG2, height=28)
        bottom.pack(side="bottom", fill="x")
        bottom.pack_propagate(False)
        self._bottom_bar = bottom

        # Refresh button (only chrome control kept on card)
        self.btn_refresh = tk.Label(
            bottom,
            text="↻",
            fg=FG_DIM,
            bg=BG2,
            padx=8,
            cursor="hand2",
        )
        reg(self.btn_refresh, "btn")
        self.btn_refresh.pack(side="left", padx=(4, 0), pady=2)
        self.btn_refresh.bind("<Button-1>", lambda e: self._manual_refresh())
        self.btn_refresh.bind("<Enter>", lambda e: self.btn_refresh.config(fg=ACCENT))
        self.btn_refresh.bind("<Leave>", lambda e: self.btn_refresh.config(fg=FG_DIM))
        self._hud_action_btns.append(self.btn_refresh)

        self.btn_pin = tk.Label(
            bottom,
            text="📌",
            fg=ACCENT if bool(self.cfg.get("display", {}).get("always_on_top", True)) else FG_DIM,
            bg=BG2,
            padx=6,
            cursor="hand2",
        )
        reg(self.btn_pin, "btn")
        self.btn_pin.pack(side="left", padx=(0, 0), pady=2)
        self.btn_pin.bind("<Button-1>", lambda e: self._toggle_pin())
        self.btn_pin.bind("<Enter>", lambda e: self.btn_pin.config(fg=ACCENT))
        self.btn_pin.bind("<Leave>", lambda e: self._sync_pin_ui())
        self._hud_action_btns.append(self.btn_pin)
        self._pin_tip = HoverTip(self.btn_pin, F["tip"])
        self._hover_tips.append(self._pin_tip)

        self.lbl_status = tk.Label(bottom, text="掃描中…", fg=FG_DIM, bg=BG2, anchor="w")
        reg(self.lbl_status, "status")
        self.lbl_status.pack(side="left", fill="x", expand=True, padx=(4, 4), pady=2)

        self.lbl_alert = tk.Label(bottom, text="", fg=WARN, bg=BG2, anchor="e")
        reg(self.lbl_alert, "status")
        self.lbl_alert.pack(side="right", padx=(0, 18), pady=2)

        self._bat_tip = HoverTip(self.lbl_status, F["tip"])
        self._bat_tip.set_text("電量、訊號、更新時間。右鍵可開設定／歷史／結束。")
        self._hover_tips.append(self._bat_tip)

        # Resize grip on bottom-right of shell
        self.resize_grip = tk.Label(
            self.shell,
            text="◢",
            fg=FG_DIM,
            bg=BG2,
            cursor="size_nw_se",
            padx=3,
            pady=0,
        )
        reg(self.resize_grip, "chip")
        self.resize_grip.place(relx=1.0, rely=1.0, anchor="se")
        self.resize_grip.bind("<ButtonPress-1>", self._start_resize)
        self.resize_grip.bind("<B1-Motion>", self._on_resize)
        self.resize_grip.bind("<ButtonRelease-1>", self._end_resize)
        self.resize_grip.bind("<Enter>", lambda e: self.resize_grip.config(fg=FG))
        self.resize_grip.bind("<Leave>", lambda e: self.resize_grip.config(fg=FG_DIM))
        self._hud_action_btns.append(self.resize_grip)

        # ── Metric body (fills remaining space above bottom) ──
        body = tk.Frame(self.shell, bg=BG)
        body.pack(side="top", fill="both", expand=True, padx=5, pady=(5, 2))
        self._body = body

        main = tk.Frame(body, bg=BG)
        main.pack(fill="both", expand=True)
        self.lbl_temp = make_card(main, "temp", "溫度", "--.-°C", big=True, bg=BG2)
        self.lbl_hum = make_card(main, "hum", "濕度", "--%", big=True, bg=BG2)

        der = tk.Frame(body, bg=BG)
        der.pack(fill="x", pady=(3, 0))
        self._der_row = der
        self.der_vars: dict[str, tk.Label] = {}
        self.der_vars["abs"] = make_card(der, "abs", "絕對濕度", "-- g/m³", bg=BG3)
        self.der_vars["dew"] = make_card(der, "dew", "露點", "--°C", bg=BG3)
        self.der_vars["vpd"] = make_card(der, "vpd", "VPD", "-- kPa", bg=BG3)

        # Drag whole card; exclude refresh / grip from drag start
        action_btns = set(self._hud_action_btns)
        for w in self._all_widgets(self.shell):
            if w in action_btns:
                w.bind("<Button-3>", self._popup)
                continue
            w.bind("<ButtonPress-1>", self._start_drag)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<ButtonRelease-1>", self._end_drag)
            w.bind("<Button-3>", self._popup)

        self.root.update_idletasks()
        disp = self.cfg.get("display", {})
        pos = disp.get("position", {"x": 80, "y": 80})
        req_h = max(self.MIN_H, self.root.winfo_reqheight())
        w = int(disp.get("width", self.DEFAULT_W) or self.DEFAULT_W)
        h_cfg = int(disp.get("height", 0) or 0)
        h = h_cfg if h_cfg >= self.MIN_H else req_h
        w = max(self.MIN_W, w)
        h = max(self.MIN_H, h)
        self.root.geometry(f"{w}x{h}+{int(pos.get('x', 80))}+{int(pos.get('y', 80))}")
        self.root.minsize(self.MIN_W, self.MIN_H)

        self.root.bind("<Configure>", self._on_root_configure, add="+")
        self.root.after(80, self._apply_layout)

    def _all_widgets(self, parent) -> list:
        out = [parent]
        for c in parent.winfo_children():
            out.extend(self._all_widgets(c))
        return out

    def _build_menu(self) -> None:
        self._pin_var = tk.BooleanVar(
            value=bool(self.cfg.get("display", {}).get("always_on_top", True))
        )
        self.menu = tk.Menu(self.root, tearoff=0, bg=BG2, fg=FG, activebackground=BG3)
        self.menu.add_command(label="立即刷新 BLE…", command=self._manual_refresh)
        self.menu.add_checkbutton(
            label="釘選置頂",
            variable=self._pin_var,
            command=self._on_pin_menu,
        )
        self.menu.add_command(label="設定 / 警報規則…", command=self._open_settings)
        self.menu.add_command(label="歷史曲線 / 匯出…", command=self._open_history)
        self.menu.add_separator()
        self.menu.add_command(label="測試通知", command=self._test_notify)
        self.menu.add_command(label="情境通知預覽…", command=self._open_scenario_preview)
        self.menu.add_command(label="重新載入設定", command=self._reload_config)
        self.menu.add_command(label="重設視窗位置", command=self._reset_hud_pos)
        self.menu.add_separator()
        self.menu.add_command(label="結束", command=self.quit)
        self._sync_pin_ui()

    def _popup(self, event: tk.Event) -> None:
        # Don't leave a drag armed when opening the menu
        self._dragging = False
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _start_drag(self, event: tk.Event) -> None:
        # Ignore while a child dialog is open (prevents accidental jumps)
        if self._any_dialog_open():
            self._dragging = False
            return
        self._dragging = True
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _on_drag(self, event: tk.Event) -> None:
        if not self._dragging:
            return
        x = event.x_root - self._drag[0]
        y = event.y_root - self._drag[1]
        # Skip no-op updates (avoids micro-jumps)
        if abs(x - self.root.winfo_x()) < 1 and abs(y - self.root.winfo_y()) < 1:
            return
        self.root.geometry(f"+{x}+{y}")
        self.cfg.setdefault("display", {}).setdefault("position", {})
        self.cfg["display"]["position"] = {"x": x, "y": y}

    def _end_drag(self, event: tk.Event) -> None:
        if self._dragging:
            self._dragging = False
            # Persist position once on release (not on every pixel of motion)
            try:
                save_config(self.cfg)
            except Exception:
                pass

    def _start_resize(self, event: tk.Event) -> None:
        self._resize_origin = (
            event.x_root,
            event.y_root,
            self.root.winfo_width(),
            self.root.winfo_height(),
        )

    def _on_resize(self, event: tk.Event) -> None:
        if not self._resize_origin:
            return
        x0, y0, w0, h0 = self._resize_origin
        w = max(self.MIN_W, w0 + (event.x_root - x0))
        h = max(self.MIN_H, h0 + (event.y_root - y0))
        self.root.geometry(f"{w}x{h}")
        # Live font scale while dragging (debounced via Configure as well)
        self._schedule_font_scale()

    def _end_resize(self, event: tk.Event) -> None:
        self._resize_origin = None
        disp = self.cfg.setdefault("display", {})
        disp["width"] = self.root.winfo_width()
        disp["height"] = self.root.winfo_height()
        try:
            save_config(self.cfg)
        except Exception:
            pass
        self._apply_font_scale()

    def _on_root_configure(self, event: tk.Event) -> None:
        # Only react to the root window itself (not child widgets)
        if event.widget is not self.root:
            return
        self._schedule_font_scale()

    def _schedule_font_scale(self) -> None:
        if self._scale_after is not None:
            try:
                self.root.after_cancel(self._scale_after)
            except Exception:
                pass
        self._scale_after = self.root.after(60, self._apply_font_scale)

    def _apply_font_scale(self) -> None:
        """Recompute CJK fonts from current window size and push to widgets."""
        self._scale_after = None
        try:
            w = max(1, int(self.root.winfo_width()))
            h = max(1, int(self.root.winfo_height()))
        except Exception:
            return
        # Ignore tiny pre-map sizes
        if w < 50 or h < 50:
            return

        s = _font_scale(w, h)
        # Quantize so tiny pixel drags don't thrash fonts
        key = (
            int(round(s * 20)),
            w // 8,
            h // 8,
        )
        if key == self._last_font_key:
            return
        self._last_font_key = key

        family = getattr(self, "_font_family", None) or _pick_cjk_font()
        self.fonts = _fonts_scaled(w, h, family)

        for widget, role in getattr(self, "_font_targets", []):
            font = self.fonts.get(role)
            if not font:
                continue
            try:
                if widget.winfo_exists():
                    widget.configure(font=font)  # type: ignore[arg-type]
            except Exception:
                pass

        # Hover tips share tip font
        tip_font = self.fonts.get("tip")
        if tip_font:
            for tip in getattr(self, "_hover_tips", []):
                tip.font = tip_font

        # Keep bottom status strip tall enough to read (never clipped)
        if getattr(self, "_bottom_bar", None) is not None:
            try:
                st = int(self.fonts["status"][1])
                self._bottom_bar.configure(height=max(26, st + 16))
            except Exception:
                pass

        # Scale card padding with UI scale
        pad_x = max(5, int(round(9 * s)))
        pad_y = max(3, int(round(6 * s)))
        for card in getattr(self, "metric_cards", {}).values():
            try:
                card.configure(padx=pad_x, pady=pad_y)
            except Exception:
                pass

        # When window is flat or compact, hide comfort chips so metrics + bottom still fit
        compact = bool(self.cfg.get("display", {}).get("compact", False))
        show_chips = (not compact) and h >= 175
        if show_chips != getattr(self, "_chip_visible", True):
            self._chip_visible = show_chips
            for chip in getattr(self, "metric_chips", {}).values():
                try:
                    if show_chips:
                        chip.pack(anchor="w", pady=(1, 0))
                    else:
                        chip.pack_forget()
                except Exception:
                    pass

    def _manual_refresh(self) -> None:
        """Kick BLE worker to scan now (longer window for weak signal)."""
        self.lbl_status.config(text="手動掃描中…（加長聆聽）", fg=ACCENT)
        self.force_scan.set()

    def _clamp_hud(self, *, persist: bool = False, force: bool = False) -> None:
        """Move HUD onto a visible monitor if it is off-screen (or always if force)."""
        try:
            self.root.update_idletasks()
            w = max(self.MIN_W, int(self.root.winfo_width() or self.MIN_W))
            h = max(self.MIN_H, int(self.root.winfo_height() or self.MIN_H))
            x = int(self.root.winfo_x())
            y = int(self.root.winfo_y())
        except Exception:
            return
        if force:
            areas = _monitor_work_areas()
            if areas:
                primary = next((a for a in areas if a[4]), areas[0])
                nx, ny = primary[0] + 80, primary[1] + 80
            else:
                nx, ny = 80, 80
        else:
            nx, ny = clamp_window_pos(x, y, w, h)
        if (nx, ny) == (x, y):
            return
        self.root.geometry(f"{w}x{h}+{nx}+{ny}")
        self.cfg.setdefault("display", {}).setdefault("position", {})
        self.cfg["display"]["position"] = {"x": nx, "y": ny}
        if persist:
            try:
                save_config(self.cfg)
            except Exception:
                pass

    def _reset_hud_pos(self) -> None:
        self._clamp_hud(persist=True, force=True)
        self._show_hud()

    def _install_tray(self) -> None:
        if getattr(self, "tray", None) is not None:
            return
        try:
            self.tray = TrayIcon(self)
        except Exception:
            self.tray = None

    def _show_hud(self) -> None:
        """Bring the frameless card back. Do not clamp from live winfo (can be -32000)."""
        try:
            try:
                self.root.deiconify()
            except Exception:
                pass
            disp = self.cfg.get("display", {})
            pos = disp.get("position") or {}
            w = int(disp.get("width") or self.DEFAULT_W)
            h = int(disp.get("height") or self.MIN_H)
            w = max(self.MIN_W, w)
            h = max(self.MIN_H, h)
            x = int(pos.get("x", 80))
            y = int(pos.get("y", 80))
            if abs(x) > 20000 or abs(y) > 20000:
                x, y = 80, 80
            x, y = clamp_window_pos(x, y, w, h)
            self.root.geometry(f"{w}x{h}+{x}+{y}")
            self.root.lift()
            self.root.attributes("-topmost", True)
            self._topmost_applied = True
            if not self._pin_wanted():
                # Stay findable for a few seconds; dropping immediately hides
                # an overrideredirect window behind everything (no taskbar).
                self.root.after(3000, self._restore_topmost_after_show)
        except Exception:
            pass

    def _restore_topmost_after_show(self) -> None:
        if self._any_dialog_open():
            return
        self._topmost_applied = None
        want = self._pin_wanted()
        try:
            from core import is_foreground_fullscreen

            drop = bool(self.cfg.get("display", {}).get("drop_topmost_when_fullscreen", True))
            if want and drop and is_foreground_fullscreen():
                want = False
        except Exception:
            pass
        self._set_hud_topmost(want)

    def _pin_wanted(self) -> bool:
        return bool(self.cfg.get("display", {}).get("always_on_top", True))

    def _apply_pin(self, on: bool, *, persist: bool = True) -> None:
        self.cfg.setdefault("display", {})["always_on_top"] = bool(on)
        if persist:
            try:
                save_config(self.cfg)
            except Exception:
                pass
        self._sync_pin_ui()
        if self._any_dialog_open():
            return
        should = bool(on)
        if should:
            try:
                from core import is_foreground_fullscreen

                if bool(self.cfg["display"].get("drop_topmost_when_fullscreen", True)) and is_foreground_fullscreen():
                    should = False
            except Exception:
                pass
        self._topmost_applied = None
        self._set_hud_topmost(should)

    def _toggle_pin(self) -> None:
        self._apply_pin(not self._pin_wanted())

    def _on_pin_menu(self) -> None:
        self._apply_pin(bool(self._pin_var.get()))

    def _sync_pin_ui(self) -> None:
        on = self._pin_wanted()
        if getattr(self, "_pin_var", None) is not None and bool(self._pin_var.get()) != on:
            self._pin_var.set(on)
        btn = getattr(self, "btn_pin", None)
        if btn is not None:
            try:
                btn.config(fg=ACCENT if on else FG_DIM)
            except Exception:
                pass
        tip = getattr(self, "_pin_tip", None)
        if tip is not None:
            tip.set_text("已釘選置頂，點一下取消。" if on else "點一下釘選，看板保持在最上層。")

    def _any_dialog_open(self) -> bool:
        for w in (self.settings_win, self.history_win, getattr(self, "scenario_win", None)):
            try:
                if w is not None and w.winfo_exists():
                    return True
            except Exception:
                continue
        return False

    def _set_hud_topmost(self, on: bool) -> None:
        """Apply -topmost only when the value actually changes (avoids flicker)."""
        if self._topmost_applied is on:
            return
        try:
            self.root.attributes("-topmost", bool(on))
            self._topmost_applied = bool(on)
        except Exception:
            pass

    def _prepare_dialog(self, win: tk.Toplevel) -> None:
        """Keep child windows above the frameless always-on-top HUD."""
        # Drop HUD topmost so it cannot cover the dialog
        self._set_hud_topmost(False)
        self._dragging = False
        try:
            win.transient(self.root)
        except Exception:
            pass
        win.attributes("-topmost", True)
        win.lift()
        try:
            win.focus_force()
        except Exception:
            pass

        if getattr(win, "_sbm_dialog_hooked", False):
            return
        win._sbm_dialog_hooked = True  # type: ignore[attr-defined]

        def _on_close() -> None:
            try:
                win.destroy()
            except Exception:
                pass
            self._restore_hud_after_dialog()

        win.protocol("WM_DELETE_WINDOW", _on_close)
        # Also restore if closed by destroy() from buttons
        win.bind("<Destroy>", lambda e, w=win: self._on_dialog_destroy(e, w))

    def _on_dialog_destroy(self, event: tk.Event, win: tk.Toplevel) -> None:
        # Only react to the Toplevel itself, not its children
        if event.widget is not win:
            return
        self.root.after(50, self._restore_hud_after_dialog)

    def _restore_hud_after_dialog(self) -> None:
        if self._any_dialog_open():
            return
        disp = self.cfg.get("display", {})
        want = bool(disp.get("always_on_top", True))
        self._set_hud_topmost(want)
        # Apply opacity only when no dialog is open. Changing root -alpha while a
        # Toplevel/messagebox is up freezes Tk on Windows (overrideredirect).
        try:
            op = float(disp.get("opacity", 0.9))
            op = max(0.35, min(1.0, op))
            self.root.attributes("-alpha", op)
        except Exception:
            pass

    def _game_mode_tick(self) -> None:
        """While a fullscreen app is focused, drop always-on-top so HUD won't cover games.

        Must not re-assert topmost while settings/history dialogs are open — that
        was covering the settings window and making the HUD jump.
        """
        try:
            if self._any_dialog_open():
                # Keep HUD under dialogs
                self._set_hud_topmost(False)
            else:
                from core import is_foreground_fullscreen

                disp = self.cfg.get("display", {})
                want_top = bool(disp.get("always_on_top", True))
                drop = bool(disp.get("drop_topmost_when_fullscreen", True))
                should_top = want_top and not (drop and is_foreground_fullscreen())
                self._set_hud_topmost(should_top)
        except Exception:
            pass
        self.root.after(2500, self._game_mode_tick)

    # ── Data loop ────────────────────────────────────────────────────────────

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "reading":
                    self._on_reading(payload)
                elif kind == "miss":
                    when, radio = payload if isinstance(payload, tuple) else (payload, "unknown")
                    self._on_miss(radio=str(radio))
                elif kind == "error":
                    msg, radio = payload if isinstance(payload, tuple) else (payload, "unknown")
                    extra = _radio_hint(str(radio))
                    self.lbl_status.config(text=f"BLE 錯誤：{msg}{extra}", fg=ERR)
                elif kind == "status":
                    self.lbl_status.config(text=str(payload), fg=ACCENT)
        except queue.Empty:
            pass
        self.root.after(250, self._poll)

    def _apply_metric(
        self,
        key: str,
        value_widget: tk.Label,
        text: str,
        level: str,
        chip: str,
        tip: str,
    ) -> None:
        color = LEVEL_COLOR.get(level, FG)
        value_widget.config(text=text, fg=color)
        if key in self.metric_chips:
            self.metric_chips[key].config(text=chip, fg=color)
        if key in self.metric_tips:
            self.metric_tips[key].set_text(tip)

    def _on_reading(self, r: Reading) -> None:
        # Battery is only in service-data packets; manufacturer-only scans
        # would otherwise wipe a good value with "—".
        if r.battery_pct is None and self.last is not None and self.last.battery_pct is not None:
            r.battery_pct = self.last.battery_pct
        self.last = r
        disp = self.cfg.get("display", {})

        hot_rules: list[str] = []
        if self.cfg.get("alerts", {}).get("enabled"):
            for rule in self.cfg["alerts"].get("rules", []):
                if not rule.get("enabled"):
                    continue
                if not rule_is_active(r, rule):
                    continue
                label = str(rule.get("label", rule.get("id")))
                note = str(rule.get("message") or "").strip()
                hot_rules.append(f"{label}：{note}" if note else label)

        # Temperature / humidity with color + chip + hover text
        lv, chip, tip = assess_temperature(r.temperature_c)
        self._apply_metric("temp", self.lbl_temp, f"{r.temperature_c:.1f}°C", lv, chip, tip)

        lv, chip, tip = assess_humidity(r.humidity_pct)
        self._apply_metric("hum", self.lbl_hum, f"{r.humidity_pct:.0f}%", lv, chip, tip)

        if disp.get("show_abs_humidity", True):
            lv, chip, tip = assess_abs_humidity(r.abs_humidity)
            self._apply_metric(
                "abs", self.der_vars["abs"], f"{r.abs_humidity:.2f} g/m³", lv, chip, tip
            )
        else:
            self._apply_metric("abs", self.der_vars["abs"], "—", "unknown", "隱藏", "已在設定中關閉顯示。")

        if disp.get("show_dew_point", True):
            lv, chip, tip = assess_dew_point(r.dew_point_c)
            self._apply_metric(
                "dew", self.der_vars["dew"], f"{r.dew_point_c:.1f}°C", lv, chip, tip
            )
        else:
            self._apply_metric("dew", self.der_vars["dew"], "—", "unknown", "隱藏", "已在設定中關閉顯示。")

        if disp.get("show_vpd", True):
            lv, chip, tip = assess_vpd(r.vpd_kpa)
            self._apply_metric(
                "vpd", self.der_vars["vpd"], f"{r.vpd_kpa:.2f} kPa", lv, chip, tip
            )
        else:
            self._apply_metric("vpd", self.der_vars["vpd"], "—", "unknown", "隱藏", "已在設定中關閉顯示。")

        stamp = datetime.fromtimestamp(r.at).strftime("%H:%M:%S")
        rssi = f"{r.rssi} dBm" if r.rssi is not None else "?"
        if r.battery_pct is None:
            bat_lv, bat_chip, bat_tip = "unknown", "—", "本輪廣播沒有電量欄（電量只在 service data）。"
        else:
            bat_lv, bat_chip, bat_tip = assess_battery(r.battery_pct)
        if disp.get("show_battery", True):
            if r.battery_pct is None:
                bat = "電量 —"
            else:
                bat = f"電量 {r.battery_pct}%（{bat_chip}）"
            self.lbl_status.config(
                text=" · ".join([bat, rssi, stamp]),
                fg=LEVEL_COLOR.get(bat_lv, FG_DIM),
            )
            self._bat_tip.set_text(
                f"{bat_tip}\n訊號 {rssi} · 更新 {stamp}\n"
                f"整體：溫度「{self.metric_chips['temp'].cget('text')}」、"
                f"濕度「{self.metric_chips['hum'].cget('text')}」。"
            )
        else:
            self.lbl_status.config(text=" · ".join([rssi, stamp]), fg=FG_DIM)
            self._bat_tip.set_text(f"訊號 {rssi} · 更新 {stamp}")

        self.shell.config(highlightbackground=LINE if not hot_rules else ERR)

        if hot_rules:
            self.lbl_alert.config(text="⚠ " + "、".join(hot_rules[:3]), fg=WARN)
        else:
            t_chip = self.metric_chips["temp"].cget("text")
            h_chip = self.metric_chips["hum"].cget("text")
            t_fg = self.lbl_temp.cget("fg")
            if t_chip == "舒適" and h_chip == "舒適":
                self.lbl_alert.config(text="環境舒適", fg=OK)
            else:
                self.lbl_alert.config(text=f"{t_chip} · {h_chip}", fg=t_fg)

        if self.cfg.get("history", {}).get("enabled", True):
            try:
                self.history.add(r)
            except Exception:
                pass

        fired = self.alerts.evaluate(r)
        acfg = self.cfg.get("alerts", {})
        for ev in fired:
            notify_alert(
                f"{self.cfg['device'].get('name', 'Meter')} · {ev['label']}",
                ev["message"],
                toast=bool(acfg.get("windows_toast", True)),
                sound=bool(acfg.get("sound", True)),
                sound_file=str(acfg.get("sound_file") or ""),
                quiet_when_fullscreen=bool(acfg.get("quiet_when_fullscreen", True)),
                quiet_sound_when_fullscreen=bool(acfg.get("quiet_sound_when_fullscreen", False)),
            )

    def _on_miss(self, radio: str = "unknown") -> None:
        hint = _radio_hint(radio)
        if radio == "off":
            self.lbl_status.config(text="藍牙已關閉，請在 Windows 設定開啟", fg=WARN)
            return
        if radio == "missing":
            self.lbl_status.config(text="找不到 BLE 適配器（Xbox／Unifying 不能用）", fg=WARN)
            return
        if self.last is None:
            self.lbl_status.config(text=f"找不到裝置{hint or '（請確認 MAC／靠近裝置）'}", fg=WARN)
        else:
            age = int(time.time() - self.last.at)
            self.lbl_status.config(text=f"本輪未掃到 · 上次 {age}s 前{hint}", fg=WARN)

    def _apply_layout(self) -> None:
        """Show / hide derived metric cards; compact drops the whole second row."""
        disp = self.cfg.get("display", {})
        compact = bool(disp.get("compact", False))
        flags = {
            "abs": bool(disp.get("show_abs_humidity", True)) and not compact,
            "dew": bool(disp.get("show_dew_point", True)) and not compact,
            "vpd": bool(disp.get("show_vpd", True)) and not compact,
        }
        der = getattr(self, "_der_row", None)
        if der is None:
            return
        any_on = any(flags.values())
        for key in ("abs", "dew", "vpd"):
            card = self.metric_cards.get(key)
            if card is None:
                continue
            card.pack_forget()
        if any_on:
            der.pack(fill="x", pady=(3, 0))
            for key in ("abs", "dew", "vpd"):
                if flags[key]:
                    self.metric_cards[key].pack(side="left", fill="both", expand=True, padx=3)
        else:
            der.pack_forget()
        self._last_font_key = None
        self._apply_font_scale()

    # ── Actions ──────────────────────────────────────────────────────────────

    def _test_notify(self) -> None:
        acfg = self.cfg.get("alerts", {})
        # Prefer previewing first enabled rule that has 通知文字
        body = "通知正常：標題＋內文都應看得見。"
        title = "Meter 測試通知"
        for rule in acfg.get("rules", []):
            if not rule.get("enabled", True):
                continue
            note = str(rule.get("message") or "").strip()
            if note:
                title = f"{self.cfg['device'].get('name', 'Meter')} · {rule.get('label', '警報')}"
                body = note + "\n（這是測試通知，示範「通知文字」會出現在這裡）"
                break
        notify_alert(
            title,
            body,
            toast=bool(acfg.get("windows_toast", True)),
            sound=bool(acfg.get("sound", True)),
            sound_file=str(acfg.get("sound_file") or ""),
            quiet_when_fullscreen=bool(acfg.get("quiet_when_fullscreen", True)),
            quiet_sound_when_fullscreen=bool(acfg.get("quiet_sound_when_fullscreen", False)),
        )

    def _make_fake_reading(self, temp_c: float, hum: float, battery: int = 80) -> Reading:
        return Reading(
            temperature_c=float(temp_c),
            humidity_pct=float(hum),
            battery_pct=int(battery),
            rssi=-55,
            address=str(self.cfg.get("device", {}).get("mac", "TEST")),
            at=time.time(),
        ).with_derived()

    def _rules_fired_for(self, reading: Reading) -> list[dict[str, Any]]:
        """Which enabled rules match this reading (ignore cooldown; for preview)."""
        if not self.cfg.get("alerts", {}).get("enabled", True):
            return []
        out: list[dict[str, Any]] = []
        for rule in self.cfg.get("alerts", {}).get("rules", []):
            if not rule.get("enabled", True):
                continue
            if not rule_is_active(reading, rule):
                continue
            label = str(rule.get("label") or rule.get("id") or "警報")
            out.append(
                {
                    "id": rule.get("id"),
                    "label": label,
                    "message": format_rule_message(rule, reading),
                    "summary": format_rule_summary(rule),
                }
            )
        return out

    def _fire_preview_toasts(self, reading: Reading, scenario_name: str) -> int:
        acfg = self.cfg.get("alerts", {})
        fired = self._rules_fired_for(reading)
        dev = self.cfg.get("device", {}).get("name", "Meter")
        # Previews always force toast (ignore game quiet) so you can verify text.
        if not fired:
            notify_alert(
                f"{dev} · 情境預覽",
                f"「{scenario_name}」無符合的啟用規則\n"
                f"{reading.temperature_c:.1f}°C · {reading.humidity_pct:.0f}% · 電量 {reading.battery_pct}%",
                toast=bool(acfg.get("windows_toast", True)),
                sound=False,
                sound_file="",
                quiet_when_fullscreen=False,
                quiet_sound_when_fullscreen=False,
            )
            return 0
        for ev in fired:
            notify_alert(
                f"{dev} · {ev['label']}",
                f"{ev['message']}\n（情境預覽：{scenario_name}）",
                toast=bool(acfg.get("windows_toast", True)),
                sound=bool(acfg.get("sound", True)),
                sound_file=str(acfg.get("sound_file") or ""),
                quiet_when_fullscreen=False,
                quiet_sound_when_fullscreen=False,
            )
        return len(fired)

    def _open_scenario_preview(self) -> None:
        """Interactive table: simulate environments → which alerts / toast text."""
        if getattr(self, "scenario_win", None) is not None:
            try:
                if self.scenario_win.winfo_exists():
                    self._prepare_dialog(self.scenario_win)
                    return
            except Exception:
                pass
        win = tk.Toplevel(self.root)
        self.scenario_win = win
        win.title("情境通知預覽")
        win.configure(bg=BG)
        win.geometry("720x520")
        self._prepare_dialog(win)

        tk.Label(
            win,
            text="選一個環境，看會觸發哪些「已啟用」規則；可再彈出實際 Windows 通知。",
            fg=FG_DIM,
            bg=BG,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=12, pady=(10, 4))

        cols = ("name", "env", "count", "rules")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=12)
        tree.heading("name", text="情境")
        tree.heading("env", text="溫 / 濕 / 電")
        tree.heading("count", text="觸發")
        tree.heading("rules", text="會彈出的規則")
        tree.column("name", width=130)
        tree.column("env", width=140)
        tree.column("count", width=50, anchor="center")
        tree.column("rules", width=360)
        tree.pack(fill="both", expand=True, padx=12, pady=6)

        detail = tk.Text(
            win,
            height=8,
            bg=BG3,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            font=("Segoe UI", 9),
            wrap="word",
        )
        detail.pack(fill="both", expand=False, padx=12, pady=4)

        # Cache scenario data by tree iid
        scenario_data: dict[str, dict[str, Any]] = {}

        def rebuild() -> None:
            tree.delete(*tree.get_children())
            scenario_data.clear()
            for i, (name, t, h, bat) in enumerate(ALERT_SCENARIOS):
                reading = self._make_fake_reading(t, h, bat)
                fired = self._rules_fired_for(reading)
                iid = f"s{i}"
                scenario_data[iid] = {
                    "name": name,
                    "temp": t,
                    "hum": h,
                    "bat": bat,
                    "reading": reading,
                    "fired": fired,
                }
                labels = "、".join(ev["label"] for ev in fired) if fired else "（無）"
                tree.insert(
                    "",
                    "end",
                    iid=iid,
                    values=(
                        name,
                        f"{t:.1f}°C / {h:.0f}% / {bat}%",
                        str(len(fired)),
                        labels,
                    ),
                )

        def show_detail(_=None) -> None:
            sel = tree.selection()
            detail.delete("1.0", tk.END)
            if not sel:
                return
            data = scenario_data.get(sel[0])
            if not data:
                return
            r: Reading = data["reading"]
            lines = [
                f"【{data['name']}】",
                f"模擬讀數：{r.temperature_c:.1f}°C · 濕度 {r.humidity_pct:.0f}% · "
                f"電量 {r.battery_pct}% · 露點 {r.dew_point_c:.1f}°C · VPD {r.vpd_kpa:.2f} kPa",
                "",
            ]
            fired = data["fired"]
            if not fired:
                lines.append("→ 沒有任何「已啟用」規則會觸發通知。")
            else:
                lines.append(f"→ 會觸發 {len(fired)} 則通知：")
                for i, ev in enumerate(fired, 1):
                    lines.append(f"\n{i}. [{ev['label']}]")
                    lines.append(f"   條件：{ev['summary']}")
                    lines.append(f"   通知內文：")
                    for part in str(ev["message"]).splitlines():
                        lines.append(f"   {part}")
            detail.insert("1.0", "\n".join(lines))

        def popup_selected() -> None:
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("情境預覽", "請先選一個情境", parent=win)
                return
            data = scenario_data[sel[0]]
            n = self._fire_preview_toasts(data["reading"], data["name"])
            messagebox.showinfo(
                "情境預覽",
                f"「{data['name']}」已送出 {n} 則通知（0 = 無規則符合，會有一則說明）。",
                parent=win,
            )

        def popup_all() -> None:
            """Fire toasts for every scenario that has matches, staggered."""
            queue_items: list[tuple[str, Reading]] = []
            for iid in tree.get_children():
                data = scenario_data[iid]
                if data["fired"]:
                    queue_items.append((data["name"], data["reading"]))
            if not queue_items:
                messagebox.showinfo("情境預覽", "目前沒有任何情境會觸發規則", parent=win)
                return
            if not messagebox.askyesno(
                "情境預覽",
                f"將依序彈出 {len(queue_items)} 個情境的通知（每個情境可能多則）。\n繼續？",
                parent=win,
            ):
                return

            state = {"i": 0}

            def step() -> None:
                if state["i"] >= len(queue_items):
                    messagebox.showinfo("情境預覽", "全部情境通知已送出", parent=win)
                    return
                name, reading = queue_items[state["i"]]
                self._fire_preview_toasts(reading, name)
                state["i"] += 1
                win.after(1200, step)

            step()

        tree.bind("<<TreeviewSelect>>", show_detail)
        rebuild()
        kids = tree.get_children()
        if kids:
            tree.selection_set(kids[0])
            show_detail()

        foot = tk.Frame(win, bg=BG)
        foot.pack(fill="x", padx=12, pady=8)
        tk.Button(
            foot, text="彈出此情境通知", command=popup_selected, bg=ACCENT, fg="#102028", relief="flat", padx=10
        ).pack(side="left")
        tk.Button(
            foot, text="全部有觸發的情境依序彈出", command=popup_all, bg=BG3, fg=FG, relief="flat", padx=10
        ).pack(side="left", padx=6)
        tk.Button(foot, text="重新整理", command=lambda: (rebuild(), show_detail()), bg=BG3, fg=FG, relief="flat").pack(
            side="left", padx=6
        )
        tk.Button(foot, text="關閉", command=win.destroy, bg=BG3, fg=FG, relief="flat", padx=10).pack(side="right")

    def _reload_config(self) -> None:
        self.cfg = load_config()
        self.alerts.update_config(self.cfg.get("alerts", {}))
        name = self.cfg["device"].get("name", "Meter Plus")
        self.root.title(name)
        if self.lbl_name is not None:
            self.lbl_name.config(text=name)
        disp = self.cfg.get("display", {})
        self.root.attributes("-alpha", float(disp.get("opacity", 0.9)))
        self._topmost_applied = None
        self._sync_pin_ui()
        self._apply_layout()
        if not self._any_dialog_open():
            self._set_hud_topmost(bool(disp.get("always_on_top", True)))
        messagebox.showinfo("設定", "已重新載入 config.json")

    def _persist(self) -> None:
        save_config(self.cfg)
        self.alerts.update_config(self.cfg.get("alerts", {}))

    def quit(self) -> None:
        try:
            self._persist()
        except Exception:
            pass
        self.stop.set()
        try:
            if getattr(self, "tray", None) is not None:
                self.tray.remove()
        except Exception:
            pass
        self.root.destroy()

    # ── Settings window ──────────────────────────────────────────────────────

    def _open_settings(self) -> None:
        if self.settings_win and self.settings_win.winfo_exists():
            self._prepare_dialog(self.settings_win)
            return
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("設定與警報")
        win.configure(bg=BG)
        win.geometry("640x720")
        self._prepare_dialog(win)

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        tab_dev = tk.Frame(nb, bg=BG)
        tab_alert = tk.Frame(nb, bg=BG)
        tab_disp = tk.Frame(nb, bg=BG)
        nb.add(tab_dev, text="裝置")
        nb.add(tab_alert, text="警報規則")
        nb.add(tab_disp, text="顯示")

        # Device
        form = tk.Frame(tab_dev, bg=BG)
        form.pack(fill="x", padx=12, pady=12)
        entries: dict[str, tk.Variable] = {}

        def row(parent, label, key, default="") -> tk.Entry:
            fr = tk.Frame(parent, bg=BG)
            fr.pack(fill="x", pady=4)
            tk.Label(fr, text=label, width=16, anchor="w", fg=FG_DIM, bg=BG).pack(side="left")
            var = tk.StringVar(value=str(default))
            ent = tk.Entry(fr, textvariable=var, bg=BG3, fg=FG, insertbackground=FG, relief="flat")
            ent.pack(side="left", fill="x", expand=True)
            entries[key] = var
            return ent

        d = self.cfg["device"]
        row(form, "顯示名稱", "name", d.get("name", ""))
        row(form, "MAC 位址", "mac", d.get("mac", ""))
        row(form, "掃描秒數", "scan_seconds", d.get("scan_seconds", 4))
        row(form, "間隔秒數", "interval_seconds", d.get("interval_seconds", 20))

        scan_fr = tk.Frame(tab_dev, bg=BG)
        scan_fr.pack(fill="x", padx=12, pady=(4, 2))
        scan_status = tk.StringVar(value="可掃描附近 SwitchBot 溫濕度計，點選後套用 MAC。")
        scan_busy = {"on": False}

        found_cols = ("mac", "kind", "reading", "rssi")
        found_tree = ttk.Treeview(tab_dev, columns=found_cols, show="headings", height=5)
        found_tree.heading("mac", text="MAC")
        found_tree.heading("kind", text="類型 / 名稱")
        found_tree.heading("reading", text="讀數")
        found_tree.heading("rssi", text="RSSI")
        found_tree.column("mac", width=150)
        found_tree.column("kind", width=160)
        found_tree.column("reading", width=180)
        found_tree.column("rssi", width=60, anchor="center")
        found_rows: dict[str, DiscoveredMeter] = {}

        def apply_found(m: DiscoveredMeter) -> None:
            entries["mac"].set(m.address)
            if m.name and m.name not in ("SwitchBot",):
                entries["name"].set(m.name)
            elif m.device_label:
                entries["name"].set(m.device_label)
            scan_status.set(f"已套用 {m.address}（記得按下方「儲存」）")

        def on_found_activate(_=None) -> None:
            sel = found_tree.selection()
            if not sel:
                return
            m = found_rows.get(sel[0])
            if m:
                apply_found(m)

        def fill_found(items: list[DiscoveredMeter]) -> None:
            found_tree.delete(*found_tree.get_children())
            found_rows.clear()
            for i, m in enumerate(items):
                iid = f"d{i}"
                found_rows[iid] = m
                if m.temperature_c is not None:
                    reading = f"{m.temperature_c:.1f}°C  {m.humidity_pct:.0f}%"
                    if m.battery_pct is not None:
                        reading += f"  電量 {m.battery_pct}%"
                else:
                    reading = "（尚無讀數，仍可選）"
                found_tree.insert(
                    "",
                    "end",
                    iid=iid,
                    values=(
                        m.address,
                        m.device_label or m.name or "SwitchBot",
                        reading,
                        f"{m.rssi}" if m.rssi is not None else "?",
                    ),
                )
            if items:
                found_tree.selection_set("d0")
                n_read = sum(1 for m in items if m.temperature_c is not None)
                scan_status.set(f"找到 {len(items)} 台（{n_read} 台有即時讀數）。雙擊或按「使用所選」。")
            else:
                scan_status.set("附近沒掃到 Meter。請開藍牙、靠近裝置，或加長掃描秒數後再試。")

        def start_nearby_scan() -> None:
            if scan_busy["on"]:
                return
            scan_busy["on"] = True
            scan_btn.config(state="disabled")
            scan_status.set("掃描中…（約 8 秒；會等目前 BLE 輪次結束）")

            def work() -> None:
                err: str | None = None
                items: list[DiscoveredMeter] = []
                try:
                    items = discover_meters_sync(8.0)
                except Exception as e:
                    err = str(e)

                def done() -> None:
                    scan_busy["on"] = False
                    try:
                        if scan_btn.winfo_exists():
                            scan_btn.config(state="normal")
                    except Exception:
                        return
                    if err:
                        scan_status.set(f"掃描失敗：{err}")
                        return
                    fill_found(items)

                try:
                    win.after(0, done)
                except Exception:
                    scan_busy["on"] = False

            threading.Thread(target=work, daemon=True).start()

        scan_btn = tk.Button(
            scan_fr,
            text="掃描附近裝置…",
            command=start_nearby_scan,
            bg=ACCENT,
            fg="#102028",
            relief="flat",
            padx=10,
        )
        scan_btn.pack(side="left")
        tk.Button(
            scan_fr,
            text="使用所選",
            command=on_found_activate,
            bg=BG3,
            fg=FG,
            relief="flat",
            padx=10,
        ).pack(side="left", padx=8)
        tk.Label(
            scan_fr,
            textvariable=scan_status,
            fg=FG_DIM,
            bg=BG,
            anchor="w",
            font=("Segoe UI", 8),
        ).pack(side="left", fill="x", expand=True)

        found_tree.pack(fill="x", padx=12, pady=4)
        found_tree.bind("<Double-1>", on_found_activate)

        note = tk.Label(
            tab_dev,
            text=(
                "歷史資料說明\n"
                "• 本程式：有 BLE 時持續記錄到本機 SQLite，可匯出 CSV。\n"
                "• SwitchBot App：裝置內約 68 天，開啟裝置頁可同步；可「匯出資料」。\n"
                "• 官方 Open API：僅即時狀態，不含歷史曲線。\n"
                "• 雲端約 2 年：需 Hub，並在 App 圖表頁開啟雲端儲存。"
            ),
            justify="left",
            fg=FG_DIM,
            bg=BG,
            font=("Segoe UI", 9),
        )
        note.pack(anchor="w", padx=14, pady=8)

        # Alerts
        a = self.cfg["alerts"]
        a_top = tk.Frame(tab_alert, bg=BG)
        a_top.pack(fill="x", padx=12, pady=8)
        en_var = tk.BooleanVar(value=bool(a.get("enabled", True)))
        toast_var = tk.BooleanVar(value=bool(a.get("windows_toast", True)))
        sound_var = tk.BooleanVar(value=bool(a.get("sound", True)))
        quiet_fs_var = tk.BooleanVar(value=bool(a.get("quiet_when_fullscreen", True)))
        quiet_snd_var = tk.BooleanVar(value=bool(a.get("quiet_sound_when_fullscreen", False)))
        tk.Checkbutton(
            a_top, text="啟用警報", variable=en_var, fg=FG, bg=BG, selectcolor=BG3, activebackground=BG
        ).pack(side="left")
        tk.Checkbutton(
            a_top, text="Windows 通知", variable=toast_var, fg=FG, bg=BG, selectcolor=BG3, activebackground=BG
        ).pack(side="left", padx=8)
        tk.Checkbutton(
            a_top, text="音效", variable=sound_var, fg=FG, bg=BG, selectcolor=BG3, activebackground=BG
        ).pack(side="left")

        quiet_fr = tk.Frame(tab_alert, bg=BG)
        quiet_fr.pack(fill="x", padx=12, pady=2)
        tk.Checkbutton(
            quiet_fr,
            text="全螢幕／遊戲中不彈 Windows 通知（建議）",
            variable=quiet_fs_var,
            fg=FG,
            bg=BG,
            selectcolor=BG3,
            activebackground=BG,
        ).pack(side="left")
        tk.Checkbutton(
            quiet_fr,
            text="全螢幕時連音效也關",
            variable=quiet_snd_var,
            fg=FG,
            bg=BG,
            selectcolor=BG3,
            activebackground=BG,
        ).pack(side="left", padx=12)

        cd_fr = tk.Frame(tab_alert, bg=BG)
        cd_fr.pack(fill="x", padx=12, pady=4)
        tk.Label(cd_fr, text="冷卻（分鐘）", fg=FG_DIM, bg=BG).pack(side="left")
        cd_var = tk.StringVar(value=str(a.get("cooldown_minutes", 15)))
        tk.Entry(cd_fr, textvariable=cd_var, width=8, bg=BG3, fg=FG, insertbackground=FG, relief="flat").pack(
            side="left", padx=8
        )
        tk.Label(cd_fr, text="解除裕度", fg=FG_DIM, bg=BG).pack(side="left")
        margin_var = tk.StringVar(value=str(a.get("rearm_margin", 0.5)))
        tk.Entry(
            cd_fr, textvariable=margin_var, width=8, bg=BG3, fg=FG, insertbackground=FG, relief="flat"
        ).pack(side="left", padx=8)

        # Custom alert sound (app-level WAV — works when Windows system sounds are off)
        snd_fr = tk.Frame(tab_alert, bg=BG)
        snd_fr.pack(fill="x", padx=12, pady=6)
        tk.Label(snd_fr, text="自訂音效", fg=FG_DIM, bg=BG).pack(side="left")
        sound_file_var = tk.StringVar(value=str(a.get("sound_file") or ""))
        tk.Entry(
            snd_fr,
            textvariable=sound_file_var,
            bg=BG3,
            fg=FG,
            insertbackground=FG,
            relief="flat",
        ).pack(side="left", fill="x", expand=True, padx=8)

        def browse_sound() -> None:
            path = filedialog.askopenfilename(
                parent=win,
                title="選擇警報音效（建議 WAV）",
                filetypes=[
                    ("WAV 音效", "*.wav"),
                    ("所有檔案", "*.*"),
                ],
            )
            if path:
                sound_file_var.set(path)

        def test_sound() -> None:
            # Preview without waiting for save
            play_alert_sound(sound_file_var.get().strip())

        def clear_sound() -> None:
            sound_file_var.set("")

        tk.Button(snd_fr, text="瀏覽…", command=browse_sound, bg=BG3, fg=FG, relief="flat").pack(
            side="left", padx=2
        )
        tk.Button(snd_fr, text="試聽", command=test_sound, bg=BG3, fg=FG, relief="flat").pack(
            side="left", padx=2
        )
        tk.Button(snd_fr, text="預設", command=clear_sound, bg=BG3, fg=FG, relief="flat").pack(
            side="left", padx=2
        )
        tk.Label(
            tab_alert,
            text="音效由本程式直接播放（不走 Windows 系統音效）。留空＝內建提示音。建議 .wav",
            fg=FG_DIM,
            bg=BG,
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 4))

        # Rules table — multi-condition (AND/OR)
        cols = ("on", "label", "logic", "conds")
        tree = ttk.Treeview(tab_alert, columns=cols, show="headings", height=7)
        tree.heading("on", text="啟用")
        tree.heading("label", text="名稱")
        tree.heading("logic", text="組合")
        tree.heading("conds", text="條件")
        tree.column("on", width=44, anchor="center")
        tree.column("label", width=90)
        tree.column("logic", width=44, anchor="center")
        tree.column("conds", width=320)
        tree.pack(fill="both", expand=True, padx=12, pady=6)

        # Working copy of conditions for the rule being edited
        edit_conds: list[dict[str, Any]] = []
        selected_id: dict[str, str | None] = {"id": None}
        cond_sel: dict[str, int | None] = {"i": None}

        def refresh_tree() -> None:
            tree.delete(*tree.get_children())
            for r in self.cfg["alerts"].get("rules", []):
                tree.insert(
                    "",
                    "end",
                    iid=r["id"],
                    values=(
                        "✓" if r.get("enabled") else "—",
                        r.get("label", r["id"]),
                        "或" if rule_logic(r) == "or" else "且",
                        format_rule_summary(r),
                    ),
                )

        def refresh_cond_list() -> None:
            cond_list.delete(0, tk.END)
            for c in edit_conds:
                cond_list.insert(tk.END, format_condition(c))

        refresh_tree()

        edit = tk.Frame(tab_alert, bg=BG)
        edit.pack(fill="x", padx=12, pady=2)
        e_label = tk.StringVar()
        e_message = tk.StringVar()
        e_logic = tk.StringVar(value="and")
        e_enabled = tk.BooleanVar(value=True)
        e_metric = tk.StringVar(value="temperature_c")
        e_op = tk.StringVar(value=">=")
        e_value = tk.StringVar(value="30")

        tk.Label(edit, text="名稱", fg=FG_DIM, bg=BG).grid(row=0, column=0, sticky="w")
        tk.Entry(edit, textvariable=e_label, width=14, bg=BG3, fg=FG, insertbackground=FG, relief="flat").grid(
            row=0, column=1, padx=4, sticky="w"
        )
        tk.Checkbutton(
            edit, text="啟用", variable=e_enabled, fg=FG, bg=BG, selectcolor=BG3, activebackground=BG
        ).grid(row=0, column=2, padx=4, sticky="w")
        tk.Label(edit, text="條件組合", fg=FG_DIM, bg=BG).grid(row=0, column=3, sticky="w")
        ttk.Combobox(
            edit,
            textvariable=e_logic,
            values=["and", "or"],
            width=6,
            state="readonly",
        ).grid(row=0, column=4, padx=4, sticky="w")
        tk.Label(edit, text="and=全部符合  or=任一符合", fg=FG_DIM, bg=BG, font=("Segoe UI", 8)).grid(
            row=0, column=5, sticky="w", padx=4
        )

        tk.Label(edit, text="通知文字", fg=FG_DIM, bg=BG).grid(row=1, column=0, sticky="w", pady=4)
        tk.Entry(
            edit, textvariable=e_message, width=48, bg=BG3, fg=FG, insertbackground=FG, relief="flat"
        ).grid(row=1, column=1, columnspan=4, padx=4, sticky="we", pady=4)
        tk.Label(edit, text="（可選，例：注意高溫高濕）", fg=FG_DIM, bg=BG, font=("Segoe UI", 8)).grid(
            row=1, column=5, sticky="w"
        )

        # Condition editor
        cond_fr = tk.Frame(tab_alert, bg=BG)
        cond_fr.pack(fill="x", padx=12, pady=2)
        tk.Label(cond_fr, text="條件列表（可加多條）", fg=FG_DIM, bg=BG).pack(anchor="w")
        cond_list = tk.Listbox(
            cond_fr,
            height=4,
            bg=BG3,
            fg=FG,
            selectbackground=ACCENT,
            selectforeground="#102028",
            relief="flat",
            activestyle="none",
        )
        cond_list.pack(fill="x", pady=2)

        crow = tk.Frame(tab_alert, bg=BG)
        crow.pack(fill="x", padx=12, pady=2)
        tk.Label(crow, text="指標", fg=FG_DIM, bg=BG).pack(side="left")
        metric_values = list(METRIC_LABELS.keys())
        metric_labels = [f"{k} — {METRIC_LABELS[k]}" for k in metric_values]
        metric_disp = tk.StringVar(value=metric_labels[0])
        metric_cb = ttk.Combobox(
            crow,
            textvariable=metric_disp,
            values=metric_labels,
            width=22,
            state="readonly",
        )
        metric_cb.pack(side="left", padx=4)

        def sync_metric_from_disp(_=None) -> None:
            text = metric_disp.get()
            key = text.split(" — ", 1)[0].strip()
            if key in METRIC_LABELS:
                e_metric.set(key)

        metric_cb.bind("<<ComboboxSelected>>", sync_metric_from_disp)

        tk.Label(crow, text="比較", fg=FG_DIM, bg=BG).pack(side="left")
        ttk.Combobox(crow, textvariable=e_op, values=list(OPS.keys()), width=5, state="readonly").pack(
            side="left", padx=4
        )
        tk.Label(crow, text="門檻", fg=FG_DIM, bg=BG).pack(side="left")
        tk.Entry(crow, textvariable=e_value, width=8, bg=BG3, fg=FG, insertbackground=FG, relief="flat").pack(
            side="left", padx=4
        )

        def set_metric_disp(key: str) -> None:
            e_metric.set(key)
            metric_disp.set(f"{key} — {METRIC_LABELS.get(key, key)}")

        def on_cond_select(_=None) -> None:
            sel = cond_list.curselection()
            if not sel:
                cond_sel["i"] = None
                return
            i = int(sel[0])
            cond_sel["i"] = i
            if 0 <= i < len(edit_conds):
                c = edit_conds[i]
                set_metric_disp(str(c.get("metric", "temperature_c")))
                e_op.set(str(c.get("op", ">=")))
                e_value.set(str(c.get("value", "")))

        cond_list.bind("<<ListboxSelect>>", on_cond_select)

        def add_or_update_cond(replace: bool) -> None:
            sync_metric_from_disp()
            try:
                val = float(e_value.get())
            except ValueError:
                messagebox.showerror("錯誤", "門檻必須是數字", parent=win)
                return
            item = {"metric": e_metric.get(), "op": e_op.get(), "value": val}
            if replace and cond_sel["i"] is not None and 0 <= cond_sel["i"] < len(edit_conds):
                edit_conds[cond_sel["i"]] = item
            else:
                edit_conds.append(item)
                cond_sel["i"] = len(edit_conds) - 1
            refresh_cond_list()
            if cond_sel["i"] is not None:
                cond_list.selection_clear(0, tk.END)
                cond_list.selection_set(cond_sel["i"])

        def remove_cond() -> None:
            i = cond_sel["i"]
            if i is None or not (0 <= i < len(edit_conds)):
                sel = cond_list.curselection()
                if not sel:
                    return
                i = int(sel[0])
            del edit_conds[i]
            cond_sel["i"] = None
            refresh_cond_list()

        tk.Button(
            crow, text="加入條件", command=lambda: add_or_update_cond(False), bg=BG3, fg=FG, relief="flat"
        ).pack(side="left", padx=4)
        tk.Button(
            crow, text="更新所選", command=lambda: add_or_update_cond(True), bg=BG3, fg=FG, relief="flat"
        ).pack(side="left", padx=2)
        tk.Button(crow, text="刪除所選條件", command=remove_cond, bg=BG3, fg=FG, relief="flat").pack(
            side="left", padx=2
        )

        def load_rule_into_editor(rule: dict[str, Any] | None) -> None:
            edit_conds.clear()
            if rule is None:
                e_label.set("自訂規則")
                e_message.set("")
                e_logic.set("and")
                e_enabled.set(True)
                edit_conds.append({"metric": "temperature_c", "op": ">=", "value": 30.0})
                set_metric_disp("temperature_c")
                e_op.set(">=")
                e_value.set("30")
            else:
                e_label.set(str(rule.get("label", "")))
                e_message.set(str(rule.get("message") or ""))
                e_logic.set(rule_logic(rule))
                e_enabled.set(bool(rule.get("enabled", True)))
                for c in rule_conditions(rule):
                    edit_conds.append(dict(c))
                if edit_conds:
                    set_metric_disp(str(edit_conds[0]["metric"]))
                    e_op.set(str(edit_conds[0]["op"]))
                    e_value.set(str(edit_conds[0]["value"]))
            cond_sel["i"] = None
            refresh_cond_list()

        def on_select(_=None) -> None:
            sel = tree.selection()
            if not sel:
                return
            rid = sel[0]
            selected_id["id"] = rid
            for r in self.cfg["alerts"]["rules"]:
                if r["id"] == rid:
                    load_rule_into_editor(r)
                    break

        tree.bind("<<TreeviewSelect>>", on_select)

        def apply_rule() -> None:
            if not edit_conds:
                messagebox.showerror("錯誤", "請至少加入一條條件", parent=win)
                return
            logic = "or" if e_logic.get() == "or" else "and"
            payload = {
                "enabled": e_enabled.get(),
                "label": e_label.get().strip() or "自訂規則",
                "message": e_message.get().strip(),
                "logic": logic,
                "conditions": [dict(c) for c in edit_conds],
            }
            # Drop legacy single fields so format_rule_summary uses conditions
            rid = selected_id["id"]
            if not rid:
                rid = f"rule_{int(time.time())}"
                row = {"id": rid, **payload}
                self.cfg["alerts"]["rules"].append(row)
                selected_id["id"] = rid
            else:
                for r in self.cfg["alerts"]["rules"]:
                    if r["id"] == rid:
                        r.pop("metric", None)
                        r.pop("op", None)
                        r.pop("value", None)
                        r.update(payload)
                        break
            refresh_tree()
            tree.selection_set(rid)
            messagebox.showinfo("規則", "已套用（記得按下方「儲存」寫入設定）", parent=win)

        def delete_rule() -> None:
            rid = selected_id["id"]
            if not rid:
                return
            self.cfg["alerts"]["rules"] = [r for r in self.cfg["alerts"]["rules"] if r["id"] != rid]
            selected_id["id"] = None
            load_rule_into_editor(None)
            selected_id["id"] = None
            refresh_tree()

        def new_rule() -> None:
            selected_id["id"] = None
            tree.selection_remove(tree.selection())
            load_rule_into_editor(None)

        btns = tk.Frame(tab_alert, bg=BG)
        btns.pack(fill="x", padx=12, pady=6)
        tk.Button(btns, text="新增規則", command=new_rule, bg=BG3, fg=FG, relief="flat").pack(
            side="left", padx=4
        )
        tk.Button(btns, text="套用規則", command=apply_rule, bg=ACCENT, fg="#102028", relief="flat").pack(
            side="left", padx=4
        )
        tk.Button(btns, text="刪除規則", command=delete_rule, bg=BG3, fg=FG, relief="flat").pack(
            side="left", padx=4
        )
        tk.Label(
            btns,
            text="例：溫度≥30 且 濕度≥60 → 通知「注意高溫高濕」",
            fg=FG_DIM,
            bg=BG,
            font=("Segoe UI", 8),
        ).pack(side="left", padx=8)

        # Display tab
        dform = tk.Frame(tab_disp, bg=BG)
        dform.pack(fill="x", padx=12, pady=12)
        disp = self.cfg["display"]
        op_var = tk.DoubleVar(value=float(disp.get("opacity", 0.9)))
        top_var = tk.BooleanVar(value=bool(disp.get("always_on_top", True)))
        show_abs = tk.BooleanVar(value=bool(disp.get("show_abs_humidity", True)))
        show_dew = tk.BooleanVar(value=bool(disp.get("show_dew_point", True)))
        show_vpd = tk.BooleanVar(value=bool(disp.get("show_vpd", True)))
        show_bat = tk.BooleanVar(value=bool(disp.get("show_battery", True)))
        drop_fs_var = tk.BooleanVar(value=bool(disp.get("drop_topmost_when_fullscreen", True)))

        op_row = tk.Frame(dform, bg=BG)
        op_row.pack(fill="x")
        tk.Label(op_row, text="透明度", fg=FG_DIM, bg=BG).pack(side="left")
        op_pct = tk.Label(op_row, text=f"{float(op_var.get()) * 100:.0f}%", fg=ACCENT, bg=BG)
        op_pct.pack(side="right")

        def on_opacity_slide(_val: str | None = None) -> None:
            # Only update label while sliding — never set root -alpha here.
            # Applying alpha with this Toplevel open freezes Tk on Windows.
            try:
                op_pct.config(text=f"{float(op_var.get()) * 100:.0f}%")
            except Exception:
                pass

        tk.Scale(
            dform,
            from_=0.4,
            to=1.0,
            resolution=0.01,
            orient="horizontal",
            variable=op_var,
            command=on_opacity_slide,
            bg=BG,
            fg=FG,
            troughcolor=BG3,
            highlightthickness=0,
            activebackground=BG3,
        ).pack(fill="x", pady=(2, 4))
        tk.Label(
            dform,
            text="拖曳滑桿後按「儲存」；關閉設定窗後才會套用到卡片（避免卡死）。",
            fg=FG_DIM,
            bg=BG,
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        compact_var = tk.BooleanVar(value=bool(disp.get("compact", False)))
        startup_var = tk.BooleanVar(value=bool(self.cfg.get("start_with_windows", False)))
        for text, var in [
            ("Windows 開機時自動啟動", startup_var),
            ("永遠置頂", top_var),
            ("全螢幕遊戲時暫時取消置頂（建議）", drop_fs_var),
            ("精簡版面（只留溫濕度）", compact_var),
            ("顯示絕對濕度", show_abs),
            ("顯示露點", show_dew),
            ("顯示 VPD", show_vpd),
            ("顯示電量", show_bat),
        ]:
            tk.Checkbutton(
                dform, text=text, variable=var, fg=FG, bg=BG, selectcolor=BG3, activebackground=BG
            ).pack(anchor="w")
        tk.Label(
            dform,
            text="開機啟動只寫入目前使用者（不需系統管理員）。可在 Windows「設定 → 應用程式 → 啟動」關閉。",
            fg=FG_DIM,
            bg=BG,
            font=("Segoe UI", 8),
            anchor="w",
            wraplength=560,
            justify="left",
        ).pack(fill="x", pady=(2, 6))

        save_hint = tk.Label(dform, text="", fg=OK, bg=BG, anchor="w")
        save_hint.pack(fill="x", pady=(8, 0))

        def save_all() -> None:
            try:
                self.cfg["device"]["name"] = entries["name"].get()
                self.cfg["device"]["mac"] = entries["mac"].get().strip().upper()
                self.cfg["device"]["scan_seconds"] = float(entries["scan_seconds"].get())
                self.cfg["device"]["interval_seconds"] = float(entries["interval_seconds"].get())
                self.cfg["alerts"]["enabled"] = en_var.get()
                self.cfg["alerts"]["windows_toast"] = toast_var.get()
                self.cfg["alerts"]["sound"] = sound_var.get()
                self.cfg["alerts"]["sound_file"] = sound_file_var.get().strip()
                self.cfg["alerts"]["quiet_when_fullscreen"] = quiet_fs_var.get()
                self.cfg["alerts"]["quiet_sound_when_fullscreen"] = quiet_snd_var.get()
                self.cfg["alerts"]["cooldown_minutes"] = float(cd_var.get())
                self.cfg["alerts"]["rearm_margin"] = float(margin_var.get())
                op = max(0.4, min(1.0, float(op_var.get())))
                self.cfg["display"]["opacity"] = op
                self.cfg["display"]["always_on_top"] = top_var.get()
                self.cfg["display"]["drop_topmost_when_fullscreen"] = drop_fs_var.get()
                self.cfg["display"]["compact"] = compact_var.get()
                self.cfg["display"]["show_abs_humidity"] = show_abs.get()
                self.cfg["display"]["show_dew_point"] = show_dew.get()
                self.cfg["display"]["show_vpd"] = show_vpd.get()
                self.cfg["display"]["show_battery"] = show_bat.get()
                self.cfg["start_with_windows"] = bool(startup_var.get())
            except ValueError:
                save_hint.config(text="請檢查數值欄位", fg=ERR)
                return
            try:
                set_windows_startup(bool(startup_var.get()))
            except Exception as e:
                save_hint.config(text=f"開機啟動寫入失敗：{e}", fg=ERR)
                return
            try:
                self._persist()
            except Exception as e:
                save_hint.config(text=f"儲存失敗：{e}", fg=ERR)
                return
            self.root.title(self.cfg["device"]["name"])
            if self.lbl_name is not None:
                try:
                    self.lbl_name.config(text=self.cfg["device"]["name"])
                except Exception:
                    pass
            # CRITICAL: do not call root.attributes("-alpha") while this dialog is open.
            self._topmost_applied = None
            self._sync_pin_ui()
            self._apply_layout()
            save_hint.config(
                text=f"已儲存。透明度 {op * 100:.0f}% 將在關閉此視窗後套用。",
                fg=OK,
            )
            try:
                win.lift()
            except Exception:
                pass

        def close_settings() -> None:
            try:
                win.destroy()
            except Exception:
                pass
            # Apply deferred opacity / topmost after dialog is gone
            self.root.after(30, self._restore_hud_after_dialog)

        foot = tk.Frame(win, bg=BG)
        foot.pack(fill="x", padx=10, pady=8)
        tk.Button(foot, text="儲存", command=save_all, bg=ACCENT, fg="#102028", relief="flat", padx=16).pack(
            side="right"
        )
        tk.Button(foot, text="關閉", command=close_settings, bg=BG3, fg=FG, relief="flat", padx=12).pack(
            side="right", padx=8
        )

    # ── History window ───────────────────────────────────────────────────────

    def _open_history(self) -> None:
        if self.history_win and self.history_win.winfo_exists():
            self._prepare_dialog(self.history_win)
            return
        win = tk.Toplevel(self.root)
        self.history_win = win
        win.title("歷史資料")
        win.configure(bg=BG)
        win.geometry("720x480")
        self._prepare_dialog(win)

        top = tk.Frame(win, bg=BG)
        top.pack(fill="x", padx=10, pady=8)
        period = tk.StringVar(value="day")
        for text, val in [("時", "hour"), ("日", "day"), ("週", "week"), ("月", "month")]:
            tk.Radiobutton(
                top,
                text=text,
                variable=period,
                value=val,
                fg=FG,
                bg=BG,
                selectcolor=BG3,
                activebackground=BG,
                command=lambda: draw(),
            ).pack(side="left", padx=4)

        stats_lbl = tk.Label(top, text="", fg=FG_DIM, bg=BG, font=("Segoe UI", 9))
        stats_lbl.pack(side="left", padx=16)

        canvas = tk.Canvas(win, bg=BG2, highlightthickness=0)
        canvas.pack(fill="both", expand=True, padx=10, pady=4)

        def draw(_=None) -> None:
            canvas.delete("all")
            since = period_since(period.get())
            rows = self.history.query_series(since=since, max_points=2000)
            st = self.history.stats(since=since)
            if st:
                stats_lbl.config(
                    text=(
                        f"n={st.get('count', 0)}  "
                        f"溫度 {st.get('temp_min', 0):.1f}~{st.get('temp_max', 0):.1f} "
                        f"(均 {st.get('temp_avg', 0):.1f})  "
                        f"濕度 {st.get('hum_min', 0):.0f}~{st.get('hum_max', 0):.0f}% "
                        f"(均 {st.get('hum_avg', 0):.1f})"
                    )
                )
            else:
                stats_lbl.config(text="尚無本機歷史（需 BLE 掃描累積）")

            w = canvas.winfo_width() or 700
            h = canvas.winfo_height() or 360
            pad_l, pad_r, pad_t, pad_b = 48, 16, 20, 36
            if len(rows) < 2:
                canvas.create_text(
                    w // 2,
                    h // 2,
                    text="資料不足，請在有 BLE 時讓程式多跑一陣子",
                    fill=FG_DIM,
                    font=("Segoe UI", 11),
                )
                return

            temps = [r["temperature_c"] for r in rows]
            hums = [r["humidity_pct"] for r in rows]
            tss = [r["ts"] for r in rows]
            t_min, t_max = min(temps), max(temps)
            if abs(t_max - t_min) < 0.5:
                t_min -= 0.5
                t_max += 0.5
            h_min, h_max = min(hums), max(hums)
            if abs(h_max - h_min) < 1:
                h_min -= 1
                h_max += 1

            plot_w = w - pad_l - pad_r
            plot_h = (h - pad_t - pad_b) / 2 - 10

            def x_at(i: int) -> float:
                return pad_l + plot_w * i / (len(rows) - 1)

            def y_temp(v: float) -> float:
                return pad_t + plot_h * (1 - (v - t_min) / (t_max - t_min))

            def y_hum(v: float) -> float:
                base = pad_t + plot_h + 20
                return base + plot_h * (1 - (v - h_min) / (h_max - h_min))

            # Temp chart
            canvas.create_text(pad_l, pad_t - 8, text="溫度", fill=TEMP, anchor="w", font=("Segoe UI", 9))
            canvas.create_text(w - pad_r, pad_t - 8, text=f"{t_max:.1f}°C", fill=FG_DIM, anchor="e")
            canvas.create_text(
                w - pad_r, pad_t + plot_h, text=f"{t_min:.1f}°C", fill=FG_DIM, anchor="e"
            )
            pts = []
            for i, v in enumerate(temps):
                pts.extend([x_at(i), y_temp(v)])
            canvas.create_line(*pts, fill=TEMP, width=2, smooth=True)

            # Humidity chart
            base = pad_t + plot_h + 20
            canvas.create_text(pad_l, base - 8, text="濕度", fill=HUM, anchor="w", font=("Segoe UI", 9))
            canvas.create_text(w - pad_r, base - 8, text=f"{h_max:.0f}%", fill=FG_DIM, anchor="e")
            canvas.create_text(w - pad_r, base + plot_h, text=f"{h_min:.0f}%", fill=FG_DIM, anchor="e")
            pts_h = []
            for i, v in enumerate(hums):
                pts_h.extend([x_at(i), y_hum(v)])
            canvas.create_line(*pts_h, fill=HUM, width=2, smooth=True)

            # Time labels
            canvas.create_text(
                pad_l,
                h - 12,
                text=datetime.fromtimestamp(tss[0]).strftime("%m-%d %H:%M"),
                fill=FG_DIM,
                anchor="w",
                font=("Segoe UI", 8),
            )
            canvas.create_text(
                w - pad_r,
                h - 12,
                text=datetime.fromtimestamp(tss[-1]).strftime("%m-%d %H:%M"),
                fill=FG_DIM,
                anchor="e",
                font=("Segoe UI", 8),
            )

        def on_resize(_=None) -> None:
            win.after(50, draw)

        canvas.bind("<Configure>", on_resize)

        foot = tk.Frame(win, bg=BG)
        foot.pack(fill="x", padx=10, pady=8)

        def do_export() -> None:
            path = filedialog.asksaveasfilename(
                parent=win,
                defaultextension=".csv",
                filetypes=[("CSV", "*.csv")],
                initialfile=f"meter_history_{datetime.now():%Y%m%d_%H%M}.csv",
            )
            if not path:
                return
            n = self.history.export_csv(Path(path), since=period_since(period.get()))
            messagebox.showinfo("匯出", f"已匯出 {n} 筆到\n{path}", parent=win)

        def do_export_all() -> None:
            path = filedialog.asksaveasfilename(
                parent=win,
                defaultextension=".csv",
                filetypes=[("CSV", "*.csv")],
                initialfile=f"meter_history_all_{datetime.now():%Y%m%d}.csv",
            )
            if not path:
                return
            n = self.history.export_csv(Path(path), since=None)
            messagebox.showinfo("匯出", f"已匯出全部 {n} 筆到\n{path}", parent=win)

        tk.Button(
            foot, text="匯出此區間 CSV", command=do_export, bg=BG3, fg=FG, relief="flat", padx=10
        ).pack(side="left")
        tk.Button(
            foot, text="匯出全部 CSV", command=do_export_all, bg=BG3, fg=FG, relief="flat", padx=10
        ).pack(side="left", padx=6)

        def do_import() -> None:
            path = filedialog.askopenfilename(
                parent=win,
                filetypes=[("CSV", "*.csv"), ("所有檔案", "*.*")],
                title="匯入 SwitchBot App 或本程式 CSV",
            )
            if not path:
                return
            try:
                n = self.history.import_csv(
                    Path(path), mac=str(self.cfg.get("device", {}).get("mac") or "")
                )
            except Exception as e:
                messagebox.showerror("匯入", f"失敗：{e}", parent=win)
                return
            messagebox.showinfo("匯入", f"已寫入 {n} 筆（重複時間會略過）", parent=win)
            draw()
        tk.Button(
            foot, text="匯入 CSV…", command=do_import, bg=BG3, fg=FG, relief="flat", padx=10
        ).pack(side="left", padx=6)
        tk.Button(foot, text="重新整理", command=draw, bg=BG3, fg=FG, relief="flat", padx=10).pack(
            side="left"
        )
        tk.Button(foot, text="關閉", command=win.destroy, bg=BG3, fg=FG, relief="flat", padx=10).pack(
            side="right"
        )

        win.after(100, draw)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if not acquire_single_instance():
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("SwitchBot Meter", "程式已經在執行。請看系統匣或桌面上的看板。")
            root.destroy()
        except Exception:
            pass
        return
    MeterApp().run()


if __name__ == "__main__":
    main()
