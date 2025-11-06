#!/usr/bin/env python3
import argparse, json, time, threading, io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from picamera2 import Picamera2
import cv2, requests, os
import subprocess
import socket
import ipaddress



BCAST_DEFAULT_PORT = 50000


def get_iface_ip(iface: str) -> str | None:
    """
    Return IPv4 address for given interface (e.g. wlan0), or None.
    Uses `ip -4 addr show`.
    """
    try:
        out = subprocess.check_output(
            ["ip", "-4", "addr", "show", iface],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None

    for line in out.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            # inet 172.16.17.9/24 ...
            return line.split()[1].split("/")[0]
    return None


def broadcast_ip_loop(stop_event: threading.Event,
                      iface: str,
                      port: int,
                      receiver_url: str,
                      interval: float = 10.0):
    """
    Periodically broadcast this Pi's IP over UDP.
    """
    hostname = socket.gethostname()

    while not stop_event.is_set():
        ip = get_iface_ip(iface)
        if ip:
            payload = {
                "type": "raspi_cam",
                "host": hostname,
                "iface": iface,
                "ip": ip,
                "receiver_url": receiver_url,
            }
            data = json.dumps(payload).encode("utf-8")

            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.settimeout(0.2)
                s.sendto(data, ("255.255.255.255", port))
            except Exception:
                # keep it quiet; we'll try again next interval
                pass
            finally:
                try:
                    s.close()
                except Exception:
                    pass

        # even if no IP yet, retry later
        stop_event.wait(interval)

DISCOVERY_PORT = 50001

def discover_receiver(timeout: float = 8.0) -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", DISCOVERY_PORT))
    s.settimeout(1.0)

    deadline = time.time() + timeout
    print(f"[discover] listening for capture_receiver on UDP *:{DISCOVERY_PORT} for up to {timeout}s")

    try:
        while time.time() < deadline:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            try:
                msg = json.loads(data.decode("utf-8", errors="ignore"))
            except Exception:
                continue
            if isinstance(msg, dict) and msg.get("type") == "capture_receiver":
                url = (msg.get("url") or "").strip()
                if url:
                    print(f"[discover] got receiver {url} from {addr}")
                    return url
    finally:
        s.close()

    print("[discover] no receiver found")
    return discover_receiver_via_scan(port=5001, timeout=0.6)


def discover_receiver_via_scan(port: int = 5001, timeout: float = 0.7) -> str | None:
    net = _get_wlan_subnet("wlan0")
    if not net:
        print("[discover-scan] no wlan0 subnet found")
        return None

    print(f"[discover-scan] scanning {net} for receiver on port {port}")

    for ip in net.hosts():
        url = f"http://{ip}:{port}/health"
        try:
            r = requests.get(url, timeout=timeout)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        try:
            j = r.json()
        except Exception:
            continue

        print(f"[discover-scan] {ip}: {j}")  # debug

        if isinstance(j, dict) and ((j.get("ok") is True) or (j.get("status") == "ok")):
            base = f"http://{ip}:{port}"
            print(f"[discover-scan] found receiver at {base}")
            return base

    print("[discover-scan] no receiver found")
    return None




def _get_wlan_subnet(iface: str = "wlan0"):
    """
    Returns an ipaddress.IPv4Network for iface (e.g. wlan0) or None.
    """
    try:
        out = subprocess.check_output(["ip", "-4", "addr", "show", iface], text=True)
    except Exception:
        return None

    ip = None
    prefix = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            # inet 172.20.10.8/28 brd 172.20.10.15 scope global wlan0
            parts = line.split()
            cidr = parts[1]             # 172.20.10.8/28
            ip_str, prefixlen = cidr.split("/")
            ip = ip_str
            prefix = int(prefixlen)
            break

    if not (ip and prefix):
        return None

    try:
        # Strict=False so host IP can be inside the network
        return ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
    except Exception:
        return None


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
                cfg = self.cam.create_still_configuration(
                    main={"size": (self.width, self.height), "format": "RGB888"}
                )
                self.cam.configure(cfg)
            self.cam.start()
            time.sleep(self.warmup)

    def capture_and_send(self, name: str, count: int, interval: float):
        with self.lock:
            self._ensure_cam()
            for i in range(max(1, count)):
                # Camera gives RGB888
                frame_rgb = self.cam.capture_array()

                # OpenCV encoder expects BGR → convert once
                frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_BGR2RGB)

                ok, jpg = cv2.imencode(".jpg", frame_bgr,
                                       [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
                if not ok:
                    raise RuntimeError("cv2.imencode failed")

                files = {"image": ("frame.jpg", io.BytesIO(jpg.tobytes()), "image/jpeg")}
                data = {"name": name, "index": str(i)}
                r = requests.post(f"{self.receiver_url}/upload",
                                  files=files, data=data, timeout=60)
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
    ap.add_argument(
        "--receiver-url",
        default=os.environ.get("RECEIVER_URL", ""),
        help="Receiver base URL, e.g. http://x.x.x.x:5001 (if empty → auto-discover)"
    )
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--quality", type=int, default=90)
    ap.add_argument("--warmup", type=float, default=0.8)
    args = ap.parse_args()

    if not args.receiver_url:
        args.receiver_url = discover_receiver_via_scan(port=5001, timeout=0.7) or ""

    if not args.receiver_url:
        raise SystemExit("[capture_service] ERROR: no receiver-url and auto-discovery failed")

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
