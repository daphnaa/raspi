#!/usr/bin/env python3
import socket
import threading
import json
import time
import argparse
import requests
from typing import Dict, Any, Optional

BROADCAST_PORT_DEFAULT = 50001
CAPTURE_PATH = "/capture"      # must match capture_service.py
CAPTURE_PORT_DEFAULT = 8088    # default capture_service port


class RaspiRegistry:
    """
    Keeps track of discovered raspi_cam broadcasters.
    Keyed by (host, ip). Stores last_seen + payload.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._nodes: Dict[str, Dict[str, Any]] = {}

    def update(self, payload: dict, src_ip: str):
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "raspi_cam":
            return

        host = str(payload.get("host") or "raspi")
        ip = str(payload.get("ip") or src_ip)
        iface = str(payload.get("iface") or "")
        receiver_url = str(payload.get("receiver_url") or "")

        key = f"{host}@{ip}"
        now = time.time()
        info = {
            "host": host,
            "ip": ip,
            "iface": iface,
            "receiver_url": receiver_url,
            "last_seen": now,
            "raw": payload,
        }
        with self._lock:
            self._nodes[key] = info

    def list_all(self):
        with self._lock:
            # return newest first
            return sorted(self._nodes.values(), key=lambda x: x["last_seen"], reverse=True)

    def latest(self) -> Optional[Dict[str, Any]]:
        items = self.list_all()
        return items[0] if items else None

    def find_by_host(self, host_sub: str) -> Optional[Dict[str, Any]]:
        host_sub = host_sub.lower()
        for info in self.list_all():
            if host_sub in info["host"].lower():
                return info
        return None


def listener_thread(reg: RaspiRegistry, listen_port: int, stop_evt: threading.Event):
    """
    UDP listener for raspi_cam broadcasts.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    # 0.0.0.0:listen_port on all interfaces
    sock.bind(("", listen_port))
    sock.settimeout(1.0)

    print(f"[listener] listening for raspi_cam on UDP *:{listen_port}")

    try:
        while not stop_evt.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception as e:
                print(f"[listener][warn] recv error: {e}")
                continue

            src_ip, src_port = addr
            try:
                msg = data.decode("utf-8", errors="ignore")
                payload = json.loads(msg)
            except Exception:
                # ignore garbage
                continue

            reg.update(payload, src_ip)
            print(f"[listener] from {src_ip}: {payload}")
    finally:
        sock.close()
        print("[listener] stopped")


def choose_target(reg: RaspiRegistry, host_hint: Optional[str]) -> Dict[str, Any]:
    """
    Pick which Pi to use.
    If host_hint is given, try to match by substring.
    Otherwise, take the latest seen.
    Raises RuntimeError if none.
    """
    if host_hint:
        info = reg.find_by_host(host_hint)
        if not info:
            raise RuntimeError(f"No raspi_cam with host containing '{host_hint}' discovered yet")
        return info

    info = reg.latest()
    if not info:
        raise RuntimeError("No raspi_cam discovered yet")
    return info


def trigger_capture(pi_info: Dict[str, Any],
                    name: str,
                    count: int,
                    interval: float,
                    capture_port: int = CAPTURE_PORT_DEFAULT,
                    timeout: float = 30.0):
    """
    Call Pi's /capture endpoint using discovered IP.
    """
    ip = pi_info["ip"]
    url = f"http://{ip}:{capture_port}{CAPTURE_PATH}"
    payload = {
        "name": name,
        "count": int(count),
        "interval": float(interval),
    }
    print(f"[capture] POST {url} {payload}")
    r = requests.post(url, json=payload, timeout=timeout)
    print(f"[capture] status={r.status_code} body={r.text}")
    r.raise_for_status()


def main():
    ap = argparse.ArgumentParser(
        description="Discover raspi_cam via UDP and trigger captures without hardcoding IP"
    )
    ap.add_argument("--listen-port", type=int, default=BROADCAST_PORT_DEFAULT,
                    help=f"UDP port to listen for raspi_cam broadcasts (default: {BROADCAST_PORT_DEFAULT})")
    ap.add_argument("--host", default="",
                    help="Optional substring of Pi hostname to select (if multiple Pis)")
    ap.add_argument("--name", default="session",
                    help="Capture name prefix to send to Pi")
    ap.add_argument("--count", type=int, default=1,
                    help="Number of frames to capture")
    ap.add_argument("--interval", type=float, default=0.0,
                    help="Seconds between frames")
    ap.add_argument("--capture-port", type=int, default=CAPTURE_PORT_DEFAULT,
                    help=f"Pi capture_service HTTP port (default: {CAPTURE_PORT_DEFAULT})")
    ap.add_argument("--wait", type=float, default=10.0,
                    help="Max seconds to wait for discovery before failing")
    args = ap.parse_args()

    reg = RaspiRegistry()
    stop_evt = threading.Event()
    t = threading.Thread(
        target=listener_thread,
        args=(reg, args.listen_port, stop_evt),
        daemon=True,
    )
    t.start()

    # Wait for discovery
    deadline = time.time() + max(0.1, args.wait)
    target = None
    while time.time() < deadline:
        try:
            target = choose_target(reg, args.host or None)
            break
        except RuntimeError:
            time.sleep(0.5)

    if not target:
        stop_evt.set()
        t.join(timeout=1.0)
        raise SystemExit("No raspi_cam discovered within wait window")

    print(f"[selected] host={target['host']} ip={target['ip']} iface={target['iface']}")

    # Trigger capture
    try:
        trigger_capture(
            target,
            name=args.name,
            count=args.count,
            interval=args.interval,
            capture_port=args.capture_port,
        )
    finally:
        stop_evt.set()
        t.join(timeout=1.0)


if __name__ == "__main__":
    main()
