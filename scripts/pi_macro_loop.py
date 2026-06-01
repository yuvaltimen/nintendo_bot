"""
Pi-side macro loop — runs nxbt directly, no HTTP daemon involved.

Cycles through an attack macro, a defense macro, and an explore macro
with a 5-second break between each. Loops indefinitely until Ctrl-C.
Handles BT drops by reconnecting automatically before the next macro.

IMPORTANT: the switch-control daemon must NOT be running at the same time.
Only one nxbt process can hold the BT adapter. Stop it first:
    sudo systemctl stop switch-control

Run with the nxbt venv and root (required for raw HCI access):
    sudo /home/yuvaltimen/nxbt/.venv/bin/python scripts/pi_macro_loop.py

Options:
    --once          Run through each macro once instead of looping forever.
    --break N       Seconds between macros (default: 5).
    --start attack|defense|explore
                    Which macro to start with (default: attack).
"""

import argparse
import logging
import signal
import sys
import time

try:
    import nxbt
except ImportError:
    print("nxbt not found. Run with the nxbt venv:")
    print("  sudo /home/yuvaltimen/nxbt/.venv/bin/python scripts/pi_macro_loop.py")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Macros
# ─────────────────────────────────────────────────────────────

ATTACK = """
L_STICK@+000+080 0.4s
0.1s
Y 0.1s
0.3s
Y 0.1s
0.3s
Y 0.1s
0.3s
Y 0.1s
0.4s
L_STICK@-100+000 B 0.15s
0.6s
L_STICK@+000+080 0.3s
Y 0.1s
0.3s
Y 0.1s
0.5s
"""
# Advance on enemy, 4-hit Y combo, sidestep-dodge left, re-engage with 2 hits.
# Setup: face a Bokoblin or similar enemy 3-4 steps away.

DEFENSE = """
ZL 0.1s
ZL 1.5s
ZL A 0.12s
0.5s
Y 0.15s
0.2s
Y 0.15s
0.2s
Y 0.15s
0.2s
0.8s
"""
# Raise shield, hold for incoming attack, parry (ZL+A), 3 counter-strikes.
# Setup: face a melee enemy with a shield equipped. The 1.5s ZL window gives
# time for the enemy to swing — parry timing still depends on enemy cadence.

EXPLORE = """
L_STICK@+000+080 2.0s
L_STICK@+050+060 1.0s
L_STICK@+000+080 1.5s
0.5s
R_STICK@+060+000 1.5s
0.5s
L_STICK@-050+060 1.0s
L_STICK@+000+080 1.5s
0.5s
R_STICK@-060+000 1.5s
0.3s
"""
# Walk forward, drift right, continue, pause to scan camera right, drift left,
# continue, pan camera back left. A low-stress loop for open terrain.
# Setup: stand Link in an open area — a field, stable yard, or road.

MACROS = [
    ("attack",  ATTACK,  "4-hit combo + sidestep + follow-up"),
    ("defense", DEFENSE, "raise shield, parry, 3 counter-strikes"),
    ("explore", EXPLORE, "walk forward with camera pans"),
]
MACRO_NAMES = [m[0] for m in MACROS]


# ─────────────────────────────────────────────────────────────
# Connection helpers
# ─────────────────────────────────────────────────────────────

def connect(nx: nxbt.Nxbt) -> int:
    """Create controller and block until the Switch connects. Returns index."""
    addrs = nx.get_switch_addresses()
    if addrs:
        log.info("Known Switch found at %s — reconnecting.", addrs)
        idx = nx.create_controller(nxbt.PRO_CONTROLLER, reconnect_address=addrs)
    else:
        log.info("No known Switch address. Put the Switch on Controllers → Change Grip/Order.")
        idx = nx.create_controller(nxbt.PRO_CONTROLLER)

    log.info("Waiting for Switch to connect...")
    nx.wait_for_connection(idx)
    log.info("Connected.")
    return idx


def is_connected(nx: nxbt.Nxbt, idx: int) -> bool:
    try:
        return nx.state[idx].get("state") == "connected"
    except Exception:
        return False


def reconnect(nx: nxbt.Nxbt, idx: int) -> int:
    """Remove the current controller and establish a fresh connection."""
    try:
        nx.remove_controller(idx)
    except Exception:
        pass
    time.sleep(0.5)
    return connect(nx)


# ─────────────────────────────────────────────────────────────
# Macro runner
# ─────────────────────────────────────────────────────────────

def run_macro(nx: nxbt.Nxbt, idx: int, name: str, script: str) -> bool:
    """
    Execute one macro. Returns True on completion, False if the connection
    dropped mid-execution (nxbt raises on a dead link).
    """
    log.info("▶  %s", name)
    try:
        nx.macro(idx, script, block=True)
        log.info("✓  %s done.", name)
        return True
    except Exception as e:
        log.warning("✗  %s failed (%s).", name, e)
        return False


def interruptible_sleep(seconds: float, stop: signal.Sigmasks) -> bool:
    """
    Sleep for `seconds`, waking every 100 ms to check whether the stop flag
    has been set. Returns True if the sleep completed, False if interrupted.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if getattr(stop, "_set", False):
            return False
        time.sleep(0.1)
    return True


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cycle through attack / defense / explore macros on the Pi.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--once", action="store_true",
                        help="Run each macro once then exit.")
    parser.add_argument("--break", dest="break_s", type=float, default=5.0,
                        help="Seconds between macros (default: 5).")
    parser.add_argument("--start", choices=MACRO_NAMES, default="attack",
                        help="Which macro to begin with (default: attack).")
    args = parser.parse_args()

    # ── signal handling ────────────────────────────────────────────────────
    _stop = {"flag": False}

    def on_sigint(sig, frame):
        if not _stop["flag"]:
            log.info("Ctrl-C — finishing current macro, then stopping.")
        _stop["flag"] = True

    signal.signal(signal.SIGINT, on_sigint)

    # ── start nxbt ─────────────────────────────────────────────────────────
    log.info("Initialising nxbt...")
    nx = nxbt.Nxbt()
    idx = connect(nx)

    # ── build ordered macro list starting from --start ────────────────────
    start_i = MACRO_NAMES.index(args.start)
    ordered = MACROS[start_i:] + MACROS[:start_i]

    # ── loop ───────────────────────────────────────────────────────────────
    cycle = 0
    try:
        while not _stop["flag"]:
            for name, script, description in ordered:
                if _stop["flag"]:
                    break

                log.info("─── Cycle %d  |  %s  |  %s", cycle + 1, name, description)

                # Reconnect if needed before attempting the macro
                if not is_connected(nx, idx):
                    log.warning("Connection lost before macro — reconnecting.")
                    idx = reconnect(nx, idx)

                success = run_macro(nx, idx, name, script)

                # If the macro itself dropped the link, reconnect for next time
                if not success and not _stop["flag"]:
                    log.warning("Reconnecting after failed macro.")
                    idx = reconnect(nx, idx)

                if _stop["flag"]:
                    break

                log.info("Break: %.1fs before next macro.", args.break_s)
                # Sleep interruptibly so Ctrl-C responds within 100 ms
                deadline = time.time() + args.break_s
                while time.time() < deadline and not _stop["flag"]:
                    time.sleep(0.1)

            cycle += 1
            if args.once:
                log.info("--once set — completed one full cycle, exiting.")
                break

    finally:
        log.info("Removing controller...")
        try:
            nx.remove_controller(idx)
        except Exception:
            pass
        log.info("Done. Pad disconnected.")


if __name__ == "__main__":
    main()
