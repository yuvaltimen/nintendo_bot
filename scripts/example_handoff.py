"""Handoff demo over the daemon. Runs on your Mac; Pi is the BT controller.

Set PI_HOST=<your-pi-hostname-or-ip> before running:
    PI_HOST=pi.local python scripts/example_handoff.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_control import RemotePad, Buttons, Sticks  # noqa: E402

host = os.environ.get("PI_HOST", "pi.local")
pad = RemotePad(host)

pad.wait_connected(timeout=15)

pad.wait_for_ready("Use your joycons to get into BOTW. Enter when ready for the script. ")

pad.press(Buttons.A)
pad.sleep(0.5)
pad.macro(
    """
 L_STICK@+000+100 B 0.7s                                                                                    
L_STICK@+000+100 B X 0.1s
L_STICK@+000+100 0.5s                                                                                        
L_STICK@+000+100 X 0.1s
LOOP 2                                                                                                       
    L_STICK@-080+080 1.2s                                  
    L_STICK@+080+080 1.2s                                                                                    
L_STICK@+000+100 0.8s
    """
)

pad.wait_for_ready("Script paused. Drive with joycons. Enter to run the next sequence. ")

pad.tilt(Sticks.LEFT_STICK, x=100, y=0, duration=1.0)
pad.sleep(0.3)
pad.press(Buttons.A, hold=0.5)

pad.wait_for_ready("Done. Enter to exit (Switch stays paired for next time). ")
