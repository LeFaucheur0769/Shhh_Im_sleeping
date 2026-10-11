"""Command line interface: logging setup, self test, and the main entry point."""

from __future__ import annotations

import argparse
import configparser
import logging
import os
import signal
import sys
import time

from .common import LOG, POWER, QUIET
from .config import Config
from .cpu_tuner import CpuTuner
from .daemon import Daemon
from .fan_tuner import FanTuner
from .monitor import Monitor


def run_selftest(cfg, dry_run):
    """Print a one-shot status report without touching anything."""
    monitor = Monitor(cfg)
    cpu = CpuTuner(dry_run)
    fans = FanTuner(cfg, dry_run)

    monitor.cpu_busy_percent()          # prime the delta
    time.sleep(min(2.0, cfg.sample_seconds))
    busy = monitor.cpu_busy_percent()

    print("Shhh, I'm sleeping -- self test")
    print("  cores               : %d" % monitor.cores)
    print("  sample interval     : %.0fs" % cfg.sample_seconds)
    print("  cpu busy            : %.1f%%" % busy)
    print("  load average (1m)   : %.2f" % monitor.load1())
    temp = monitor.max_temp()
    print("  max temperature     : %s" % ("n/a" if temp is None else "%.1f C" % temp))
    print("  sensors             : %d" % len(monitor.sensors))
    print("  governors available : %s"
          % (", ".join(sorted(cpu.available_governors)) or "none"))
    print("  controllable fans   : %s" % (", ".join(fans.pwms) or "none"))
    procs = monitor.hot_processes()
    print("  heavy processes     : %s" % (", ".join(sorted(set(procs))) or "none"))
    print("  quiet hours now     : %s" % ("yes" if cfg.in_quiet_hours() else "no"))
    print("  would switch to     : %s"
          % (POWER if (temp is not None and temp >= cfg.critical_celsius) else QUIET))
    return 0


def setup_logging(verbose=False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Adaptive quiet/power mode daemon (Shhh, I'm sleeping).",
    )
    parser.add_argument("-c", "--config", metavar="PATH",
                        help="path to the INI configuration file")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="log what would be done without touching sysfs")
    parser.add_argument("-t", "--test", "--status", dest="test", action="store_true",
                        help="print a single status report and exit")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="enable debug logging")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    try:
        cfg = Config.load(args.config)
    except (configparser.Error, ValueError) as exc:
        LOG.error("configuration error: %s", exc)
        return 2

    if os.geteuid() != 0 and not args.test:
        LOG.warning("not running as root -- sysfs writes will probably fail")

    if args.test:
        return run_selftest(cfg, args.dry_run)

    daemon = Daemon(cfg, dry_run=args.dry_run)
    signal.signal(signal.SIGTERM, daemon.request_stop)
    signal.signal(signal.SIGINT, daemon.request_stop)

    try:
        daemon.run()
    except Exception:  # noqa: BLE001 - last-resort cleanup
        LOG.exception("unhandled error, restoring settings")
        daemon.cpu.restore()
        daemon.fans.restore()
        return 1
    return 0
