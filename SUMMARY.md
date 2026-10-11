# Work Summary

Summary of everything done to turn the original monolithic `main.py` into a safe,
modular, testable systemd daemon.

## 1. Goal

A Python daemon that keeps the machine **quiet when idle** and switches to
**full power under load**, running as a systemd service — without ever harming
the computer (no killed processes, no stalled fans, no overwritten user settings).

## 2. Code review of the original `main.py`

The original 683-line script was reviewed and **6 safety issues** were found:

1. **Fan stop risk**: the old code wrote `0` to `pwmN` while in manual mode —
   per the hwmon ABI that *stops* the fan instead of returning it to automatic.
2. **Crash on missing temperature**: `evaluate()` crashed when `temp_override`
   was configured but no temperature sensor sample existed (`None` comparison).
3. **Manual fan control without sensors**: fans could be forced into manual
   mode even when no temperature sensor was available to react to overheating.
4. **No graduated fan ramp**: fans jumped straight from idle duty to full speed
   at the critical threshold, with no intermediate ramp and no stall floor.
5. **Docker noise**: `dockerd`/`containerd` housekeeping CPU flipped the machine
   into power mode for no reason.
6. **Frequency cap overwrite**: quiet mode wrote the hardware max frequency to
   `scaling_max_freq`, destroying any cap the user had set themselves.

## 3. Split into a package

`main.py` became a thin entry point; the logic lives in `shhh_im_sleeping/`:

| File | Responsibility |
|---|---|
| `common.py` | shared helpers: logging, sysfs read/write, `parse_hours()`, mode constants |
| `config.py` | config loading (`/etc/shhh-im-sleeping/config.ini` first), validation, clamping, quiet-hours logic |
| `monitor.py` | CPU busy %, load1, temperature sensor discovery, hot-process listing with Docker ignore filters |
| `cpu_tuner.py` | governor switching, frequency caps, turbo boost; saves and restores originals |
| `fan_tuner.py` | hwmon fan control: `target_pwm()` ramp logic + ABI-safe `FanTuner` |
| `daemon.py` | main loop: sampling, hysteresis, quiet hours, temperature-miss handling, restore on exit |
| `cli.py` | argparse CLI (`-c`, `-n`, `-t`, `--status`, `-v`), selftest, signal handling, exit codes |
| `main.py` | thin entry point calling `cli.main()` |

## 4. Safety fixes implemented

### Fans (`fan_tuner.py`)
- Duty is **set before** enabling manual mode (`pwmN` written, then `pwmN_enable=1`).
- Returning to automatic writes `0` to **`pwmN_enable`** (not `pwmN`), so the fan
  can never be stopped by the daemon.
- `min_fan_pwm` floor (~50) prevents manual fans from stalling.
- Graduated ramp: manual fans ramp to `fan_ramp_pwm=180` from `fan_ramp_celsius=70`
  in **both** modes, **before** the critical override (duty 255 at
  `critical_celsius=85`). Fans left on automatic (`pwm = 0`) are never touched,
  even at critical temperature.
- Manual control is **disabled entirely** when no controllable PWM fans exist or
  no temperature sensors are available.
- After **3 consecutive failed temperature reads** (`MAX_TEMP_MISSES = 3`), fans
  stay on automatic control until the sensors recover — the daemon never flips
  back to blind manual control.
- Only files matching `^pwm\d+$` are accepted; only fans with live
  `fanN_input` RPM > 0 are claimed.
- Sysfs writes are skipped when the target duty is unchanged (`_last_target`).
- Fans are re-evaluated **every sampling cycle** so the temperature ramp is live.

### CPU (`cpu_tuner.py`)
- With `quiet_max_freq_khz = 0` the daemon **never touches `scaling_max_freq`** —
  a user-set cap is left alone (this was the last bug found, via dry-run testing).
- Configured caps are clamped between `cpuinfo_min_freq` and `cpuinfo_max_freq`.
- Original governor/frequency values are remembered at startup and restored on exit.
- CPU tuning only happens on mode switches (no repeated sysfs writes).

### Daemon (`daemon.py`)
- Hysteresis: busy for `power_on_seconds` before switching up, idle for
  `power_off_seconds` before switching down (no flapping).
- `_last_temp` tracking fixes the crash when temperature is unavailable.
- Restore of original settings runs on SIGTERM, SIGINT, and on crashes.
- The daemon **only writes to `/sys`** — it never kills or renices processes.

### Monitor (`monitor.py`)
- Default `ignore_patterns` for `dockerd`, `containerd`, `docker-proxy`,
  `containerd-shim.*` so Docker housekeeping does not trigger power mode.
- CPU used **inside** containers still counts as real load (intended behavior).

### External code review fixes

A follow-up review of the split code found four issues, all fixed:

1. **Fan-control oscillation** (`daemon.update_fans`): the old guard consulted
   the previous cycle's target, so after `MAX_TEMP_MISSES` tripped, the daemon
   alternated between automatic and *blind manual* control every cycle.  The
   decision is now based only on `sensors_ok` and `temp_misses >=
   MAX_TEMP_MISSES`; once blinded, fans stay automatic until sensors recover
   (with a one-shot warning log).
2. **Startup fan mode** (`daemon.run`): the initial `fans.apply(QUIET, ...)`
   now passes `allow_manual=bool(self.monitor.sensors)` instead of defaulting
   to manual.
3. **systemd ordering cycle**: `After=multi-user.target` combined with
   `WantedBy=multi-user.target` (which implicitly orders the target *after* the
   service) is a cycle systemd would break with a warning on every boot; the
   explicit `After=` line was removed.
4. **`target_pwm()` restructure**: the critical override (255) and the
   temperature ramp now apply to manual targets in **both** modes; targets of
   `0` return automatic before any floor or override can turn them manual.

Plus polish: dead `read_float()` removed from `common.py`, `main.py` installed
as `0644` (it is not executed directly), and README/config docs updated for the
temperature-override latch (stays POWER if sensors die — the safe direction)
and the noisy ACPI thermal-zone fallback (set `sensor_names` on laptops).

### Security audit (hardware hazards + code security)

A dedicated security pass over the whole codebase:

**Hardware hazards**

- **Fan path validation**: explicitly configured fan files must resolve (after
  symlink resolution with `os.path.realpath`) to real files under `/sys/` — a
  config or symlink trick can no longer point the root daemon at an arbitrary
  file.
- **GPU chips excluded from auto-discovery**: hwmon chips whose `name` is
  `amdgpu`, `nvidia`, `radeon`, `nouveau`, `i915` or `xe` are skipped, so the
  daemon never fights the graphics driver over GPU fan control.
- **Temperature thresholds clamped** in `_sanitise()`: `critical_celsius` to
  [40, 110], `exit_celsius` to [30, 110] (and `exit >= critical` pulls it to
  `critical - 10`), `fan_ramp_celsius >= 30`. A bad config can no longer
  produce a daemon that never ramps fans or ramps constantly.

**Code security**

- Grep audit for `eval`, `exec`, `subprocess`, `os.system`, `shell=True`,
  `__import__`, `pickle`, `input(` — **no matches**: the daemon contains no
  dangerous primitives, it only reads `/proc` + `/sys` and writes sysfs
  attributes.
- **Config-file trust**: the daemon runs as root and obeys its config file, so
  `Config.load()` now warns when that file is not root-owned or is
  group/world-writable.

**Systemd hardening** (every directive chosen to stay compatible with the
daemon's job of reading `/proc` and writing `/sys`):

- Added: `NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`,
  `ProtectSystem=strict`, `ProtectHome`, `ProtectKernelModules`,
  `ProtectKernelLogs`, `ProtectControlGroups`,
  `RestrictAddressFamilies=AF_UNIX`, `RestrictNamespaces`, `RestrictSUIDSGID`,
  `LockPersonality`, `SystemCallArchitectures=native`,
  `Environment=PYTHONDONTWRITEBYTECODE=1` (the install dir becomes read-only).
- Deliberately **not** used: `ProtectKernelTunables` (mounts `/sys` read-only,
  the daemon could not work) and `ProcSubset=pid` (hides the `/proc` entries
  the load monitor scans).
- Validated by running the full selftest under `systemd-run --user` with the
  complete hardening property set: `status=0/SUCCESS`.

**Documentation**: a `Disclaimers` section was added to the README covering
hardware-modification risk, firmware as the real thermal safety net, conflicts
with `fancontrol`/`auto-cpufreq`/TLP, SIGKILL leaving settings in place,
performance impact of quiet mode, and config-file trust.

### Live hardware fix: `-EBUSY` on Super I/O fan chips (v1.0.1)

After deploying on the target machine (i5-13600KF, hwmon10 ≈ `nct6775`/`it87`
family), journald showed `cannot write /sys/class/hwmon/hwmon10/pwm6:
[Errno 16] Device or resource busy`. Root cause: the kernel hwmon ABI says
writing `pwmN` while `pwmN_enable == 0` **may return -EBUSY**, and these
Super I/O chips do exactly that — so the "duty first, then enable" order was
refused every cycle.

Fix in `FanTuner._go_manual()`:

- Try duty-then-enable first (safe for drivers that allow it, and it keeps a
  manual fan from ever sitting at duty 0).
- On refusal, fall back to enable-first, then retry the duty; the fan keeps
  its previous (validated non-zero) duty in the meantime.
- If both orders fail, the fan is **dropped**: control is handed back to the
  firmware (enable restored to `0` only if that was its original state), the
  fan is removed from the control set with a one-time warning, and its
  original values are no longer restored on exit.
- `write_file()` gained a `log=True` parameter so the expected first-attempt
  refusal logs at debug level instead of producing a false warning.

Three new tests emulate the `-EBUSY` ABI with a fake hwmon tree: fallback
order, already-manual fans keeping duty-then-enable order, and uncontrollable
fans being dropped with firmware control left intact.

## 5. Testing

- `tests/test_units.py` — **28 unit tests** (27 pass, 1 skipped on machines
  without writable PWM fans):
  - `parse_hours()` (valid, empty/"off", invalid, out-of-range)
  - config defaults, sanitising/clamping, quiet-hours midnight wrap
  - `target_pwm()`: idle duty, ramp, floor, critical override, automatic (0),
    power-mode manual duty, power-mode ramp, critical override in power mode,
    automatic stays automatic at critical
  - security tests: temperature thresholds clamped, world-writable config
    triggers the ownership warning, `TestFanPathSafety` (explicit path outside
    `/sys/` rejected, pwm-named symlink escaping `/sys` rejected, real-sysfs
    path accepted when available, GPU-managed chips skipped by auto-discovery
    via a fake hwmon tree)
  - `-EBUSY` fallback tests (`TestFanEbusyFallback`): enable-first retry on
    chips that refuse duty writes in automatic mode, already-manual fans keep
    duty-then-enable order, uncontrollable fans are dropped and left automatic
- Two bugs were found **by the tests** and fixed:
  - `target_pwm()` applied the `min_fan_pwm` floor to the power-mode target `0`,
    turning "automatic" into manual duty 50.
  - Tests corrected to match real `parse_hours()` return semantics.
- Live smoke tests on the real machine (16 cores, 2 temp sensors, no controllable
  PWM fans, user cap 3301000 kHz vs hardware max 4683471 kHz):
  - `python3 main.py --test` — read-only selftest, fan tuning auto-disabled.
  - `python3 main.py --dry-run` — full daemon loop + SIGTERM restore verified;
    after the fix, **zero** writes to `scaling_max_freq`.

## 6. Deployment artifacts

- `config.ini.example` — every option documented with defaults.
- `shhh-im-sleeping.service` — systemd unit (`Restart=on-failure`, runs at boot).
- `install.sh` — installs to `/opt/shhh-im-sleeping`, config to
  `/etc/shhh-im-sleeping/config.ini`, enables + restarts the service.
- `README.md` — full rewrite: usage, config reference, Docker notes, safety
  design, uninstall instructions (placeholder text removed).

## 7. Git history

| Commit | Contents |
|---|---|
| `fbd38f3` | Initial commit (`.gitignore`, `LICENSE`, placeholder README) |
| `4f65243` | Package split, all safety fixes, tests, deployment files, README |
| `4be7393` | Fixes from the external code review (fan oscillation, startup fan mode, systemd ordering, `target_pwm()` restructure) |
| `69f9c36` | Security audit: fan path validation, GPU-chip exclusion, threshold clamping, config-ownership warning, systemd hardening, security tests, README disclaimers |
| `1633d58` | Live-hardware fix (v1.0.1): `-EBUSY` fallback on Super I/O fan chips, fan drop-out on uncontrollable fans, `write_file(log=False)`, 3 new tests |

## 8. Defaults preserved from the original

`cpu_busy_percent=35`, `load_factor=0.75`, `power_on_seconds=15`,
`critical_celsius=85`, `exit_celsius=75`, `quiet_fan_pwm=76`,
`power_fan_pwm=0`, `fan_ramp_celsius=70`, `fan_ramp_pwm=180`.
