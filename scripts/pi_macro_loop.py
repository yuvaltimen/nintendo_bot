"""
Pi-side macro loop — runs nxbt directly, no HTTP daemon involved.

Cycles through an attack macro, a defense macro, and an explore macro
with a 5-second break between each. Loops indefinitely until Ctrl-C.

Start-up sequence
─────────────────
1. Initialise nxbt and create the virtual Pro Controller.
2. Wait for the Switch to accept the connection (with live status + timeout).
3. Send a silent test input to confirm the controller is active in-game,
   not just connected at the Bluetooth level.
4. Prompt you to confirm in-game and position Link before the loop fires.
5. Run the macro cycle.

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
    --timeout N     Seconds to wait for the Switch to connect (default: 60).
    --no-prompt     Skip the handoff prompt — start the loop immediately after
                    the test input succeeds. Useful for unattended runs.
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

# Silent test: hold all inputs neutral for 400ms. The Switch registers this
# as controller activity without moving Link or pressing anything visible.
_TEST_INPUT = "0.4s"

MACROS = [
    ("attack",  ATTACK,  "4-hit combo + sidestep + follow-up"),
    ("defense", DEFENSE, "raise shield, parry, 3 counter-strikes"),
    ("explore", EXPLORE, "walk forward with camera pans"),
]
MACRO_NAMES = [m[0] for m in MACROS]


# ─────────────────────────────────────────────────────────────
# Connection helpers
# ─────────────────────────────────────────────────────────────

def _nxbt_state(nx: nxbt.Nxbt, idx: int) -> str:
    try:
        return nx.state[idx].get("state", "unknown")
    except Exception:
        return "unknown"


def connect(nx: nxbt.Nxbt, timeout: float = 60.0) -> int:
    """
    Create the virtual Pro Controller and wait for the Switch to accept it.

    Polls nxbt state every 250 ms (instead of blocking in wait_for_connection)
    so we can print live status updates and respect the timeout.

    For reconnect (Switch previously paired):
      - Wake the Switch — press any button on your joycons.
      - The controller reconnects automatically; no Switch-side UI needed.

    For first-time pair:
      - Put the Switch on: Controllers → Change Grip/Order.
    """
    addrs = nx.get_switch_addresses()
    if addrs:
        log.info("Known Switch at %s — reconnecting.", addrs)
        log.info("  → Wake the Switch if it's asleep (press any joycon button).")
        idx = nx.create_controller(nxbt.PRO_CONTROLLER, reconnect_address=addrs)
    else:
        log.info("No known Switch address — first-time pair.")
        log.info("  → Put the Switch on: Controllers → Change Grip/Order")
        idx = nx.create_controller(nxbt.PRO_CONTROLLER)

    log.info("Waiting for Switch to accept the controller (timeout: %ds)...", int(timeout))

    deadline = time.time() + timeout
    last_status_t = 0.0

    while time.time() < deadline:
        state = _nxbt_state(nx, idx)

        if state == "connected":
            log.info("Switch accepted the controller (nxbt state=connected).")
            return idx

        if state == "crashed":
            raise RuntimeError(
                "nxbt crashed during connection attempt. "
                "Check for zombie processes: ps aux | grep nxbt"
            )

        # Print a status line every 5 seconds so it's clear we're still trying
        if time.time() - last_status_t >= 5.0:
            elapsed = int(timeout - (deadline - time.time()))
            log.info("  Still waiting... state=%s  (%ds elapsed)", state, elapsed)
            last_status_t = time.time()

        time.sleep(0.25)

    raise TimeoutError(
        f"Switch did not connect within {timeout}s.\n"
        "  - Is the Switch powered on?\n"
        "  - Reconnect: press any button on your joycons to wake it.\n"
        "  - First-time pair: go to Controllers → Change Grip/Order.\n"
        "  - Check for zombie nxbt processes: ps aux | grep nxbt"
    )


def is_connected(nx: nxbt.Nxbt, idx: int) -> bool:
    return _nxbt_state(nx, idx) == "connected"


def reconnect(nx: nxbt.Nxbt, idx: int, timeout: float = 60.0) -> int:
    """Remove the current controller and establish a fresh connection."""
    try:
        nx.remove_controller(idx)
    except Exception:
        pass
    time.sleep(0.5)
    return connect(nx, timeout=timeout)


def verify_active(nx: nxbt.Nxbt, idx: int) -> bool:
    """
    Send a silent test input (neutral state, no buttons) to confirm the
    controller is accepted by the Switch as an active input device — not
    just connected at the Bluetooth/nxbt level.

    A real joycon may already be the active controller. This test confirms
    the emulated Pro Controller is also in the Switch's active controller
    list. The neutral input is invisible: it doesn't move Link or press
    anything.

    Returns True if the Switch acknowledged the input, False on failure.
    """
    log.info("Sending silent test input to verify controller is active in-game...")
    try:
        nx.macro(idx, _TEST_INPUT, block=True)
        log.info("Test input acknowledged — emulated Pro Controller is active.")
        return True
    except Exception as e:
        log.warning("Test input failed (%s).", e)
        return False


def handoff_prompt() -> None:
    """
    Pause and wait for the user to confirm in-game before the loop fires.

    Gives time to:
    - Check the Switch screen and confirm the Pro Controller icon is visible
      in the controller list (top of the pause menu or in-game HUD).
    - Position Link where you want the macro sequence to start.
    - Switch input back to joycons to navigate if needed.
    """
    print()
    print("┌─────────────────────────────────────────────────────────┐")
    print("│  Emulated Pro Controller is connected and active.       │")
    print("│                                                         │")
    print("│  On the Switch, confirm the controller appears in:      │")
    print("│    System Settings → Controllers → Controller Count     │")
    print("│    or the controller icon in the top-right corner.      │")
    print("│                                                         │")
    print("│  Position Link where you want the macro loop to begin.  │")
    print("│  Once the loop starts, real joycons still work — both   │")
    print("│  controllers send inputs simultaneously.                │")
    print("│                                                         │")
    print("│  Press Enter to start the macro loop, Ctrl-C to abort.  │")
    print("└─────────────────────────────────────────────────────────┘")
    try:
        input("  > ")
    except (EOFError, KeyboardInterrupt):
        print()
        raise


# ─────────────────────────────────────────────────────────────
# Macro runner
# ─────────────────────────────────────────────────────────────

def run_macro(nx: nxbt.Nxbt, idx: int, name: str, script: str) -> bool:
    """
    Execute one macro. Returns True on completion, False if the connection
    dropped mid-execution.
    """
    log.info("▶  %s", name)
    try:
        nx.macro(idx, script, block=True)
        log.info("✓  %s done.", name)
        return True
    except Exception as e:
        log.warning("✗  %s failed (%s).", name, e)
        return False


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
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="Seconds to wait for Switch connection (default: 60).")
    parser.add_argument("--no-prompt", action="store_true",
                        help="Skip the handoff prompt and start immediately after the test input.")
    args = parser.parse_args()

    # ── signal handling ────────────────────────────────────────────────────
    _stop = {"flag": False}

    def on_sigint(sig, frame):
        if not _stop["flag"]:
            log.info("Ctrl-C — finishing current macro, then stopping.")
        _stop["flag"] = True

    signal.signal(signal.SIGINT, on_sigint)

    # ── connect and verify ─────────────────────────────────────────────────
    log.info("Initialising nxbt...")
    nx = nxbt.Nxbt()

    try:
        idx = connect(nx, timeout=args.timeout)
    except (TimeoutError, RuntimeError) as e:
        log.error("Connection failed: %s", e)
        sys.exit(1)

    # Verify the controller is active in-game, not just at the BT layer.
    # Retry once on failure in case the link just needed a moment to settle.
    if not verify_active(nx, idx):
        log.warning("Retrying test input after 2s...")
        time.sleep(2.0)
        if not verify_active(nx, idx):
            log.error(
                "Controller connected at BT level but test input failed twice.\n"
                "  The Switch may not have accepted the controller as an input device.\n"
                "  Try: switch to game view, press a joycon button, then re-run."
            )
            try:
                nx.remove_controller(idx)
            except Exception:
                pass
            sys.exit(1)

    # Handoff prompt unless suppressed
    if not args.no_prompt:
        try:
            handoff_prompt()
        except (KeyboardInterrupt, EOFError):
            log.info("Aborted at handoff prompt.")
            try:
                nx.remove_controller(idx)
            except Exception:
                pass
            sys.exit(0)

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

                # Reconnect if the link dropped since the last macro
                if not is_connected(nx, idx):
                    log.warning("Connection lost — reconnecting.")
                    try:
                        idx = reconnect(nx, idx, timeout=args.timeout)
                    except (TimeoutError, RuntimeError) as e:
                        log.error("Reconnect failed: %s", e)
                        _stop["flag"] = True
                        break

                success = run_macro(nx, idx, name, script)

                # Reconnect after a mid-macro failure so the next macro is ready
                if not success and not _stop["flag"]:
                    log.warning("Reconnecting after failed macro.")
                    try:
                        idx = reconnect(nx, idx, timeout=args.timeout)
                    except (TimeoutError, RuntimeError) as e:
                        log.error("Reconnect failed: %s", e)
                        _stop["flag"] = True
                        break

                if _stop["flag"]:
                    break

                log.info("Break: %.1fs before next macro.", args.break_s)
                deadline = time.time() + args.break_s
                while time.time() < deadline and not _stop["flag"]:
                    time.sleep(0.1)

            cycle += 1
            if args.once:
                log.info("--once: completed one full cycle, exiting.")
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
