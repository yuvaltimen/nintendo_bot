"""
Local AI agent loop: HDMI capture → YOLO → phi4 (Ollama) → macros → Switch.

Two concurrent pieces:
  • Main thread   — capture card → YOLO → OpenCV display window (always live)
  • REPL thread   — terminal prompt → phi4 calls → pad macros

The display window stays open and shows live frames whether or not a goal is
running. Type a goal at the prompt and the agent starts. Ctrl-C (in the
terminal) or Q (in the display window) cancels the current run and returns to
the prompt — the window keeps showing the feed.

────────────────────────────────────────────────────────
One-time setup:

  ollama pull phi4       # ~9 GB, once
  ollama list            # confirm it's there

  # No extra pip installs — opencv + ultralytics already installed.

────────────────────────────────────────────────────────
Usage:

  CAPTURE_DEVICE=1 python scripts/phi4_agent.py

  BotW Agent > walk toward the nearest NPC and talk to them
    [sent]   'L_STICK@+000+070 2.0s'   → person at center-left, moving toward them
    [wait]   Person now close — pausing
    [sent]   'A 0.1s'                  → within range, pressing A
  ^C
  [stopping — halting Pi pad] done.
  [stopped]

  BotW Agent > quit

────────────────────────────────────────────────────────
Environment variables:
  PI_HOST         Pi hostname/IP          (default: raspberrypi.local)
  CAPTURE_DEVICE  Capture card index      (default: 1)
  YOLO_MODEL      YOLO model path         (default: yolov8n.pt)
  OLLAMA_HOST     Ollama base URL         (default: http://localhost:11434)
  OLLAMA_MODEL    Model to use            (default: phi4)
────────────────────────────────────────────────────────
"""

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_control import RemotePad  # noqa: E402

# ── defaults ──────────────────────────────────────────────────────────────────

FRAME_W, FRAME_H = 1280, 720
OLLAMA_HOST  = os.environ.get("OLLAMA_HOST",  "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "phi4")

# ── prompt ────────────────────────────────────────────────────────────────────

CONTROLLER_REF = """\
Nintendo Switch Pro Controller — macro string syntax:
  Movement   L_STICK@+000+100 (forward)  L_STICK@+000-100 (back)
             L_STICK@+100+000 (right)    L_STICK@-100+000 (left)
             Diagonals OK: L_STICK@+070+070  (values -100 to +100)
  Camera     R_STICK@+100+000 (pan right)  R_STICK@-100+000 (pan left)
             R_STICK@+000-100 (look up)    R_STICK@+000+100 (look down)
  Actions    A (interact/climb)  B (jump)  Y (attack)  X (paraglider mid-air)
  Triggers   ZL (raise shield/lock-on hold)  ZR (draw bow/aim hold)
  Other      PLUS (map)  MINUS (whistle)  DPAD_UP/DOWN/LEFT/RIGHT
  Combos     ZL + L_STICK@+000-100 + B  →  backflip dodge
             ZL + A                     →  shield parry
Macro format: items on one line fire simultaneously for their duration.
  "L_STICK@+000+100 B 0.7s"   run + jump together for 0.7 s
  "Y 0.1s\\n0.3s\\nY 0.1s"      attack, pause, attack
  "ZR 1.5s"                   hold bow 1.5 s (releasing fires the arrow)
  ""                          do nothing this tick"""

SYSTEM_PROMPT = f"""You are an AI agent playing The Legend of Zelda: Breath of the Wild.

Each turn you receive a text description of the game scene (objects detected by YOLO
with their screen positions), a list of recent actions, and the current goal.

Respond with ONLY a valid JSON object — no extra text, no markdown fences:
{{"reasoning": "<one sentence: what you see and why you chose this action>",
  "macro": "<macro string to execute, or empty string to wait this tick>"}}

{CONTROLLER_REF}

Rules:
- Keep macros under 3 seconds total duration.
- Prefer small, incremental movements.
- If nothing useful is visible, return an empty macro string.
- Do not repeat the same macro more than 3 turns in a row if the scene has not changed."""


# ── shared state ──────────────────────────────────────────────────────────────

class SharedState:
    """
    Thread-safe bridge between the main (display) thread and the REPL thread.

    Main thread writes: latest_scene_desc (after each YOLO inference).
    REPL thread writes: goal, last_reasoning, last_macro, in_phi4_call.
    Both threads read everything through get_display_info() / get_scene_desc().
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Written by main thread, read by REPL thread
        self._scene_desc: str = "No detections yet."

        # Written by REPL thread, read by main thread for display
        self._goal:         str  = ""
        self._reasoning:    str  = ""
        self._macro:        str  = ""
        self._in_phi4_call: bool = False

        # Control
        self.stop_goal  = threading.Event()   # cancel current goal run
        self.exit_app   = threading.Event()   # shut everything down

    # ── main thread writes ────────────────────────────────────────────────
    def set_scene(self, desc: str) -> None:
        with self._lock:
            self._scene_desc = desc

    # ── REPL thread writes ────────────────────────────────────────────────
    def set_goal(self, goal: str) -> None:
        with self._lock:
            self._goal = goal
            self._reasoning = ""
            self._macro = ""
            self._in_phi4_call = False

    def clear_goal(self) -> None:
        with self._lock:
            self._goal = ""
            self._in_phi4_call = False

    def set_phi4_status(self, reasoning: str = "", macro: str = "", in_call: bool = False) -> None:
        with self._lock:
            if reasoning:
                self._reasoning = reasoning
            if macro is not None:
                self._macro = macro
            self._in_phi4_call = in_call

    # ── cross-thread reads ────────────────────────────────────────────────
    def get_scene(self) -> str:
        with self._lock:
            return self._scene_desc

    def has_active_goal(self) -> bool:
        with self._lock:
            return bool(self._goal)

    def get_display_info(self) -> tuple[str, str, str, bool]:
        """Returns (goal, reasoning, macro, in_phi4_call)."""
        with self._lock:
            return self._goal, self._reasoning, self._macro, self._in_phi4_call


# ── scene description ─────────────────────────────────────────────────────────

def describe_scene(results) -> str:
    if results is None or results.boxes is None or len(results.boxes) == 0:
        return "YOLO detected no objects. The scene appears clear."
    orig_h, orig_w = results.orig_shape[:2]
    lines = []
    for box in results.boxes:
        label  = results.names[int(box.cls)]
        conf   = float(box.conf)
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        cx = (x1 + x2) / 2 / orig_w
        cy = (y1 + y2) / 2 / orig_h
        h_pos = "left"   if cx < 0.33 else ("center" if cx < 0.67 else "right")
        v_pos = "top"    if cy < 0.33 else ("middle"  if cy < 0.67 else "bottom")
        lines.append(f"  - {label} ({conf:.0%}) at screen {h_pos}/{v_pos}")
    return "Detected objects:\n" + "\n".join(lines)


# ── Ollama ────────────────────────────────────────────────────────────────────

def check_ollama() -> tuple[bool, str]:
    try:
        req = urllib.request.Request(f"{OLLAMA_HOST}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        pulled = [m["name"].split(":")[0] for m in data.get("models", [])]
        if OLLAMA_MODEL.split(":")[0] in pulled:
            return True, f"{OLLAMA_MODEL} is ready"
        return False, (
            f"Model '{OLLAMA_MODEL}' not found. Available: {pulled or 'none'}\n"
            f"  Fix: ollama pull {OLLAMA_MODEL}"
        )
    except urllib.error.URLError:
        return False, (
            f"Cannot reach Ollama at {OLLAMA_HOST}.\n"
            "  Fix: open the Ollama app, or run: ollama serve"
        )


def call_phi4(messages: list[dict], stop_event: threading.Event, timeout: float = 90.0) -> str:
    """
    Stream a phi4 response. Checks stop_event between tokens so cancellation
    fires within one token (~50 ms) rather than waiting for the full response.
    Returns empty string if cancelled.
    """
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": True,
        "options": {"temperature": 0.2, "num_predict": 200},
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )

    text = ""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw_line in resp:
            if stop_event.is_set():
                return ""
            chunk = json.loads(raw_line)
            text += chunk.get("message", {}).get("content", "")
            if chunk.get("done"):
                break
    return text


def parse_response(raw: str) -> tuple[str, str]:
    cleaned = (
        raw.strip()
        .removeprefix("```json").removeprefix("```")
        .removesuffix("```").strip()
    )
    try:
        obj = json.loads(cleaned)
        return obj.get("reasoning", ""), obj.get("macro", "")
    except json.JSONDecodeError:
        first = raw.strip().splitlines()[0].strip() if raw.strip() else ""
        if any(first.startswith(p) for p in ("L_STICK", "R_STICK", "A ", "B ", "Y ", "ZL", "ZR")):
            return "(raw macro)", first
        return "(parse error)", ""


# ── REPL thread ───────────────────────────────────────────────────────────────

def goal_loop(goal: str, pad: RemotePad, state: SharedState, interval: float) -> None:
    """Run one goal until stop_goal is set. Runs in the REPL thread."""
    last_call_t: float = 0.0
    action_history: list[str] = []

    print(f"[running] {goal}\n", flush=True)

    while not state.stop_goal.is_set() and not state.exit_app.is_set():
        now = time.time()
        if now - last_call_t < interval:
            time.sleep(0.05)
            continue

        last_call_t = now
        scene = state.get_scene()
        history_str = "\n".join(action_history[-5:]) or "(none yet)"

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Goal: {goal}\n\n{scene}\n\n"
                    f"Recent actions:\n{history_str}\n\n"
                    "What should Link do next?"
                ),
            },
        ]

        state.set_phi4_status(in_call=True)
        try:
            raw = call_phi4(messages, state.stop_goal)
        except urllib.error.URLError as e:
            state.set_phi4_status(in_call=False)
            print(f"  [ollama error]  {e}", flush=True)
            continue
        except Exception as e:
            state.set_phi4_status(in_call=False)
            print(f"  [error]  {e}", flush=True)
            continue

        state.set_phi4_status(in_call=False)

        if state.stop_goal.is_set() or not raw:
            break

        reasoning, macro = parse_response(raw)
        state.set_phi4_status(reasoning=reasoning, macro=macro or "")

        if macro and not state.stop_goal.is_set():
            action_history.append(f"[{time.strftime('%H:%M:%S')}] {macro!r}")
            try:
                pad.macro(macro)
                print(f"  [sent]   {macro!r}", flush=True)
            except Exception as e:
                print(f"  [pad error]  {e}", flush=True)
            if reasoning:
                print(f"           → {reasoning}", flush=True)
        else:
            print(f"  [wait]   {reasoning or '(no action)'}", flush=True)


def repl_thread_fn(pad: RemotePad, state: SharedState, interval: float) -> None:
    """Runs in a background daemon thread. Owns the terminal prompt."""
    print(f"""
phi4 Agent — Breath of the Wild
  model    : {OLLAMA_MODEL}  (via Ollama at {OLLAMA_HOST})
  interval : {interval}s between LLM calls
  display  : always-on in separate window

Commands:
  <goal text>   start the agent with this goal
  status        check Pi pad connection
  quit / exit   exit the script
  Ctrl-C        cancel current run, return to this prompt
  Q (window)    same as Ctrl-C
""", flush=True)

    while not state.exit_app.is_set():
        try:
            goal = input("BotW Agent > ").strip()
        except (EOFError, KeyboardInterrupt):
            state.exit_app.set()
            break

        if not goal:
            continue
        if goal.lower() in ("quit", "exit"):
            state.exit_app.set()
            break
        if goal.lower() == "status":
            try:
                print(f"  {pad.status()}", flush=True)
            except Exception as e:
                print(f"  error: {e}", flush=True)
            continue

        state.stop_goal.clear()
        state.set_goal(goal)
        try:
            goal_loop(goal, pad, state, interval)
        finally:
            state.clear_goal()

        if not state.exit_app.is_set():
            print("[stopped]\n", flush=True)


# ── main thread — display loop ────────────────────────────────────────────────

def display_loop(
    cap: cv2.VideoCapture,
    yolo,
    state: SharedState,
    pad: RemotePad,
    scale: float,
) -> None:
    """
    Runs on the main thread. Captures frames, runs YOLO, updates the display.
    Always live — does not pause when the REPL is waiting for input.
    """
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_count = 0
    t_start = time.time()

    while not state.exit_app.is_set():
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.005)
            continue

        frame_count += 1
        now     = time.time()
        results = yolo(frame, verbose=False)[0]

        # Push latest scene description to shared state for the REPL thread
        state.set_scene(describe_scene(results))

        goal, reasoning, macro, in_phi4 = state.get_display_info()

        # ── build annotated right panel ────────────────────────────────────
        annotated = results.plot()
        elapsed   = now - t_start
        fps       = frame_count / elapsed if elapsed > 0 else 0

        # Status bar: fps + model + "thinking…" indicator
        status_txt = f"{fps:.1f} fps  |  {OLLAMA_MODEL}"
        if in_phi4:
            status_txt += "  |  thinking..."
        cv2.putText(annotated, status_txt,
                    (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (0, 255, 0), 2)

        # Bottom overlays
        if goal:
            cv2.putText(annotated, f"goal: {goal[:72]}",
                        (10, h - 50), cv2.FONT_HERSHEY_SIMPLEX, 0.47, (255, 230, 0), 1)
        if reasoning:
            cv2.putText(annotated, reasoning[:90],
                        (10, h - 30), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (190, 190, 190), 1)
        if macro:
            cv2.putText(annotated, f"cmd: {macro}",
                        (10, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.53, (0, 140, 255), 2)

        # Left panel: clean frame with minimal overlay
        clean = frame.copy()
        if goal:
            label = "RUNNING" if not in_phi4 else "THINKING"
            colour = (0, 200, 80) if not in_phi4 else (0, 200, 255)
            cv2.putText(clean, label, (10, 26),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.72, colour, 2)
        else:
            cv2.putText(clean, "IDLE — type a goal in terminal",
                        (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (160, 160, 160), 1)

        # ── combine and scale ──────────────────────────────────────────────
        combined = cv2.hconcat([clean, annotated])
        if scale != 1.0:
            combined = cv2.resize(
                combined,
                (int(combined.shape[1] * scale), int(combined.shape[0] * scale)),
            )

        cv2.imshow("phi4 agent — live  |  clean frame / YOLO + reasoning", combined)

        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            # Cancel current goal (if any); keep the window open
            if state.has_active_goal():
                print("\n[stopping — halting Pi pad]", end=" ", flush=True)
                state.stop_goal.set()
                try:
                    pad.stop()
                    print("done.", flush=True)
                except (OSError, RuntimeError):
                    print("(pad.stop failed)", flush=True)
            # If no goal is running, Q exits
            else:
                state.exit_app.set()
                break


# ── signal handler (Ctrl-C in terminal) ───────────────────────────────────────

def install_signal_handler(state: SharedState, pad: RemotePad) -> None:
    """
    Ctrl-C while a goal is running: cancel goal + halt pad, return to prompt.
    Ctrl-C while idle (no goal): exit.
    """
    def handler(_sig, _frame):
        if state.has_active_goal():
            print("\n[stopping — halting Pi pad]", end=" ", flush=True)
            state.stop_goal.set()
            try:
                pad.stop()
                print("done.", flush=True)
            except (OSError, RuntimeError):
                print("(pad.stop failed)", flush=True)
        else:
            print("\nExiting.", flush=True)
            state.exit_app.set()

    signal.signal(signal.SIGINT, handler)


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="phi4 (Ollama) agent loop for BotW.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--device", "-d", type=int,
                        default=int(os.environ.get("CAPTURE_DEVICE", "0")))
    parser.add_argument("--yolo-model",
                        default=os.environ.get("YOLO_MODEL", "yolov8n.pt"))
    parser.add_argument("--interval", type=float, default=5.0,
                        help="Seconds between phi4 calls (default: 5.0).")
    parser.add_argument("--scale", type=float, default=0.65,
                        help="Display window scale factor (default: 0.65 to fit a MacBook).")
    args = parser.parse_args()

    # ── pre-flight ─────────────────────────────────────────────────────────
    print(f"Checking Ollama ({OLLAMA_MODEL})... ", end="", flush=True)
    ok, msg = check_ollama()
    if not ok:
        print(f"FAIL\n\n{msg}")
        sys.exit(1)
    print(msg)

    print(f"Connecting to Pi daemon ({os.environ.get('PI_HOST', 'raspberrypi.local')})... ", end="", flush=True)
    pad = RemotePad(os.environ.get("PI_HOST", "raspberrypi.local"))
    pad.wait_connected(timeout=15)
    print("connected.")

    print(f"Loading YOLO ({args.yolo_model})... ", end="", flush=True)
    from ultralytics import YOLO
    yolo = YOLO(args.yolo_model)
    print("ready.")

    print(f"Opening capture device {args.device}... ", end="", flush=True)
    cap = cv2.VideoCapture(args.device, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        print(f"FAIL\nCannot open device {args.device}. Run: python scripts/vision_loop.py --scan")
        sys.exit(1)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  FRAME_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"{w}x{h}.")

    # ── shared state + signal handler ──────────────────────────────────────
    state = SharedState()
    install_signal_handler(state, pad)

    # ── start REPL in background thread ───────────────────────────────────
    t = threading.Thread(
        target=repl_thread_fn,
        args=(pad, state, args.interval),
        daemon=True,   # exits automatically when main thread exits
    )
    t.start()

    # ── main thread: display loop (must own cv2.imshow on macOS) ──────────
    try:
        display_loop(cap, yolo, state, pad, args.scale)
    finally:
        state.exit_app.set()
        cap.release()
        cv2.destroyAllWindows()
        print("\nCapture released. Pad stays connected on the Pi.")


if __name__ == "__main__":
    main()
