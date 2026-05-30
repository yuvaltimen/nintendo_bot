# Switch Control

A Mac-driven command-and-control system for a Nintendo Switch. The Raspberry Pi runs a long-lived daemon that holds a virtual Pro Controller's Bluetooth connection forever and exposes an HTTP API; your Mac sends commands to it. The Pi becomes "just a Bluetooth controller" from your perspective — same as your physical joycons.

The end goal is to plug an HDMI capture card into the Mac, run a YOLO model + LLM on the captured frames, and have the LLM call the same HTTP API to play prompted scenarios. The architecture in this repo is built to scale into that shape: the LLM is just another client of the daemon.

For Pi-side Bluetooth setup, troubleshooting, and the bluetoothctl D-Bus agent workaround, see `PI_CONTROLLER.md`. This README assumes that's done.

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

The daemon keeps the BT pad connected across many of your script runs. It heartbeats every 5 seconds and auto-reconnects on drop. Your Mac scripts are stateless clients — start, send commands, exit; the BT link survives.

## One-time setup

### Pi: install the daemon dependencies

```bash
# On the Pi, in the existing nxbt venv
sudo /home/yuvaltimen/nxbt/.venv/bin/pip install fastapi uvicorn pydantic
```

### Pi: sync this repo onto the Pi

```bash
# From your Mac, once (and after any daemon code changes — script-level
# edits don't need a re-sync since they run on the Mac)
rsync -av --delete --exclude='__pycache__' --exclude='.venv' --exclude='.git' \
  /Users/yuvaltimen/Coding/nintendo/ pi:~/Coding/nintendo/
```

### Pi: start the daemon

Manual (good for first run / debugging):
```bash
ssh pi
cd ~/Coding/nintendo
sudo PYTHONUNBUFFERED=1 /home/yuvaltimen/nxbt/.venv/bin/python scripts/pi_daemon.py
```

You should see log lines like:
```
[INFO] daemon: starting daemon on 0.0.0.0:8765
[INFO] daemon: bluetoothctl D-Bus agent started (pid=...)
INFO:     Uvicorn running on http://0.0.0.0:8765
[INFO] daemon: connected (reconnect_count=1)
```

If this is your first-time pair, put the Switch on Change Grip/Order before starting the daemon. After that, the Pi remembers the Switch's MAC and reconnects automatically on every daemon start.

### Pi: optional autostart via systemd

After the daemon works manually, install the unit so it starts at boot:
```bash
sudo cp ~/Coding/nintendo/systemd/switch-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now switch-control
sudo journalctl -u switch-control -f       # tail logs
```
Now the Pi is fully autonomous: power it on, it pairs with the Switch, the API is live on port 8765. You can leave the Pi headless.

### Mac: nothing

The client is stdlib-only. No pip install on the Mac. Set the Pi hostname once:
```bash
# in ~/.zshrc on your Mac
export PI_HOST=pi.local          # or the Pi's IP
```

## The dev loop

This is the UX upgrade. Compared to the old setup where every iteration was edit→rsync→ssh→sudo→python:

```
edit a script on your Mac  →  python scripts/foo.py  →  watch the Switch
```

That's it. No SSH between iterations. No rsync. The Pi daemon is always up; your Mac script connects, sends commands, exits. Iteration cycle: ~1 second.

A daemon code change still requires a sync + daemon restart on the Pi (`sudo systemctl restart switch-control` if you installed the unit), but you'll rarely touch the daemon.

## The two ways to drive

### Mode 1: interactive REPL — for exploring

```bash
python scripts/interactive.py
```

Drops you into a Python REPL on your Mac with `pad`, `Buttons`, `Sticks` pre-bound. Every line you type fires immediately over HTTP to the Pi. Best for learning new combos, exploring game state, or debugging timing.

```python
>>> pad.press(Buttons.A)
>>> pad.press(Buttons.A, hold=2.0)
>>> pad.macro("L_STICK@+100+000 A 1.0s")
>>> pad.status()
{'state': 'connected', 'connected': True, 'reconnect_count': 3, ...}
```

Ctrl-D to exit. The pad stays connected on the Pi — your next REPL session picks up instantly.

### Mode 2: handoff scripts — for replaying

```bash
python scripts/example_handoff.py
```

Pre-written sequences with `pad.wait_for_ready("...")` pauses between sections so you can drive with joycons (navigate menus, position your character) before letting the script fire the next macro.

```python
from switch_control import RemotePad, Buttons, Sticks
pad = RemotePad(os.environ["PI_HOST"])

pad.wait_connected()
pad.wait_for_ready("Get into BOTW. Enter to hand off.")
pad.press(Buttons.A)
pad.macro("""
    L_STICK@+000+100 B 0.7s
    L_STICK@+000+100 B X 0.1s
    L_STICK@+000+100 0.5s
    L_STICK@+000+100 X 0.1s
""")
pad.wait_for_ready("Back to you. Enter to exit.")
```

**Typical workflow:** prototype in interactive mode until a sequence works, then paste it into a handoff script for replay.

## Joycons + Pi pad in parallel

The Switch accepts inputs from every paired controller at once. Your real joycons stay paired the entire time; whichever side is sending inputs wins. So:
- **Script idling** (blocked on `wait_for_ready`) → only your joycons drive.
- **Script running a macro** → script drives.

Handoff is just the script pausing on `input()`.

## Pairing and recovery

### First-ever pair

Switch must be on `Controllers → Change Grip/Order`. Then either:
- Start the daemon manually (it tries to pair on startup), OR
- If the daemon is already running but no Switch has ever paired: `pad.pair_fresh()` from the Mac REPL.

### Every subsequent connection

Just run a Mac script. The Pi daemon already has the Switch's MAC bonded. If the Pi reboots, the daemon reconnects on startup. If the Switch was off, wake it; the daemon's watchdog will reconnect within ~10 seconds.

### Auto-reconnect

The daemon's watchdog runs a no-op heartbeat macro every 5 seconds. After two failed heartbeats it triggers a reconnect. From your Mac, this looks like:
1. A command fails with `NotConnected: pad not connected (state=reconnecting)`.
2. ~5-10 seconds later, `pad.status()["connected"]` returns `True` again.
3. Next command works.

If you want a command to survive reconnects automatically:
```python
pad.run_resilient(lambda p: p.macro("..."), retries=1, recover_timeout=20)
```

### Manual recovery

If the BT drops and you don't want to wait for the watchdog:
```python
pad.reconnect()              # async; returns immediately
pad.wait_connected()         # block until daemon reports connected
```

If a fresh first-time pair is needed (e.g., the Switch forgot the Pi):
```python
# Put Switch on Change Grip/Order first
pad.pair_fresh()
pad.wait_connected()
```

## Macro DSL essentials

Macros are the primary input language. Everything on one line fires simultaneously for the trailing duration; anything not on the next line is released.

```python
pad.macro("L_STICK@+100+000 A 1.0s")   # stick right + A simultaneously, 1s
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

Stick syntax: `L_STICK` / `R_STICK` (not `LEFT_STICK`). Both axes are 3 digits with mandatory sign: `L_STICK@-075+050`. Sum can exceed 100 for diagonals.

Full reference: `/Users/yuvaltimen/Coding/nxbt/docs/Macros.md`.

## Example: BOTW paraglide

Stand Link at a cliff edge with the paraglider unlocked and stamina available:
```python
pad.macro("""
    L_STICK@+000+100 B 0.7s
    L_STICK@+000+100 B X 0.1s
    L_STICK@+000+100 0.5s
    L_STICK@+000+100 X 0.1s
    LOOP 2
        L_STICK@-080+080 1.2s
        L_STICK@+080+080 1.2s
    L_STICK@+000+100 0.8s
""")
```
Sprint forward, running jump, deploy paraglider mid-air, pendulum left-right twice, settle for landing. Demonstrates concurrent inputs (stick + B + X), state-by-restatement, and `LOOP`.

## Future: LLM/YOLO as a client

The HTTP API is the only public surface. An autonomous agent looks like:
```python
while True:
    frame = capture_card.read()                       # HDMI frame
    decision = llm.decide(frame, scenario_prompt)    # YOLO bboxes + Claude
    pad.macro(decision.macro)                         # send to Pi
```

The daemon's auto-reconnect means the agent doesn't need to handle BT drops itself. The OpenAPI schema at `http://pi:8765/docs` is auto-generated by FastAPI and can be fed to the LLM for tool-use.

## Awkward UX — read once

### The daemon needs sudo to access BlueZ HCI
The systemd unit runs as root. The manual command uses `sudo`. There's no way around this — nxbt needs raw HCI access.

### The bluetoothctl D-Bus agent must be running
The daemon spawns it as a subprocess at startup. If you ever bring up nxbt without this daemon and bonding fails with `Authentication Failure (0x05)`, you've hit the issue from PI_CONTROLLER.md §5.7d. The daemon handles this automatically.

### Wi-Fi/BT antenna sharing on Pi 4
Same caveat as before: the Pi 4's onboard radios share an antenna, and sustained BT traffic during macros can be interrupted by Wi-Fi load. If you see frequent reconnects under load, either disable Wi-Fi (`sudo rfkill block wifi`) with the Pi on Ethernet, or use an external USB Bluetooth dongle. The "controller disconnected" overlay you see on the Switch is this dropout — the daemon's watchdog should clear it within ~10s.

### Long macros and reconnects
If the BT drops in the middle of a 30-second macro, the macro endpoint returns `NotConnected` and the macro is lost. After the watchdog reconnects, re-issue the macro. For mission-critical sequences, break long macros into smaller chunks with `pad.run_resilient`.

### mDNS (`pi.local`)
If `pi.local` doesn't resolve from your Mac, use the Pi's IP address directly. Check with `ip addr` on the Pi or your router's DHCP table.

### Switch sleep kills the BT link
Switch auto-sleep drops the controller bond. Disable auto-sleep for long automation sessions: System Settings → Sleep Mode → Auto-Sleep → Never. The watchdog will still reconnect after the Switch wakes, but the disruption is annoying.

### Don't run the daemon and the old direct scripts at the same time
Only one nxbt process can hold the BT adapter. If you have the daemon running and try to run a script that imports `switch_control.pad.SwitchPad` directly, it'll fight the daemon. Use the client (`RemotePad`) for everything.

## File map

```
nintendo/
├── README.md                                this file
├── PI_CONTROLLER.md                         Pi-side BT setup + troubleshooting
├── switch_control/
│   ├── __init__.py                          re-exports RemotePad, Buttons, Sticks
│   ├── client.py                            HTTP client (Mac, stdlib only)
│   ├── daemon.py                            HTTP server + watchdog (Pi only)
│   └── pad.py                               nxbt wrapper (Pi only, used by daemon)
├── scripts/
│   ├── pi_daemon.py                         entry point: run on the Pi
│   ├── interactive.py                       entry point: run on the Mac, REPL
│   └── example_handoff.py                   entry point: run on the Mac, handoff demo
└── systemd/
    └── switch-control.service               optional autostart on the Pi
```

## Quick reference

| Action | Command |
|---|---|
| Start daemon (Pi, manual) | `sudo /home/yuvaltimen/nxbt/.venv/bin/python scripts/pi_daemon.py` |
| Start daemon (Pi, systemd) | `sudo systemctl start switch-control` |
| Daemon logs | `sudo journalctl -u switch-control -f` |
| Mac REPL | `python scripts/interactive.py` |
| Mac handoff demo | `python scripts/example_handoff.py` |
| Check connection | `curl http://pi.local:8765/status` |
| Force reconnect | `curl -X POST http://pi.local:8765/reconnect` |
| Force fresh pair | `curl -X POST http://pi.local:8765/pair` (Switch on Change Grip/Order) |
| API docs (browser) | `http://pi.local:8765/docs` |
