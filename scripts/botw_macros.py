"""
BotW exploration macros for Breath of the Wild testing.

Three categories, 15 macros total:
  Town    — casual_stroll, npc_interact, sneaky_passage, shop_browse
  Combat  — attack_combo, shield_parry, aerial_attack, bow_volley
  Explore — paraglide_swing, cliff_climb, horizon_scan, call_and_ride,
             cook_meal, map_survey, sprint_jump_glide

Setup required before each macro is listed in its docstring.

────────────────────────────────────────────────────────────────
Interactive REPL (paste into scripts/interactive.py session):

    >>> import sys; sys.path.insert(0, '.')
    >>> from scripts.botw_macros import *
    >>> casual_stroll(pad)

Or just paste any MACRO_* string directly:
    >>> pad.macro(CASUAL_STROLL)

────────────────────────────────────────────────────────────────
Standalone runner (handoff prompts between each macro):

    PI_HOST=pi.local python scripts/botw_macros.py [macro_name]

    # list all macros:
    python scripts/botw_macros.py --list

    # run one specific macro:
    python scripts/botw_macros.py casual_stroll

    # run all in sequence (with prompts):
    python scripts/botw_macros.py
────────────────────────────────────────────────────────────────

Stick coordinate convention (nxbt DSL):
  L_STICK@SXXXSYYY  where S is + or -, XXX/YYY are 0-padded 3-digit magnitudes.
  X: -100 = hard left,  +100 = hard right
  Y: +100 = forward,    -100 = backward
  Camera (R_STICK): same axes, but from the camera's perspective.
"""

import os
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_control import RemotePad, Buttons, Sticks  # noqa: E402


# ─────────────────────────────────────────────────────────────
# TOWN STROLLS
# ─────────────────────────────────────────────────────────────

CASUAL_STROLL = textwrap.dedent("""\
    L_STICK@+000+080 2.5s
    L_STICK@+050+060 1.0s
    L_STICK@+000+080 2.0s
    R_STICK@+060+000 1.5s
    L_STICK@+000+080 1.0s
    L_STICK@-050+060 1.0s
    L_STICK@+000+080 2.0s
    0.8s
    R_STICK@-060+000 1.5s
    0.5s
""")


def casual_stroll(pad: RemotePad) -> None:
    """Walk a loose figure-8 path through town, pausing to glance around.

    Setup: Stand Link in an open area of a town or stable with room to move.
    """
    pad.macro(CASUAL_STROLL)


NPC_INTERACT = textwrap.dedent("""\
    L_STICK@+000+070 1.5s
    0.3s
    A 0.1s
    1.5s
    A 0.1s
    1.2s
    A 0.1s
    1.2s
    A 0.1s
    1.2s
    A 0.1s
    1.0s
    B 0.1s
""")


def npc_interact(pad: RemotePad) -> None:
    """Walk up to a nearby NPC, open their dialog, advance through 4 lines, dismiss.

    Setup: Face an NPC roughly 3-4 steps away; conversation bubble should be reachable.
    """
    pad.macro(NPC_INTERACT)


SNEAKY_PASSAGE = textwrap.dedent("""\
    L_STICK_PRESS 0.1s
    0.4s
    L_STICK@+000+040 2.0s
    L_STICK@+030+025 1.2s
    L_STICK@+000+040 2.5s
    L_STICK@-030+025 1.0s
    L_STICK@+000+040 1.5s
    0.5s
    L_STICK_PRESS 0.1s
    0.3s
""")


def sneaky_passage(pad: RemotePad) -> None:
    """Toggle crouch, creep slowly through a narrow passage, un-crouch at the end.

    Setup: Position Link at the entrance of a building, alley, or guard patrol route.
    Stamina doesn't matter — sneaking uses no resources.
    """
    pad.macro(SNEAKY_PASSAGE)


SHOP_BROWSE = textwrap.dedent("""\
    L_STICK@+000+070 1.2s
    0.3s
    A 0.1s
    1.2s
    DPAD_RIGHT 0.2s
    0.5s
    DPAD_RIGHT 0.2s
    0.5s
    DPAD_RIGHT 0.2s
    0.5s
    DPAD_LEFT 0.2s
    0.4s
    DPAD_LEFT 0.2s
    0.4s
    B 0.1s
    0.5s
""")


def shop_browse(pad: RemotePad) -> None:
    """Walk to a shop vendor, open their inventory, scroll right through 3 items, scroll back, exit.

    Setup: Face a shop keep (general goods, armor, etc.) about 2 steps away.
    Note: A on the item will purchase if you have enough rupees — this macro stops before confirming.
    """
    pad.macro(SHOP_BROWSE)


# ─────────────────────────────────────────────────────────────
# COMBAT
# ─────────────────────────────────────────────────────────────

ATTACK_COMBO = textwrap.dedent("""\
    L_STICK@+000+080 0.4s
    0.1s
    Y 0.1s
    0.25s
    Y 0.1s
    0.25s
    Y 0.1s
    0.25s
    Y 0.1s
    0.4s
    L_STICK@-100+000 B 0.15s
    0.6s
    L_STICK@+000+080 0.3s
    Y 0.1s
    0.3s
    Y 0.1s
    0.5s
""")


def attack_combo(pad: RemotePad) -> None:
    """Advance, land a 4-hit Y combo, sidestep-dodge left, re-engage with 2 more hits.

    Setup: Face an enemy roughly 3-4 steps away (Bokoblin, Lizalfos, etc.).
    Works best without ZL lock-on so Link tracks automatically.
    """
    pad.macro(ATTACK_COMBO)


SHIELD_PARRY = textwrap.dedent("""\
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
""")


def shield_parry(pad: RemotePad) -> None:
    """Raise shield and hold it, then parry (ZL+A at the window), follow with 3 rapid strikes.

    Setup: Face a melee enemy with shield equipped. Time the A press to the enemy's swing.
    This macro approximates the timing — real parries depend on the enemy's attack cadence.
    The 1.5s window gives you time to manually tweak the parry moment when testing.
    """
    pad.macro(SHIELD_PARRY)


AERIAL_ATTACK = textwrap.dedent("""\
    L_STICK@+000+090 0.3s
    B 0.15s
    0.25s
    Y 0.15s
    0.8s
    Y 0.1s
    0.3s
""")


def aerial_attack(pad: RemotePad) -> None:
    """Sprint at an enemy, jump, hit Y mid-air for an aerial spinning attack, land and follow up.

    Setup: Face an enemy roughly 4-5 steps away. A two-handed weapon amplifies the aerial hit.
    If Link is on a ledge or higher ground, the aerial knockdown is much more reliable.
    """
    pad.macro(AERIAL_ATTACK)


BOW_VOLLEY = textwrap.dedent("""\
    ZR 1.2s
    ZR R_STICK@+030+000 0.4s
    ZR R_STICK@-015-010 0.3s
    0.15s
    1.0s
    ZR 1.0s
    0.15s
    1.0s
""")


def bow_volley(pad: RemotePad) -> None:
    """Draw bow, hold to aim, fine-adjust camera onto target, release first arrow, re-draw, shoot again.

    Setup: Face an enemy or target at mid-range with arrows in inventory.
    Holding ZR and nudging R_STICK enters aim mode and pans the reticle.
    Release ZR (not restating it on the next line) fires the arrow.
    """
    pad.macro(BOW_VOLLEY)


# ─────────────────────────────────────────────────────────────
# OPEN EXPLORATION
# ─────────────────────────────────────────────────────────────

PARAGLIDE_SWING = textwrap.dedent("""\
    L_STICK@+000+100 B 0.7s
    L_STICK@+000+100 B X 0.1s
    L_STICK@+000+100 0.5s
    L_STICK@+000+100 X 0.1s
    LOOP 2
        L_STICK@-080+080 1.2s
        L_STICK@+080+080 1.2s
    L_STICK@+000+100 0.8s
""")


def paraglide_swing(pad: RemotePad) -> None:
    """Running jump off a ledge, deploy paraglider mid-air, pendulum left-right twice, settle forward.

    Setup: Stand Link at a cliff edge with the paraglider unlocked and sufficient stamina.
    The running jump (B) leads into an X press to deploy mid-arc.
    """
    pad.macro(PARAGLIDE_SWING)


CLIFF_CLIMB = textwrap.dedent("""\
    L_STICK@+000+100 0.7s
    L_STICK@+000+100 1.2s
    0.9s
    L_STICK@+000+100 1.4s
    0.8s
    L_STICK@+000+100 2.0s
    0.6s
    L_STICK@+000+100 1.0s
    0.5s
""")


def cliff_climb(pad: RemotePad) -> None:
    """Sprint into a climbable surface, scale in two stamina-rest segments, summit.

    Setup: Face a climbable cliff or wall (rough rock surface). Link auto-grabs when
    the stick is held into the surface. The neutral pauses simulate stamina recovery
    on ledge outcroppings — adjust pause lengths to match actual stamina bar length.
    """
    pad.macro(CLIFF_CLIMB)


HORIZON_SCAN = textwrap.dedent("""\
    R_STICK@+070+000 3.0s
    R_STICK@+070+000 3.0s
    R_STICK@+000-070 1.2s
    R_STICK@+000+060 1.5s
    R_STICK@-070+000 3.0s
    0.5s
""")


def horizon_scan(pad: RemotePad) -> None:
    """Stand still and pan the camera: slow right sweep, tilt up to sky, tilt down, sweep back left.

    Setup: Stand Link anywhere outdoors with an interesting view. Works great from
    a tower, hill, or the peak of a shrine. Link stays stationary the whole time.
    """
    pad.macro(HORIZON_SCAN)


CALL_AND_RIDE = textwrap.dedent("""\
    MINUS 1.5s
    0.1s
    2.5s
    A 0.1s
    1.0s
    L_STICK@+000+100 1.0s
    A 0.1s
    0.3s
    A 0.1s
    L_STICK@+000+100 2.0s
    L_STICK@-060+080 1.2s
    L_STICK@+000+100 2.5s
    A 0.1s
    L_STICK@+000+100 2.0s
""")


def call_and_ride(pad: RemotePad) -> None:
    """Hold whistle to call registered horse, wait, mount, spur twice, gallop, steer left, spur and gallop.

    Setup: Be on foot in an open area where your registered horse can reach you.
    Holding MINUS is the "call horse" whistle. A mounts when the horse is alongside.
    A during gallop applies a spur — each spur increases bonded horse's willingness.
    """
    pad.macro(CALL_AND_RIDE)


COOK_MEAL = textwrap.dedent("""\
    L_STICK@+000+060 1.0s
    0.3s
    A 0.1s
    1.2s
    A 0.1s
    0.5s
    DPAD_UP 0.15s
    0.4s
    A 0.1s
    0.4s
    DPAD_UP 0.15s
    0.4s
    A 0.1s
    0.4s
    DPAD_UP 0.15s
    0.4s
    A 0.1s
    0.5s
    A 0.1s
    3.5s
    A 0.1s
    0.5s
""")


def cook_meal(pad: RemotePad) -> None:
    """Approach cooking pot, enter cooking interface, select 3 ingredients with D-pad, cook, dismiss result.

    Setup: Stand next to a lit cooking pot (found in stables, villages, enemy camps).
    Have at least 3 cooking ingredients in your inventory. The macro picks the top 3
    items from the list — arrange ingredients you want combined ahead of time.
    """
    pad.macro(COOK_MEAL)


MAP_SURVEY = textwrap.dedent("""\
    PLUS 0.15s
    1.0s
    R_STICK@+080+000 2.0s
    R_STICK@+000+080 1.5s
    R_STICK@-080+000 2.5s
    R_STICK@+000-080 1.0s
    R_STICK@+000+000 0.5s
    PLUS 0.15s
    0.3s
""")


def map_survey(pad: RemotePad) -> None:
    """Open the map, pan right and down, sweep left, tilt back up, close map.

    Setup: Anywhere in the game — works any time you want a scripted map pan.
    R_STICK on the map screen scrolls the view. PLUS opens and closes the map.
    """
    pad.macro(MAP_SURVEY)


SPRINT_JUMP_GLIDE = textwrap.dedent("""\
    L_STICK@+000+100 1.0s
    L_STICK@+000+100 B 0.2s
    L_STICK@+000+100 0.3s
    L_STICK@+000+100 X 0.1s
    L_STICK@+000+100 0.8s
    L_STICK@+050+080 1.0s
    L_STICK@-050+080 1.0s
    L_STICK@+000+100 1.5s
""")


def sprint_jump_glide(pad: RemotePad) -> None:
    """Running jump off a slope, glider deploy, steer in an S-curve, glide to landing.

    Setup: Stand at the top of a hill or ramp with paraglider unlocked and stamina available.
    Lighter than the full paraglide_swing — useful for shorter descents and testing
    how the glider behaves on gently sloped terrain vs. sharp cliffs.
    """
    pad.macro(SPRINT_JUMP_GLIDE)


# ─────────────────────────────────────────────────────────────
# Registry (name → (function, category, description))
# ─────────────────────────────────────────────────────────────

MACROS = {
    # Town
    "casual_stroll":    (casual_stroll,    "Town",    "Figure-8 walk with a camera glance"),
    "npc_interact":     (npc_interact,     "Town",    "Approach NPC, talk, advance 4 dialog lines"),
    "sneaky_passage":   (sneaky_passage,   "Town",    "Crouch, creep through area, uncrouch"),
    "shop_browse":      (shop_browse,      "Town",    "Open vendor, scroll 3 items, exit"),
    # Combat
    "attack_combo":     (attack_combo,     "Combat",  "4-hit Y combo, sidestep dodge, 2-hit follow-up"),
    "shield_parry":     (shield_parry,     "Combat",  "Raise shield, parry (ZL+A), 3 counter-strikes"),
    "aerial_attack":    (aerial_attack,    "Combat",  "Sprint, jump, Y aerial hit, land follow-up"),
    "bow_volley":       (bow_volley,       "Combat",  "Draw bow, aim, shoot twice"),
    # Exploration
    "paraglide_swing":  (paraglide_swing,  "Explore", "Run off cliff, glider deploy, pendulum x2"),
    "cliff_climb":      (cliff_climb,      "Explore", "Sprint into wall, climb in segments, summit"),
    "horizon_scan":     (horizon_scan,     "Explore", "Stationary 270° camera pan + sky tilt"),
    "call_and_ride":    (call_and_ride,    "Explore", "Whistle horse, mount, spur, gallop, steer"),
    "cook_meal":        (cook_meal,        "Explore", "Cooking pot: 3 ingredients → cook → dismiss"),
    "map_survey":       (map_survey,       "Explore", "Open map, pan around, close"),
    "sprint_jump_glide":(sprint_jump_glide,"Explore", "Sprint, glider deploy, S-curve descent"),
}


# ─────────────────────────────────────────────────────────────
# Standalone runner
# ─────────────────────────────────────────────────────────────

def _list_macros() -> None:
    from itertools import groupby
    grouped = {}
    for name, (_, cat, desc) in MACROS.items():
        grouped.setdefault(cat, []).append((name, desc))
    for cat, entries in grouped.items():
        print(f"\n  {cat}")
        for name, desc in entries:
            print(f"    {name:<22} {desc}")
    print()


def _run_all(pad: RemotePad) -> None:
    for name, (fn, cat, desc) in MACROS.items():
        pad.wait_for_ready(
            f"\n[{cat}] {name} — {desc}\n"
            f"  Setup: see docstring (python -c \"from scripts.botw_macros import {name}; help({name})\")\n"
            f"  Press Enter to run, Ctrl-C to skip to next, Ctrl-D to exit. "
        )
        try:
            fn(pad)
            print(f"  ✓ {name} done.")
        except KeyboardInterrupt:
            print(f"  skipped.")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Run BotW macros via the Pi daemon.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("macro", nargs="?", help="Macro name to run (omit to run all with prompts)")
    parser.add_argument("--list", "-l", action="store_true", help="List all macros and exit")
    args = parser.parse_args()

    if args.list:
        print("\nAvailable BotW macros:")
        _list_macros()
        return

    host = os.environ.get("PI_HOST", "pi.local")
    port = int(os.environ.get("PI_PORT", "8765"))
    pad = RemotePad(host, port)

    print(f"Connecting to daemon at {host}:{port}...")
    pad.wait_connected(timeout=15)
    print("Connected.\n")

    if args.macro:
        if args.macro not in MACROS:
            print(f"Unknown macro '{args.macro}'. Run with --list to see available macros.")
            sys.exit(1)
        fn, cat, desc = MACROS[args.macro]
        print(f"[{cat}] {args.macro} — {desc}")
        print(f"Docstring: {fn.__doc__}\n")
        pad.wait_for_ready("Position Link as described above. Press Enter to run. ")
        fn(pad)
        print("Done.")
    else:
        print("Running all macros in sequence. You will be prompted before each one.")
        _run_all(pad)
        print("\nAll macros complete. Pad remains connected on the Pi.")


if __name__ == "__main__":
    main()
