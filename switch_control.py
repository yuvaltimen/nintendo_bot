"""Ergonomic wrapper around nxbt. Pi-only — imports nxbt at module load."""

import time

import nxbt

Buttons = nxbt.Buttons
Sticks = nxbt.Sticks
PRO_CONTROLLER = nxbt.PRO_CONTROLLER
JOYCON_L = nxbt.JOYCON_L
JOYCON_R = nxbt.JOYCON_R


class SwitchPad:
    """Virtual Pro Controller that coexists with real joycons paired in parallel."""

    def __init__(self, controller_type=PRO_CONTROLLER):
        self.nx = nxbt.Nxbt()
        self.controller_type = controller_type
        self.idx = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.idx is not None:
            # Swallow: the Switch may have already torn down the link.
            try:
                self.nx.remove_controller(self.idx)
            except Exception:
                pass

    def pair(self, reconnect=True):
        """Create the virtual controller and block until the Switch bonds."""
        kwargs = {}
        if reconnect:
            addrs = self.nx.get_switch_addresses()
            if addrs:
                kwargs["reconnect_address"] = addrs
        self.idx = self.nx.create_controller(self.controller_type, **kwargs)
        self.nx.wait_for_connection(self.idx)
        return self

    def wait_for_ready(self, prompt="Press Enter when the script should take over... "):
        """Idle the script. Joycons drive the Switch while this is blocking."""
        try:
            input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()

    def press(self, *buttons, hold=0.1):
        """Press a button (or combo) for `hold` seconds."""
        self.nx.press_buttons(self.idx, list(buttons), down=hold)

    def tilt(self, stick, x=0, y=0, duration=0.5):
        """Tilt a stick to (x, y) in [-100..100] for `duration` seconds, then recenter."""
        self.nx.tilt_stick(self.idx, stick, x=x, y=y, tilted=duration)

    def macro(self, script, block=True):
        """Run an nxbt macro DSL string (e.g. 'A 0.1s\\n0.5s\\nB 0.1s')."""
        return self.nx.macro(self.idx, script, block=block)

    def sleep(self, seconds):
        """time.sleep, re-exposed so scripts don't need their own import."""
        time.sleep(seconds)
