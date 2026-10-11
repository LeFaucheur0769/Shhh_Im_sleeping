"""Unit tests for the pure logic pieces (no sysfs access needed).

Run with:  python3 -m unittest discover -s tests
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shhh_im_sleeping.common import POWER, QUIET, parse_hours
from shhh_im_sleeping.config import Config
from shhh_im_sleeping.fan_tuner import target_pwm


class TestParseHours(unittest.TestCase):
    def test_simple_window(self):
        start, end = parse_hours("23:00-07:00")
        self.assertEqual(start, datetime.time(23, 0))
        self.assertEqual(end, datetime.time(7, 0))

    def test_midnight_boundary(self):
        start, end = parse_hours("00:00-23:59")
        self.assertEqual(start, datetime.time(0, 0))
        self.assertEqual(end, datetime.time(23, 59))

    def test_empty_or_off_returns_none(self):
        self.assertIsNone(parse_hours(""))
        self.assertIsNone(parse_hours(None))
        self.assertIsNone(parse_hours("off"))

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            parse_hours("nonsense")
        with self.assertRaises(ValueError):
            parse_hours("25:99-30:00")  # datetime() rejects out-of-range


class TestConfig(unittest.TestCase):
    def test_defaults_are_safe(self):
        cfg = Config()
        self.assertEqual(cfg.quiet_fan_pwm, 76)
        self.assertEqual(cfg.power_fan_pwm, 0)      # automatic fans by default
        self.assertGreaterEqual(cfg.min_fan_pwm, 1)
        self.assertLess(cfg.fan_ramp_celsius, cfg.critical_celsius)

    def test_sanitise_clamps_nonsense(self):
        cfg = Config()
        cfg.sample_seconds = 0
        cfg.power_on_seconds = -5
        cfg.quiet_fan_pwm = 9999
        cfg.min_fan_pwm = -10
        cfg._sanitise()
        self.assertGreaterEqual(cfg.sample_seconds, 1)
        self.assertGreaterEqual(cfg.power_on_seconds, 1)
        self.assertLessEqual(cfg.quiet_fan_pwm, 255)
        self.assertGreaterEqual(cfg.min_fan_pwm, 1)

    def test_load_from_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as fh:
            fh.write("[thresholds]\ncpu_busy_percent = 50\n")
            path = fh.name
        try:
            cfg = Config.load(path)
            self.assertEqual(cfg.cpu_busy_percent, 50)
        finally:
            os.unlink(path)

    def test_quiet_hours_wrap_around_midnight(self):
        cfg = Config()
        cfg.schedule_enabled = True
        cfg.quiet_hours = parse_hours("23:00-07:00")
        self.assertTrue(cfg.in_quiet_hours(datetime.datetime(2026, 1, 1, 23, 30)))
        self.assertTrue(cfg.in_quiet_hours(datetime.datetime(2026, 1, 1, 3, 0)))
        self.assertTrue(cfg.in_quiet_hours(datetime.datetime(2026, 1, 1, 6, 59)))
        self.assertFalse(cfg.in_quiet_hours(datetime.datetime(2026, 1, 1, 7, 0)))
        self.assertFalse(cfg.in_quiet_hours(datetime.datetime(2026, 1, 1, 12, 0)))

    def test_quiet_hours_disabled(self):
        cfg = Config()
        self.assertFalse(cfg.in_quiet_hours())


class TestTargetPwm(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_power_mode_default_is_automatic(self):
        self.assertEqual(target_pwm(POWER, 60.0, self.cfg), 0)

    def test_quiet_mode_uses_quiet_pwm(self):
        self.assertEqual(target_pwm(QUIET, 40.0, self.cfg), self.cfg.quiet_fan_pwm)

    def test_quiet_mode_ramps_before_critical(self):
        mid = self.cfg.fan_ramp_celsius + 5
        hot = self.cfg.critical_celsius + 5
        ramped = target_pwm(QUIET, mid, self.cfg)
        critical = target_pwm(QUIET, hot, self.cfg)
        self.assertGreaterEqual(ramped, self.cfg.fan_ramp_pwm)
        self.assertLess(ramped, 255)
        self.assertEqual(critical, 255)

    def test_floor_prevents_stall(self):
        self.cfg.quiet_fan_pwm = 10
        self.cfg.min_fan_pwm = 50
        self.assertEqual(target_pwm(QUIET, 40.0, self.cfg), 50)

    def test_quiet_pwm_zero_means_automatic(self):
        self.cfg.quiet_fan_pwm = 0
        self.assertEqual(target_pwm(QUIET, 40.0, self.cfg), 0)

    def test_missing_temperature_falls_back_to_quiet_pwm(self):
        self.assertEqual(target_pwm(QUIET, None, self.cfg), self.cfg.quiet_fan_pwm)

    def test_power_mode_manual_duty(self):
        self.cfg.power_fan_pwm = 100
        self.assertEqual(target_pwm(POWER, 40.0, self.cfg), 100)

    def test_power_mode_automatic_stays_automatic_when_hot(self):
        hot = self.cfg.critical_celsius + 5
        self.assertEqual(target_pwm(POWER, hot, self.cfg), 0)

    def test_critical_override_applies_in_power_mode(self):
        self.cfg.power_fan_pwm = 100
        hot = self.cfg.critical_celsius + 5
        self.assertEqual(target_pwm(POWER, hot, self.cfg), 255)

    def test_power_mode_ramps_too(self):
        self.cfg.power_fan_pwm = 100
        mid = self.cfg.fan_ramp_celsius + 5
        self.assertEqual(target_pwm(POWER, mid, self.cfg), self.cfg.fan_ramp_pwm)


if __name__ == "__main__":
    unittest.main()
