"""Controls chassis/CPU fans through hwmon PWM files.

hwmon sysfs ABI notes (the reason for the careful ordering here):

* ``pwmN_enable`` 0 = automatic (BIOS/driver) control, 1 = manual.
* ``pwmN`` is only meaningful in manual mode; writing 0 to it in manual
  mode *stops the fan*, it does NOT hand control back.  Control returns to
  the firmware by writing 0 to ``pwmN_enable``.
* Therefore: to go manual, set the duty first, then enable.  To go back to
  automatic, just clear the enable flag.
"""

from __future__ import annotations

import glob
import os
import re

from .common import HWMON_ROOT, LOG, POWER, QUIET, read_file, read_int, write_file

PWM_NAME_RE = re.compile(r"^pwm\d+$")


def target_pwm(mode, temp, cfg):
    """Fan duty for one cycle.  0 means 'hand control back to automatic'."""
    target = cfg.power_fan_pwm if mode == POWER else cfg.quiet_fan_pwm
    if target <= 0:
        # Automatic: the firmware handles temperature itself, including
        # critical temperatures, so never turn this into a manual duty.
        return 0
    if temp is not None:
        if temp >= cfg.critical_celsius:
            return 255  # critical override applies in both modes
        if temp >= cfg.fan_ramp_celsius:
            target = max(target, cfg.fan_ramp_pwm)
    return max(cfg.min_fan_pwm, min(255, int(target)))


class FanTuner:
    """Controls chassis/CPU fans through hwmon PWM files."""

    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        self.dry_run = dry_run
        self._original_pwm = {}     # pwm path   -> duty before we touched it
        self._original_enable = {}  # enable path -> enable flag before touch
        self._last_target = None    # avoid rewriting sysfs every cycle
        self.pwms = self._resolve_paths(cfg)
        if self.pwms:
            LOG.info("controlling %d fan(s): %s", len(self.pwms), ", ".join(self.pwms))
        else:
            LOG.info("no controllable PWM fans found -- fan tuning disabled")

    # -- discovery ---------------------------------------------------------- #

    @staticmethod
    def _resolve_paths(cfg):
        if cfg.fan_paths.strip().lower() != "auto":
            paths = []
            for raw in cfg.fan_paths.split(","):
                path = raw.strip()
                if not path:
                    continue
                if not PWM_NAME_RE.match(os.path.basename(path)):
                    LOG.warning("ignoring fan path %r: not a pwmN file", path)
                    continue
                if not os.path.isfile(path):
                    LOG.warning("ignoring fan path %r: does not exist", path)
                    continue
                paths.append(path)
            return paths

        found = []
        for hwmon in sorted(glob.glob(os.path.join(HWMON_ROOT, "hwmon*"))):
            for pwm in sorted(glob.glob(os.path.join(hwmon, "pwm*"))):
                base = os.path.basename(pwm)
                # pwm1_channel, pwm1_label etc. are metadata, not controls.
                if not PWM_NAME_RE.match(base):
                    continue
                fan_input = os.path.join(hwmon, base.replace("pwm", "fan") + "_input")
                rpm = read_int(fan_input, 0) or 0
                if rpm > 0:  # only claim fans that are actually spinning
                    found.append(pwm)
        return found

    # -- actuation ----------------------------------------------------------- #

    def apply(self, mode, cfg, temp=None, allow_manual=True):
        """Set fans for *mode*, ramping with *temp*.

        ``allow_manual=False`` (no temperature sensors available) forces
        automatic control: guessing fan speeds blind is how fans get stalled.
        """
        if not self.pwms:
            return
        target = target_pwm(mode, temp, cfg)
        if not allow_manual and target > 0:
            target = 0
        if target == self._last_target:
            return  # nothing changed, don't hammer sysfs
        self._last_target = target

        for pwm in self.pwms:
            enable_path = pwm + "_enable"
            self._original_pwm.setdefault(pwm, read_file(pwm))
            self._original_enable.setdefault(enable_path, read_file(enable_path))

            if target == 0:
                # Back to firmware control: clear the enable flag (NOT pwm=0,
                # which in manual mode stops the fan).
                write_file(enable_path, 0, self.dry_run)
            else:
                # Duty first, then switch to manual: never leave a manual
                # fan with a 0 duty, even for an instant.
                write_file(pwm, target, self.dry_run)
                write_file(enable_path, 1, self.dry_run)

    def restore(self):
        """Put every fan exactly back the way it was found."""
        for pwm, value in self._original_pwm.items():
            if value is not None:
                write_file(pwm, value, self.dry_run)
        for enable_path, value in self._original_enable.items():
            if value is not None:
                write_file(enable_path, value, self.dry_run)
