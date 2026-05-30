# Raspberry Pi 4 → Nintendo Switch Bluetooth Controller (NXBT)

This guide takes you from a freshly flashed Raspberry Pi 4 to a working Python script that sends controller inputs to a Nintendo Switch over Bluetooth. It is the path that works in 2025/2026 against the current NXBT release and current BlueZ.

The four things that break most attempts:

1. **BlueZ's `input` plugin grabs the HID Control/Interrupt L2CAP ports** (PSMs 17 and 19) before NXBT can bind to them. You have to tell `bluetoothd` not to load that plugin.
2. **NXBT's dependency `dbus-python` does not build on Python 3.12+.** Pin to Python 3.11.9 via `pyenv` — do not rely on the system Python.
3. **NXBT leaves zombie processes behind** after Ctrl+C or after errors. They hold the BT adapter and silently block every subsequent run. Always `sudo pkill -9 -f nxbt` between attempts until the demo works.
4. **Stale pairing state on both the Pi and the Switch** causes `Authentication Failure (0x05)` loops — the Switch retries auto-reconnect to a remembered MAC using an old link key. Wipe both sides before pairing.

Do these things up front and the rest is mechanical.

---

## 0. What you need

- Raspberry Pi 4 (built-in Bluetooth radio is fine).
- A microSD card you are willing to reflash.
- A Nintendo Switch. The Switch must be on the **Change Grip/Order** menu (`Controllers → Change Grip/Order`) for first-time pairing.
- Your laptop, SSH'd into the Pi.
- Optional but recommended: an ethernet cable or USB tether. The first-pair experiment temporarily disables Wi-Fi; ethernet/tether keeps SSH alive. The guide includes a tmux-based workaround if you only have Wi-Fi.

> **Pairing quirk:** the Switch accepts a brand-new controller pairing only from the Change Grip/Order screen. After that, it remembers the controller's MAC and reconnects from anywhere. If something goes sideways mid-pair, use `Controllers → Disconnect Controllers → press and hold L+R` to forget the half-paired controller before retrying.

---

## 1. Flash a fresh image

If you've previously hacked on `bluetoothctl` or `/etc/bluetooth/*`, reflash — it's faster than untangling.

Use the Raspberry Pi Imager on your laptop:

- **OS:** Raspberry Pi OS (64-bit) — Bookworm.
- **Imager → gear icon (advanced options):**
  - Set hostname (e.g. `nxbt-pi`).
  - Enable SSH, password authentication or use your public key.
  - Set username + password.
  - Configure Wi-Fi for your home network.
  - Set locale/timezone.

Boot the Pi, wait ~60 seconds, then from your laptop:

```bash
ssh yuvaltimen@<hostname>.local
```

Update once:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

SSH back in after reboot.

---

## 2. Disable the BlueZ `input` plugin

This is the single most important step. Without it NXBT throws `Address already in use` / `Operation not permitted` on PSM 17 or 19.

Find the path to `bluetoothd` — it differs between Pi OS versions:

```bash
systemctl cat bluetooth | grep ExecStart
```

You will see one of:

- `/usr/libexec/bluetooth/bluetoothd` (Bookworm)
- `/usr/lib/bluetooth/bluetoothd` (Bullseye)

Use **exactly** that path in the override below.

`sudo systemctl edit bluetooth` is the textbook way to add a drop-in, but its editor staging flow is fragile — it sometimes refuses to save with confusing errors like *"Found modifications outside of the staging area"* or *"new contents are empty, not writing file"*. Write the override file directly instead, which produces the identical end state:

```bash
sudo mkdir -p /etc/systemd/system/bluetooth.service.d
sudo tee /etc/systemd/system/bluetooth.service.d/override.conf > /dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd --noplugin=input
EOF
```

(Replace the path on the last `ExecStart=` line if `systemctl cat` showed something different. The empty `ExecStart=` line is required — it clears the inherited command before the new one is set.)

Reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart bluetooth
```

**Verify** — the check most people skip:

```bash
systemctl cat bluetooth | grep ExecStart      # active line must show --noplugin=input
systemctl status bluetooth --no-pager         # active (running), no errors
rfkill list bluetooth                         # Soft blocked: no, Hard blocked: no
bluetoothctl list                             # Controller XX:XX:... [default]
```

If `rfkill` reports a soft block on Bluetooth, clear it:

```bash
sudo rfkill unblock bluetooth
sudo systemctl restart bluetooth
```

> Side effect: while `input` is disabled you cannot use Bluetooth keyboards/mice on this Pi. Fine — you're SSH'd in.

---

## 3. Install Python 3.11.9 via pyenv

`pip install nxbt` builds `dbus-python` from source. That build fails on Python 3.12+. Pin 3.11.9 with pyenv.

Install build dependencies:

```bash
sudo apt update && sudo apt install -y \
  build-essential pkg-config git curl xz-utils \
  bluetooth bluez libbluetooth-dev \
  libdbus-1-dev libglib2.0-dev libssl-dev zlib1g-dev \
  libbz2-dev libreadline-dev libsqlite3-dev \
  libffi-dev liblzma-dev libncursesw5-dev \
  tk-dev libxml2-dev libxmlsec1-dev
```

Install pyenv:

```bash
curl https://pyenv.run | bash

cat >> ~/.bashrc <<'EOF'
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
EOF

source ~/.bashrc
pyenv --version
```

Build Python 3.11.9. Takes **15–25 minutes** on a Pi 4 — go make coffee.

```bash
pyenv install 3.11.9
```

---

## 4. Install NXBT in a venv

```bash
mkdir -p ~/nxbt && cd ~/nxbt
pyenv local 3.11.9
python --version       # should print Python 3.11.9
python -m venv .venv
source .venv/bin/activate
pip install -U pip setuptools wheel
pip install nxbt
nxbt --help
```

If `pip install nxbt` errors on `dbus-python`, you are not on 3.11.9. Run `which python` and `python --version` inside the venv and fix that before continuing.

---

## 5. First pairing — clean-slate end-to-end experiment

This is where most attempts fail. The single combined experiment below addresses every failure mode we've seen in practice:

- **Zombie nxbt processes** from earlier runs holding the BT adapter.
- **Stale link keys** on both Pi and Switch causing `Authentication Failure (0x05)` retry loops.
- **Wi-Fi / Bluetooth coexistence** on the Pi 4's shared antenna.
- **Python output buffering** silently hiding nxbt's status when piped to a log.
- **Pairing timing** — the Switch only accepts new controllers from Change Grip/Order, and the Pi only advertises for 180s after the demo starts.

Run the sub-steps in order, in one sitting.

### 5.1 Wipe stale state on the Pi

```bash
sudo pkill -9 -f nxbt                                  # kill all nxbt processes
sudo pkill -9 -f 'bin/python.*nxbt'                    # belt-and-suspenders for orphaned workers
ps -ef | grep nxbt | grep -v grep                      # MUST print nothing before continuing

sudo systemctl stop bluetooth
sudo rm -rf /var/lib/bluetooth/*/cache                 # clear BlueZ scan cache
sudo rm -rf /var/lib/bluetooth/*/[0-9A-F]*:*           # clear remembered peers
sudo systemctl start bluetooth
```

Confirm clean state:

```bash
systemctl cat bluetooth | grep ExecStart    # active line shows --noplugin=input
rfkill list bluetooth                       # both blocks: no
bluetoothctl show | grep -E 'Powered|Discoverable'
```

### 5.2 Wipe stale state on the Switch

On the Switch:

1. `Controllers → Disconnect Controllers`
2. On that screen, **press and hold L+R simultaneously** until the screen confirms. This wipes every paired controller — a quick tap is not enough.
3. Back out, then go to `Controllers → Change Grip/Order` and **leave that screen open**.

### 5.3 Set up logging + Wi-Fi-off harness

Wi-Fi and Bluetooth on the Pi 4 share an antenna; disabling Wi-Fi for the initial pair removes a common source of dropped handshakes. If you have ethernet/USB-tether, skip the safety-net dance and just `sudo rfkill block wifi` directly. If you only have Wi-Fi for SSH, the tmux approach below preserves the session and self-recovers.

First, a 5-minute safety net so a hang doesn't strand you off Wi-Fi:

```bash
sudo bash -c '(sleep 300; rfkill unblock wifi) &'
```

Start `btmon` in its own tmux session so you can see the BT-level conversation:

```bash
sudo tmux new -d -s btmon 'btmon 2>&1 | tee /tmp/btmon.log'
```

### 5.4 Start the demo

Two non-obvious things in this command:

- `PYTHONUNBUFFERED=1` — **not** `stdbuf -oL`. Python's stdout has its own buffering layer above libc that `stdbuf` doesn't touch; without `PYTHONUNBUFFERED=1`, piping nxbt's output to `tee` produces an empty log even when nxbt is running fine.
- **Absolute path** to the venv binary. `~` expands to `/root` under `sudo`, not your home, so `~/nxbt/.venv/bin/nxbt` will silently fail with "No such file or directory".

(Replace `yuvaltimen` with your username.)
```bash
sudo tmux new -d -s nxbt 'exec > >(tee /tmp/nxbt-demo.log) 2>&1
  set -x
  rfkill block wifi
  sleep 2
  PYTHONUNBUFFERED=1 /home/yuvaltimen/nxbt/.venv/bin/nxbt demo
  echo "demo exited with $?"
  sleep 5
  rfkill unblock wifi
'
```

Your SSH session will die when Wi-Fi blocks. Wait. After up to 5 minutes Wi-Fi comes back (either because the demo completes naturally, or because the safety net fires).

### 5.5 Inspect what happened

SSH back in once Wi-Fi returns. Run all of these:

```bash
# 1. Is the demo still running, or did it crash?
sudo pgrep -af nxbt

# 2. What did nxbt say?
cat /tmp/nxbt-demo.log

# 3. What did the BT stack actually do?
grep -E 'Powered|Discoverable|Scan enable|Connect Request|Authentication' /tmp/btmon.log
```

Interpretation matrix:

| pgrep | nxbt-demo.log | btmon last event | Diagnosis |
| --- | --- | --- | --- |
| empty | traceback | — | nxbt crashed; read the traceback |
| empty | only `Running Demo...` | — | nxbt died silently; check `dmesg` for OOM |
| many python procs | only `Running Demo...` | `Discoverable` reached, no `Connect Request` | Pi advertising fine, Switch can't see / find it. See 5.6. |
| many python procs | only `Running Demo...` | `Connect Request` + `Authentication Failure (0x05)` | Bonding handshake failing. See 5.7. |
| many python procs | demo output past "Running Demo..." | — | Probably worked — check the Switch screen. |

If nxbt is hung and you want the exact Python line it's blocked on, install `py-spy` and dump the stack of the actual Python worker (not the bash wrapper — `pgrep -f 'nxbt.*demo'` returns the bash script first, which py-spy can't read):

```bash
sudo apt install -y python3-pip
sudo pip3 install py-spy --break-system-packages
sudo py-spy dump --pid $(sudo pgrep -f 'python.*nxbt' | head -1)
```

### 5.6 Switch can't see the Pi (no `Connect Request` in btmon)

- Confirm `Change Grip/Order` is still open on the Switch (it times out).
- Confirm Pi is actually advertising: `bluetoothctl show | grep -E 'Powered|Discoverable'` should print `yes` for both.
- Move the Pi physically within 30cm of the Switch for the first pair — the Pi 4's onboard antenna is weak.
- Re-do 5.1 to be sure no zombie is silently holding the adapter.

### 5.7 Bonding fails — persistent `Authentication Failure (0x05)`

This is the failure mode where the Switch *does* connect (you see `Connect Request` → `Connect Complete` in btmon), but the bonding key exchange always fails with status `0x05`, and the Switch retries on a ~23s cadence. The diagnostic-by-elimination is:

- If 5.1 + 5.2 fix it → it was stale state.
- If 5.1 + 5.2 do not fix it → it's a BlueZ/NXBT bonding-flow incompatibility. Apply remediations a/b/c in order until btmon shows a different auth result.

**a. Full power-cycle the Switch.** "Disconnect Controllers → L+R" sometimes doesn't fully clear bonded entries on certain firmware versions; a power-cycle does. Hold the Switch's power button for 3s → Power Options → Turn Off. Wait ~10s. Power on. Then redo 5.2 (L+R) and try again.

**b. Force "Just Works" repairing in BlueZ.** This relaxes BlueZ's bonding flow so it'll accept the Switch's re-pair attempts without needing key continuity:

```bash
sudo sed -i 's/^#\?JustWorksRepairing.*/JustWorksRepairing = always/' /etc/bluetooth/main.conf
grep JustWorksRepairing /etc/bluetooth/main.conf       # confirm uncommented
# If the key isn't in the file at all, append it under [General]:
grep -q '^JustWorksRepairing' /etc/bluetooth/main.conf \
  || sudo sed -i '/^\[General\]/a JustWorksRepairing = always' /etc/bluetooth/main.conf
sudo systemctl restart bluetooth
```

**c. Add `--compat` to bluetoothd.** Some BlueZ versions need legacy/compat mode for NXBT's bonding agent to register correctly on the D-Bus path NXBT expects:

```bash
sudo tee /etc/systemd/system/bluetooth.service.d/override.conf > /dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd --noplugin=input --compat
EOF
sudo systemctl daemon-reload
sudo systemctl restart bluetooth
```

**d. Piggyback on `bluetoothctl`'s D-Bus agent.** If a/b/c don't move the failure code, this is almost certainly the root cause. NXBT does not register a pairing agent on the BlueZ D-Bus path on newer BlueZ versions, so when the Switch initiates bonding, BlueZ asks "who confirms this pair?" and nobody answers — the kernel aborts with `Authentication Failure (0x05)`. Confirm the diagnosis first, then apply the workaround.

Confirm: with nxbt running and the Switch on `Change Grip/Order`, in another SSH session:

```bash
sudo busctl --system tree org.bluez | grep -i agent
```

If this prints nothing, NXBT is the problem, not BlueZ or the Switch. (Sanity check: `sudo bluetoothctl` → `agent NoInputNoOutput` → `default-agent` registers an agent and the same `busctl` command will then list it; bonding with bluetoothctl as agent completes with `Simple Pairing Complete: Success, Bonded: yes`, then disconnects with reason 3 because bluetoothctl doesn't speak HID — that's expected and confirms the stack itself is fine.)

Workaround — let `bluetoothctl` own the agent while NXBT handles HID:

```bash
# Terminal 1 — leave this prompt open for the entire pairing attempt
sudo bluetoothctl
# at the bluetoothctl prompt:
agent NoInputNoOutput
default-agent
pairable on
```

```bash
# Terminal 2 — kill any stale nxbt, then start the demo
sudo pkill -9 -f nxbt
sudo pkill -9 -f 'bin/python.*nxbt'
sudo PYTHONUNBUFFERED=1 /home/yuvaltimen/nxbt/.venv/bin/nxbt demo
```

On the Switch: `Change Grip/Order`. Watch the `bluetoothctl` prompt — a `Request authorization` / `Accept pairing (yes/no):` prompt may appear; type `yes` (the `NoInputNoOutput` capability usually auto-accepts). Bonding completes with NXBT driving the HID side. May take two tries — the first attempt sometimes races and aborts; the second goes through.

After a/b/c/d, **redo 5.1 + 5.2 first**, then rerun 5.3/5.4. Compare the new `btmon.log` auth section to the previous one — even a different failure code is progress.

Once the demo works once, the Switch remembers the Pi's MAC and subsequent reconnects are tolerant of imperfect conditions. You can re-enable Wi-Fi after the first successful pair.

---

## 6. Drive the Switch from your own Python script

Create `~/nxbt/drive.py`:

```python
import time
import nxbt

nx = nxbt.Nxbt()

# First time pairing? Have Change Grip/Order open.
# After the first pair, reconnect via the remembered Switch MAC:
addrs = nx.get_switch_addresses()
if addrs:
    controller_idx = nx.create_controller(
        nxbt.PRO_CONTROLLER,
        reconnect_address=addrs,
    )
else:
    controller_idx = nx.create_controller(nxbt.PRO_CONTROLLER)

nx.wait_for_connection(controller_idx)
print("Connected to Switch")

time.sleep(2)

# Press A for 0.1s (default press/release timing)
nx.press_buttons(controller_idx, [nxbt.Buttons.A])
time.sleep(0.5)

# Hold B for 1 second
nx.press_buttons(controller_idx, [nxbt.Buttons.B], down=1.0)
time.sleep(0.5)

# Tilt the left stick fully right for 1 second
nx.tilt_stick(
    controller_idx,
    nxbt.Sticks.LEFT_STICK,
    x=100, y=0,
    tilted=1.0,
)

# Run a macro: A then wait then B
macro = """
A 0.1s
0.5s
B 0.1s
"""
macro_id = nx.macro(controller_idx, macro, block=True)

nx.remove_controller(controller_idx)
print("Done")
```

Run it (still needs root, still needs the venv interpreter, still use absolute paths):

```bash
sudo /home/yuvaltimen/nxbt/.venv/bin/python /home/yuvaltimen/nxbt/drive.py
```

Don't run `python drive.py` after `sudo su` — that loses the pyenv shim. Always invoke the venv's interpreter by absolute path under `sudo`.

---

## 7. Reference

### Controller types

- `nxbt.PRO_CONTROLLER` — most compatible, use this by default
- `nxbt.JOYCON_L`
- `nxbt.JOYCON_R`

### Buttons (`nxbt.Buttons.*`)

`A`, `B`, `X`, `Y`, `L`, `R`, `ZL`, `ZR`, `JCL_SR`, `JCL_SL`, `JCR_SR`, `JCR_SL`, `PLUS`, `MINUS`, `HOME`, `CAPTURE`, `DPAD_UP`, `DPAD_DOWN`, `DPAD_LEFT`, `DPAD_RIGHT`, `L_STICK_PRESS`, `R_STICK_PRESS`.

Pass them as a list: `nx.press_buttons(idx, [nxbt.Buttons.A, nxbt.Buttons.B])` to press multiple simultaneously.

### Sticks (`nxbt.Sticks.*`)

`LEFT_STICK`, `RIGHT_STICK`. X and Y range from `-100` to `100`.

### Macros

Multi-line string, one action per line. Buttons on a line are pressed together; a bare duration releases everything.

```
A B 0.2s
0.1s
DPAD_UP 0.5s
```

Call with `nx.macro(controller_idx, macro_str, block=True)`.

---

## 8. Troubleshooting deep dive

### Inspecting BT traffic with btmon

`btmon` is your X-ray. Run it in a separate tmux session whenever something isn't working:

```bash
sudo tmux new -d -s btmon 'btmon 2>&1 | tee /tmp/btmon.log'
```

Useful greps after a failed pair:

```bash
grep -E 'Connect Request|Authentication|Connect Complete' /tmp/btmon.log
grep -E 'Powered|Discoverable|Scan enable' /tmp/btmon.log
```

Outcomes:

- No `Connect Request` lines → Switch never saw the Pi. Switch-side or coexistence issue.
- `Connect Request` + `Connect Complete` + `Authentication Failure` → stale link keys. Redo 5.1 + 5.2.
- `Connect Complete` with no auth failure but demo still hangs → NXBT's higher-level handshake failed; check `nxbt-demo.log` for tracebacks.

### Zombie processes

`nxbt`'s shutdown path has a known multiprocessing bug (`TypeError: ...takes 1 positional argument but 2 were given`). When the parent dies, the multiprocessing workers get reparented to PID 1 and keep running — still holding the BT adapter. Every subsequent attempt will mysteriously hang at "Waiting for Switch to connect…".

Always check before starting an attempt:

```bash
ps -ef | grep nxbt | grep -v grep
```

If anything shows up, kill it:

```bash
sudo pkill -9 -f nxbt
sudo pkill -9 -f 'bin/python.*nxbt'
```

Avoid Ctrl+C on a hung demo — it tends to leave orphans. Prefer killing the whole process group from another shell.

### Common errors

**`Address already in use` / `Operation not permitted` on PSM 17 or 19**
The `input` plugin is still loaded, or a zombie nxbt is holding the ports. Re-check step 2 and run the pkill above.

**`org.bluez.Error.Failed` on `set_powered`**
BlueZ can't power the radio. Run `rfkill list` — if soft-blocked, `sudo rfkill unblock bluetooth`. If hard-blocked, check `/boot/firmware/config.txt` for `dtoverlay=disable-bt`.

**`Authentication Failure (0x05)` repeating in btmon**
Stale link keys on the Switch and/or Pi. Redo 5.1 + 5.2.

**`dbus-python` build fails during `pip install nxbt`**
Wrong Python. Confirm `python --version` inside the venv prints `3.11.9`.

**Demo prints nothing when piped to a log**
Python output buffering. Use `PYTHONUNBUFFERED=1`, not `stdbuf -oL` — Python's stdout buffering layer sits above libc and `stdbuf` doesn't reach it.

**`~/...` resolves to `/root` inside `sudo`**
`~` expands to root's home under `sudo`, not yours. Use absolute paths inside any sudo'd script or heredoc.

**Connects, then drops after a few seconds**
Wi-Fi/Bluetooth coexistence on the Pi 4's shared radio. Re-pair with Wi-Fi blocked (5.3 / 5.4), or move the Pi away from 2.4GHz sources during the pair.

**`systemctl edit bluetooth` won't save** (e.g. *"new contents are empty, not writing file"*)
Editor staging issue. Skip `systemctl edit` and write `/etc/systemd/system/bluetooth.service.d/override.conf` directly with `tee` (step 2).

**Demo works, your script doesn't**
You're not running under `sudo` with the venv's interpreter. Always `sudo /home/yuvaltimen/nxbt/.venv/bin/python your_script.py`. Running `python your_script.py` after `sudo su` loses the pyenv shim.

---

## 9. What "done" looks like

From your laptop:

```bash
ssh user@nxbt-pi.local
sudo /home/yuvaltimen/nxbt/.venv/bin/python /home/yuvaltimen/nxbt/drive.py
```

…and the Switch reacts. That's the whole goal.
