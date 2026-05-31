# PI_CONTROLLER.md — Bluetooth deep-surgery reference

> **Normal operations live in README.md.** This file covers three things you only need when something is broken:
> 1. Wiping stale BT state on the Pi and the Switch
> 2. Diagnosing and fixing `Authentication Failure (0x05)` bonding failures
> 3. Inspecting raw BT traffic with `btmon`
>
> If you're setting up a fresh Pi, start with README.md §One-time Pi setup — it has the full setup commands. Come back here only if pairing fails.

---

## Wiping stale state

Stale link keys are the most common cause of pairing failures. Always wipe both sides before retrying.

### On the Pi

```bash
sudo pkill -9 -f nxbt
sudo pkill -9 -f 'bin/python.*nxbt'
ps -ef | grep nxbt | grep -v grep        # must print nothing before continuing

sudo systemctl stop bluetooth
sudo rm -rf /var/lib/bluetooth/*/cache
sudo rm -rf /var/lib/bluetooth/*/[0-9A-F]*:*
sudo systemctl start bluetooth
```

Confirm clean state:

```bash
systemctl cat bluetooth | grep ExecStart          # must show --noplugin=input
rfkill list bluetooth                              # Soft blocked: no, Hard blocked: no
bluetoothctl show | grep -E 'Powered|Discoverable'
```

### On the Switch

1. `Controllers → Disconnect Controllers`
2. On that screen, **press and hold L+R simultaneously** until the screen confirms. (A quick tap is not enough — hold until you see the confirmation.)
3. Back out, then go to `Controllers → Change Grip/Order` and leave it open.

---

## Monitoring BT traffic with btmon

`btmon` is an X-ray for the BT stack. Run it in a tmux session whenever something isn't working — it captures everything at the HCI level.

```bash
sudo tmux new -d -s btmon 'btmon 2>&1 | tee /tmp/btmon.log'
```

Useful greps after a failed pair:

```bash
grep -E 'Connect Request|Authentication|Connect Complete' /tmp/btmon.log
grep -E 'Powered|Discoverable|Scan enable' /tmp/btmon.log
```

Interpreting what you see:

| btmon output | Diagnosis |
|---|---|
| No `Connect Request` lines | Switch never saw the Pi — Switch-side or range/coexistence issue |
| `Connect Request` + `Authentication Failure (0x05)` | Stale link keys → wipe both sides and retry |
| `Connect Complete` but demo hangs | NXBT's higher-level HID handshake failed — check nxbt logs for tracebacks |
| `Connect Complete` + `Simple Pairing Complete: Success` then disconnects with reason 3 | This is **normal** when testing with bluetoothctl as agent — the BT stack works, nxbt just doesn't speak HID from bluetoothctl |

---

## Switch can't see the Pi

If `btmon` shows no `Connect Request` after putting the Switch on Change Grip/Order:

- Change Grip/Order times out after ~3 minutes — make sure it's still open.
- Confirm the Pi is actually advertising: `bluetoothctl show | grep -E 'Powered|Discoverable'` should print `yes` for both.
- Move the Pi physically within 30cm of the Switch. The Pi 4's onboard antenna is weak and the first-pair BT handshake is sensitive to distance.
- Run the zombie check — a stale nxbt process silently holds the adapter and blocks advertising.

---

## Authentication Failure (0x05)

This is the failure where the Switch *does* connect (you see `Connect Request` → `Connect Complete` in btmon) but the bonding key exchange fails with `0x05`, and the Switch retries on a ~23-second cadence.

**Always start with the stale-state wipe** (both sides). If that doesn't fix it, apply remediations a–d in order.

### a. Full power-cycle the Switch

`Disconnect Controllers → L+R` sometimes doesn't fully clear bonded entries on certain firmware versions. A power-cycle does.

Hold the Switch's power button 3s → `Power Options → Turn Off`. Wait ~10s. Power on. Then redo the Switch wipe (hold L+R) and retry.

### b. Force "Just Works" repairing in BlueZ

Relaxes BlueZ's bonding flow so it accepts the Switch's re-pair attempts without needing key continuity.

```bash
sudo sed -i 's/^#\?JustWorksRepairing.*/JustWorksRepairing = always/' /etc/bluetooth/main.conf
grep JustWorksRepairing /etc/bluetooth/main.conf       # confirm uncommented

# If the key isn't in the file at all, append it under [General]:
grep -q '^JustWorksRepairing' /etc/bluetooth/main.conf \
  || sudo sed -i '/^\[General\]/a JustWorksRepairing = always' /etc/bluetooth/main.conf

sudo systemctl restart bluetooth
```

### c. Add `--compat` to bluetoothd

Some BlueZ versions need legacy/compat mode for NXBT's bonding agent to register on the correct D-Bus path.

```bash
sudo tee /etc/systemd/system/bluetooth.service.d/override.conf > /dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd --noplugin=input --compat
EOF
sudo systemctl daemon-reload && sudo systemctl restart bluetooth
```

### d. Piggyback on `bluetoothctl`'s D-Bus agent

If a–c don't move the failure code, this is the root cause. NXBT does not register a pairing agent on the BlueZ D-Bus path on newer BlueZ versions. When the Switch initiates bonding, BlueZ asks "who confirms this pair?" and nobody answers — the kernel aborts with `0x05`.

**Confirm the diagnosis:**

With nxbt running and the Switch on Change Grip/Order, in another SSH session:

```bash
sudo busctl --system tree org.bluez | grep -i agent
```

If this prints nothing, NXBT is the problem. (Sanity check: `sudo bluetoothctl` → `agent NoInputNoOutput` → `default-agent` registers an agent and the same `busctl` command lists it.)

**Workaround** — let `bluetoothctl` own the agent while NXBT handles HID:

```bash
# Terminal 1 — keep this prompt open for the entire pairing attempt
sudo bluetoothctl
# at the bluetoothctl prompt:
agent NoInputNoOutput
default-agent
pairable on
```

```bash
# Terminal 2 — kill any stale nxbt, then start the daemon
sudo pkill -9 -f nxbt
sudo pkill -9 -f 'bin/python.*nxbt'
sudo PYTHONUNBUFFERED=1 /home/yuvaltimen/nxbt/.venv/bin/python ~/Coding/nintendo/scripts/pi_daemon.py
```

Switch: go to Change Grip/Order. Watch the `bluetoothctl` prompt — a `Request authorization` prompt may appear; type `yes`. Bonding completes with NXBT driving the HID side. May take two attempts — the first sometimes races and aborts; the second goes through.

**Note:** The daemon already incorporates this workaround by spawning a `bluetoothctl` agent subprocess at startup. You only need the manual steps above if you're testing with nxbt directly (outside the daemon).

After applying any remediation, **always redo the stale-state wipe on both sides** before retrying.

---

## Zombie processes

nxbt's shutdown path has a known multiprocessing bug. When the parent dies, workers get reparented to PID 1 and keep running — still holding the BT adapter. Every subsequent attempt mysteriously hangs at "Waiting for Switch to connect…".

Check before every attempt:

```bash
ps -ef | grep nxbt | grep -v grep
```

Kill anything that shows up:

```bash
sudo pkill -9 -f nxbt
sudo pkill -9 -f 'bin/python.*nxbt'
```

Avoid Ctrl-C on a hung process — it tends to leave orphans. Prefer killing the whole process group from another shell.

**Inspecting a hung nxbt** — if you need to know which Python line it's blocked on:

```bash
sudo apt install -y python3-pip
sudo pip3 install py-spy --break-system-packages
sudo py-spy dump --pid $(sudo pgrep -f 'python.*nxbt' | head -1)
```

---

## Wi-Fi/BT coexistence during first pair

The Pi 4's onboard radios share an antenna. Disabling Wi-Fi for the initial pair removes a common source of dropped handshakes. If you're on Ethernet or USB tether, just `sudo rfkill block wifi` directly. If you're SSH'd over Wi-Fi only, use this safety-net approach:

```bash
# 5-minute auto-unblock so a hang doesn't strand you:
sudo bash -c '(sleep 300; rfkill unblock wifi) &'

# Then block Wi-Fi and start the daemon:
sudo rfkill block wifi
sleep 2
sudo PYTHONUNBUFFERED=1 /home/yuvaltimen/nxbt/.venv/bin/python ~/Coding/nintendo/scripts/pi_daemon.py
```

Your SSH session will die when Wi-Fi blocks. Wait up to 5 minutes — it comes back when the daemon finishes or the safety net fires. After the first successful pair, re-enable Wi-Fi (`sudo rfkill unblock wifi`) and subsequent reconnects will work fine with Wi-Fi on.

---

## Common errors quick-reference

| Error | Likely cause | Fix |
|---|---|---|
| `Address already in use` / `Operation not permitted` PSM 17/19 | `input` plugin still loaded or zombie nxbt | Check `--noplugin=input`, kill zombies |
| `org.bluez.Error.Failed` on `set_powered` | BT radio soft-blocked | `sudo rfkill unblock bluetooth` |
| `Authentication Failure (0x05)` repeating | Stale link keys | Wipe both sides (§Wiping stale state), then apply a–d above |
| `dbus-python` build fails during `pip install nxbt` | Wrong Python version | Confirm `python --version` inside the venv prints `3.11.9` |
| Demo prints nothing when piped to a log | Python output buffering | Use `PYTHONUNBUFFERED=1`, not `stdbuf -oL` |
| `~/...` resolves to `/root` inside `sudo` | `~` expands to root's home under `sudo` | Always use absolute paths (`/home/yuvaltimen/...`) |
| Connects then drops after a few seconds | Wi-Fi/BT coexistence | Block Wi-Fi during first pair (§Wi-Fi/BT coexistence) |
| `systemctl edit bluetooth` won't save | Editor staging issue | Write the override file directly with `tee` |
