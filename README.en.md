[中文版本](README.md)

# Raspberry Pi Zero W + OV5647 CSI Camera Surveillance

A lightweight surveillance service for **Raspberry Pi Zero W / Raspberry Pi OS Lite 32-bit (Debian Trixie, headless)**, built on the modern **libcamera / picamera2** stack with hardware H.264 encoding.

## Features

- **CSI OV5647 capture**: main channel records video, a low-resolution channel runs motion detection; hardware H.264 encoded MP4.
- **Motion marking**: frame-difference + contour detection draws a motion box and timestamp on the event snapshot JPEG; the video body stays clean (Zero W performance).
- **Two recording modes** (`recorder.mode`):
  - `motion`: segment recording triggered by motion, stops `post_motion_seconds` after motion ceases;
  - `continuous`: 7×24 continuous recording, cut every `segment_seconds`.
- **SMB backup upload**: background thread + persistent queue, auto-resumes after network loss/reboot; videos and snapshots go to date-based folders.
- **Low-battery email**: pluggable backend (command / MCP3008 divider / INA219), tiered low/critical voltage alerts with cooldown to avoid spam.
- **Built-in web panel**: Basic Auth login, MJPEG live preview, runtime status, tiered hot-reload config (safe items apply instantly; structural items prompt a restart).
- **Boot autostart**: systemd unit `Restart=always`, auto-restarts on crash; install script enables it in one step.

## Directory layout

```
rpi-zero-surveillance/
├── config.yaml            # live config (web save rewrites it wholesale, no comments)
├── config.example.yaml    # commented reference template, read-only guide/copy source
├── requirements.txt
├── install.sh             # one-shot dependency installer for the Pi
├── surveillance.service    # systemd boot-autostart unit
├── README.md
└── monitor/
    ├── main.py            # main loop orchestration
    ├── camera.py         # picamera2 wrapper / hardware recording
    ├── motion.py         # frame-diff detection + snapshot annotation
    ├── recorder.py       # recording state machine / snapshots / local cleanup
    ├── smb_uploader.py   # SMB queued upload
    ├── battery.py        # battery monitoring backends
    ├── mailer.py         # SMTP sending
    └── config.py         # config loading
```

## Install (on the Raspberry Pi)

```bash
# copy the whole rpi-zero-surveillance folder to the Pi, e.g. /home/pi/
cd /home/pi/rpi-zero-surveillance
chmod +x install.sh
./install.sh
```

Wiring: align the OV5647 ribbon to the Zero W CSI port (metal face toward the PCB back / HDMI side, follow the board silkscreen). If `libcamera` cannot find the camera, append `dtoverlay=ov5647` to `/boot/firmware/config.txt` and reboot.

## Configuration (must-edit items)

Meaning, ranges and tuning are in `config.example.yaml` (commented template; the program does not read it). Edit `config.yaml` directly or use the web form.

1. **SMB**: `smb.host / share / username / remote_dir`; put the password in the `SMB_PASSWORD` environment variable.
2. **Mail**: `mail.smtp_host / port / username / recipients`; use `use_ssl: true` for 465, `use_starttls: true` for 587; put the password in `MAIL_PASSWORD`.
3. **Battery**: pick `battery.backend` = `command / mcp3008 / ina219` and fill thresholds/parameters.
4. **Recording mode**: `recorder.mode: motion` or `continuous`.

> The web “Save config” rewrites `config.yaml` wholesale (comments are not preserved), which is why all comments live in `config.example.yaml`.

Put secrets in `/etc/surveillance.env` (`chmod 600`):

```bash
MAIL_PASSWORD="your-mail-authorization-code"
SMB_PASSWORD="smb-password"
WEB_PASSWORD="web-login-password"
```

## Trial run

```bash
export MAIL_PASSWORD=xxx SMB_PASSWORD=xxx WEB_PASSWORD=xxx
python3 -m monitor.main --config config.yaml
```

Seeing `监控已启动 / surveillance started` means it is healthy; on motion, videos appear in `recordings/` and boxed snapshots in `snapshots/`.

File naming:
- Video: `Video{start YYYYMMDD-HHMMSS}.mp4`, e.g. `Video20261023-140644.mp4`
- Snapshot: `{YYYYMMDD-HHMMSS}_{ms}_snap.jpg`, e.g. `20261023-140645_066_snap.jpg`

## Local dev simulation (preview the web UI without a Pi)

On a PC, a synthetic moving block replaces the camera to check preview/detection/status/config; no real recording, no SMB upload, no battery read:

```bash
python3 -m monitor.main --dev --config config.yaml
# open http://127.0.0.1:8080, user admin, password admin (dev default)
```

Dev mode disables SMB upload and battery monitoring automatically; it is UI demo only. Run without `--dev` on the Pi for real capture/recording.

## Web panel

After start, open `http://<pi-ip>:8080` and log in with `web.username` (default admin) and the `WEB_PASSWORD` environment variable.

- **Preview**: MJPEG live stream (`/stream`); resolution/framerate/quality adjust instantly in the “preview stream” section.
- **Status**: live pill shows recording/idle, mode, battery voltage, SMB pending queue, mail on/off, uptime (refreshes every 2s).
- **Config**: grouped form (motion / recording / battery / mail / SMB / preview).
  - **Hot-reload (instant)**: sensitivity, area, confirm frames, post-motion seconds, segment length, local retention, battery thresholds/poll/cooldown, SMTP account/recipients, SMB retry/snapshot toggle, preview params.
  - **Requires restart**: camera resolution/framerate/bitrate, recording mode, SMB host/share/account/dir, battery backend, web port — saved to file, apply with `sudo systemctl restart surveillance`; the page also has a “restart service” button.
- **Save & backup**: “Save config” writes the whole config back to `config.yaml`; before writing it backs up the current file as `config-bak-YYYYMMDD-HHMMSS_ms.yaml`, keeping only the newest 3 and deleting older ones.

> The web button runs `sudo -n systemctl restart surveillance`; passwordless sudo is required:
> `sudo visudo -f /etc/sudoers.d/surveillance` and write
> `pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart surveillance`
> Without it, the button falls back to a process self-restart (equivalent, bypassing systemd).

## Boot autostart (systemd)

Step 6 of `install.sh` already installs and enables it manually:

```bash
sudo cp surveillance.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now surveillance
journalctl -u surveillance -f      # follow logs
```

The service uses `Restart=always` and comes back after crash/power loss; after editing `config.yaml`, run `sudo systemctl restart surveillance`.

Logs go to `logs/surveillance.log` with daily rotation: a new file at midnight (old files carry the `surveillance.log.YYYYMMDD` suffix), keeping the last 14 days.

## Battery backends

| backend | use case | key params |
|---|---|---|
| `command` (default) | a script/driver already outputs voltage | `battery.command` prints one float voltage (V) |
| `mcp3008` | SPI ADC + voltage divider | convert `divider_ratio` from your divider circuit |
| `ina219` | I2C current/voltage sensor / UPS HAT | `i2c_bus / address` |

Convert thresholds to your cell count: single Li-ion ~3.5V warning, 3.3V critical; multiply by series count for multi-cell. To add hardware, implement `read_voltage()` in `battery.py`.

## Tuning tips (false positives / missed targets)

- False alarms from light/shadow/swaying leaves: raise `motion.sensitivity`, increase `min_area`, raise `confirm_frames`.
- Small targets missed: lower `sensitivity`, reduce `min_area`.
- Zero W lag: lower `camera.main_width/height` or `fps`, shrink `lores`.

## FAQ

- **`Failed to find camera`**: check ribbon, camera enabled in `raspi-config`, add `dtoverlay=ov5647` if needed.
- **SMB cannot connect**: confirm guest/account access, firewall port 445, `remote_dir` path; the queue lives in `upload_queue.json` and resumes automatically after network returns.
- **Mail not sent**: most providers require an “authorization code” instead of the login password; match SSL/STARTTLS to the port.
