"""Reads CPU utilisation, load average, temperatures and process names."""

from __future__ import annotations

import glob
import os

from .common import (
    HWMON_ROOT,
    LOG,
    PROC_LOADAVG,
    PROC_STAT,
    THERMAL_ROOT,
    read_file,
    read_int,
)


class Monitor:
    """Reads CPU utilisation, load average, temperatures and process names."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.cores = os.cpu_count() or 1
        self._prev_cpu = self._read_cpu_times()
        self.sensors = self._discover_sensors()
        self.temp_misses = 0

    # -- CPU --------------------------------------------------------------- #

    @staticmethod
    def _read_cpu_times():
        raw = read_file(PROC_STAT)
        if not raw:
            return (0, 0)
        fields = raw.splitlines()[0].split()[1:]
        try:
            values = [int(v) for v in fields]
        except ValueError:
            return (0, 0)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return (idle, sum(values))

    def cpu_busy_percent(self):
        """Busy CPU percentage since the previous call."""
        current = self._read_cpu_times()
        previous, self._prev_cpu = self._prev_cpu, current
        total = current[1] - previous[1]
        idle = current[0] - previous[0]
        if total <= 0:
            return 0.0
        return max(0.0, min(100.0, 100.0 * (total - idle) / total))

    # -- load -------------------------------------------------------------- #

    @staticmethod
    def load1():
        raw = read_file(PROC_LOADAVG)
        if not raw:
            return 0.0
        try:
            return float(raw.split()[0])
        except (IndexError, ValueError):
            return 0.0

    # -- temperature ------------------------------------------------------- #

    def _discover_sensors(self):
        sensors = []
        for hwmon in sorted(glob.glob(os.path.join(HWMON_ROOT, "hwmon*"))):
            name = read_file(os.path.join(hwmon, "name")) or ""
            if self.cfg.sensor_names and name not in self.cfg.sensor_names:
                continue
            sensors.extend(sorted(glob.glob(os.path.join(hwmon, "temp*_input"))))
        if not sensors:
            # fall back to the generic thermal zones
            for zone in sorted(glob.glob(os.path.join(THERMAL_ROOT, "thermal_zone*"))):
                sensors.append(os.path.join(zone, "temp"))
        return sensors

    def max_temp(self):
        """Highest temperature in degrees Celsius, or None if unavailable.

        Counts consecutive total failures so the daemon can fall back to
        automatic fan control when sensors disappear.
        """
        highest = None
        for path in self.sensors:
            raw = read_int(path)
            if raw is None:
                continue
            celsius = raw / 1000.0
            if not -50.0 < celsius < 200.0:  # sanity filter
                continue
            if highest is None or celsius > highest:
                highest = celsius
        if highest is None and self.sensors:
            self.temp_misses += 1
            if self.temp_misses % 3 == 1:
                LOG.warning("temperature sensors unreadable (%d misses in a row)",
                            self.temp_misses)
        else:
            self.temp_misses = 0
        return highest

    # -- processes --------------------------------------------------------- #

    def hot_processes(self):
        """Names of running processes matching the configured patterns.

        Processes matching the ignore patterns (Docker housekeeping daemons
        by default) never trigger power mode on their own.
        """
        if not self.cfg.process_patterns:
            return []
        matches = []
        try:
            entries = os.listdir("/proc")
        except OSError:
            return []
        for entry in entries:
            if not entry.isdigit():
                continue
            comm = read_file("/proc/%s/comm" % entry)
            if not comm:
                continue
            if self.cfg.ignore_patterns and any(
                    pattern.search(comm) for pattern in self.cfg.ignore_patterns):
                continue
            if any(pattern.search(comm) for pattern in self.cfg.process_patterns):
                matches.append(comm)
        return matches
