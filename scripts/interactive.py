"""Pair, then drop into a Python REPL with `pad` already bound.

Best UX for live experimentation: stays connected as long as the REPL is open;
your joycons drive whenever you're not typing a command.
"""

import code
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_control import SwitchPad, Buttons, Sticks  # noqa: E402

with SwitchPad() as pad:
    pad.pair()
    print("\nPaired. Try: pad.press(Buttons.A)  /  pad.macro('A 0.1s\\n0.5s\\nB 0.1s')")
    print("Ctrl-D to exit.\n")
    code.interact(local={"pad": pad, "Buttons": Buttons, "Sticks": Sticks})
