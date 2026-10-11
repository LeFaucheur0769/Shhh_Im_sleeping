"""Unit tests for the pure logic pieces (no sysfs access needed).

Run with:  python3 -m unittest discover -s tests
"""

import datetime
import glob
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shhh_im_sleeping.common import POWER, QUIET, parse_hours
from shhh_im_sleeping.config import Config
from shhh_im_sleeping import fan_tuner
from shhh_im_sleeping.fan_tuner import FanTuner, target_pwm


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
        with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as fh:
            fh.write("[thresholds]\ncpu_busy_percent = 50\n")
            path = fh.name
        try:
            # assertLogs also swallows the "not root-owned" warning that a
            # temp file necessarily triggers in a test environment.
            with self.assertLogs("shhh-im-sleeping", level="WARNING"):
                cfg = Config.load(path)
            self.assertEqual(cfg.cpu_busy_percent, 50)
        finally:
            os.unlink(path)

    def test_temperature_thresholds_are_clamped(self):
        cfg = Config()
        cfg.critical_celsius = 5000.0
        cfg.exit_celsius = 4990.0
        cfg._sanitise()
        self.assertLessEqual(cfg.critical_celsius, 110.0)
        self.assertLess(cfg.exit_celsius, cfg.critical_celsius)

        cfg = Config()
        cfg.critical_celsius = -20.0
        cfg._sanitise()
        self.assertGreaterEqual(cfg.critical_celsius, 40.0)
        self.assertLess(cfg.exit_celsius, cfg.critical_celsius)

        cfg = Config()
        cfg.fan_ramp_celsius = 5.0
        cfg._sanitise()
        self.assertGreaterEqual(cfg.fan_ramp_celsius, 30.0)

    def test_world_writable_config_warns(self):
        with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as fh:
            fh.write("[monitor]\nsample_seconds = 5\n")
            path = fh.name
        try:
            os.chmod(path, 0o666)
            with self.assertLogs("shhh-im-sleeping", level="WARNING") as captured:
                Config.load(path)
            self.assertTrue(any("root-owned" in message for message in captured.output))
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


class TestFanPathSafety(unittest.TestCase):
    """Explicit fan paths must never become an arbitrary-file writer."""

    def _cfg(self, paths):
        cfg = Config()
        cfg.fan_paths = paths
        return cfg

    def test_explicit_path_outside_sysfs_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "pwm1")
            with open(fake, "w") as fh:
                fh.write("128")
            with self.assertLogs("shhh-im-sleeping", level="WARNING"):
                found = FanTuner._resolve_paths(self._cfg(fake))
            self.assertEqual(found, [])

    def test_symlink_outside_sysfs_rejected(self):
        # A pwm-named symlink whose target escapes /sys must be rejected.
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "real_file")
            with open(target, "w") as fh:
                fh.write("0")
            link = os.path.join(tmp, "pwm1")
            os.symlink(target, link)
            with self.assertLogs("shhh-im-sleeping", level="WARNING"):
                found = FanTuner._resolve_paths(self._cfg(link))
            self.assertEqual(found, [])

    @unittest.skipUnless(
        any(glob.glob("/sys/class/hwmon/hwmon*/pwm[0-9]")
            or glob.glob("/sys/class/hwmon/hwmon*/pwm[0-9][0-9]")),
        "no plain pwmN sysfs file on this machine",
    )
    def test_real_sysfs_path_accepted(self):
        for path in glob.glob("/sys/class/hwmon/hwmon*/pwm*"):
            import re
            if re.match(r"^pwm\d+$", os.path.basename(path)):
                self.assertIn(path, FanTuner._resolve_paths(self._cfg(path)))
                return

    def test_auto_discovery_skips_driver_managed_gpu_chips(self):
        with tempfile.TemporaryDirectory() as tmp:
            gpu = os.path.join(tmp, "hwmon1")
            mobo = os.path.join(tmp, "hwmon2")
            for name, chip in ((gpu, "amdgpu"), (mobo, "nct6797")):
                os.mkdir(name)
                with open(os.path.join(name, "name"), "w") as fh:
                    fh.write(chip + "\n")
                with open(os.path.join(name, "pwm1"), "w") as fh:
                    fh.write("128\n")
                with open(os.path.join(name, "fan1_input"), "w") as fh:
                    fh.write("1200\n")
            old_root = fan_tuner.HWMON_ROOT
            fan_tuner.HWMON_ROOT = tmp
            try:
                found = FanTuner._resolve_paths(self._cfg("auto"))
            finally:
                fan_tuner.HWMON_ROOT = old_root
            self.assertEqual(found, [os.path.join(mobo, "pwm1")])


class TestFanEbusyFallback(unittest.TestCase):
    """nct6775/it87-family drivers refuse duty writes while a fan is still
    in automatic mode (hwmon ABI: -EBUSY when pwmN_enable is 0)."""

    def _fake_hwmon(self, tmp, enable="0"):
        hwmon = os.path.join(tmp, "hwmon3")
        os.mkdir(hwmon)
        files = {
            "name": "nct6775\n",
            "pwm6": "128\n",
            "fan6_input": "1200\n",
            "pwm6_enable": enable + "\n",
        }
        for name, value in files.items():
            with open(os.path.join(hwmon, name), "w") as fh:
                fh.write(value)
        return os.path.join(hwmon, "pwm6")

    def _fake_write(self, fail_duty=False):
        """Emulate the hwmon ABI: duty writes fail while pwmN_enable is 0."""
        calls = []

        def fake_write(path, value, dry_run=False, log=True):
            calls.append((os.path.basename(path), str(value)))
            if os.path.basename(path).endswith("_enable"):
                with open(path, "w") as fh:
                    fh.write(str(value))
                return True
            if fail_duty:
                return False
            with open(path + "_enable") as fh:
                if fh.read().strip() == "0":
                    return False  # -EBUSY, like nct6775
            with open(path, "w") as fh:
                fh.write(str(value))
            return True

        return fake_write, calls

    def _run(self, tmp, pwm, fake_write):
        cfg = Config()
        cfg.fan_paths = "auto"
        old_write, old_root = fan_tuner.write_file, fan_tuner.HWMON_ROOT
        fan_tuner.write_file, fan_tuner.HWMON_ROOT = fake_write, tmp
        try:
            tuner = FanTuner(cfg)
            tuner.apply(QUIET, cfg, 40.0)
        finally:
            fan_tuner.write_file, fan_tuner.HWMON_ROOT = old_write, old_root
        return tuner

    def _read(self, path):
        with open(path) as fh:
            return fh.read().strip()

    def test_duty_refused_while_automatic_falls_back_to_enable_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            pwm = self._fake_hwmon(tmp)
            fake_write, calls = self._fake_write()
            tuner = self._run(tmp, pwm, fake_write)
            # duty attempt, then enable=1, then duty again (the retry works)
            self.assertEqual(calls, [("pwm6", "76"), ("pwm6_enable", "1"),
                                     ("pwm6", "76")])
            self.assertEqual(self._read(pwm), "76")
            self.assertEqual(self._read(pwm + "_enable"), "1")
            self.assertEqual(tuner.pwms, [pwm])

    def test_already_manual_fan_keeps_duty_then_enable_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            pwm = self._fake_hwmon(tmp, enable="1")
            fake_write, calls = self._fake_write()
            self._run(tmp, pwm, fake_write)
            self.assertEqual(calls, [("pwm6", "76"), ("pwm6_enable", "1")])
            self.assertEqual(self._read(pwm), "76")

    def test_uncontrollable_fan_is_dropped_and_left_automatic(self):
        with tempfile.TemporaryDirectory() as tmp:
            pwm = self._fake_hwmon(tmp)
            fake_write, calls = self._fake_write(fail_duty=True)
            with self.assertLogs("shhh-im-sleeping", level="WARNING") as captured:
                tuner = self._run(tmp, pwm, fake_write)
            self.assertTrue(any("dropped" in message for message in captured.output))
            self.assertEqual(tuner.pwms, [])
            # firmware control handed back, duty untouched, nothing restored later
            self.assertEqual(self._read(pwm), "128")
            self.assertEqual(self._read(pwm + "_enable"), "0")
            calls.clear()
            tuner.restore()
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
