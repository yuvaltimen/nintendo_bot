"""
Capture BotW gameplay frames for YOLO training annotation.

Reads from the capture card and saves every Nth frame as a JPEG.
Play BotW normally while this runs — the more diverse the gameplay
(different areas, enemies, weather, time of day), the better the model.

Usage:
  # Default: save 1 frame per second at 30fps capture:
  CAPTURE_DEVICE=1 python scripts/collect_frames.py --output data/raw

  # Denser sampling (every 15 frames ≈ 2 fps):
  python scripts/collect_frames.py --output data/raw --every 15

Target: 100–200 annotated frames per class. A 20-minute session at
--every 30 yields ~1,200 frames — enough for a solid starter dataset
once you annotate the subset that contains the classes you care about.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import cv2

FRAME_W = 1280
FRAME_H = 720


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture BotW frames for YOLO annotation.")
    parser.add_argument(
        "--device", "-d", type=int,
        default=int(os.environ.get("CAPTURE_DEVICE", "1")),
        help="Capture device index (default: $CAPTURE_DEVICE or 1).",
    )
    parser.add_argument(
        "--output", "-o", default="data/raw",
        help="Directory to save frames (default: data/raw).",
    )
    parser.add_argument(
        "--every", type=int, default=30,
        help="Save every Nth frame (default: 30 → ~1fps at 30fps).",
    )
    args = parser.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(args.device, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        raise SystemExit(
            f"Cannot open capture device {args.device}. "
            "Run: python scripts/vision_loop.py --scan"
        )
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    frame_n = 0
    saved = 0
    t_start = time.time()

    print(f"Saving 1 in every {args.every} frames → {out_dir}/")
    print("Play BotW now. Explore different areas and encounter different enemies.")
    print("Press Q to stop.\n")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.01)
                continue

            frame_n += 1
            if frame_n % args.every == 0:
                path = out_dir / f"frame_{int(time.time() * 1000)}.jpg"
                cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
                saved += 1

            preview = frame.copy()
            elapsed = time.time() - t_start
            rate = saved / elapsed if elapsed > 0 else 0
            cv2.putText(
                preview,
                f"Saved {saved} frames  ({rate:.1f}/s)  → {out_dir}/",
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (0, 255, 0), 2,
            )
            cv2.imshow("Frame capture — play BotW, press Q to stop", preview)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        cv2.destroyAllWindows()
        elapsed = time.time() - t_start
        print(f"\n{saved} frames saved in {elapsed:.0f}s  →  {out_dir}/")
        print("\nNext steps:")
        print("  1. Upload data/raw/ to Roboflow (roboflow.com) — free tier handles up to 1000 images.")
        print("  2. Annotate bounding boxes for each class you care about.")
        print("  3. Export as 'YOLOv8' format → downloads dataset.yaml + train/val/test splits.")
        print("  4. Train:  yolo train data=path/to/dataset.yaml model=yolov8n.pt epochs=50 imgsz=640")
        print("  5. Use:    python scripts/agent_loop.py --yolo-model runs/detect/train/weights/best.pt ...")


if __name__ == "__main__":
    main()
