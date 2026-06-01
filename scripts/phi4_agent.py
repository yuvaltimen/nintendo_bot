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

from switch_control import RemotePad, scrub_macro, extend_macro_to_interval  # noqa: E402

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
        self._scene_desc: str  = "No detections yet."
        self._latest_frame     = None   # raw BGR frame
        self._latest_results   = None   # ultralytics Results object
        self._latest_annotated = None   # YOLO-annotated frame (no display overlays)

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

    def set_frame_data(self, raw_frame, results, annotated_frame) -> None:
        """Store all three frame representations atomically."""
        with self._lock:
            self._latest_frame     = raw_frame
            self._latest_results   = results
            self._latest_annotated = annotated_frame

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

    def get_frame_data(self):
        """Returns (raw_frame, results, annotated_frame)."""
        with self._lock:
            return self._latest_frame, self._latest_results, self._latest_annotated

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


# ── rule-based policy (used with --rules, no LLM required) ──────────────────

# YOLO class names treated as enemies.
# Works with COCO model ("person") and any custom BotW model.
# Add or remove entries here to tune the detection set.
ENEMY_CLASSES: frozenset[str] = frozenset({
    "person",                                              # COCO fallback
    "bokoblin", "blue_bokoblin", "silver_bokoblin", "gold_bokoblin",
    "lizalfos", "moblin", "lynel", "guardian", "hinox", "talus",
})

RULE_CONF_THRESHOLD = 0.45   # ignore detections below this confidence

# Pre-built macro strings for each rule action.
# Edit these to change how Link responds to each situation.
_RULE_DODGE_LEFT  = "ZL L_STICK@-100+000 B 0.15s\n0.6s"
_RULE_DODGE_RIGHT = "ZL L_STICK@+100+000 B 0.15s\n0.6s"

_RULE_DEFEND = (
    "ZL 0.1s\n"
    "ZL 1.5s\n"
    "ZL A 0.12s\n"
    "0.5s\n"
    "Y 0.15s\n0.2s\n"
    "Y 0.15s\n0.2s\n"
    "Y 0.15s\n0.5s"
)

_RULE_ATTACK = (
    "L_STICK@+000+080 0.4s\n0.1s\n"
    "Y 0.1s\n0.3s\n"
    "Y 0.1s\n0.3s\n"
    "Y 0.1s\n0.3s\n"
    "Y 0.1s\n0.5s\n"
    "L_STICK@-100+000 B 0.15s\n0.6s\n"
    "L_STICK@+000+080 0.3s\n"
    "Y 0.1s\n0.3s"
)

_RULE_PARAGLIDE = (
    "L_STICK@+000+100 B 0.7s\n"
    "L_STICK@+000+100 B X 0.1s\n"
    "L_STICK@+000+100 0.5s\n"
    "L_STICK@+000+100 X 0.1s\n"
    "L_STICK@+000+100 0.8s"
)


def _detect_water_by_color(frame, min_fraction: float = 0.12) -> bool:
    """
    Rough water detection using color analysis on the bottom third of the frame.
    BotW water is typically bright blue/teal. Returns True if enough of the
    bottom region matches that hue profile.

    Tune min_fraction up if you get false positives (blue sky at the horizon),
    or down if water isn't being detected.
    """
    frame_h = frame.shape[0]
    region = frame[frame_h * 2 // 3:, :]     # bottom third, BGR
    # cv2/ultralytics already load numpy; no import needed
    b = region[:, :, 0].astype("int32")
    g = region[:, :, 1].astype("int32")
    r = region[:, :, 2].astype("int32")
    water_pixels = (b - r > 25) & (b - g > 10) & (b > 90)
    return float(water_pixels.mean()) > min_fraction


def rule_policy(frame, results, w: int, _h: int, interval: float) -> tuple[str, str]:
    """
    Rule-based policy — runs every interval tick with no LLM involved.
    Returns (rule_name, macro_string).

    Priority order:
    1. Large enemy bounding box (close range) → dodge away from enemy + defend
    2. Medium enemy box (mid range)           → full attack combo
    3. Small enemy box (far)                  → approach in enemy's direction
    4. Water detected by color heuristic      → sprint off ledge + paraglide
    5. Default                                → keep moving forward

    To customise behaviour, edit ENEMY_CLASSES, RULE_CONF_THRESHOLD,
    _RULE_ATTACK, _RULE_DEFEND, _RULE_PARAGLIDE above, or add new rules here.
    """
    dur = round(max(0.5, interval - 0.5), 1)   # duration for movement hold

    enemies: list[tuple[str, float, float, float]] = []
    if results is not None and results.boxes is not None:
        for box in results.boxes:
            cls_name = results.names[int(box.cls)]
            conf     = float(box.conf)
            if cls_name in ENEMY_CLASSES and conf >= RULE_CONF_THRESHOLD:
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                cx    = (x1 + x2) / 2
                box_w = x2 - x1
                enemies.append((cls_name, conf, cx, box_w))

    if enemies:
        # Sort by bounding box width: wider = closer
        enemies.sort(key=lambda e: e[3], reverse=True)
        cls_name, conf, cx, box_w = enemies[0]
        label = f"{cls_name} {conf:.0%}"

        if box_w > w * 0.35:
            # Very close — dodge away from the enemy's side, then parry
            dodge = _RULE_DODGE_LEFT if cx > w / 2 else _RULE_DODGE_RIGHT
            return f"defend ({label})", dodge + "\n" + _RULE_DEFEND

        if box_w > w * 0.15:
            # Mid range — attack
            return f"attack ({label})", _RULE_ATTACK

        # Far — approach in the direction of the enemy
        if cx < w * 0.35:
            return f"approach-left ({label})", f"L_STICK@-060+080 B {dur}s"
        if cx > w * 0.65:
            return f"approach-right ({label})", f"L_STICK@+060+080 B {dur}s"
        return f"approach ({label})", f"L_STICK@+000+090 B {dur}s"

    # Water detected in the lower frame by color → paraglide
    if frame is not None and _detect_water_by_color(frame):
        return "paraglide (water ahead)", _RULE_PARAGLIDE

    # Default: keep moving forward
    return "explore", f"L_STICK@+000+080 B {dur}s"


def rules_loop(pad: RemotePad, state: SharedState, interval: float) -> None:
    """
    Rule-based agent loop — no LLM, no prompts, no latency.

    Runs on the REPL thread. Checks YOLO detections every `interval` seconds
    and sends the macro returned by rule_policy(). Ctrl-C or Q in the display
    window stops it via state.stop_goal.
    """
    print(f"[rules] interval: {interval}s | Edit rule_policy() to customise.\n",
          flush=True)

    last_action_t = 0.0

    while not state.stop_goal.is_set() and not state.exit_app.is_set():
        now = time.time()
        if now - last_action_t < interval:
            time.sleep(0.02)
            continue

        last_action_t = now
        frame, results, _ = state.get_frame_data()

        if frame is None or results is None:
            time.sleep(0.05)
            continue

        h_px, w_px = frame.shape[:2]
        rule_name, macro_raw = rule_policy(frame, results, w_px, h_px, interval)
        macro = scrub_macro(macro_raw)

        state.set_phi4_status(reasoning=f"[rule] {rule_name}", macro=macro or "")

        if macro and not state.stop_goal.is_set():
            try:
                pad.macro(macro, retries=2, recover_timeout=15.0)
                print(f"  [{rule_name}]  {macro!r}", flush=True)
            except Exception as e:
                print(f"  [pad error]  {e}", flush=True)


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

        # The interval is passed explicitly so the model knows how long its
        # movement macros should run to keep Link moving continuously.
        movement_target = round(interval - 0.5, 1)
        interval_hint = (
            f"interval: {interval}s — movement macros should be ~{movement_target}s total"
        )

        if vision:
            _, _, annotated = state.get_frame_data()
            if annotated is None:
                time.sleep(0.1)
                continue
            image_b64 = encode_jpeg(annotated)
            system = SYSTEM_PROMPT_VISION
            user_text = (
                f"Goal: {goal}\n"
                f"{interval_hint}\n\n"
                f"Recent actions:\n{history_str}\n\n"
                "What should Link do next?"
            )
        else:
            image_b64 = None
            system = SYSTEM_PROMPT
            user_text = (
                f"Goal: {goal}\n"
                f"{interval_hint}\n\n"
                f"{state.get_scene()}\n\n"
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

        # 1. Scrub comments and normalize Unicode dashes.
        macro = scrub_macro(macro_raw) if macro_raw else ""
        if macro != macro_raw and macro_raw:
            print(f"  [scrubbed] removed non-DSL content from phi4 output", flush=True)
            print(f"  [raw]    {macro_raw!r}", flush=True)
            print(f"  [clean]  {macro!r}", flush=True)

        # 2. Auto-extend pure movement macros to fill the interval so Link
        #    keeps moving continuously rather than stopping between decisions.
        if macro:
            extended = extend_macro_to_interval(macro, interval)
            if extended != macro:
                print(f"  [extended] movement macro padded to fill {interval}s interval",
                      flush=True)
                macro = extended

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


def repl_thread_fn(
    pad: RemotePad,
    state: SharedState,
    interval: float,
    vision: bool = False,
    rules: bool = False,
) -> None:
    """Runs in a background daemon thread. Owns the terminal prompt."""

    if rules:
        # Rules mode: skip Ollama and the REPL entirely, just run rule_policy forever.
        state.set_goal("[rules mode]")
        try:
            rules_loop(pad, state, interval)
        finally:
            state.clear_goal()
        return

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
        # Store raw frame + results + clean annotated frame before adding overlays.
        # Rules loop reads raw frame + results; vision mode reads annotated frame.
        annotated_clean = results.plot()
        state.set_scene(describe_scene(results))
        state.set_frame_data(frame, results, annotated_clean)

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
    parser.add_argument("--interval", type=float, default=None,
                        help=(
                            "Seconds between decisions. "
                            "Default: 0.5 for --rules mode, 5.0 for LLM mode."
                        ))
    parser.add_argument("--scale", type=float, default=0.65,
                        help="Display window scale factor (default: 0.65 to fit a MacBook).")
    parser.add_argument(
        "--model", "-m",
        default=None,
        help=(
            "Ollama model name (default: $OLLAMA_MODEL or phi4). "
            "Examples: llama3.1:8b  llama3.2-vision:11b  llava"
        ),
    )
    parser.add_argument(
        "--vision", action="store_true",
        help=(
            "Send the YOLO-annotated frame as a JPEG instead of a text scene description. "
            "Requires a vision-capable model: llama3.2-vision:11b, llava, moondream, etc."
        ),
    )
    parser.add_argument(
        "--rules", action="store_true",
        help=(
            "Use the rule-based policy instead of an LLM. "
            "No Ollama required. Checks YOLO detections each interval and sends macros "
            "based on enemy proximity and water detection. Edit rule_policy() to customise."
        ),
    )
    args = parser.parse_args()

    # Resolve interval default based on mode
    interval = args.interval if args.interval is not None else (0.5 if args.rules else 5.0)

    # --model overrides the environment variable
    global OLLAMA_MODEL
    if args.model:
        OLLAMA_MODEL = args.model

    # ── pre-flight ─────────────────────────────────────────────────────────
    if not args.rules:
        print(f"Checking Ollama ({OLLAMA_MODEL})... ", end="", flush=True)
        ok, msg = check_ollama()
        if not ok:
            print(f"FAIL\n\n{msg}")
            sys.exit(1)
        print(msg)
    else:
        print(f"Rules mode — Ollama not required.")
        print(f"  Edit ENEMY_CLASSES and rule_policy() in phi4_agent.py to customise.")

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

    if args.vision and not args.rules:
        print("Vision mode: annotated JPEG will be sent to the LLM each call.")
        print(f"  Make sure OLLAMA_MODEL ({OLLAMA_MODEL}) is a vision-capable model.\n")

    # ── start REPL/rules thread ────────────────────────────────────────────
    t = threading.Thread(
        target=repl_thread_fn,
        args=(pad, state, interval),
        kwargs={"vision": args.vision, "rules": args.rules},
        daemon=True,
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
