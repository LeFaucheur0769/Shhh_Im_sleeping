"""The daemon: samples the system and switches between quiet and power modes."""

from __future__ import annotations

import threading

from .common import LOG, POWER, QUIET
from .cpu_tuner import CpuTuner
from .fan_tuner import FanTuner
from .monitor import Monitor

# If the temperature sensors fail this many cycles in a row while the fans
# are under manual control, hand the fans back to the firmware.  Blind
# manual fan control is the one failure mode that can actually cook a
# machine, so we never keep it.
MAX_TEMP_MISSES = 3


class Daemon:
    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        self.monitor = Monitor(cfg)
        self.cpu = CpuTuner(dry_run)
        self.fans = FanTuner(cfg, dry_run)
        self.mode = None
        self.temp_override = False
        self.power_hits = 0
        self.quiet_hits = 0
        self._last_temp = None
        self._stop = threading.Event()

    # -- plumbing ---------------------------------------------------------- #

    def request_stop(self, signum=None, _frame=None):
        if signum is not None:
            LOG.info("received signal %s, shutting down", signum)
        self._stop.set()

    def switch(self, mode, reason):
        if mode == self.mode:
            return
        LOG.info("mode %s -> %s (%s)", self.mode or "startup", mode, reason)
        self.cpu.apply(mode, self.cfg)
        self.mode = mode

    def update_fans(self, sample):
        """Refresh fan duty every cycle so the temperature ramp is live.

        CPU tuning only happens on mode switches, but fans must react to
        temperature continuously even while the mode stays quiet.
        """
        allow_manual = bool(self.monitor.sensors)
        if not allow_manual and self.fans.pwms:
            LOG.debug("no temperature sensors: keeping fans on automatic control")
        if (self.fans._last_target and self.fans._last_target > 0
                and sample["temp"] is None
                and self.monitor.temp_misses >= MAX_TEMP_MISSES):
            LOG.warning("temperature unreadable for %d cycles -- returning fans "
                        "to automatic control", self.monitor.temp_misses)
            allow_manual = False
        self.fans.apply(self.mode or QUIET, self.cfg,
                        temp=sample["temp"], allow_manual=allow_manual)

    # -- decision logic ---------------------------------------------------- #

    def evaluate(self, sample):
        """Return (want_power, reasons, force) for one sample."""
        cfg = self.cfg
        reasons = []

        # Temperature is tracked with hysteresis and always wins.
        if sample["temp"] is not None:
            self._last_temp = sample["temp"]
            if sample["temp"] >= cfg.critical_celsius:
                self.temp_override = True
            elif sample["temp"] <= cfg.exit_celsius:
                self.temp_override = False
        if self.temp_override:
            # sample["temp"] may be None while the override is still latched,
            # so report the last temperature we actually saw.
            shown = sample["temp"] if sample["temp"] is not None else self._last_temp
            if shown is None:
                reasons.append("temperature override (no recent reading)")
            else:
                reasons.append("temperature %.0fC" % shown)

        in_quiet_hours = cfg.in_quiet_hours()
        if in_quiet_hours:
            # During quiet hours only an extreme load may wake the machine up.
            if sample["load1"] >= cfg.hard_load_per_core * self.monitor.cores:
                reasons.append("load %.1f overrides quiet hours" % sample["load1"])
        else:
            if sample["busy"] >= cfg.cpu_busy_percent:
                reasons.append("cpu %.0f%%" % sample["busy"])
            if sample["load1"] >= cfg.load_factor * self.monitor.cores:
                reasons.append("load %.1f" % sample["load1"])
            if sample["procs"]:
                names = sorted(set(sample["procs"]))[:3]
                reasons.append("process " + ", ".join(names))

        force = self.temp_override
        return bool(reasons), reasons, force

    # -- main loop --------------------------------------------------------- #

    def run(self):
        cfg = self.cfg
        need = max(1, int(round(cfg.power_on_seconds / cfg.sample_seconds)))
        release = max(1, int(round(cfg.quiet_after_seconds / cfg.sample_seconds)))

        LOG.info("sampling every %.0fs, %d core(s) detected", cfg.sample_seconds,
                 self.monitor.cores)
        if self.monitor.sensors:
            LOG.info("watching %d temperature sensor(s)", len(self.monitor.sensors))
        else:
            LOG.warning("no temperature sensors found -- overheating protection "
                        "is off and fans stay automatic")

        # Start quiet: the whole point of the service.
        self.cpu.apply(QUIET, cfg)
        self.fans.apply(QUIET, cfg, temp=None)
        self.mode = QUIET

        try:
            while not self._stop.is_set():
                if self._stop.wait(cfg.sample_seconds):
                    break

                sample = {
                    "busy": self.monitor.cpu_busy_percent(),
                    "load1": self.monitor.load1(),
                    "temp": self.monitor.max_temp(),
                    "procs": self.monitor.hot_processes(),
                }
                want_power, reasons, force = self.evaluate(sample)

                if want_power:
                    self.power_hits += 1
                    self.quiet_hits = 0
                else:
                    self.quiet_hits += 1
                    self.power_hits = 0

                LOG.debug("cpu=%.0f%% load=%.2f temp=%s mode=%s hits=%d/%d",
                          sample["busy"], sample["load1"],
                          "n/a" if sample["temp"] is None else "%.0fC" % sample["temp"],
                          self.mode, self.power_hits, need)

                if self.mode != POWER and (force or self.power_hits >= need):
                    self.switch(POWER, "; ".join(reasons) or "sustained load")
                elif self.mode == POWER and self.quiet_hits >= release and not force:
                    self.switch(QUIET, "system idle again")

                self.update_fans(sample)
        finally:
            LOG.info("restoring original CPU/fan settings")
            self.cpu.restore()
            self.fans.restore()
