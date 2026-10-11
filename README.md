# Shhh, I'm sleeping

A small Python daemon for Linux that keeps your machine **quiet when idle** and
switches to **full power when you're actually working**.

It watches CPU utilisation, load average and temperatures, then tunes:

- CPU frequency governors (`powersave` ↔ `performance`)
- CPU frequency caps and Intel Turbo Boost
- Chassis/CPU fan speeds through hwmon PWM files

It never kills or renices processes — it only writes to `/sys`.

> **⚠️ This tool modifies hardware behaviour (fan speeds, CPU power limits).**
> Read the [disclaimers](#disclaimers) before installing, and try
> `python3 main.py --test` and `--dry-run` first.

## How it works

| Signal | Quiet mode | Power mode |
|---|---|---|
| CPU busy % or load average | below thresholds for a while | above thresholds for `power_on_seconds` |
| Temperature | ramps manual fans before the critical threshold | forced by `critical_celsius`, released below `exit_celsius` |
| Quiet hours (optional) | forces quiet mode in the window | — |

Hysteresis prevents flapping: the machine must stay busy for `power_on_seconds`
before switching up, and idle for `power_off_seconds` before switching down.

## Requirements

- Linux with `sysfs` (`/sys/class/hwmon`, `/sys/devices/system/cpu`)
- Python 3.8+ (stdlib only, no dependencies)
- root (the daemon only writes to `/sys`; run it as a systemd service)

## Install

```bash
sudo ./install.sh
```

This copies the code to `/opt/shhh-im-sleeping`, creates
`/etc/shhh-im-sleeping/config.ini` from the example (only on first install),
and enables the `shhh-im-sleeping` systemd service.

Try it first without changing anything:

```bash
python3 main.py --test      # read-only self test: sensors, governors, fans
python3 main.py --dry-run   # run the daemon, log decisions, write nothing
```

## Configuration

See [`config.ini.example`](config.ini.example) for every option with defaults.
The service reads `/etc/shhh-im-sleeping/config.ini`.

Note on temperature sensors: the daemon prefers known CPU chips (`coretemp`,
`k10temp`, ...).  If none is found it falls back to ACPI thermal zones, which on
laptops often track noisy zones (`iwlwifi`, GPU, power adapter) and can report
odd temperatures — set `sensor_names` explicitly if the reported temperature
looks wrong.

Key options:

| Option | Default | Meaning |
|---|---|---|
| `cpu_busy_percent` | 35 | CPU busy % that counts as "working" |
| `load_factor` | 0.75 | load1 per core that also counts as "working" |
| `power_on_seconds` | 15 | how long it must look busy before switching up |
| `critical_celsius` | 85 | fans forced to full speed above this |
| `quiet_fan_pwm` | 76 | fan duty in quiet mode (0 = keep automatic) |
| `power_fan_pwm` | 0 | 0 = hand fans back to the firmware's automatic control |
| `quiet_max_freq_khz` | 0 | 0 = never touch your own frequency cap |
| `quiet_hours` | 23:00-07:00 | optional window that forces quiet mode |

### Docker

The daemon ignores Docker housekeeping processes (`dockerd`, `containerd`,
`docker-proxy`, `containerd-shim`) so container management doesn't flip the
machine into power mode. CPU used **inside** containers still counts as real
load — configure `ignore_patterns` in `[monitor]` if you want different rules.

## Safety design

- **Fans**: manual control is only enabled after the duty is set, and control
  is handed back to the firmware by writing `0` to `pwm_enable` (writing `0` to
  `pwm` in manual mode would *stop* the fan). A `min_fan_pwm` floor prevents
  stalls, a graduated ramp starts at 70 °C, and 85 °C forces full speed.
- **No temperature sensors?** Manual fan control is disabled entirely.
- **Sensor failure**: once temperature reads fail `MAX_TEMP_MISSES` (3) times in
  a row, fans stay on automatic control until the sensors recover — the daemon
  never flips back to blind manual control.
- **Temperature override latch**: if the daemon is holding power mode because of
  a critical temperature and the sensors then go unreadable, it *stays* in power
  mode until a fresh reading drops below `exit_celsius`.  Staying in power mode
  is the safe direction when the thermometer is blind.
- **Your settings are preserved**: original governor/frequency/fan values are
  saved at startup and restored on exit (SIGTERM, crash, or uninstall).
- **Frequency caps**: with `quiet_max_freq_khz = 0` the daemon never writes
  `scaling_max_freq`, so a cap you set yourself is left alone.
- The daemon only ever writes to `/sys`; it never kills processes.
- **Systemd sandbox**: the service runs with `NoNewPrivileges`, `ProtectSystem`,
  `PrivateDevices`, `ProtectKernelModules` and related hardening, so a compromised
  daemon could not load kernel modules or escape to the wider system.
- **Fan paths are validated**: configured fan files must resolve (after symlink
  resolution) to real files under `/sys/`, and auto-discovery never touches
  GPU fan chips (`amdgpu`, `nvidia`, `radeon`, `nouveau`, `i915`, `xe`) that the
  graphics driver already manages.

## Disclaimers

- **Use at your own risk.** Writing to fan and CPU sysfs files changes how your
  hardware behaves. Wrong settings can mean an under-cooled or over-noisy
  machine. The author(s) accept no liability for damage, data loss or lost
  uptime caused by using this software.
- **Your firmware is still the real safety net.** BIOS/UEFI and kernel drivers
  do the actual thermal emergency management (forced full fans, throttling,
  shutdown). This daemon is a comfort tuner, not a cooling system — do **not**
  disable `fancontrol`, `thermald` or vendor thermal services to "make room"
  for it unless you know exactly what you are doing.
- **Test before you run it as a service.** `python3 main.py --test` shows what
  sensors, governors and fans the daemon sees; `--dry-run` makes it take every
  decision without writing anything. Watch a full quiet/power cycle before
  trusting it unattended.
- **Fan control is machine-specific.** Many laptops expose no writable PWM fans
  at all (the daemon then disables fan tuning by itself). Where fans *are*
  writable, a too-low `min_fan_pwm` or a wrong `quiet_fan_pwm` can make the
  machine hot — start with `quiet_fan_pwm = 0` (keep automatic control).
- **Conflicts with other tuners.** Do not run this alongside `fancontrol`,
  `auto-cpufreq`, TLP, `powertop --auto-tune` or vendor tools that write the
  same sysfs files; they will fight each other. Pick one tuner per file.
- **A hard kill leaves settings in place.** The daemon restores your original
  settings on SIGTERM, SIGINT and crashes — but `kill -9` (SIGKILL) or a power
  loss cannot be handled: your governors/fans stay as last written until the
  daemon runs again or you reboot.
- **Quiet mode reduces performance.** Expect slower builds, compiles and
  latency-sensitive workloads until the load thresholds are crossed. Not
  recommended on servers or workstations with deterministic performance needs.
- **The config file is trusted input.** The daemon runs as root and obeys
  `/etc/shhh-im-sleeping/config.ini`; keep it root-owned and not
  group/world-writable (the daemon warns if it is not).
- **Not a security tool.** It reads `/proc` and `/sys` for load and temperature
  only; it does not sandbox, monitor or protect your processes.

## Uninstall

```bash
sudo systemctl disable --now shhh-im-sleeping
sudo rm /etc/systemd/system/shhh-im-sleeping.service
sudo rm -rf /opt/shhh-im-sleeping
```

The service restores your original CPU/fan settings when it stops.

## Tests

```bash
python3 -m unittest discover -s tests   # unit tests (no sysfs needed)
python3 main.py --test                  # live read-only self test
```

## License

MIT — see [LICENSE](LICENSE).
