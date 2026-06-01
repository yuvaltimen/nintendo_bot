# Switch Control

A Mac-driven command-and-control system for a Nintendo Switch. The Raspberry Pi runs a long-lived daemon that holds a virtual Pro Controller's Bluetooth connection and exposes an HTTP API; your Mac sends commands to it.

The end goal: plug an HDMI capture card into the Mac, run YOLO + LLM on the captured frames, and have the LLM call the same HTTP API to play prompted scenarios. The architecture scales into that shape - the LLM is just another client of the daemon.

---

## SHORTCUT:

On the Switch: go to `Controllers → Change Grip/Order` (first pair) or wake it from sleep.

On the Pi:
```bash
sudo systemctl start switch-control
```

From the Mac:
```bash
python scripts/interactive.py          # interactive REPL
python scripts/botw_macros.py --list   # scripted macros
CAPTURE_DEVICE=1 python scripts/phi4_agent.py  # LLM agent
```

---

## Resume a session

Pi already set up and paired at least once? This is all you need:

```bash
# 1. Start (or confirm) the daemon on the Pi
ssh pi "sudo systemctl start switch-control"

# 2. Wake the Switch - press any button on your joycons

# 3. Open the interactive REPL on your Mac
python scripts/interactive.py

# 4. Or run a BotW macro directly
python scripts/botw_macros.py --list
python scripts/botw_macros.py casual_stroll
```

The daemon reconnects to the Switch within ~10 seconds of it waking. If `systemctl start` says the unit is already running, that's fine - it's a no-op.

> **Something broken?** → [Troubleshooting](#troubleshooting) below, or the deep BT diagnosis tree in `PI_CONTROLLER.md`.

---

## Architecture

```
Nintendo Switch (docked)
   ├─ HDMI out → passive splitter
   │                 ├─ TV / monitor          (you watch here)
   │                 └─ USB capture card → Mac
   │                       ↓ OpenCV VideoCapture
   │                       ↓ YOLO inference
   │                       ↓ policy()
   └─ Bluetooth ← Pi daemon ← HTTP ← Mac scripts / vision_loop.py
                                         (same pad API for both)
```

The daemon keeps the BT pad connected across many script runs. It heartbeats every 5 seconds and auto-reconnects on drop. Mac scripts are stateless clients - start, send commands, exit; the BT link survives.

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

Re-run this any time you change daemon code (`switch_control/daemon.py` or `switch_control/pad.py`). Script-only changes don't need a re-sync - they run on the Mac.

### 6. First-time pair

Put the Switch on `Controllers → Change Grip/Order`. Start the daemon manually so you can watch the logs:

```bash
ssh pi
sudo PYTHONUNBUFFERED=1 /home/yuvaltimen/nxbt/.venv/bin/python ~/Coding/nintendo/scripts/pi_daemon.py
```

Watch for `[INFO] daemon: connected`. The Switch shows a new Pro Controller on the Change Grip/Order screen. After the first successful pair the Switch remembers the Pi's MAC forever - reconnects are automatic from here on.

> **Pairing fails?** → `PI_CONTROLLER.md` §Wipe stale state + §Authentication Failure for the full diagnosis tree.

### 7. Enable autostart (optional but recommended)

```bash
sudo cp ~/Coding/nintendo/systemd/switch-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now switch-control
sudo journalctl -u switch-control -f      # confirm it started
```

With this, powering on the Pi is enough - the daemon starts, reconnects to the Switch, and the API is live on port 8765.

---

## Mac dependencies

The core client (`switch_control/client.py`) is stdlib-only. The agent and vision scripts need additional packages:

```bash
# Required for vision_loop.py, agent_loop.py, phi4_agent.py:
pip install opencv-python ultralytics

# Required for agent_loop.py (Claude API) only:
pip install anthropic
```

Set persistent environment variables once:

```bash
# ~/.zshrc
export PI_HOST=raspberrypi.local       # or the Pi's IP
export CAPTURE_DEVICE=1                # USB capture card device index
export ANTHROPIC_API_KEY=sk-ant-...   # for agent_loop.py
export OLLAMA_MODEL=phi4               # default model for phi4_agent.py
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

### Mode 1: Interactive REPL - for exploring

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

### Mode 2: Handoff scripts - for replaying

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

Hold a stick across multiple presses - restate it on every line:

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

## HDMI capture and vision loop

This section is the end-to-end walkthrough for getting `scripts/vision_loop.py` running: hardware wiring, Mac software setup, finding the capture device, verifying the feed, and writing a policy that sends macros back to the Switch.

### Hardware you need

| Item | Notes |
|---|---|
| **Passive HDMI splitter** (1-in, 2-out) | ~$10–15 on Amazon. Passive splitters work - the Switch doesn't use HDCP. |
| **USB HDMI capture card** (UVC-compliant) | ~$15–25 generic ("HDMI USB capture card UVC"), or Elgato Cam Link 4K (~$100) for reliability. Avoid the Elgato HD60 - it requires proprietary software. |

The Switch outputs HDMI **only when docked**. Handheld mode has no video out.

### Wiring

```
Switch dock
  HDMI out → passive splitter
                ├── TV or monitor          ← you watch here, zero latency
                └── capture card HDMI in
                      USB → Mac
```

### One-time Mac setup

```bash
pip install opencv-python ultralytics
```

`ultralytics` automatically uses Apple Silicon's Metal (MPS) GPU backend - no extra configuration needed.

### Step 1: Find the capture card's device index

Plug the capture card in and run:

```bash
python scripts/vision_loop.py --scan
```

Expected output:

```
[0]  readable      1280x720  @ 30 fps   ← Mac built-in camera
[1]  readable      1920x1080 @ 30 fps   ← capture card  ← use this
```

The index changes if you plug into a different USB port or change the port order. Re-run `--scan` if the index ever stops working.

### Step 2: Verify the feed before enabling control

Run with `--dry-run` - this opens the display window, runs YOLO inference, and prints what commands *would* be sent, but never touches the Switch:

```bash
python scripts/vision_loop.py --device 1 --dry-run
```

You should see a side-by-side window: raw game feed on the left, YOLO boxes on the right. Confirm the image looks correct (right orientation, no colour channel swap) before proceeding.

Set `CAPTURE_DEVICE` so you don't have to type `--device` every time:

```bash
# ~/.zshrc
export CAPTURE_DEVICE=1
```

### Step 3: Choose a display mode

Three modes - pick based on your setup:

| Flag | Window shown | When to use |
|---|---|---|
| *(none, default)* | Side-by-side: clean left, YOLO annotations right | Tuning the policy - see the raw game and what the model detects at the same time |
| `--clean` | Clean frame only, last command shown at bottom | Capture card is your only monitor; want a full view without annotation clutter |
| `--no-display` | None | TV/monitor on the splitter is your display; run the script headless |

### Step 4: Write the policy

Open `scripts/vision_loop.py` and edit the `policy()` function near the top of the file. It receives every frame that passes the cooldown gate and returns a macro string or `None`:

```python
def policy(frame, results, w: int, h: int) -> str | None:
    # results.boxes contains detected objects for this frame.
    # results.names maps class id → label string.
    # Return a macro string to send, or None to do nothing.

    for box in results.boxes:
        cls_name = results.names[int(box.cls)]
        conf = float(box.conf)
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        box_cx = (x1 + x2) / 2

        if cls_name == "person" and conf > 0.5:
            if abs(box_cx - w / 2) < w / 4:   # roughly centred
                return "Y 0.1s"                 # attack

    return None
```

The macro string uses the same DSL as `pad.macro()` - see [Macro DSL](#macro-dsl). The `--cooldown` flag (default 0.5s) gates how often `policy()` can fire a command.

The default YOLO model (`yolov8n.pt`) is trained on COCO classes (person, car, dog, etc.) - useful for structural testing but not BotW-specific. Swap in a fine-tuned model via `--model path/to/model.pt` or `$YOLO_MODEL` once you have one.

### Step 5: Run live

```bash
# Splitter → TV is your primary display, script runs headless:
python scripts/vision_loop.py --no-display

# Capture card is your only monitor, clean view:
python scripts/vision_loop.py --clean

# Tuning mode - side-by-side clean + annotated:
python scripts/vision_loop.py
```

Press `Q` in the display window or `Ctrl-C` to stop. The Pi pad stays connected.

### Latency expectations

| Stage | Time |
|---|---|
| Frame capture + buffer drain | ~10–33ms |
| YOLO inference (yolov8n, Apple Silicon) | ~5–15ms |
| HTTP to Pi + Bluetooth to Switch | ~30–50ms |
| **Total round-trip** | **~50–100ms** |

50–100ms is fine for strategic decisions (navigate toward a target, attack when an enemy is centred, interact with an object). It is **not** tight enough for frame-perfect inputs like parry timing - use scripted macros from `botw_macros.py` for those.

### Troubleshooting capture

**`Cannot open capture device 1`** - re-run `--scan`, the index may have shifted. Also try passing `--device 0` explicitly.

**Image is upside-down or mirrored** - add a `cv2.flip(frame, 1)` at the top of `policy()`, or flip before inference in `run_loop`.

**YOLO is slow / dropping frames** - switch to `yolov8n.pt` (nano) if not already using it, or lower the capture resolution: edit `FRAME_W`/`FRAME_H` at the top of `vision_loop.py`.

**Commands firing too rapidly** - increase `--cooldown` (default 0.5s). For exploration loops, 1–2s is more appropriate.

---

## AI agent integration

This section closes the full loop: `HDMI → YOLO → Claude → macros → Pi → Switch`. `scripts/agent_loop.py` replaces the hand-written `policy()` function in `vision_loop.py` with a live Claude call — the model sees the annotated game frame, reasons about what Link should do, and returns a macro string.

### One-time setup

```bash
pip install anthropic

# Add to ~/.zshrc so it persists across sessions:
export ANTHROPIC_API_KEY=sk-ant-...
```

### Step 1: dry-run — watch Claude reason without moving Link

Always start here. Claude will print its reasoning and the macro it *would* send, without touching the game. This lets you tune the goal wording and verify the prompt before anything moves.

```bash
CAPTURE_DEVICE=1 python scripts/agent_loop.py \
  --goal "walk toward the nearest person and interact with them" \
  --dry-run
```

You'll see output like:

```
[dry-run]  'L_STICK@+000+080 1.5s'
           → I can see a person labeled at screen center-left. Moving forward to close distance.
[wait]     Person is now close. Stopping to prepare interaction.
[dry-run]  'A 0.1s'
           → Person is within interaction range. Pressing A to start dialogue.
```

### Step 2: go live

```bash
CAPTURE_DEVICE=1 python scripts/agent_loop.py \
  --goal "explore the area and interact with any NPCs"
```

The display window shows the clean game feed on the left and the YOLO-annotated feed on the right, with Claude's last reasoning line and the macro it sent overlaid. A green progress bar fills left-to-right counting down to the next LLM call.

### Tuning the agent

**Goal wording is the most important lever.** Be specific about what you want Link to do and what success looks like. Vague goals produce wandering behaviour.

| Less effective | More effective |
|---|---|
| `"play the game"` | `"walk north along the road toward Kakariko Village"` |
| `"fight"` | `"lock on to the nearest Bokoblin with ZL and attack with Y until it's gone"` |
| `"explore"` | `"find and activate the shrine — it appears as a glowing blue pillar"` |

**`--interval`** controls how often Claude is called. At 2s (default) the agent makes one decision every 2 seconds — fine for exploration and NPC interactions. Increase to 3–5s to reduce API spend during long runs.

**`--claude-model`** — use `claude-haiku-4-5-20251001` while iterating on the prompt (cheapest, ~10ms latency). Switch to `claude-sonnet-4-5` for the final run quality.

```bash
# Fast + cheap during development:
python scripts/agent_loop.py --goal "..." --claude-model claude-haiku-4-5-20251001 --interval 3.0

# Full quality:
python scripts/agent_loop.py --goal "..." --claude-model claude-sonnet-4-5 --interval 2.0
```

### What Claude sees

Each call sends:

1. **The YOLO-annotated frame** as a JPEG (base64). Claude sees bounding boxes, class labels, and confidence scores drawn over the game image.
2. **Structured detection JSON** — label, confidence, and normalised screen position (cx/cy from 0 to 1) for each detected object.
3. **Last 5 actions** the agent took, so Claude can detect loops or lack of progress.

The response is a JSON object:

```json
{
  "reasoning": "I can see a person labeled at center-left with 0.87 confidence. Moving forward to close distance.",
  "macro": "L_STICK@+000+080 1.5s"
}
```

An empty `macro` field means Claude decided to wait this tick.

### Prompt caching

The system prompt (controller reference + goal description) is sent with `cache_control: ephemeral`. At the default 2s call interval the cache stays warm — only the per-frame image and detections are billed at the full input token rate. On a 5-minute run this typically cuts token costs by 60–70%.

### API cost reference

| Model | Approx cost/call | 5-min run (~150 calls) |
|---|---|---|
| `claude-haiku-4-5-20251001` | ~$0.001 | ~$0.15 |
| `claude-sonnet-4-5` | ~$0.008 | ~$1.20 |

---

## Local model agent (phi4 via Ollama)

`scripts/phi4_agent.py` runs phi4 **locally via Ollama** — no API key, no cost per call, fully private. The trade-off vs. `agent_loop.py` (Claude) is speed: phi4 takes 3–8 s per response on Apple Silicon vs. ~1 s for Claude Sonnet.

### Architecture

The script runs two concurrent pieces:

```
Main thread   capture card → YOLO inference → OpenCV display window (always live)
REPL thread   BotW Agent > prompt → phi4 streaming calls → pad.macro()
```

The display window stays open and updates continuously regardless of whether a goal is running or the prompt is waiting for input. `cv2.imshow` must run on the main thread on macOS (Cocoa requirement) — this design ensures that.

Cancellation uses a `threading.Event` (`stop_goal`) rather than `KeyboardInterrupt`. When you press Ctrl-C or Q in the window, a signal handler sets `stop_goal`, which `call_phi4()` checks between every streamed token (~50 ms response time). `pad.stop()` fires at the same moment.

### One-time setup

```bash
# opencv + ultralytics are required (see Mac dependencies above).
# Pull a model — phi4 is the default, but llama3.1:8b reasons better:
ollama pull phi4               # 14B, default — adequate for simple goals
ollama pull llama3.1:8b        # 8B, faster + better directional reasoning
ollama pull llama3.2-vision:11b  # 11B vision model, required for --vision mode

# Verify:
ollama list
```

### Connecting and starting

```bash
# Default (phi4):
CAPTURE_DEVICE=1 python scripts/phi4_agent.py

# Switch model at the command line:
python scripts/phi4_agent.py --model llama3.1:8b

# Vision mode — sends the annotated JPEG frame to the model instead of text:
python scripts/phi4_agent.py --model llama3.2-vision:11b --vision
```

Startup runs four pre-flight checks in order and tells you exactly what to fix if any step fails:

```
Checking Ollama (llama3.1:8b)...   llama3.1:8b is ready
Connecting to Pi daemon...         connected.
Loading YOLO (yolov8n.pt)...       ready.
Opening capture device 1...        1280x720.
```

Then the REPL prompt opens in the terminal and the display window opens simultaneously.

### The display window

The window shows two panels side by side, scaled to fit your screen (`--scale 0.65` by default):

```
┌─────────────────────────┬─────────────────────────┐
│  Clean game feed        │  YOLO annotations        │
│                         │                          │
│  IDLE / RUNNING /       │  fps  |  phi4            │
│  THINKING badge         │  [thinking... if active] │
│  (top-left corner)      │                          │
│                         │  goal text (yellow)      │
│                         │  last reasoning (gray)   │
│                         │  last cmd: ... (blue)    │
└─────────────────────────┴─────────────────────────┘
                          ▓▓▓▓▓▓░░░░  ← cooldown bar
```

The **green cooldown bar** at the bottom edge fills left-to-right and resets each time phi4 is called. When it's full, a new phi4 call fires.

The badge on the left panel tells you what's happening:
- `IDLE` (gray) — waiting at the prompt, no goal active
- `RUNNING` (green) — goal active, waiting for the next phi4 call
- `THINKING` (yellow) — phi4 is generating a response right now

### Using the REPL

Type goals in the terminal. The display window updates live while you type.

```
BotW Agent > walk toward the nearest NPC and talk to them
[running] walk toward the nearest NPC and talk to them

  [sent]   'L_STICK@+000+070 2.0s'
           → person detected at center-left (82% conf). Moving forward.
  [wait]   Person now in close range — pausing to position.
  [sent]   'A 0.1s'
           → within interaction distance, pressing A.
^C
[stopping — halting Pi pad] done.
[stopped]

BotW Agent > status
  {'state': 'connected', 'connected': True, 'reconnect_count': 1, ...}

BotW Agent > defeat the enemy on the right side of the screen
[running] defeat the enemy on the right side of the screen
  ...

BotW Agent > quit
```

Built-in commands:

| Input | Effect |
|---|---|
| `<any text>` | Start agent with that goal |
| `status` | Print Pi pad connection state |
| `quit` or `exit` | Stop and close everything |
| Ctrl-C | Cancel current run, return to prompt |
| Q (display window) | Cancel current run if one is running; exit if idle |

### How cancellation works

Pressing Ctrl-C fires Python's `SIGINT` handler in the main thread. The handler:

1. Checks `state.has_active_goal()` — if a goal is running, sets `state.stop_goal`.
2. Calls `pad.stop()` → `POST /stop` to the Pi daemon → any running macro halts immediately.
3. Prints `[stopped]` and returns to the `BotW Agent >` prompt.

If no goal is running, Ctrl-C instead sets `state.exit_app` and closes the script.

`call_phi4()` checks `stop_goal` between every streamed token from Ollama. phi4 streams one token every ~30–80 ms, so cancellation fires within one token interval — it does not wait for the full response.

Q in the display window follows the same path via the `cv2.waitKey` check in `display_loop`.

### Tuning

**`--model` — the most impactful single change.** phi4 (14B) struggles with spatial reasoning for complex goals. `llama3.1:8b` responds faster and makes more coherent directional decisions. `llama3.2-vision:11b` with `--vision` is the highest quality option — it sees the actual game frame.

```bash
# Recommended default for most goals:
python scripts/phi4_agent.py --model llama3.1:8b --interval 3.0

# Best quality — sends the YOLO-annotated frame as an image:
python scripts/phi4_agent.py --model llama3.2-vision:11b --vision --interval 8.0

# Cheapest / fastest — phi4:
python scripts/phi4_agent.py --model phi4 --interval 7.0
```

**Calibrate `--interval` to your model.** If `interval` is shorter than the model's actual response time, the agent never gets to act.

```bash
# Time a minimal response to calibrate:
time ollama run llama3.1:8b "Reply with the single word: ready"
# Set interval ~1-2 s above that.
```

Approximate response times on Apple Silicon M-series:

| Model | Typical latency | Recommended interval |
|---|---|---|
| `phi4` | 4–7 s | `--interval 8.0` |
| `llama3.1:8b` | 2–4 s | `--interval 4.0` |
| `llama3.2-vision:11b` | 5–10 s | `--interval 10.0` |

**Movement macros are automatically extended** to fill the interval so Link moves continuously between decisions. A macro that phi4 returns as `L_STICK@+000+100 B 1.5s` will be padded to `~interval - 0.5s` automatically. Combat/interaction macros are never extended (they have timing constraints).

**Goal wording is the most impactful prompt lever.** Be specific about direction and success criteria:

| Less effective | More effective |
|---|---|
| `"fight"` | `"lock on to the enemy at screen-right with ZL and attack with Y"` |
| `"explore"` | `"walk forward along the dirt road and stop at any building"` |
| `"find shrine"` | `"scan left and right with the camera looking for a glowing blue pillar"` |

**`--scale`** adjusts the display window size. Default 0.65 fits a 13" MacBook screen:

```bash
python scripts/phi4_agent.py --scale 1.0    # external monitor
```

**`--yolo-model`** — swap to `models/botw.pt` once trained. All models reason better with BotW-specific class names like "bokoblin" vs. COCO's "person":

```bash
python scripts/phi4_agent.py --model llama3.1:8b --yolo-model models/botw.pt
```

### Debugging

**`Checking Ollama... FAIL` on startup**

Ollama isn't running. Open the Ollama app, or:
```bash
ollama serve       # starts the Ollama server in foreground
```
If it says the model isn't found, `ollama pull phi4` and retry.

**Agent runs but never sends `[sent]` — only `[wait]`**

phi4 is returning an empty macro string every turn. Likely causes:
1. The goal is too vague — try a more specific goal.
2. YOLO detects nothing useful — watch the right panel. If no boxes appear for the object you're describing, phi4 has nothing to reason about.
3. phi4 is misunderstanding the scene description — run a manual test:

```bash
ollama run phi4
>>> You are controlling Link in Breath of the Wild. Detected objects: - person (87%) at screen center/middle. Goal: walk toward the person. Respond with JSON only: {"reasoning": "...", "macro": "..."}
```

If phi4 gives a good answer here but not in the agent, the issue is prompt formatting — add `print(messages[-1]["content"])` inside `goal_loop` temporarily to inspect what the agent is actually sending.

**`[parse error]` appearing repeatedly**

phi4 is not returning valid JSON. This happens when the model adds markdown fences (` ```json `) or extra explanation text. The `parse_response` function strips common wrappers, but sometimes phi4 ignores the format instruction.

Try adding a stronger format reminder to the user message in `goal_loop`:
```python
"content": (
    f"Goal: {goal}\n\n{scene}\n\n"
    f"Recent actions:\n{history_str}\n\n"
    "Respond with ONLY the JSON object, no other text:\n"   # ← add this
    "What should Link do next?"
),
```

**phi4 responses are very slow (> 10 s)**

Check whether another process is using Ollama simultaneously:
```bash
ollama ps    # shows currently loaded models and GPU/CPU usage
```

If phi4 is running on CPU (no Metal), it'll be much slower. Confirm Metal is being used:
```bash
ollama run phi4 "hi" 2>&1 | grep -i metal
# Should show Metal being used. If not, reinstall Ollama.
```

**Display window is black or frozen**

OpenCV's AVFoundation backend on macOS sometimes needs a frame to be read before `imshow` initialises. This usually self-clears after 2–3 seconds. If it persists:
```bash
python scripts/vision_loop.py --scan      # verify capture device is readable
python scripts/vision_loop.py --device 1  # test standalone display before running agent
```

**Pad receives commands but the Switch doesn't respond**

```bash
# In a separate terminal or the status command:
curl http://raspberrypi.local:8765/status
```

If `state` is `reconnecting`, wait 10 s. If `crashed`, restart the daemon:
```bash
ssh pi "sudo systemctl restart switch-control"
```

**The agent loops on the same action**

phi4 will repeat a macro if the scene description doesn't change between calls (YOLO sees the same objects in the same positions). The system prompt instructs phi4 not to repeat the same macro more than 3 times, but this isn't always respected. If it's stuck:
1. Increase `--interval` so the scene has more time to change between calls.
2. Add more variety to the goal: `"explore, and if you've been doing the same thing for 3 turns, try something different"`.
3. Look at the YOLO panel — if it's detecting the same `person` at the same position every frame, the character may be stuck against geometry and needs a different movement direction.

### Choosing a model

| | `phi4` (default) | `llama3.1:8b` | `llama3.2-vision:11b --vision` | `agent_loop.py` (Claude) |
|---|---|---|---|---|
| Cost | Free | Free | Free | ~$0.001–$0.01/call |
| Latency | 4–7 s | 2–4 s | 5–10 s | ~1 s |
| Input | YOLO text | YOLO text | Annotated JPEG | Annotated JPEG + JSON |
| Reasoning | Weak spatial | Good directional | Good visual | Best overall |
| Best for | Simple movement | Most goals | When frame context matters | Production-quality runs |

Start with `llama3.1:8b` for general exploration goals. Switch to `llama3.2-vision:11b --vision` when the goal involves objects YOLO can't label (terrain shape, UI state, text on screen). Use `agent_loop.py` with Claude Sonnet for the highest-quality results.

---

## Training a custom YOLO model

The default `yolov8n.pt` is trained on COCO — it knows "person", "car", "dog". It does not know "Bokoblin", "heart container", or "shrine". A BotW-specific model is what turns rough detection into useful control signal.

### Recommended starter classes

Don't try to label everything at once. Pick the classes that will actually drive decisions in your control policy:

| Category | Classes |
|---|---|
| Enemies | `bokoblin`, `blue_bokoblin`, `silver_bokoblin`, `lizalfos`, `moblin`, `guardian` |
| HUD | `heart_full`, `heart_half`, `heart_empty`, `stamina_wheel` |
| Environment | `shrine`, `treasure_chest`, `cooking_pot`, `npc` |
| Items | `weapon_on_ground`, `rupee` |

Start with 3–5 enemy types and the HUD elements. Those alone unlock enemy-detection logic and health-aware behaviour.

### Data pipeline — two sources

#### Source 1: Hyrule Compendium API

The Hyrule Compendium is a public REST API that exposes every BotW enemy, item, and equipment entry with a reference photo. Search for "Hyrule Compendium API" on GitHub — the main project by gadhagod is the canonical one. It returns JSON with image URLs for all ~400 entries.

Write a small script to download and organise them:

```python
# Rough outline — fill in the actual API URL from the GitHub project
import requests, pathlib, time

BASE = "https://<compendium-api-host>/api/v3"
categories = ["monsters", "equipment", "materials", "treasure", "creatures"]

for cat in categories:
    entries = requests.get(f"{BASE}/category/{cat}").json()["data"]
    for entry in entries:
        name  = entry["name"].replace(" ", "_")
        image = entry.get("image")
        if not image:
            continue
        out = pathlib.Path(f"data/compendium/{cat}/{name}.jpg")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(requests.get(image).content)
        time.sleep(0.1)   # be polite to the API
```

Compendium images are clean reference shots — good for bootstrapping label definitions, but they show enemies in isolation against plain backgrounds. In-game screenshots transfer much better.

#### Source 2: Self-captured gameplay frames

`scripts/collect_frames.py` captures live gameplay frames from your capture card.

```bash
# Start the capture card feed first, then:
CAPTURE_DEVICE=1 python scripts/collect_frames.py --output data/raw --every 30
```

Play BotW normally for 20–30 minutes. Deliberately visit:
- Hyrule Field (open combat, Bokoblins, Guardians)
- Kakariko Village (NPCs, cooking pot, shrine)
- A forest or cliff area (climbing, paragliding)
- A dungeon or shrine interior (different lighting)

At `--every 30` (~1fps) a 20-minute session produces ~1,200 frames. You won't annotate all of them — Roboflow lets you filter to frames that actually contain interesting objects.

```
data/
├── raw/           ← self-captured gameplay frames (unannotated)
└── compendium/    ← reference images per entity from the API
```

### Annotating with Roboflow

[Roboflow](https://roboflow.com) is the fastest path from raw images to a trained-ready dataset. Free tier: 1,000 images, unlimited projects.

1. Create a project → Object Detection → class names from your starter list above.
2. Upload `data/raw/` (drag the folder in).
3. Draw bounding boxes around each instance. Roboflow has auto-label assistance that speeds this up significantly once you have ~20 examples per class.
4. Let Roboflow generate the train/val/test split (80/10/10 default is fine).
5. **Export → YOLOv8** → Download ZIP. It contains `dataset.yaml` and the split directories.

Aim for **100–200 annotated instances per class** before the first training run. You can always add more later.

### Training

```bash
pip install ultralytics   # if not already installed

# CPU / Intel Mac:
yolo train data=path/to/dataset.yaml model=yolov8n.pt epochs=50 imgsz=640

# Apple Silicon (uses Metal):
yolo train data=path/to/dataset.yaml model=yolov8n.pt epochs=50 imgsz=640 device=mps
```

Training logs go to `runs/detect/train/`. Watch `mAP50` in the output — above 0.5 is usable, above 0.7 is solid for this task. Training takes ~10–30 minutes on Apple Silicon for a small dataset.

Copy the best checkpoint somewhere stable:

```bash
mkdir -p models
cp runs/detect/train/weights/best.pt models/botw.pt
```

### Validate visually before using it

```bash
# See what the model actually detects on your live feed:
CAPTURE_DEVICE=1 python scripts/vision_loop.py --model models/botw.pt --dry-run
```

Watch the annotated window. If boxes are consistently wrong or missing, you need more annotated data for those classes or more training epochs. Add images → re-annotate → re-export → retrain is a fast cycle (each training run takes ~15 minutes).

### Run the full loop with the custom model

```bash
# vision_loop (rule-based policy):
CAPTURE_DEVICE=1 python scripts/vision_loop.py --model models/botw.pt

# agent_loop (LLM policy):
CAPTURE_DEVICE=1 python scripts/agent_loop.py \
  --yolo-model models/botw.pt \
  --goal "find and defeat the nearest Bokoblin"
```

### Iteration tips

- **More data beats more epochs.** If validation mAP plateaus, add images — don't just increase epochs.
- **Hard negatives matter.** If the model confuses a torch with a shrine, add 20–30 annotated torch examples explicitly labeled as a different class (or background).
- **Keep the model small.** `yolov8n` (nano) runs at ~60fps on Apple Silicon. `yolov8s` (small) gives better accuracy at ~40fps — still plenty for a 2s agent interval. Don't go larger unless you have a GPU.
- **Label what the camera sees, not the compendium.** Enemies look different from behind, at night, or in rain. Diverse capture conditions matter more than total frame count.

---

## Pairing and recovery

### First-ever pair

Switch must be on `Controllers → Change Grip/Order`. Then either:
- Start the daemon - it tries to pair on startup, or
- If the daemon is already running: `pad.pair_fresh()` from the REPL.

### Every subsequent connection

Just run a Mac script. The Pi daemon has the Switch's MAC bonded. If the Pi rebooted, the daemon reconnects on startup. If the Switch was off, wake it - the watchdog reconnects within ~10 seconds.

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

**Daemon needs sudo** - nxbt needs raw HCI access. The systemd unit runs as root; the manual command uses `sudo`. No way around this.

**bluetoothctl D-Bus agent** - the daemon spawns it automatically at startup. If you ever see `Authentication Failure (0x05)` running nxbt outside the daemon, this is why. The daemon handles it transparently.

**Wi-Fi/BT antenna sharing on Pi 4** — the onboard radios share one antenna. Under sustained Bluetooth traffic (agent runs, macro loops), Wi-Fi load causes BT drops → `nxbt state=crashed` reconnect loops. Two fixes:

- **Ethernet (best):** plug in a cable, then `sudo rfkill block wifi && sudo systemctl restart switch-control`. Eliminates contention entirely.
- **USB Bluetooth dongle (~$10):** get a CSR8510-based dongle, plug it in, then disable onboard BT permanently: `echo 'dtoverlay=disable-bt' | sudo tee -a /boot/firmware/config.txt && sudo reboot`. The USB dongle becomes the only BT adapter; Wi-Fi is unaffected. Verify with `hciconfig -a` (one adapter), then restart the daemon.

See the `nxbt state=crashed reconnect loop` troubleshooting entry for diagnosis commands.

**Long macros and reconnects** - if BT drops mid-macro, the endpoint returns `NotConnected` and the macro is lost. After the watchdog reconnects, re-issue it. Break long sequences into chunks wrapped in `pad.run_resilient`.

**mDNS (`raspberrypi.local`)** - if it doesn't resolve from your Mac, use the Pi's IP directly. Find it with `ip addr` on the Pi or your router's DHCP table.

**Switch auto-sleep** - drops the controller bond. Disable for long sessions: `System Settings → Sleep Mode → Auto-Sleep → Never`. The watchdog reconnects after wake, but the interruption is annoying mid-macro.

**Don't mix daemon and old direct scripts** - only one nxbt process can hold the BT adapter. Always use `RemotePad` from Mac scripts. Never import `switch_control.pad.SwitchPad` directly while the daemon is running.

---

## Troubleshooting

### `nxbt state=crashed` reconnect loop (most common during long agent runs)

**Symptom:** Daemon logs repeat `nxbt state=crashed — triggering recovery reconnect` every ~13 seconds, with 503s between each reconnect. The Switch stays connected, macros work briefly, then crash again.

**Cause: Wi-Fi/BT antenna sharing on Pi 4.** The onboard radios share one antenna. SSH traffic (especially tailing daemon logs) saturates Wi-Fi and stomps on the Bluetooth signal. nxbt's internal recovery fails → `state=crashed`. The watchdog reconnects in ~1.5 s and the cycle repeats.

**Immediate check:** look at what `bluez_link_up` says in the warning line. As of the latest daemon version it logs:
```
nxbt state=crashed (bluez_link_up=False) — triggering recovery reconnect
```
If `bluez_link_up=False`, BlueZ also sees the link as down — this confirms a radio-level drop, not just nxbt confusion.

**Fix 1 (recommended): disable Wi-Fi on the Pi and use Ethernet.**
```bash
# SSH in over Ethernet first, then:
sudo rfkill block wifi
sudo systemctl restart switch-control
```
This eliminates antenna contention entirely. Reconnect loops almost always stop immediately.

**Fix 2: if Ethernet isn't available**, keep Wi-Fi on but reduce contention:
```bash
# On the Pi — limit SSH keepalive traffic and reduce scan activity
sudo iwconfig wlan0 power off     # disable Wi-Fi power management (reduces spurious activity)
```

**Mac-side resilience (already in place after recent update):** All agent scripts now call `pad.macro(macro, retries=2, recover_timeout=15.0)`. If a 503 arrives during a reconnect window, the Mac waits up to 15 s for the daemon to recover and retries the macro automatically — the agent loop survives without losing its current goal.

**After fixing the antenna issue**, you can tail daemon logs with less impact by using `journalctl` over SSH rather than a live tmux session:
```bash
# Pull the last 50 lines on demand rather than streaming:
ssh pi "sudo journalctl -u switch-control -n 50 --no-pager"
```

---

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
| daemon unreachable | `ssh pi "sudo systemctl status switch-control"` - daemon probably isn't running. |

### Daemon won't start

```bash
sudo journalctl -u switch-control -n 50    # read the error
rfkill list                                 # if BT is soft-blocked: sudo rfkill unblock bluetooth
systemctl status bluetooth --no-pager
```

### Switch can't see the Pi (first-time pair)

- Change Grip/Order times out after ~3 minutes - re-open it.
- Move the Pi within 30cm of the Switch for the first pair - the Pi 4 antenna is weak.
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
├── BOTW_SKILL.md                    LLM skill reference: DSL syntax, button map, sequences
├── switch_control/
│   ├── __init__.py                  re-exports RemotePad, Buttons, Sticks, scrub_macro
│   ├── client.py                    HTTP client + scrub_macro + extend_macro_to_interval
│   ├── daemon.py                    HTTP server + watchdog (Pi only)
│   └── pad.py                       nxbt wrapper (Pi only, used by daemon)
├── scripts/
│   ├── pi_daemon.py                 entry point: run on the Pi (HTTP daemon)
│   ├── pi_macro_loop.py             run directly on the Pi — cycles attack/defense/explore
│   ├── interactive.py               entry point: run on the Mac, REPL
│   ├── example_handoff.py           handoff demo
│   ├── botw_macros.py               15 BotW macros (town / combat / explore)
│   ├── vision_loop.py               HDMI capture → YOLO → rule-based control
│   ├── agent_loop.py                HDMI capture → YOLO → Claude → macros
│   ├── phi4_agent.py                HDMI capture → YOLO → local Ollama model → macros (REPL)
│   └── collect_frames.py            capture gameplay frames for YOLO training
├── models/
│   └── botw.pt                      custom BotW YOLO model (after training)
├── data/
│   ├── raw/                         self-captured gameplay frames (unannotated)
│   └── compendium/                  reference images from the Hyrule Compendium API
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
| Pi macro loop (no daemon) | `sudo /home/yuvaltimen/nxbt/.venv/bin/python scripts/pi_macro_loop.py` |
| Pi macro loop (one cycle) | `sudo ... pi_macro_loop.py --once` |
| Pi macro loop (start at defense) | `sudo ... pi_macro_loop.py --start defense --break 3` |
| Scan capture devices | `python scripts/vision_loop.py --scan` |
| Vision loop (dry-run) | `python scripts/vision_loop.py --device 1 --dry-run` |
| Vision loop (live) | `CAPTURE_DEVICE=1 python scripts/vision_loop.py` |
| Agent loop (dry-run) | `python scripts/agent_loop.py --goal "..." --dry-run` |
| Agent loop (live) | `CAPTURE_DEVICE=1 python scripts/agent_loop.py --goal "..."` |
| **Local agent (REPL)** | `CAPTURE_DEVICE=1 python scripts/phi4_agent.py` |
| Local agent — switch model | `python scripts/phi4_agent.py --model llama3.1:8b` |
| Local agent — vision mode | `python scripts/phi4_agent.py --model llama3.2-vision:11b --vision` |
| Local agent — custom YOLO | `python scripts/phi4_agent.py --model llama3.1:8b --yolo-model models/botw.pt` |
| Capture training frames | `CAPTURE_DEVICE=1 python scripts/collect_frames.py --output data/raw` |
| Train YOLO model | `yolo train data=dataset.yaml model=yolov8n.pt epochs=50 imgsz=640 device=mps` |
| Agent with custom model | `python scripts/agent_loop.py --yolo-model models/botw.pt --goal "..."` |
| Check connection | `curl http://raspberrypi.local:8765/status` |
| Force reconnect | `curl -X POST http://raspberrypi.local:8765/reconnect` |
| Force fresh pair | `curl -X POST http://raspberrypi.local:8765/pair` (Switch on Change Grip/Order) |
| API docs (browser) | `http://raspberrypi.local:8765/docs` |