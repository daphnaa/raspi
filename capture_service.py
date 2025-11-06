#!/usr/bin/env python3
import argparse, json, time, threading, io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from picamera2 import Picamera2
import cv2, requests

class CaptureWorker:
    def __init__(self, receiver_url, width, height, quality, warmup):
        self.receiver_url = receiver_url.rstrip("/")
        self.width, self.height = width, height
        self.quality, self.warmup = quality, warmup
        self.lock = threading.Lock()
        self.cam = None

    def _ensure_cam(self):
        if self.cam is None:
            self.cam = Picamera2()
            if self.width and self.height:
                cfg = self.cam.create_still_configuration(main={"size": (self.width, self.height)})
                self.cam.configure(cfg)
            self.cam.start()
            time.sleep(self.warmup)

    def capture_and_send(self, name: str, count: int, interval: float):
        with self.lock:  # serialize captures
            self._ensure_cam()
            for i in range(max(1, count)):
                frame = self.cam.capture_array()
                ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
                if not ok:
                    raise RuntimeError("cv2.imencode failed")
                files = {"image": ("frame.jpg", io.BytesIO(jpg.tobytes()), "image/jpeg")}
                data = {"name": name, "index": str(i)}
                r = requests.post(f"{self.receiver_url}/upload", files=files, data=data, timeout=60)
                r.raise_for_status()
                if i < count - 1 and interval > 0:
                    time.sleep(interval)

class Handler(BaseHTTPRequestHandler):
    worker: CaptureWorker = None  # set at server bootstrap

    def _json(self, code=200, obj=None):
        body = json.dumps(obj or {}, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._json(200, {"status": "ok"})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/capture":
            return self._json(404, {"error": "not found"})

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json(400, {"error": "invalid json"})

        name = str(payload.get("name", "capture"))
        count = int(payload.get("count", 1))
        interval = float(payload.get("interval", 0.0))

        try:
            self.worker.capture_and_send(name=name, count=count, interval=interval)
            return self._json(200, {"ok": True, "sent": count})
        except Exception as e:
            return self._json(500, {"error": str(e)})

def main():
    ap = argparse.ArgumentParser(description="HTTP-triggered Picamera2 capture → HTTP upload")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--receiver-url", required=True, help="Receiver base URL, e.g. http://192.168.1.50:5001")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--quality", type=int, default=90)
    ap.add_argument("--warmup", type=float, default=0.7)
    args = ap.parse_args()

    worker = CaptureWorker(args.receiver_url, args.width, args.height, args.quality, args.warmup)
    Handler.worker = worker
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"[capture_service] listening on {args.bind}:{args.port} → sending to {args.receiver_url}/upload")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
