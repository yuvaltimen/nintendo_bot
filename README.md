# Switch Control

A Mac-driven command-and-control system for a Nintendo Switch. The Raspberry Pi runs a long-lived daemon that holds a virtual Pro Controller's Bluetooth connection and exposes an HTTP API; your Mac sends commands to it.

The end goal: plug an HDMI capture card into the Mac, run YOLO + LLM on the captured frames, and have the LLM call the same HTTP API to play prompted scenarios. The architecture scales into that shape — the LLM is just another client of the daemon.

---

## SHORTCUT:

On the Switch:

`Go to Controller Pairing screen`

On the Pi:
```
sudo PYTHONUNBUFFERED=1 /home/yuvaltimen/nxbt/.venv/bin/python scripts/pi_daemon.py
```

From the Mac:
```python
python scripts/interactive.py

# To connect and exit:
pad.press(Buttons.A)  # x2

# Or 

python scripts/botw_macros.py horizontal_scan
```



---

## Resume a session

Pi already set up and paired at least once? This is all you need:

```bash
# 1. Start (or confirm) the daemon on the Pi
ssh pi "sudo systemctl start switch-control"

# 2. Wake the Switch — press any button on your joycons

# 3. Open the interactive REPL on your Mac
python scripts/interactive.py

# 4. Or run a BotW macro directly
python scripts/botw_macros.py --list
python scripts/botw_macros.py casual_stroll
```

The daemon reconnects to the Switch within ~10 seconds of it waking. If `systemctl start` says the unit is already running, that's fine — it's a no-op.

> **Something broken?** → [Troubleshooting](#troubleshooting) below, or the deep BT diagnosis tree in `PI_CONTROLLER.md`.

---

## Architecture

```
Mac (you / scripts / future LLM)
   ↓ HTTP (port 8765)
Raspberry Pi
   ├─ switch-control daemon (uvicorn + FastAPI)
   ├─ nxbt → BlueZ → virtual Pro Controller
   └─ bluetoothctl pairing agent (started by the daemon)
   ↓ Bluetooth
Nintendo Switch  ←  (your real joycons paired in parallel)
```

The daemon keeps the BT pad connected across many script runs. It heartbeats every 5 seconds and auto-reconnects on drop. Mac scripts are stateless clients — start, send commands, exit; the BT link survives.

---

## One-time Pi setup

Do this once on a fresh Pi. If the daemon is already running successfully, skip this entire section.

> For full step-by-step detail and debugging at every step, see `PI_CONTROLLER.md`. This section gives the commands only.

### 1. Flash the Pi

Use Raspberry Pi Imager → Raspberry Pi OS 64-bit (Bookworm). In advanced options: hostname (`pi`), SSH enabled, Wi-Fi configured, username `yuvaltimen`.

```bash
ssh yuvaltimen@raspberrypi.local
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

### 2. Disable the BlueZ `input` plugin

Without this, nxbt throws `Address already in use` on PSM 17 or 19.

```bash
# Check your bluetoothd path:
systemctl cat bluetooth | grep ExecStart

sudo mkdir -p /etc/systemd/system/bluetooth.service.d
sudo tee /etc/systemd/system/bluetooth.service.d/override.conf > /dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd --noplugin=input
EOF

sudo systemctl daemon-reload && sudo systemctl restart bluetooth

# Verify:
systemctl cat bluetooth | grep ExecStart    # must show --noplugin=input
rfkill list bluetooth                       # Soft blocked: no
```

Substitute the exact `bluetoothd` path from `systemctl cat` if yours differs from `/usr/libexec/bluetooth/bluetoothd`.

### 3. Install Python 3.11.9 via pyenv

`dbus-python` (an nxbt dependency) fails to build on Python 3.12+.

```bash
sudo apt install -y \
  build-essential pkg-config git curl xz-utils bluetooth bluez \
  libbluetooth-dev libdbus-1-dev libglib2.0-dev libssl-dev zlib1g-dev \
  libbz2-dev libreadline-dev libsqlite3-dev libffi-dev liblzma-dev \
  libncursesw5-dev tk-dev libxml2-dev libxmlsec1-dev

curl https://pyenv.run | bash
# Follow the installer's instructions to add pyenv to ~/.bashrc, then:
source ~/.bashrc
pyenv install 3.11.9    # ~20 minutes on Pi 4
```

### 4. Install nxbt and daemon dependencies

```bash
mkdir -p ~/nxbt && cd ~/nxbt
pyenv local 3.11.9
python -m venv .venv
source .venv/bin/activate
pip install -U pip setuptools wheel
pip install nxbt
sudo /home/yuvaltimen/nxbt/.venv/bin/pip install fastapi uvicorn pydantic
```

### 5. Sync this repo to the Pi

```bash
# From your Mac:
rsync -av --delete --exclude='__pycache__' --exclude='.venv' --exclude='.git' \
  /Users/yuvaltimen/Coding/nintendo/ pi:~/Coding/nintendo/
```

Re-run this any time you change daemon code (`switch_control/daemon.py` or `switch_control/pad.py`). Script-only changes don't need a re-sync — they run on the Mac.

### 6. First-time pair

Put the Switch on `Controllers → Change Grip/Order`. Start the daemon manually so you can watch the logs:

```bash
ssh pi
sudo PYTHONUNBUFFERED=1 /home/yuvaltimen/nxbt/.venv/bin/python ~/Coding/nintendo/scripts/pi_daemon.py
```

Watch for `[INFO] daemon: connected`. The Switch shows a new Pro Controller on the Change Grip/Order screen. After the first successful pair the Switch remembers the Pi's MAC forever — reconnects are automatic from here on.

> **Pairing fails?** → `PI_CONTROLLER.md` §Wipe stale state + §Authentication Failure for the full diagnosis tree.

### 7. Enable autostart (optional but recommended)

```bash
sudo cp ~/Coding/nintendo/systemd/switch-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now switch-control
sudo journalctl -u switch-control -f      # confirm it started
```

With this, powering on the Pi is enough — the daemon starts, reconnects to the Switch, and the API is live on port 8765.

---

## Mac: nothing

The client is stdlib-only. No pip install on the Mac. Set the Pi hostname once:

```bash
# ~/.zshrc
export PI_HOST=raspberrypi.local    # or the Pi's IP address
```

---

## The dev loop

```
edit a script on your Mac  →  python scripts/foo.py  →  watch the Switch
```

No SSH between iterations. No rsync. The Pi daemon is always up; Mac scripts connect, send commands, and exit. Iteration cycle: ~1 second.

A daemon code change still requires a sync + `sudo systemctl restart switch-control` on the Pi, but you'll rarely touch the daemon.

---

## Two ways to drive

### Mode 1: Interactive REPL — for exploring

```bash
python scripts/interactive.py
```

Drops you into a Python REPL on your Mac with `pad`, `Buttons`, `Sticks` pre-bound. Every line fires immediately over HTTP to the Pi. Best for prototyping combos, debugging timing, and exploring game state.

```python
>>> pad.press(Buttons.A)
>>> pad.press(Buttons.B, hold=2.0)
>>> pad.macro("L_STICK@+100+000 A 1.0s")
>>> pad.tilt(Sticks.LEFT_STICK, x=0, y=100, duration=1.5)
>>> pad.status()
{'state': 'connected', 'connected': True, 'reconnect_count': 3, ...}
```

Ctrl-D to exit. The pad stays connected on the Pi.

### Mode 2: Handoff scripts — for replaying

```bash
python scripts/example_handoff.py
python scripts/botw_macros.py casual_stroll
```

Pre-written sequences with `pad.wait_for_ready("...")` pauses between sections. You drive with joycons to position Link, press Enter, the script fires the next macro.

**Typical workflow:** prototype in the REPL until a sequence feels right, paste the macro string into a handoff script for repeatable replay.

---

## Joycons + Pi pad in parallel

The Switch accepts inputs from every paired controller at once. Whichever side is sending inputs wins:

- **Script paused** on `wait_for_ready` → only your joycons drive.
- **Script running a macro** → script drives.

Handoff is just the script blocking on `input()`. Your joycons stay paired the entire time.

---

## Macro DSL

Macros are the primary input language. Everything on one line fires simultaneously for the trailing duration; anything not restated on the next line is released.

```python
pad.macro("L_STICK@+100+000 A 1.0s")    # stick right + A together for 1 second
```

Hold a stick across multiple presses — restate it on every line:

```python
pad.macro("""
    L_STICK@+000+100 A 0.1s
    L_STICK@+000+100 0.4s
    L_STICK@+000+100 B 0.1s
""")
```

Loops:

```python
pad.macro("""
    LOOP 5
        A 0.1s
        0.4s
""")
```

**Stick syntax:** `L_STICK` / `R_STICK` (not `LEFT_STICK`). Both axes are 3 digits with mandatory sign: `L_STICK@-075+050`. Diagonal magnitudes can each exceed 100.

**Coordinate convention:**
- X axis: `-100` = hard left, `+100` = hard right
- Y axis: `+100` = forward, `-100` = backward (same for R_STICK camera)

**Full DSL reference:** `~/nxbt/docs/Macros.md` on the Pi (or `/Users/yuvaltimen/Coding/nxbt/docs/Macros.md` locally).

---

## BotW macros

`scripts/botw_macros.py` has 15 ready-to-run macros across three categories. Each function has a docstring describing the required game-state setup.

| Category | Macros |
|---|---|
| Town | `casual_stroll`, `npc_interact`, `sneaky_passage`, `shop_browse` |
| Combat | `attack_combo`, `shield_parry`, `aerial_attack`, `bow_volley` |
| Explore | `paraglide_swing`, `cliff_climb`, `horizon_scan`, `call_and_ride`, `cook_meal`, `map_survey`, `sprint_jump_glide` |

```bash
# List all macros with descriptions:
python scripts/botw_macros.py --list

# Run one macro (with a handoff prompt):
python scripts/botw_macros.py casual_stroll

# Run all in sequence (prompts between each):
python scripts/botw_macros.py
```

From the interactive REPL:

```python
>>> import sys; sys.path.insert(0, '.')
>>> from scripts.botw_macros import *
>>> casual_stroll(pad)    # or any other macro name
```

---

## Pairing and recovery

### First-ever pair

Switch must be on `Controllers → Change Grip/Order`. Then either:
- Start the daemon — it tries to pair on startup, or
- If the daemon is already running: `pad.pair_fresh()` from the REPL.

### Every subsequent connection

Just run a Mac script. The Pi daemon has the Switch's MAC bonded. If the Pi rebooted, the daemon reconnects on startup. If the Switch was off, wake it — the watchdog reconnects within ~10 seconds.

### Auto-reconnect

The watchdog heartbeats every 5 seconds. After two missed heartbeats it triggers a reconnect. From your Mac this looks like:

1. A command fails: `NotConnected: pad not connected (state=reconnecting)`.
2. ~10 seconds later `pad.status()["connected"]` returns `True`.
3. Next command works normally.

To make a command survive a reconnect automatically:

```python
pad.run_resilient(lambda p: p.macro("..."), retries=1, recover_timeout=20)
```

### Manual recovery

```python
pad.reconnect()       # async trigger, returns immediately
pad.wait_connected()  # block until daemon reports connected
```

Force a fresh pair (Switch has forgotten the Pi):

```python
# Put Switch on Change Grip/Order first
pad.pair_fresh()
pad.wait_connected()
```

---

## Known gotchas

**Daemon needs sudo** — nxbt needs raw HCI access. The systemd unit runs as root; the manual command uses `sudo`. No way around this.

**bluetoothctl D-Bus agent** — the daemon spawns it automatically at startup. If you ever see `Authentication Failure (0x05)` running nxbt outside the daemon, this is why. The daemon handles it transparently.

**Wi-Fi/BT antenna sharing on Pi 4** — the onboard radios share an antenna. Under heavy BT traffic, Wi-Fi load can cause reconnects. Fix: use Ethernet and `sudo rfkill block wifi`, or use a USB Bluetooth dongle.

**Long macros and reconnects** — if BT drops mid-macro, the endpoint returns `NotConnected` and the macro is lost. After the watchdog reconnects, re-issue it. Break long sequences into chunks wrapped in `pad.run_resilient`.

**mDNS (`raspberrypi.local`)** — if it doesn't resolve from your Mac, use the Pi's IP directly. Find it with `ip addr` on the Pi or your router's DHCP table.

**Switch auto-sleep** — drops the controller bond. Disable for long sessions: `System Settings → Sleep Mode → Auto-Sleep → Never`. The watchdog reconnects after wake, but the interruption is annoying mid-macro.

**Don't mix daemon and old direct scripts** — only one nxbt process can hold the BT adapter. Always use `RemotePad` from Mac scripts. Never import `switch_control.pad.SwitchPad` directly while the daemon is running.

---

## Troubleshooting

### `Address already in use` / `Operation not permitted` on PSM 17 or 19

BlueZ `input` plugin is still loaded, or a zombie nxbt process is holding the ports.

```bash
# Kill zombies:
sudo pkill -9 -f nxbt
sudo pkill -9 -f 'bin/python.*nxbt'
ps -ef | grep nxbt | grep -v grep     # must print nothing

# Confirm plugin is disabled:
systemctl cat bluetooth | grep ExecStart    # must show --noplugin=input
```

### `NotConnected` error from a Mac script

```bash
curl http://raspberrypi.local:8765/status    # check the 'state' field
```

| `state` | Fix |
|---|---|
| `reconnecting` | Wait 10–15s and retry. |
| `crashed` | `curl -X POST http://raspberrypi.local:8765/reconnect` or `sudo systemctl restart switch-control` on the Pi. |
| `unpaired` | Switch on Change Grip/Order → `pad.pair_fresh()`. |
| daemon unreachable | `ssh pi "sudo systemctl status switch-control"` — daemon probably isn't running. |

### Daemon won't start

```bash
sudo journalctl -u switch-control -n 50    # read the error
rfkill list                                 # if BT is soft-blocked: sudo rfkill unblock bluetooth
systemctl status bluetooth --no-pager
```

### Switch can't see the Pi (first-time pair)

- Change Grip/Order times out after ~3 minutes — re-open it.
- Move the Pi within 30cm of the Switch for the first pair — the Pi 4 antenna is weak.
- Kill any zombie nxbt processes (see above).

### Authentication Failure (0x05) repeating

Stale link keys. Wipe both sides:

```bash
# Pi:
sudo systemctl stop bluetooth
sudo rm -rf /var/lib/bluetooth/*/[0-9A-F]*:*
sudo systemctl start bluetooth

# Switch: Controllers → Disconnect Controllers → hold L+R until confirmed
```

Then retry the first-time pair flow. If it still fails → `PI_CONTROLLER.md` §Authentication Failure for the full `a/b/c/d` remediation tree including the BlueZ agent workaround.

### `raspberrypi.local` doesn't resolve

```bash
ssh pi "ip addr show wlan0 | grep 'inet '"    # get the Pi's IP
export PI_HOST=<ip>
```

---

## File map

```
nintendo/
├── README.md                        this file
├── PI_CONTROLLER.md                 deep BT surgery: stale-state wipe, Auth Failure tree, btmon
├── switch_control/
│   ├── __init__.py                  re-exports RemotePad, Buttons, Sticks
│   ├── client.py                    HTTP client (Mac, stdlib only)
│   ├── daemon.py                    HTTP server + watchdog (Pi only)
│   └── pad.py                       nxbt wrapper (Pi only, used by daemon)
├── scripts/
│   ├── pi_daemon.py                 entry point: run on the Pi
│   ├── interactive.py               entry point: run on the Mac, REPL
│   ├── example_handoff.py           handoff demo
│   └── botw_macros.py               15 BotW macros (town / combat / explore)
└── systemd/
    └── switch-control.service       optional autostart on the Pi
```

---

## Quick reference

| Action | Command |
|---|---|
| **Start session** | `ssh pi "sudo systemctl start switch-control"` then `python scripts/interactive.py` |
| Start daemon (Pi, manual) | `sudo /home/yuvaltimen/nxbt/.venv/bin/python scripts/pi_daemon.py` |
| Restart daemon | `sudo systemctl restart switch-control` |
| Daemon logs | `sudo journalctl -u switch-control -f` |
| Sync repo to Pi | `rsync -av --delete --exclude='__pycache__' --exclude='.venv' --exclude='.git' /Users/yuvaltimen/Coding/nintendo/ pi:~/Coding/nintendo/` |
| Mac REPL | `python scripts/interactive.py` |
| List BotW macros | `python scripts/botw_macros.py --list` |
| Run a BotW macro | `python scripts/botw_macros.py <name>` |
| Check connection | `curl http://raspberrypi.local:8765/status` |
| Force reconnect | `curl -X POST http://raspberrypi.local:8765/reconnect` |
| Force fresh pair | `curl -X POST http://raspberrypi.local:8765/pair` (Switch on Change Grip/Order) |
| API docs (browser) | `http://raspberrypi.local:8765/docs` |