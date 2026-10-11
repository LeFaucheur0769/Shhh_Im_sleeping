"""Controls cpufreq governors, frequency limits and turbo boost."""

from __future__ import annotations

import glob
import os

from .common import CPU_ROOT, LOG, POWER, read_file, read_int, write_file


class CpuTuner:
    """Controls cpufreq governors, frequency limits and turbo boost.

    Every sysfs file is remembered before the first write and restored on
    exit, so the machine always ends up where the user left it.
    """

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self._original = {}

        self.governor_files = sorted(
            glob.glob(os.path.join(CPU_ROOT, "cpu*/cpufreq/scaling_governor"))
        )
        self.freq_dirs = sorted({
            os.path.dirname(path)
            for path in glob.glob(os.path.join(CPU_ROOT, "cpu*/cpufreq/scaling_max_freq"))
        })

        self.available_governors = set()
        if self.governor_files:
            raw = read_file(os.path.join(os.path.dirname(self.governor_files[0]),
                                         "scaling_available_governors"))
            if raw:
                self.available_governors = set(raw.split())

        pstate = os.path.join(CPU_ROOT, "intel_pstate")
        self.no_turbo = os.path.join(pstate, "no_turbo")
        self.min_perf = os.path.join(pstate, "min_perf_pct")

        if not self.governor_files:
            LOG.info("no cpufreq sysfs interface found -- CPU tuning disabled")
        elif self.available_governors:
            LOG.info("available governors: %s", ", ".join(sorted(self.available_governors)))

    # -- helpers ----------------------------------------------------------- #

    def _remember(self, path):
        if path not in self._original:
            self._original[path] = read_file(path)

    def _pick_governor(self, wanted):
        """Fall back gracefully on platforms that only expose 'powersave'."""
        if not self.available_governors or wanted in self.available_governors:
            return wanted
        fallback = "powersave" if "powersave" in self.available_governors \
            else sorted(self.available_governors)[0]
        LOG.debug("governor %r unavailable, using %r", wanted, fallback)
        return fallback

    # -- public API -------------------------------------------------------- #

    def apply(self, mode, cfg):
        governor = self._pick_governor(
            cfg.power_governor if mode == POWER else cfg.quiet_governor
        )
        for path in self.governor_files:
            self._remember(path)
            write_file(path, governor, self.dry_run)

        for directory in self.freq_dirs:
            hardware_max = read_int(os.path.join(directory, "cpuinfo_max_freq"))
            if hardware_max is None:
                continue
            hardware_min = read_int(os.path.join(directory, "cpuinfo_min_freq"))
            limit_path = os.path.join(directory, "scaling_max_freq")
            if mode != POWER and cfg.quiet_max_freq_khz <= 0:
                continue  # no cap requested: leave the user's own setting alone
            self._remember(limit_path)
            if mode == POWER:
                target = hardware_max
            else:
                # Clamp between what the hardware can actually do: writing a
                # cap below cpuinfo_min_freq is rejected by the kernel anyway,
                # clamping keeps the log clean and the intent intact.
                target = min(hardware_max, cfg.quiet_max_freq_khz)
                if hardware_min is not None:
                    target = max(hardware_min, target)
            write_file(limit_path, target, self.dry_run)

        if os.path.exists(self.no_turbo):
            self._remember(self.no_turbo)
            if mode == POWER:
                turbo_off = 1 if cfg.power_no_turbo else 0
            else:
                turbo_off = 1 if cfg.quiet_no_turbo else 0
            write_file(self.no_turbo, turbo_off, self.dry_run)

        # intel_pstate in active mode ignores scaling_governor, so nudge it
        if os.path.exists(self.min_perf) and governor == "powersave":
            self._remember(self.min_perf)
            write_file(self.min_perf, 100 if mode == POWER else 0, self.dry_run)

    def restore(self):
        for path, value in self._original.items():
            if value is not None:
                write_file(path, value, self.dry_run)
