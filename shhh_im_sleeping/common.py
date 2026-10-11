"""Shared constants and small sysfs/proc helpers."""

from __future__ import annotations

import logging
import re
from datetime import time as dtime

LOG = logging.getLogger("shhh-im-sleeping")

QUIET = "quiet"
POWER = "power"

PROC_STAT = "/proc/stat"
PROC_LOADAVG = "/proc/loadavg"
CPU_ROOT = "/sys/devices/system/cpu"
HWMON_ROOT = "/sys/class/hwmon"
THERMAL_ROOT = "/sys/class/thermal"


def read_file(path):
    """Return the stripped contents of *path*, or None if unreadable."""
    try:
        with open(path, "r") as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return None


def read_int(path, default=None):
    raw = read_file(path)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def read_float(path, default=None):
    raw = read_file(path)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def write_file(path, value, dry_run=False):
    """Write *value* to a (usually sysfs) file.  Never raises."""
    if dry_run:
        LOG.info("[dry-run] %s <- %s", path, value)
        return True
    try:
        with open(path, "w") as fh:
            fh.write(str(value))
        return True
    except OSError as exc:
        LOG.warning("cannot write %s: %s", path, exc)
        return False


def parse_hours(text):
    """'23:00-07:00' -> (time(23, 0), time(7, 0)); None when disabled."""
    text = (text or "").strip()
    if not text or text.lower() in {"none", "off", "disabled", "false"}:
        return None
    match = re.match(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$", text)
    if not match:
        raise ValueError("invalid quiet_hours %r (expected HH:MM-HH:MM)" % text)
    h1, m1, h2, m2 = (int(part) for part in match.groups())
    return dtime(h1, m1), dtime(h2, m2)
