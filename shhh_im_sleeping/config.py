"""Configuration loading and validation."""

from __future__ import annotations

import configparser
import os
import re
from datetime import datetime
from pathlib import Path

from .common import LOG, parse_hours

DEFAULT_CONFIG_PATHS = (
    "/etc/shhh-im-sleeping/config.ini",
    str(Path(__file__).resolve().parent.parent / "config.ini"),
)

# Docker housekeeping daemons are chatty but not "real work": they must not
# drag the machine into power mode on their own.  Container *workloads* still
# count normally because their processes are the actual load.
DEFAULT_IGNORE_PATTERNS = [
    re.compile(r"dockerd"),
    re.compile(r"containerd"),
    re.compile(r"docker-proxy"),
    re.compile(r"containerd-shim.*"),
]


def _warn_if_config_is_writable_by_others(path):
    """The daemon runs as root and obeys this file: warn if others can edit it."""
    try:
        st = os.stat(path)
    except OSError:
        return
    if st.st_uid != 0 or (st.st_mode & 0o022):
        LOG.warning(
            "config file %s is owned by uid %d with mode %04o: this daemon runs "
            "as root, so keep it root-owned and not group/world-writable",
            path, st.st_uid, st.st_mode & 0o777,
        )


class Config:
    """Runtime configuration, loaded from an INI file (all keys optional)."""

    def __init__(self):
        # [monitor]
        self.sample_seconds = 5.0

        # [thresholds]
        self.cpu_busy_percent = 35.0
        self.load_factor = 0.75
        self.power_on_seconds = 15.0
        self.quiet_after_seconds = 90.0

        # [temperature]
        self.critical_celsius = 85.0
        self.exit_celsius = 75.0
        self.sensor_names = {"coretemp", "k10temp", "cpu_thermal", "acpitz"}

        # [quiet]
        self.quiet_governor = "powersave"
        self.quiet_max_freq_khz = 0
        self.quiet_no_turbo = True

        # [power]
        self.power_governor = "performance"
        self.power_no_turbo = False

        # [fans]
        self.fan_paths = "auto"
        self.quiet_fan_pwm = 76
        self.power_fan_pwm = 0
        self.min_fan_pwm = 50          # never stall a manual fan below this
        self.fan_ramp_celsius = 70.0   # start ramping fans up at this temp
        self.fan_ramp_pwm = 180        # duty to use once ramped

        # [processes]
        self.process_patterns = []
        self.ignore_patterns = list(DEFAULT_IGNORE_PATTERNS)

        # [schedule]
        self.schedule_enabled = False
        self.quiet_hours = None
        self.hard_load_per_core = 2.0

    # -- loading ----------------------------------------------------------- #

    @classmethod
    def load(cls, path=None):
        cfg = cls()
        parser = configparser.ConfigParser(interpolation=None)
        candidates = [path] if path else list(DEFAULT_CONFIG_PATHS)
        used = None
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                parser.read(candidate)
                used = candidate
                break
        if used:
            LOG.info("loaded configuration from %s", used)
            _warn_if_config_is_writable_by_others(used)
        else:
            LOG.warning("no configuration file found, using built-in defaults")
        cfg._apply(parser)
        return cfg

    @staticmethod
    def _compile_patterns(raw, what):
        patterns = []
        for chunk in re.split(r"[\n,]", raw or ""):
            chunk = chunk.strip()
            if not chunk or chunk.startswith("#"):
                continue
            try:
                patterns.append(re.compile(chunk))
            except re.error as exc:
                LOG.warning("ignoring invalid %s pattern %r: %s", what, chunk, exc)
        return patterns

    def _apply(self, p):
        if p.has_section("monitor"):
            self.sample_seconds = p.getfloat("monitor", "sample_seconds", fallback=self.sample_seconds)

        if p.has_section("thresholds"):
            self.cpu_busy_percent = p.getfloat("thresholds", "cpu_busy_percent", fallback=self.cpu_busy_percent)
            self.load_factor = p.getfloat("thresholds", "load_factor", fallback=self.load_factor)
            self.power_on_seconds = p.getfloat("thresholds", "power_on_seconds", fallback=self.power_on_seconds)
            self.quiet_after_seconds = p.getfloat("thresholds", "quiet_after_seconds", fallback=self.quiet_after_seconds)

        if p.has_section("temperature"):
            self.critical_celsius = p.getfloat("temperature", "critical_celsius", fallback=self.critical_celsius)
            self.exit_celsius = p.getfloat("temperature", "exit_celsius", fallback=self.exit_celsius)
            raw = p.get("temperature", "sensor_names", fallback=None)
            if raw is not None:
                names = {n.strip() for n in raw.replace("\n", ",").split(",") if n.strip()}
                self.sensor_names = names  # empty set == accept every hwmon chip

        if p.has_section("quiet"):
            self.quiet_governor = p.get("quiet", "governor", fallback=self.quiet_governor)
            self.quiet_max_freq_khz = p.getint("quiet", "max_freq_khz", fallback=self.quiet_max_freq_khz)
            self.quiet_no_turbo = p.getboolean("quiet", "no_turbo", fallback=self.quiet_no_turbo)

        if p.has_section("power"):
            self.power_governor = p.get("power", "governor", fallback=self.power_governor)
            self.power_no_turbo = p.getboolean("power", "no_turbo", fallback=self.power_no_turbo)

        if p.has_section("fans"):
            self.fan_paths = p.get("fans", "paths", fallback=self.fan_paths)
            self.quiet_fan_pwm = p.getint("fans", "quiet_pwm", fallback=self.quiet_fan_pwm)
            self.power_fan_pwm = p.getint("fans", "power_pwm", fallback=self.power_fan_pwm)
            self.min_fan_pwm = p.getint("fans", "min_pwm", fallback=self.min_fan_pwm)
            self.fan_ramp_celsius = p.getfloat("fans", "ramp_celsius", fallback=self.fan_ramp_celsius)
            self.fan_ramp_pwm = p.getint("fans", "ramp_pwm", fallback=self.fan_ramp_pwm)

        if p.has_section("processes"):
            self.process_patterns = self._compile_patterns(
                p.get("processes", "patterns", fallback=""), "process")
            raw = p.get("processes", "ignore_patterns", fallback=None)
            if raw is not None:  # explicit list replaces the Docker defaults
                self.ignore_patterns = self._compile_patterns(raw, "ignore")

        if p.has_section("schedule"):
            self.schedule_enabled = p.getboolean("schedule", "enabled", fallback=self.schedule_enabled)
            self.hard_load_per_core = p.getfloat("schedule", "hard_load_per_core", fallback=self.hard_load_per_core)
            raw = p.get("schedule", "quiet_hours", fallback=None)
            if raw is not None:
                try:
                    self.quiet_hours = parse_hours(raw)
                except ValueError as exc:
                    LOG.warning("%s", exc)

        self._sanitise()

    def _sanitise(self):
        """Clamp values so a bad config file can never do something silly."""
        # Temperature thresholds outside plausible silicon ranges would either
        # never trigger or trigger constantly; clamp them to sane bounds.
        self.critical_celsius = max(40.0, min(110.0, self.critical_celsius))
        self.exit_celsius = max(30.0, min(110.0, self.exit_celsius))
        if self.exit_celsius >= self.critical_celsius:
            self.exit_celsius = self.critical_celsius - 10.0
        self.fan_ramp_celsius = max(30.0, self.fan_ramp_celsius)
        self.sample_seconds = max(1.0, self.sample_seconds)
        self.power_on_seconds = max(1.0, self.power_on_seconds)
        self.quiet_after_seconds = max(1.0, self.quiet_after_seconds)

        # Fan duties live in 1..255 when manual; 0 is reserved for "automatic".
        self.min_fan_pwm = max(1, min(255, self.min_fan_pwm))
        self.fan_ramp_pwm = max(self.min_fan_pwm, min(255, self.fan_ramp_pwm))
        if self.quiet_fan_pwm > 0:
            self.quiet_fan_pwm = max(self.min_fan_pwm, min(255, self.quiet_fan_pwm))
        if self.power_fan_pwm > 0:
            self.power_fan_pwm = max(self.min_fan_pwm, min(255, self.power_fan_pwm))
        if self.fan_ramp_celsius >= self.critical_celsius:
            self.fan_ramp_celsius = self.critical_celsius - 10.0

        if self.quiet_max_freq_khz < 0:
            self.quiet_max_freq_khz = 0

    def in_quiet_hours(self, moment=None):
        if not self.schedule_enabled or self.quiet_hours is None:
            return False
        now = (moment or datetime.now()).time()
        start, end = self.quiet_hours
        if start <= end:
            return start <= now < end
        return now >= start or now < end  # window wraps around midnight
