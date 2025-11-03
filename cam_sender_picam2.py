#!/usr/bin/env python3
"""
Capture N frames with Picamera2 and stream each as a JPEG over TCP to a receiver.

Protocol per frame:
  magic "IMG1" (4 bytes)
  name_len (uint16, big-endian)
  img_len  (uint32, big-endian)
  name     (name_len bytes, utf-8)
  img      (img_len bytes)
"""

import argparse, time, socket, struct
from datetime import datetime
from pathlib import Path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="Receiver IP/hostname")
    ap.add_argument("--port", type=int, default=5001, help="Receiver TCP port (default: 5001)")
    ap.add_argument("--name", default="capture", help="Base name sent to receiver")
    ap.add_argument("--count", type=int, default=5, help="How many frames to send")
    ap.add_argument("--interval", type=float, default=1.0, help="Seconds between frames")
    ap.add_argument("--width", type=int, default=0, help="Capture width")
    ap.add_argument("--height", type=int, default=0, help="Capture height")
    ap.add_argument("--quality", type=int, default=90, help="JPEG quality (0-100)")
    ap.add_argument("--warmup", type=float, default=0.7, help="Sensor warm-up seconds")
    args = ap.parse_args()

    # Late imports keep usage helpful even if deps missing
    from picamera2 import Picamera2
    import cv2

    cam = Picamera2()
    if args.width and args.height:
        cfg = cam.create_still_configuration(main={"size": (args.width, args.height)})
        cam.configure(cfg)
    cam.start()
    time.sleep(args.warmup)

    # one TCP connection for all frames (efficient)
    with socket.create_connection((args.host, args.port), timeout=5) as sock:
        for i in range(args.count):
            # capture to numpy array, encode to JPEG in-memory
            frame = cam.capture_array()
            ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), args.quality])
            if not ok:
                raise RuntimeError("cv2.imencode failed")
            img_bytes = jpg.tobytes()
            name_bytes = args.name.encode("utf-8")

            # send header + payload
            sock.sendall(b"IMG1")
            sock.sendall(struct.pack("!H", len(name_bytes)))
            sock.sendall(struct.pack("!I", len(img_bytes)))
            sock.sendall(name_bytes)
            sock.sendall(img_bytes)

            # optional read of ACK (non-blocking ok)
            try:
                sock.settimeout(0.5)
                _ = sock.recv(2)
                sock.settimeout(None)
            except Exception:
                pass

            if i < args.count - 1 and args.interval > 0:
                time.sleep(args.interval)

    cam.stop()

if __name__ == "__main__":
    main()
