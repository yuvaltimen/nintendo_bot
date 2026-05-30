# Switch Control

A tiny Python wrapper around [nxbt](https://github.com/Brikwerk/nxbt) for scripting a Nintendo Switch from a Raspberry Pi. The point of this repo is fast iteration: explore live in a REPL, then promote what works into a replayable script — all while your real joycons stay paired in parallel so you can drive manually whenever the script isn't.

For the Pi-side prep (BlueZ config, the bluetoothctl D-Bus agent workaround, what to do when bonding fails), see `PI_CONTROLLER.md`. This README assumes that's done and your Pi can already pair with your Switch.

## How this works (the one idea)

The Switch accepts inputs from every paired controller at once. We pair a virtual Pro Controller from the Pi via nxbt; you keep your real joycons paired too. At any instant, whichever side is sending inputs wins. So:

- **Script idling** (blocked on `input()` or sitting at a REPL prompt) → only your joycons drive.
- **Script running a macro / `press` / `tilt`** → the script drives; if you're also pressing joycon buttons at the same time, both inputs are merged (this is usually undesirable — let the script have the floor).

That's the whole model. "Handoff" is just the script pausing on a prompt.

## Two modes

### Mode 1: Interactive REPL — for exploring

`scripts/interactive.py` pairs once, then drops you into a Python REPL with `pad`, `Buttons`, and `Sticks` already bound. Each line you type fires immediately. This is the right mode when:

- You don't yet know which exact buttons / timing produce the result you want.
- You're learning the macro DSL.
- You want to poke around in a game state and feel out the latency.

### Mode 2: Handoff scripts — for replaying

`scripts/example_handoff.py` is a normal Python script: pair, do a sequence, exit. Use `pad.wait_for_ready("...")` to pause between sections so you can drive with joycons (navigate menus, recover from unexpected state, position your character) before letting the script fire the next macro. This is the right mode when:

- You have a working sequence and want to replay it reliably.
- The full automation is too long to type into a REPL.
- You're building a test or farming loop.

**Typical flow:** prototype the moves in interactive mode, then paste them into a handoff script once they work.

## Quickstart (Switch state required at each step)

### One-time per Switch: first pair

This is the only step that requires a specific screen on the Switch.

On the **Switch**:
1. Controllers → Disconnect Controllers → hold **L+R** until confirmed (wipes paired controllers; a tap isn't enough).
2. Back out → Controllers → **Change Grip/Order** → leave that screen open.

On the **Pi** (SSH in from your Mac):
```bash
sudo PYTHONUNBUFFERED=1 /home/yuvaltimen/nxbt/.venv/bin/python scripts/interactive.py
```

Within ~10s the Switch should show a new Pro Controller and the REPL prompt appears. You're paired. From this point forward, the Pi remembers the Switch's MAC and reconnects without Change Grip/Order.

If bonding fails with `Authentication Failure (0x05)`, see `PI_CONTROLLER.md` §5.7 — most likely you need the bluetoothctl D-Bus agent workaround (§5.7d) running in a second SSH session.

### Every subsequent run: just go

Now the Switch can be on any screen — home menu, in-game, wherever. Re-pair your joycons normally (if they've fallen asleep, press the SYNC button). Then on the Pi:

```bash
sudo PYTHONUNBUFFERED=1 /home/yuvaltimen/nxbt/.venv/bin/python scripts/interactive.py
```

Reconnect is fast (~2s). Switch shows the Pi pad rejoining. No screen prep needed.

## Running interactive mode

Once paired and at the `>>>` prompt:

```python
>>> pad.press(Buttons.A)
>>> pad.press(Buttons.A, hold=2.0)           # hold A for 2 seconds
>>> pad.press(Buttons.L, Buttons.R)          # combo
>>> pad.tilt(Sticks.LEFT_STICK, x=100, duration=1.0)   # full right for 1s, auto-recenter
>>> pad.macro("""
... A 0.1s
... 0.5s
... B 0.1s
... """)
>>> pad.sleep(2)
```

Exit cleanly with **Ctrl-D**. This removes the virtual controller — the Switch will show "Pro Controller disconnected". Your joycons are unaffected.

**Tip:** while at the REPL prompt, your joycons are completely free. Use them to navigate to whatever game state you want to test from, then type your next command at the REPL.

## Running handoff scripts

```bash
sudo PYTHONUNBUFFERED=1 /home/yuvaltimen/nxbt/.venv/bin/python scripts/example_handoff.py
```

The script pairs, then prints prompts like `Use your joycons to get into BOTW. Enter when ready for the script.` — drive your joycons until you're in position, press Enter, script runs its next chunk, repeats.

## The dev loop (Mac → Pi)

You edit on Mac (since this is the machine you mentioned). The Pi runs the code. `nxbt` doesn't install on macOS, so don't try to run anything locally — `python switch_control.py` on Mac will fail at the `import nxbt` line. That's fine; the Mac is purely the editor.

### One-time: an SSH alias

In `~/.ssh/config` on your Mac:

```
Host pi
    HostName <pi-ip-or-mdns-name>
    User yuvaltimen
```

### One-time: a sync helper

Pick one. Both are fine.

**Option A — rsync from Mac (re-run after every edit):**
```bash
rsync -av --delete --exclude='__pycache__' --exclude='.venv' --exclude='.git' \
  /Users/yuvaltimen/Coding/nintendo/ pi:~/nintendo/
```
Wrap it in a shell function so it's one keystroke:
```bash
# in ~/.zshrc on the Mac
swsync() { rsync -av --delete --exclude='__pycache__' --exclude='.venv' --exclude='.git' \
  /Users/yuvaltimen/Coding/nintendo/ pi:~/nintendo/ ; }
```
Then `swsync` after every edit.

**Option B — edit directly over SSHFS or VS Code Remote SSH.** Same effect; the Pi sees your changes immediately. VS Code Remote-SSH is the smoothest if you don't mind the dependency.

### One-time: a run helper on the Pi

On the Pi, in `~/.bashrc`:
```bash
alias swrun='sudo PYTHONUNBUFFERED=1 /home/yuvaltimen/nxbt/.venv/bin/python'
```
Then runs become `cd ~/nintendo && swrun scripts/interactive.py`.

### The iteration cycle

1. Edit `scripts/foo.py` on Mac.
2. `swsync` on Mac.
3. On the Pi (in an SSH session): `swrun scripts/foo.py`.
4. Watch what happens on the Switch.
5. Adjust, repeat. Cycle time is ~3 seconds.

For exploratory work, skip steps 1–2 and just run `interactive.py` once — type new commands directly at the REPL until you find a sequence that works, then copy the lines into a script.

## When to use what

| Situation | Mode |
|---|---|
| "What does B do on this screen?" | Interactive |
| "I have no idea what timing to use" | Interactive |
| "Replay this 40-step BOTW menu sequence" | Handoff script |
| "Farm rupees in a loop overnight" | Handoff script (with `while True:`) |
| "Get the character to a specific room, then automate" | Handoff script with `wait_for_ready` checkpoints; drive there with joycons, then hit Enter |
| "Test that X then Y produces Z" | Handoff script |

## Awkward UX — read this once

These are the things that will bite you. None are bugs in our code; they're consequences of how nxbt + sudo + the Switch interact.

### Must run from a real interactive terminal

Both modes use `input()` / a REPL, which need a tty. **Do not** wrap these in `tmux new -d -s ...` (detached) — `input()` will hang forever with no prompt visible. If you want a persistent session, use `tmux new -s sw` (attached) inside your SSH session; you can detach with `Ctrl-b d` and reattach with `tmux attach -t sw` later.

### sudo is non-negotiable; PATH does weird things under it

`nxbt` needs root for BlueZ HCI access. But `sudo` resets `PATH` and `$HOME`, so:
- `~/nxbt/...` expands to `/root/nxbt/...` under sudo, which doesn't exist. **Always use absolute paths.**
- `python scripts/interactive.py` under sudo finds system python, not your venv. **Always invoke the venv's binary directly.**

This is why every run command in this README is `sudo PYTHONUNBUFFERED=1 /home/yuvaltimen/nxbt/.venv/bin/python ...` — every piece of that prefix matters.

### `PYTHONUNBUFFERED=1` matters

Python's stdout buffers in ways `stdbuf` can't fix. If you pipe to `tee` or run inside tmux without this, your prints and REPL prompts can appear in chunks or not at all. Always include it.

### `pair()` blocks forever if it fails

If the Switch can't see the Pi, `pad.pair()` hangs indefinitely with no error. Ctrl-C to bail. Then check:
- Is `Change Grip/Order` actually open on the Switch (first-time pair only)?
- Has the Switch gone to sleep? Wake it up.
- Is something else holding the BT adapter? See `PI_CONTROLLER.md` §5.1.
- Bonding failing with `Authentication Failure (0x05)`? See §5.7.

### Stick tilts block for the duration

`pad.tilt(Sticks.LEFT_STICK, x=100, duration=1.0)` doesn't return for 1 second. If you need to press a button *while* a stick is held, use a macro:

```python
pad.macro("""
    LEFT_STICK@+100+000 1.0s A 0.1s
""")
```

(See nxbt's macro DSL for the full syntax.)

### macro DSL newlines matter

Each line is one timestep. `A 0.1s` and `B 0.1s` on the same line is a different thing from being on separate lines. When in doubt:
- One button-press-then-wait per line.
- A bare duration on its own line (`0.5s`) is a pause.
- Indentation inside triple-quoted strings is fine; the parser tolerates leading whitespace.

### Phantom controllers in the Switch's controller list

If you re-pair without disconnecting first, you can end up with two "Pro Controller" entries in the Switch's controller list — the live one and a stale ghost. Harmless, but if you hit the 8-controller limit, do an L+R wipe (see first-pair steps) to clear them.

### The Switch sleeping kills the bond

If the Switch goes to sleep mid-script (auto-sleep timer, or you press the power button), the Bluetooth link drops and your next `pad.press(...)` will silently do nothing. The script keeps running but the Switch isn't receiving anything. Wake the Switch back up; the Pi pad usually reconnects within a few seconds, but if not, exit the script and re-run it.

For long-running scripts (rupee farming, idle gameplay), disable auto-sleep on the Switch: System Settings → Sleep Mode → Auto-Sleep → Never.

### Ctrl-C during a macro

A blocking `pad.macro(..., block=True)` running on the main thread will swallow your first Ctrl-C until the macro finishes (nxbt's macro runner doesn't propagate the signal cleanly). If a runaway macro is in flight, Ctrl-C twice or just kill the SSH session — `__exit__` will not run, so the controller stays "connected" from the Switch's POV for ~30s until it times out. No real harm; just wait it out before re-running.

## Writing your own script

The boilerplate is small:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_control import SwitchPad, Buttons, Sticks

with SwitchPad() as pad:
    pad.pair()

    pad.wait_for_ready("Get the Switch to the starting state. Enter to run. ")

    # ... your sequence here ...

    pad.wait_for_ready("Done. Enter to exit. ")
```

The `sys.path.insert` line is only needed because scripts live in `scripts/` while the library is in the repo root. Save your file as `scripts/<whatever>.py`, sync, run.

### Patterns that come up

**Repeat a sequence N times:**
```python
for _ in range(10):
    pad.press(Buttons.A)
    pad.sleep(0.2)
```

**Conditional checkpoints:**
```python
pad.macro("A 0.1s\n2s")
pad.wait_for_ready("Did the menu open correctly? If yes, Enter. If no, fix it manually and then Enter. ")
pad.press(Buttons.A)
```

**Tilt + button at the same instant:** use a macro (see "stick tilts block" above).

**Run something until you stop it:** wrap in `while True:` and Ctrl-C to exit. The `with` block's `__exit__` will tear down the controller cleanly.

## File map

```
nintendo/
├── README.md                    # this file
├── PI_CONTROLLER.md             # Pi-side BT setup + troubleshooting
├── switch_control.py            # the SwitchPad library
└── scripts/
    ├── interactive.py           # pair → REPL
    └── example_handoff.py       # pair → manual → script → manual → ...
```

That's it. Edit, `swsync`, `swrun`. Iterate.
