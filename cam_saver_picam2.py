#!/usr/bin/env python3
"""
Save one or more frames from the Raspberry Pi camera using Picamera2.

Examples:
  cam_saver_picam2.py --out /home/pi/captures --name boot --count 5 --interval 1 --width 1280 --height 720
  cam_saver_picam2.py --out /mnt/usb/caps --name sessionA --count 1

Requires: python3-picamera2, Pillow
"""

import argparse
import time
from pathlib import Path
from datetime import datetime

def timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def main():
    parser = argparse.ArgumentParser(description="Save frames from camera to a given path/name.")
    parser.add_argument("--out", required=True, help="Output directory (will be created).")
    parser.add_argument("--name", default="capture", help="Base filename prefix (default: capture).")
    parser.add_argument("--count", type=int, default=1, help="How many frames to capture (default: 1).")
    parser.add_argument("--interval", type=float, default=0.0, help="Seconds between frames (default: 0).")
    parser.add_argument("--width", type=int, default=0, help="Requested width (optional).")
    parser.add_argument("--height", type=int, default=0, help="Requested height (optional).")
    parser.add_argument("--format", default="jpg", choices=["jpg", "jpeg", "png", "bmp"], help="Output format (default: jpg).")
    parser.add_argument("--warmup", type=float, default=0.5, help="Seconds to warm up the sensor (default: 0.5).")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Import late so usage text prints even if picamera2 isn't installed
    from picamera2 import Picamera2

    picam = Picamera2()
    if args.width and args.height:
        config = picam.create_still_configuration(main={"size": (args.width, args.height)})
        picam.configure(config)

    picam.start()
    time.sleep(args.warmup)

    for i in range(args.count):
        fname = f"{args.name}_{timestamp()}_{i:04d}.{args.format}"
        fpath = out_dir / fname
        # capture_file() writes directly without extra conversions
        picam.capture_file(str(fpath))
        if args.interval > 0 and i < args.count - 1:
            time.sleep(args.interval)

    picam.stop()

if __name__ == "__main__":
    main()
