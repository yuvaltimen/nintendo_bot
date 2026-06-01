"""
LLM agent loop: HDMI capture → YOLO → Claude → macros → Switch.

Claude receives the YOLO-annotated frame plus a structured list of detections
and returns the next controller input as a macro string. The system prompt is
cached via Anthropic's prompt cache — only the image and detections are billed
as uncached tokens after the first call.

────────────────────────────────────────────────────────
Quick start:

  # 1. Set your API key (add to ~/.zshrc for persistence):
  export ANTHROPIC_API_KEY=sk-ant-...

  # 2. Dry-run — see what Claude decides without moving Link:
  python scripts/agent_loop.py --goal "walk toward the nearest NPC" --dry-run

  # 3. Live:
  CAPTURE_DEVICE=1 python scripts/agent_loop.py --goal "explore the area"

  # 4. With a custom BotW YOLO model (once trained):
  python scripts/agent_loop.py --yolo-model models/botw.pt --goal "defeat nearby Bokoblins"

Environment variables:
  ANTHROPIC_API_KEY   Required.
  PI_HOST             Pi hostname/IP (default: pi.local)
  CAPTURE_DEVICE      Capture card device index (default: 1)
  YOLO_MODEL          YOLO model path (default: yolov8n.pt)
────────────────────────────────────────────────────────

Cost guidance (approximate):
  Each call ≈ 800–1200 input tokens (image + detections + cached prompt) + ~150 output tokens.
  At --interval 2.0 (default) that's ~30 calls/minute.

  claude-haiku-4-5-20251001  ~$0.001/call  ← use during development
  claude-sonnet-4-5           ~$0.008/call  ← use for serious runs
  claude-opus-4-7             ~$0.05/call   ← overkill for this task

  The system prompt (controller reference + goal) is prompt-cached, so
  only the per-frame image + detections are charged at the full input rate.
────────────────────────────────────────────────────────
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_control import RemotePad, scrub_macro  # noqa: E402

FRAME_W = 1280
FRAME_H = 720

# ── skill file ────────────────────────────────────────────────────────────────

_SKILL_PATH = Path(__file__).resolve().parent.parent / "BOTW_SKILL.md"

def _load_skill() -> str:
    """Load BOTW_SKILL.md from the repo root. Falls back to a minimal inline
    reference if the file is missing (e.g. running outside the repo)."""
    if _SKILL_PATH.exists():
        return _SKILL_PATH.read_text(encoding="utf-8")
    # Minimal fallback
    return (
        "Pro Controller macro syntax: items on one line fire simultaneously.\n"
        "B=sprint/dodge  X=jump/paraglider  Y=attack  A=interact  ZL=shield  ZR=bow\n"
        "L_STICK@+000+100=forward  R_STICK@+100+000=camera right\n"
    )

# BOTW_SKILL is the complete system prompt — edit BOTW_SKILL.md to change
# the agent's instructions, button reference, or helpful sequences.
# The goal is passed in each user turn, not the system prompt, so the
# system prompt stays constant and the Anthropic prompt cache stays warm.
BOTW_SKILL = _load_skill()


# ─────────────────────────────────────────────────────────────
# Capture
# ─────────────────────────────────────────────────────────────

def open_capture(device: int) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(device, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        raise RuntimeError(
            f"Cannot open capture device {device}. "
            "Run: python scripts/vision_loop.py --scan"
        )
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


# ─────────────────────────────────────────────────────────────
# Claude integration
# ─────────────────────────────────────────────────────────────

def encode_jpeg(frame, quality: int = 80) -> str:
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.standard_b64encode(buf).decode("utf-8")


def detections_to_json(results) -> list[dict]:
    if results is None or results.boxes is None:
        return []
    orig_h, orig_w = results.orig_shape[:2]
    out = []
    for box in results.boxes:
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        out.append({
            "label": results.names[int(box.cls)],
            "confidence": round(float(box.conf), 2),
            "position": {
                "cx": round((x1 + x2) / 2 / orig_w, 2),
                "cy": round((y1 + y2) / 2 / orig_h, 2),
            },
        })
    return out


def _parse_response(raw: str) -> tuple[str, str]:
    """Extract (reasoning, macro) from LLM response, tolerating extra text."""
    import re as _re

    def _from_obj(obj: dict) -> tuple[str, str]:
        return obj.get("reasoning", ""), obj.get("macro", "")

    # 1. Strip markdown fences, try direct parse.
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

    # 2. Extract first {...} block from anywhere in the response.
    match = _re.search(r'\{[^{]+\}', raw, _re.DOTALL)
    if match:
        try:
            return _from_obj(json.loads(match.group()))
        except json.JSONDecodeError:
            pass

    # 3. Raw macro fallback.
    first = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    if any(first.startswith(p) for p in ("L_STICK", "R_STICK", "A ", "B ", "Y ", "ZL", "ZR", "LOOP", "DPAD")):
        return "(raw macro)", first

    return "(parse error)", ""


def call_claude(
    client,
    annotated_frame,
    results,
    goal: str,
    history: list[str],
    claude_model: str,
) -> tuple[str, str]:
    """Call Claude with the current frame + detections. Returns (reasoning, macro)."""
    frame_b64 = encode_jpeg(annotated_frame)
    detections = detections_to_json(results)
    history_str = "\n".join(history[-5:]) if history else "(none yet)"

    response = client.messages.create(
        model=claude_model,
        max_tokens=256,
        system=[
            {
                "type": "text",
                "text": BOTW_SKILL,
                # System prompt is now fully static (goal is in the user turn),
                # so the cache stays warm across goal changes too.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": frame_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"Goal: {goal}\n\n"
                            f"Detections:\n{json.dumps(detections, indent=2)}\n\n"
                            f"Recent actions:\n{history_str}\n\n"
                            "What should Link do next?"
                        ),
                    },
                ],
            }
        ],
    )

    raw = response.content[0].text.strip()
    return _parse_response(raw)


# ─────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────

def run_agent(
    device: int,
    yolo_model_path: str,
    claude_model: str,
    goal: str,
    pad: RemotePad | None,
    dry_run: bool,
    show: bool,
    agent_interval: float,
) -> None:
    import anthropic
    from ultralytics import YOLO

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set. Export it and retry.")

    client = anthropic.Anthropic(api_key=api_key)

    print(f"Loading YOLO: {yolo_model_path}")
    model = YOLO(yolo_model_path)

    cap = open_capture(device)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    print(f"Capture  : device {device}  {w}x{h}")
    print(f"Goal     : {goal}")
    print(f"LLM      : {claude_model}  |  call every {agent_interval}s  |  dry-run: {dry_run}")
    print("Press Q to stop.\n")

    last_call_t = 0.0
    last_reasoning = ""
    last_macro = ""
    action_history: list[str] = []
    frame_count = 0
    t_start = time.time()

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.005)
                continue

            frame_count += 1
            now = time.time()
            results = model(frame, verbose=False)[0]

            if now - last_call_t >= agent_interval:
                last_call_t = now
                try:
                    reasoning, macro_raw = call_claude(
                        client, results.plot(), results, goal, action_history, claude_model
                    )
                    macro = scrub_macro(macro_raw) if macro_raw else ""
                    if macro != macro_raw:
                        print(f"[scrubbed] Claude added comments — stripped {len(macro_raw) - len(macro)} chars")
                        print(f"[raw]      {macro_raw!r}")
                        print(f"[clean]    {macro!r}")
                    last_reasoning = reasoning
                    last_macro = macro or ""

                    if macro:
                        action_history.append(f"[{time.strftime('%H:%M:%S')}] {macro!r}")
                        if dry_run:
                            print(f"[dry-run]  {macro!r}")
                        else:
                            pad.macro(macro, retries=2, recover_timeout=15.0)
                            print(f"[sent]     {macro!r}")
                        if reasoning:
                            print(f"           → {reasoning}")
                    else:
                        print(f"[wait]     {reasoning or '(no action)'}")

                except Exception as e:
                    print(f"[error]    {e}")

            if show:
                elapsed = now - t_start
                fps = frame_count / elapsed if elapsed > 0 else 0
                display = results.plot()

                # Green cooldown bar fills left-to-right until next LLM call
                bar_w = int(w * min(1.0, (now - last_call_t) / agent_interval))
                cv2.rectangle(display, (0, h - 5), (bar_w, h), (0, 210, 90), -1)

                cv2.putText(display, f"{fps:.1f} fps",
                            (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                cv2.putText(display, f"goal: {goal[:72]}",
                            (10, h - 50), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 230, 0), 1)
                if last_reasoning:
                    cv2.putText(display, last_reasoning[:90],
                                (10, h - 30), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (190, 190, 190), 1)
                if last_macro:
                    cv2.putText(display, f"cmd: {last_macro}",
                                (10, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 140, 255), 2)

                combined = cv2.hconcat([frame, display])
                cv2.imshow("Agent — live | YOLO + reasoning", combined)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        if show:
            cv2.destroyAllWindows()
        elapsed = time.time() - t_start
        print(f"\n{frame_count} frames  |  {len(action_history)} commands sent  |  {elapsed:.1f}s elapsed")


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Claude agent loop — vision → LLM → Switch.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--goal", "-g", required=True,
        help='What Link should try to do. E.g. "explore the area" or "defeat nearby enemies".',
    )
    parser.add_argument(
        "--device", "-d", type=int,
        default=int(os.environ.get("CAPTURE_DEVICE", "1")),
    )
    parser.add_argument(
        "--yolo-model",
        default=os.environ.get("YOLO_MODEL", "yolov8n.pt"),
        help="YOLO model file (default: yolov8n.pt). Swap to models/botw.pt once trained.",
    )
    parser.add_argument(
        "--claude-model",
        default="claude-sonnet-4-5",
        help="Claude model ID. Use claude-haiku-4-5-20251001 for cheaper dev iterations.",
    )
    parser.add_argument(
        "--interval", type=float, default=2.0,
        help="Seconds between Claude calls (default: 2.0). Increase to cut API costs.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print macro strings without sending them to the Switch.")
    parser.add_argument("--no-display", action="store_true",
                        help="Disable the OpenCV display window.")
    args = parser.parse_args()

    pad = None
    if not args.dry_run:
        host = os.environ.get("PI_HOST", "pi.local")
        print(f"Connecting to daemon at {host}...")
        pad = RemotePad(host)
        pad.wait_connected(timeout=15)
        print("Connected.\n")

    run_agent(
        device=args.device,
        yolo_model_path=args.yolo_model,
        claude_model=args.claude_model,
        goal=args.goal,
        pad=pad,
        dry_run=args.dry_run,
        show=not args.no_display,
        agent_interval=args.interval,
    )


if __name__ == "__main__":
    main()
