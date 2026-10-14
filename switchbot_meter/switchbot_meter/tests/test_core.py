"""Unit tests for parse, psychrometrics, AlertEngine, and CSV import."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import (  # noqa: E402
    AlertEngine,
    Reading,
    _decode_th3,
    _service_payload,
    absolute_humidity_gm3,
    dew_point_c,
    is_windows_startup_enabled,
    parse_advertisement,
    parse_history_csv,
    parse_meter_manufacturer_data,
    parse_meter_service_data,
    rule_is_active,
    set_windows_startup,
    startup_launch_command,
    vpd_kpa,
    windows_startup_command,
)


def _reading(temp: float, hum: float, bat: int | None = 80, at: float | None = None) -> Reading:
    return Reading(
        temperature_c=temp,
        humidity_pct=hum,
        battery_pct=bat,
        rssi=-60,
        address="AA:BB:CC:DD:EE:FF",
        at=time.time() if at is None else at,
    ).with_derived()


class TestPsychrometrics(unittest.TestCase):
    def test_dew_point_room(self) -> None:
        d = dew_point_c(25.0, 50.0)
        self.assertAlmostEqual(d, 13.8, delta=0.3)

    def test_dew_point_saturated(self) -> None:
        self.assertAlmostEqual(dew_point_c(20.0, 100.0), 20.0, delta=0.15)

    def test_abs_humidity_positive(self) -> None:
        a = absolute_humidity_gm3(25.0, 50.0)
        self.assertGreater(a, 10.0)
        self.assertLess(a, 13.0)

    def test_vpd_zero_at_100(self) -> None:
        self.assertAlmostEqual(vpd_kpa(25.0, 100.0), 0.0, delta=0.02)

    def test_vpd_increases_as_rh_drops(self) -> None:
        self.assertGreater(vpd_kpa(25.0, 30.0), vpd_kpa(25.0, 70.0))

    def test_rh_clamped(self) -> None:
        self.assertGreater(dew_point_c(20.0, 0.0), -80.0)
        self.assertGreaterEqual(vpd_kpa(20.0, -5.0), 0.0)


class TestParse(unittest.TestCase):
    def test_service_data_celsius(self) -> None:
        data = bytes([0x69, 0x00, 73, 0x04, 0x80 | 31, 68])
        p = parse_meter_service_data(data)
        assert p is not None
        self.assertEqual(p["device_type"], 0x69)
        self.assertEqual(p["battery_pct"], 73)
        self.assertAlmostEqual(p["temperature_c"], 31.4)
        self.assertEqual(p["humidity_pct"], 68.0)
        self.assertFalse(p["display_f"])

    def test_fahrenheit_display_flag_does_not_convert(self) -> None:
        data = bytes([0x54, 0x00, 100, 0x04, 0x80 | 31, 0x80 | 68])
        p = parse_meter_service_data(data)
        assert p is not None
        self.assertAlmostEqual(p["temperature_c"], 31.4)
        self.assertTrue(p["display_f"])

    def test_negative_temp(self) -> None:
        data = bytes([0x54, 0x00, 80, 0x05, 5, 40])
        p = parse_meter_service_data(data)
        assert p is not None
        self.assertAlmostEqual(p["temperature_c"], -5.5)

    def test_reject_bot(self) -> None:
        self.assertIsNone(parse_meter_service_data(bytes([0x48, 0, 50, 0, 0x80 | 20, 40])))

    def test_th3_roundtrip(self) -> None:
        th = _decode_th3(bytes([0x04, 0x80 | 31, 68]))
        assert th is not None
        self.assertAlmostEqual(th[0], 31.4)
        self.assertEqual(th[1], 68.0)

    def test_manufacturer_has_no_battery(self) -> None:
        mfr = bytes([0xEB, 0x6B, 0x00, 0x46, 0x64, 0x58, 0x00, 11, 0x04, 0x80 | 31, 68])
        m = parse_meter_manufacturer_data(mfr)
        assert m is not None
        self.assertIsNone(m["battery_pct"])
        self.assertAlmostEqual(m["temperature_c"], 31.4)

    def test_service_uuid_128bit(self) -> None:
        class Adv:
            service_data = {
                "0000fd3d-0000-1000-8000-00805f9b34fb": bytes(
                    [0x69, 0x00, 73, 0x04, 0x80 | 31, 68]
                )
            }
            manufacturer_data = {}
            rssi = -70

        self.assertIsNotNone(_service_payload(Adv()))
        p = parse_advertisement(Adv())
        assert p is not None
        self.assertEqual(p["battery_pct"], 73)

    def test_mfr_only_advertisement_no_fake_battery(self) -> None:
        mfr = bytes([0xEB, 0x6B, 0x00, 0x46, 0x64, 0x58, 0x00, 11, 0x04, 0x80 | 31, 68])

        class Adv:
            service_data = {}
            manufacturer_data = {0x0969: mfr}
            rssi = -80

        p = parse_advertisement(Adv())
        assert p is not None
        self.assertIsNone(p["battery_pct"])


class TestAlertEngine(unittest.TestCase):
    def test_fires_once_then_cooldown(self) -> None:
        cfg = {
            "enabled": True,
            "cooldown_minutes": 10,
            "rearm_margin": 0.5,
            "rules": [
                {
                    "id": "hot",
                    "enabled": True,
                    "label": "高溫",
                    "logic": "and",
                    "conditions": [{"metric": "temperature_c", "op": ">=", "value": 30}],
                }
            ],
        }
        eng = AlertEngine(cfg)
        first = eng.evaluate(_reading(31.0, 50.0))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["id"], "hot")
        again = eng.evaluate(_reading(31.2, 50.0))
        self.assertEqual(again, [])

    def test_rearm_after_clear(self) -> None:
        cfg = {
            "enabled": True,
            "cooldown_minutes": 0,
            "rearm_margin": 0.5,
            "rules": [
                {
                    "id": "hot",
                    "enabled": True,
                    "label": "高溫",
                    "conditions": [{"metric": "temperature_c", "op": ">=", "value": 30}],
                }
            ],
        }
        eng = AlertEngine(cfg)
        self.assertEqual(len(eng.evaluate(_reading(30.5, 50.0))), 1)
        self.assertEqual(eng.evaluate(_reading(30.2, 50.0)), [])  # still in band, disarmed
        self.assertEqual(eng.evaluate(_reading(29.0, 50.0)), [])  # cleared
        self.assertEqual(len(eng.evaluate(_reading(30.5, 50.0))), 1)  # re-armed

    def test_and_vs_or(self) -> None:
        and_rule = {
            "id": "both",
            "enabled": True,
            "logic": "and",
            "conditions": [
                {"metric": "temperature_c", "op": ">=", "value": 30},
                {"metric": "humidity_pct", "op": ">=", "value": 60},
            ],
        }
        or_rule = {**and_rule, "id": "either", "logic": "or"}
        hot_dry = _reading(31.0, 40.0)
        self.assertFalse(rule_is_active(hot_dry, and_rule))
        self.assertTrue(rule_is_active(hot_dry, or_rule))
        self.assertTrue(rule_is_active(_reading(31.0, 70.0), and_rule))

    def test_disabled_master_switch(self) -> None:
        eng = AlertEngine(
            {
                "enabled": False,
                "cooldown_minutes": 0,
                "rules": [
                    {
                        "id": "hot",
                        "enabled": True,
                        "conditions": [{"metric": "temperature_c", "op": ">=", "value": 1}],
                    }
                ],
            }
        )
        self.assertEqual(eng.evaluate(_reading(40.0, 50.0)), [])


class TestHistoryCsv(unittest.TestCase):
    def test_our_export_headers(self) -> None:
        text = (
            "ts_iso,mac,temperature_c,humidity_pct\n"
            "2026-08-01T12:00:00,AA:BB,25.5,55\n"
        )
        rows = parse_history_csv(text)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["temperature_c"], 25.5)
        self.assertEqual(rows[0]["mac"], "AA:BB")

    def test_switchbot_app_chinese(self) -> None:
        text = "時間,溫度(°C),濕度(%)\n2026/08/01 12:00,26.1,61\n"
        rows = parse_history_csv(text)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["temperature_c"], 26.1)
        self.assertAlmostEqual(rows[0]["humidity_pct"], 61.0)

    def test_skips_garbage(self) -> None:
        text = "ts_iso,temperature_c,humidity_pct\nnot-a-date,25,50\n"
        self.assertEqual(parse_history_csv(text), [])


class TestWindowsStartup(unittest.TestCase):
    VALUE = "SwitchBotMeter_Unittest"

    def tearDown(self) -> None:
        try:
            set_windows_startup(False, name=self.VALUE)
        except Exception:
            pass

    def test_launch_command_is_quoted(self) -> None:
        cmd = startup_launch_command()
        self.assertTrue(cmd.startswith('"'), cmd)
        self.assertIn("app.py", cmd.lower())

    def test_set_and_clear_run_key(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows only")
        set_windows_startup(False, name=self.VALUE)
        self.assertFalse(is_windows_startup_enabled(self.VALUE))
        set_windows_startup(True, name=self.VALUE, command='"C:\\tmp\\SwitchBotMeter.exe"')
        self.assertTrue(is_windows_startup_enabled(self.VALUE))
        self.assertEqual(windows_startup_command(self.VALUE), '"C:\\tmp\\SwitchBotMeter.exe"')
        set_windows_startup(False, name=self.VALUE)
        self.assertFalse(is_windows_startup_enabled(self.VALUE))
        set_windows_startup(False, name=self.VALUE)


if __name__ == "__main__":
    unittest.main()
