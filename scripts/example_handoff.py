"""Demonstrates the script <-> joycon handoff pattern.

Both controllers stay paired the entire time. The script just pauses on
`wait_for_ready` while you drive with joycons; you hit Enter to let the script
fire its macros, then it pauses again to give control back.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_control import SwitchPad, Buttons, Sticks  # noqa: E402

with SwitchPad() as pad:
    pad.pair()

    pad.wait_for_ready("Use your joycons to get into BOTW. Enter when ready for the script. ")

    pad.press(Buttons.A)
    pad.sleep(0.5)
    pad.macro(
        """
        DPAD_DOWN 0.1s
        0.3s
        A 0.1s
        0.5s
        B 0.1s
        """
    )

    pad.wait_for_ready("Script paused. Drive with joycons. Enter to run the next sequence. ")

    pad.tilt(Sticks.LEFT_STICK, x=100, y=0, duration=1.0)
    pad.sleep(0.3)
    pad.press(Buttons.A, hold=0.5)

    pad.wait_for_ready("Done. Enter to exit (Switch stays paired for next time). ")
