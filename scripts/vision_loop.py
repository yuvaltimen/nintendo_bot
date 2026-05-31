"""
Switch HDMI capture → YOLO inference → control loop.

Reads frames from a USB capture card, runs YOLO, calls policy() once per
frame, and sends any returned macro string to the Pi daemon.

────────────────────────────────────────────────────────
Quick start:

  # 1. Find your capture card's device index:
  python scripts/vision_loop.py --scan

  # 2. Verify the stream looks right (no commands sent):
  python scripts/vision_loop.py --device 1 --dry-run

  # 3. Run live — side-by-side clean + annotated windows (default):
  CAPTURE_DEVICE=1 python scripts/vision_loop.py

  # 3a. Clean window only — use when this is your primary game monitor:
  CAPTURE_DEVICE=1 python scripts/vision_loop.py --clean

  # 3b. OBS Virtual Camera as source (OBS owns the physical card, you watch in OBS):
  CAPTURE_DEVICE=2 python scripts/vision_loop.py --no-display

Environment variables:
  PI_HOST          Pi hostname/IP (default: pi.local)
  CAPTURE_DEVICE   Device index for the capture card (default: 1)
  YOLO_MODEL       Model path or ultralytics name (default: yolov8n.pt)
────────────────────────────────────────────────────────

The only function you need to edit is policy() below.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_control import RemotePad  # noqa: E402

FRAME_W = 1280
FRAME_H = 720
TARGET_FPS = 30


# ─────────────────────────────────────────────────────────────
# POLICY — edit this to define the control logic.
#
# Called on every frame that passes the cooldown gate.
# Return a macro string to send to the Switch, or None.
#
# Args:
#   frame    — BGR numpy array (H×W×3)
#   results  — ultralytics Results object (results.boxes, results.names, etc.)
#   w, h     — frame dimensions for normalizing coordinates
# ─────────────────────────────────────────────────────────────

def policy(frame, results, w: int, h: int) -> str | None:
    """Stub policy — replace with your own logic.

    Example below: attack (Y) if a detected object is centered horizontally.
    """
    if results is None or results.boxes is None or len(results.boxes) == 0:
        return None

    center_x = w / 2

    for box in results.boxes:
        cls_name = results.names[int(box.cls)]
        conf = float(box.conf)
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        box_cx = (x1 + x2) / 2

        # Example rule: attack anything labeled "person" near the screen center.
        # Swap in your own class names and conditions.
        if cls_name == "person" and conf > 0.5:
            if abs(box_cx - center_x) < w / 4:
                return "Y 0.1s"

    return None


# ─────────────────────────────────────────────────────────────
# Capture helpers
# ─────────────────────────────────────────────────────────────

def scan_devices(max_idx: int = 8) -> None:
    print("Scanning for video capture devices...\n")
    found = False
    for i in range(max_idx):
        cap = cv2.VideoCapture(i, cv2.CAP_AVFOUNDATION)
        if cap.isOpened():
            ret, _ = cap.read()
            fw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            fh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            status = "readable" if ret else "no frames"
            print(f"  [{i}]  {status:<12}  {fw}x{fh} @ {fps:.0f} fps")
            found = True
        cap.release()
    if not found:
        print("  No devices found. Is the capture card plugged in?")
    print()


def open_capture(device: int) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(device, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        raise RuntimeError(
            f"Cannot open capture device {device}. "
            "Run with --scan to list available devices."
        )
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
    cap.set(cv2.CAP_PROP_FPS, TARGET_FPS)
    # Keep buffer minimal so we always read the freshest frame.
    # The Switch runs at 30fps; a stale buffered frame adds 33ms+ of lag.
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


# ─────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────

def run_loop(
    device: int,
    model_path: str,
    pad: RemotePad | None,
    dry_run: bool,
    show: bool,
    clean: bool,
    command_cooldown: float,
) -> None:
    from ultralytics import YOLO  # imported here so --scan works without ultralytics

    print(f"Loading YOLO model: {model_path}")
    model = YOLO(model_path)

    cap = open_capture(device)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    actual_fps = cap.get(cv2.CAP_PROP_FPS)
    print(f"Capture device {device}: {w}x{h} @ {actual_fps:.0f} fps")
    print(f"Command cooldown: {command_cooldown}s | dry-run: {dry_run}")
    if show:
        mode = "clean + annotated side-by-side" if not clean else "clean only"
        print(f"Display: {mode}")
    print("Press Q in the display window, or Ctrl-C, to stop.\n")

    last_command_t = 0.0
    frame_count = 0
    t_start = time.time()
    last_macro = None

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.005)
                continue

            frame_count += 1
            now = time.time()

            results = model(frame, verbose=False)[0]

            macro = None
            if now - last_command_t >= command_cooldown:
                macro = policy(frame, results, w, h)
                if macro:
                    last_command_t = now
                    last_macro = macro
                    if dry_run:
                        print(f"[dry-run]  {macro!r}")
                    else:
                        try:
                            pad.macro(macro)
                            print(f"[sent]     {macro!r}")
                        except Exception as e:
                            print(f"[error]    {e}")

            if show:
                elapsed = now - t_start
                fps_live = frame_count / elapsed if elapsed > 0 else 0

                if clean:
                    # Raw frame, no annotations — use this as your primary game monitor.
                    display = frame.copy()
                    cv2.putText(
                        display, f"{fps_live:.1f} fps",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2,
                    )
                    if last_macro:
                        cv2.putText(
                            display, f"last cmd: {last_macro}",
                            (10, h - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2,
                        )
                    cv2.imshow("Switch — live", display)
                else:
                    # Side-by-side: clean frame on the left, YOLO annotations on the right.
                    annotated = results.plot()
                    cv2.putText(
                        annotated, f"{fps_live:.1f} fps",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2,
                    )
                    if last_macro:
                        cv2.putText(
                            annotated, f"last cmd: {last_macro}",
                            (10, h - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 140, 255), 2,
                        )
                    combined = cv2.hconcat([frame, annotated])
                    cv2.imshow("Switch — live | YOLO", combined)

                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        if show:
            cv2.destroyAllWindows()
        elapsed = time.time() - t_start
        avg_fps = frame_count / elapsed if elapsed > 0 else 0
        print(f"\n{frame_count} frames in {elapsed:.1f}s ({avg_fps:.1f} fps avg)")


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Switch HDMI capture + YOLO control loop.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--scan", action="store_true",
        help="List all capture devices and exit.",
    )
    parser.add_argument(
        "--device", "-d", type=int,
        default=int(os.environ.get("CAPTURE_DEVICE", "1")),
        help="Capture device index (default: $CAPTURE_DEVICE or 1).",
    )
    parser.add_argument(
        "--model", "-m",
        default=os.environ.get("YOLO_MODEL", "yolov8n.pt"),
        help="YOLO model file or ultralytics name (default: yolov8n.pt).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print commands instead of sending them to the Switch.",
    )
    parser.add_argument(
        "--no-display", action="store_true",
        help="Disable the OpenCV preview window (useful headless / over SSH).",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help=(
            "Show a clean frame window only (no YOLO boxes). "
            "Use this when the capture card is your primary game monitor — "
            "gives you a full view of the game without annotation clutter. "
            "Default (without --clean) shows clean + annotated side-by-side."
        ),
    )
    parser.add_argument(
        "--cooldown", type=float, default=0.5,
        help="Minimum seconds between commands (default: 0.5).",
    )
    args = parser.parse_args()

    if args.scan:
        scan_devices()
        return

    pad = None
    if not args.dry_run:
        host = os.environ.get("PI_HOST", "pi.local")
        print(f"Connecting to daemon at {host}...")
        pad = RemotePad(host)
        pad.wait_connected(timeout=15)
        print("Connected.\n")

    run_loop(
        device=args.device,
        model_path=args.model,
        pad=pad,
        dry_run=args.dry_run,
        show=not args.no_display,
        clean=args.clean,
        command_cooldown=args.cooldown,
    )


if __name__ == "__main__":
    main()
