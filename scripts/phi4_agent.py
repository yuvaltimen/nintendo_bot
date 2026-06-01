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
import base64
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

from switch_control import RemotePad, scrub_macro  # noqa: E402

# ── defaults ──────────────────────────────────────────────────────────────────

FRAME_W, FRAME_H = 1280, 720
OLLAMA_HOST  = os.environ.get("OLLAMA_HOST",  "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "phi4")

# ── skill file ────────────────────────────────────────────────────────────────

_SKILL_PATH = Path(__file__).resolve().parent.parent / "BOTW_SKILL.md"

def _load_skill() -> str:
    """Load BOTW_SKILL.md from the repo root. Falls back to a minimal inline
    reference if the file is missing."""
    if _SKILL_PATH.exists():
        return _SKILL_PATH.read_text(encoding="utf-8")
    return (
        "Pro Controller macro syntax: items on one line fire simultaneously.\n"
        "B=sprint/dodge  X=jump/paraglider  Y=attack  A=interact  ZL=shield  ZR=bow\n"
        "L_STICK@+000+100=forward  R_STICK@+100+000=camera right\n"
    )

# BOTW_SKILL is the complete system prompt — agent instructions, response
# format, rules, button reference, and helpful sequences all live in
# BOTW_SKILL.md. Edit that file; both text and vision modes use it directly.
BOTW_SKILL = _load_skill()
SYSTEM_PROMPT = BOTW_SKILL
SYSTEM_PROMPT_VISION = BOTW_SKILL


# ── image encoding ────────────────────────────────────────────────────────────

def encode_jpeg(frame, quality: int = 75) -> str:
    """Encode a BGR numpy frame as a base64 JPEG string for the Ollama images field."""
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.standard_b64encode(buf).decode("utf-8")


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
        self._latest_annotated = None   # clean YOLO-annotated frame (no display overlays)

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

    def set_annotated_frame(self, frame) -> None:
        with self._lock:
            self._latest_annotated = frame

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

    def get_annotated_frame(self):
        with self._lock:
            return self._latest_annotated

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


def call_phi4(
    messages: list[dict],
    stop_event: threading.Event,
    image_b64: str | None = None,
    timeout: float = 90.0,
) -> str:
    """
    Stream a response from Ollama. Checks stop_event between tokens so
    cancellation fires within one token (~50 ms). Returns empty string if
    cancelled.

    image_b64: base64-encoded JPEG to attach to the last user message.
               Requires a vision-capable model (e.g. llama3.2-vision, llava).
               Standard phi4 will ignore or error on the images field.
    """
    if image_b64:
        # Attach the image to the last user message in-place.
        # Ollama expects: {"role": "user", "content": "...", "images": ["<b64>"]}
        last = messages[-1]
        messages = messages[:-1] + [{**last, "images": [image_b64]}]

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
    """Extract (reasoning, macro) from the model's response.

    phi4 sometimes wraps the JSON in explanation text or markdown fences even
    when instructed not to. This function tries several strategies in order so
    that most real-world model outputs are recovered cleanly.
    """
    import re as _re

    def _from_obj(obj: dict) -> tuple[str, str]:
        return obj.get("reasoning", ""), obj.get("macro", "")

    # 1. Strip common markdown fences and try a direct parse.
    cleaned = raw.strip()
    for fence in ("```json", "```"):
        if cleaned.startswith(fence):
            cleaned = cleaned[len(fence):]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()
    try:
        return _from_obj(json.loads(cleaned))
    except json.JSONDecodeError:
        pass

    # 2. Find the first {...} block in the raw output.
    #    phi4 often writes a sentence, then the JSON on a new line.
    match = _re.search(r'\{[^{]+\}', raw, _re.DOTALL)
    if match:
        try:
            return _from_obj(json.loads(match.group()))
        except json.JSONDecodeError:
            pass

    # 3. If the response itself looks like a raw macro string (model forgot JSON),
    #    use the first line as the macro.
    first = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    if any(first.startswith(p) for p in ("L_STICK", "R_STICK", "A ", "B ", "Y ", "ZL", "ZR", "LOOP", "DPAD")):
        return "(raw macro)", first

    return "(parse error)", ""


# ── REPL thread ───────────────────────────────────────────────────────────────

def goal_loop(
    goal: str,
    pad: RemotePad,
    state: SharedState,
    interval: float,
    vision: bool = False,
) -> None:
    """Run one goal until stop_goal is set. Runs in the REPL thread.

    vision=True: encode the latest YOLO-annotated frame as a JPEG and attach
    it to the Ollama message instead of the text scene description. Requires a
    vision-capable model (set OLLAMA_MODEL=llama3.2-vision or similar).
    """
    last_call_t: float = 0.0
    action_history: list[str] = []

    mode_tag = "[vision]" if vision else "[text]"
    print(f"[running] {goal}  {mode_tag}\n", flush=True)

    while not state.stop_goal.is_set() and not state.exit_app.is_set():
        now = time.time()
        if now - last_call_t < interval:
            time.sleep(0.05)
            continue

        last_call_t = now
        history_str = "\n".join(action_history[-5:]) or "(none yet)"

        if vision:
            annotated = state.get_annotated_frame()
            if annotated is None:
                time.sleep(0.1)
                continue
            image_b64 = encode_jpeg(annotated)
            system = SYSTEM_PROMPT_VISION
            user_text = (
                f"Goal: {goal}\n\n"
                f"Recent actions:\n{history_str}\n\n"
                "What should Link do next?"
            )
        else:
            image_b64 = None
            system = SYSTEM_PROMPT
            user_text = (
                f"Goal: {goal}\n\n{state.get_scene()}\n\n"
                f"Recent actions:\n{history_str}\n\n"
                "What should Link do next?"
            )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]

        state.set_phi4_status(in_call=True)
        try:
            raw = call_phi4(messages, state.stop_goal, image_b64=image_b64)
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

        reasoning, macro_raw = parse_response(raw)

        # Scrub comments from phi4's output and log any difference.
        # nxbt crashes on tokens like "#" or "attack" — see scrub_macro() docs.
        macro = scrub_macro(macro_raw) if macro_raw else ""
        if macro != macro_raw:
            stripped_lines = [
                l for l in macro_raw.splitlines()
                if l.strip() and not scrub_macro(l)
            ]
            print(
                f"  [scrubbed] phi4 added {len(macro_raw) - len(macro)} chars of comments "
                f"({len(stripped_lines)} lines dropped).",
                flush=True,
            )
            print(f"  [raw]    {macro_raw!r}", flush=True)
            print(f"  [clean]  {macro!r}", flush=True)

        state.set_phi4_status(reasoning=reasoning, macro=macro or "")

        if macro and not state.stop_goal.is_set():
            action_history.append(f"[{time.strftime('%H:%M:%S')}] {macro!r}")
            try:
                pad.macro(macro, retries=2, recover_timeout=15.0)
                print(f"  [sent]   {macro!r}", flush=True)
            except Exception as e:
                print(f"  [pad error]  {e}", flush=True)
            if reasoning:
                print(f"           → {reasoning}", flush=True)
        else:
            print(f"  [wait]   {reasoning or '(no action)'}", flush=True)


def repl_thread_fn(pad: RemotePad, state: SharedState, interval: float, vision: bool = False) -> None:
    """Runs in a background daemon thread. Owns the terminal prompt."""
    mode_line = "vision (annotated JPEG → LLM)" if vision else "text (YOLO detections → LLM)"
    print(f"""
phi4 Agent — Breath of the Wild
  model    : {OLLAMA_MODEL}  (via Ollama at {OLLAMA_HOST})
  interval : {interval}s between LLM calls
  input    : {mode_line}
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
            goal_loop(goal, pad, state, interval, vision=vision)
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

        # Push latest scene data to shared state for the REPL thread.
        # Store the clean annotated frame (YOLO boxes only, no display overlays)
        # before we mutate it with fps text / goal / reasoning overlays.
        annotated_clean = results.plot()
        state.set_scene(describe_scene(results))
        state.set_annotated_frame(annotated_clean)

        goal, reasoning, macro, in_phi4 = state.get_display_info()

        # ── build annotated right panel (display copy, with overlays) ──────
        annotated = annotated_clean.copy()
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
                        help="Seconds between LLM calls (default: 5.0).")
    parser.add_argument("--scale", type=float, default=0.65,
                        help="Display window scale factor (default: 0.65 to fit a MacBook).")
    parser.add_argument(
        "--vision", action="store_true",
        help=(
            "Send the YOLO-annotated frame as a JPEG image instead of a text scene description. "
            "Requires a vision-capable model — set OLLAMA_MODEL to llama3.2-vision, llava, "
            "moondream, or another Ollama vision model. Standard phi4 (text-only) will error."
        ),
    )
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

    if args.vision:
        print("Vision mode: annotated JPEG will be sent to the LLM each call.")
        print(f"  Make sure OLLAMA_MODEL ({OLLAMA_MODEL}) is a vision-capable model.\n")

    # ── start REPL in background thread ───────────────────────────────────
    t = threading.Thread(
        target=repl_thread_fn,
        args=(pad, state, args.interval),
        kwargs={"vision": args.vision},
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
