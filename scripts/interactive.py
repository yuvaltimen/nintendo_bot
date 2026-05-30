"""Pair-via-daemon, then drop into a Python REPL on your Mac.

Run from your Mac (no SSH, no rsync needed once daemon is up on the Pi):
    PI_HOST=pi.local python scripts/interactive.py

The REPL gets `pad`, `Buttons`, `Sticks` pre-bound; commands hit the Pi over HTTP.
"""

import code
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_control import RemotePad, Buttons, Sticks  # noqa: E402

host = os.environ.get("PI_HOST", "raspberrypi.local")
port = int(os.environ.get("PI_PORT", "8765"))

pad = RemotePad(host, port)

print(f"Connecting to daemon at {host}:{port}...")
pad.wait_connected(timeout=15)
print("Daemon reports connected.")
print("Try: pad.press(Buttons.A)  /  pad.macro('A 0.1s\\n0.5s\\nB 0.1s')")
print("Status: pad.status()  /  Recover: pad.reconnect()")
print("Ctrl-D to exit.\n")

code.interact(local={"pad": pad, "Buttons": Buttons, "Sticks": Sticks})
