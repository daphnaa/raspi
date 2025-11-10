#!/usr/bin/env python3
from flask import Flask, request
from pathlib import Path
import datetime as dt
import json
import os
import io
import time
import re
import threading
import socket
import subprocess
import requests

# ----------------- Config -----------------

SAVE_ROOT = Path("/tmp/incoming_frames")

# VLM describe endpoint (VILA-style JSON API)
VLM_URL = os.environ.get("VLM_URL", "http://127.0.0.1:8080/describe")
# "path" → send {"image_path": "..."}
# "upload" → send multipart with the image bytes
VLM_MODE = os.environ.get("VLM_MODE", "path").lower()

# Optional path remapping for VLM container/host differences
REMAP_SRC = os.environ.get("REMAP_SRC", "/tmp/incoming_frames")
REMAP_DST = os.environ.get("REMAP_DST", "/tmp/incoming_frames")

# Seconds to reuse same session folder for same "name"
SESSION_REUSE_SEC = int(os.environ.get("SESSION_REUSE_SEC", "120"))

# Discovery / beacon config
DISCOVERY_PORT = 50001                     # Pi listens here for JSON beacons
LISTEN_PORT = int(os.environ.get("UPLOAD_PORT", "5001"))  # our Flask port
# Comma-separated list of ifaces to try for beacon IP (Jetson wifi/eth)
BEACON_IFACES = os.environ.get("BEACON_IFACES", "wlP1p1s0,wlan0,eth0").split(",")

SAVE_ROOT.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)

# ----------------- Pose parsing from filename -----------------

_RX_COMPACT = re.compile(
    r".*?/x(?P<x>-?\d{1,6})y(?P<y>-?\d{1,6})z(?P<z>-?\d{1,6})yaw(?P<yaw>-?\d{1,9})(?:__[^/]+)?\.[A-Za-z0-9]+$"
)
_RX_UNDERSCORE = re.compile(
    r".*?_x(?P<x>-?\d+(?:\.\d+)?)_y(?P<y>-?\d+(?:\.\d+)?)_z(?P<z>-?\d+(?:\.\d+)?)_yaw(?P<yaw>-?\d+(?:\.\d+)?)(?:\.[A-Za-z0-9]+)$"
)


def _parse_pose_from_name(path: Path):
    s = str(path)
    m = _RX_COMPACT.match(s)
    if m:
        gd = m.groupdict()
        try:
            x_mm = int(gd["x"])
            y_mm = int(gd["y"])
            z_mm = int(gd["z"])
            yaw_u = int(gd["yaw"])
            return {
                "x": x_mm / 1000.0,
                "y": y_mm / 1000.0,
                "z": z_mm / 1000.0,
                "yaw": yaw_u / 1_000_000.0,
            }
        except Exception:
            pass

    m = _RX_UNDERSCORE.match(s)
    if m:
        gd = m.groupdict()
        try:
            return {
                "x": float(gd["x"]),
                "y": float(gd["y"]),
                "z": float(gd["z"]),
                "yaw": float(gd["yaw"]),
            }
        except Exception:
            pass

    # default if nothing encoded
    return {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}


# ----------------- Session dir management -----------------

_sessions = {}


def _session_dir(name: str | None, session_hint: str | None) -> Path:
    """
    If 'session' form-field is set → use that subdir.
    Else reuse a per-name folder for SESSION_REUSE_SEC seconds.
    Else create fresh YYYY_MM_DD___HH_MM_SS under SAVE_ROOT.
    """
    now = time.time()

    if session_hint:
        d = SAVE_ROOT / session_hint
        d.mkdir(parents=True, exist_ok=True)
        return d

    key = (name or "_default").strip() or "_default"
    sess = _sessions.get(key)
    if sess and (now - sess[1] <= SESSION_REUSE_SEC):
        return sess[0]

    stamp = dt.datetime.now().strftime("%Y_%m_%d___%H_%M_%S")
    d = SAVE_ROOT / stamp
    d.mkdir(parents=True, exist_ok=True)
    _sessions[key] = (d, now)
    return d


def _remap_for_vlm(p: str) -> str:
    if REMAP_SRC and REMAP_DST and p.startswith(REMAP_SRC):
        return REMAP_DST + p[len(REMAP_SRC):]
    return p


# ----------------- Network helpers for beacon -----------------

def get_iface_ip(iface: str) -> str | None:
    """
    Return IPv4 address for a given interface, or None.
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
            return line.split()[1].split("/")[0]
    return None


def _get_local_ip() -> str | None:
    """
    Generic outbound-IP heuristic (UDP connect to 8.8.8.8).
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def beacon_loop(stop_event: threading.Event, upload_port: int):
    """
    Periodically broadcast:
      {
        "type": "capture_receiver",
        "url": "http://<ip>:<upload_port>",
        "host": "<hostname>",
        "version": 1
      }
    on UDP DISCOVERY_PORT so the Pi can auto-discover us.
    """
    hostname = socket.gethostname()
    last_ip = None

    while not stop_event.is_set():
        ip = None

        # try configured ifaces first
        for iface in BEACON_IFACES:
            iface = iface.strip()
            if not iface:
                continue
            ip = get_iface_ip(iface)
            if ip:
                break

        # fallback to generic
        if not ip:
            ip = _get_local_ip()

        if ip and ip != last_ip:
            print(f"[beacon] advertising capture_receiver at http://{ip}:{upload_port}")
            last_ip = ip
        elif not ip:
            print("[beacon] WARN: no IP detected for beacon")

        payload = {
            "type": "capture_receiver",
            "url": f"http://{ip}:{upload_port}" if ip else "",
            "host": hostname,
            "version": 1,
        }
        data = json.dumps(payload).encode("utf-8")

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(0.2)
            s.sendto(data, ("255.255.255.255", DISCOVERY_PORT))
        except Exception:
            pass
        finally:
            try:
                s.close()
            except Exception:
                pass

        stop_event.wait(5.0)


# ----------------- VLM calls -----------------

def vlm_call_upload(img_bytes: bytes, timeout=60.0):
    files = {"image": ("frame.jpg", io.BytesIO(img_bytes), "image/jpeg")}
    r = requests.post(VLM_URL, files=files, timeout=timeout)
    r.raise_for_status()
    try:
        j = r.json()
        return j, json.dumps(j, ensure_ascii=False)
    except ValueError:
        txt = r.text.strip()
        return None, txt


def vlm_call_path(image_path: str, timeout=60.0):
    payload = {"image_path": image_path}
    r = requests.post(VLM_URL, json=payload, timeout=timeout)
    r.raise_for_status()
    try:
        j = r.json()
        return j, json.dumps(j, ensure_ascii=False)
    except ValueError:
        txt = r.text.strip()
        return None, txt


# ----------------- Async VLM + JSON writer -----------------

def process_vlm_async(img_path: Path, stem: str, session_dir: Path):
    try:
        # tiny delay to avoid partial-write races
        time.sleep(0.05)

        vlm_json, vlm_raw, err = None, None, None
        try:
            if VLM_MODE == "upload":
                vlm_json, vlm_raw = vlm_call_upload(img_path.read_bytes())
            else:
                vlm_json, vlm_raw = vlm_call_path(_remap_for_vlm(str(img_path)))
        except Exception as e:
            err = str(e)

        pose = _parse_pose_from_name(img_path)
        auto_prompt = "Describe the objects in the image"
        response_text = None

        if isinstance(vlm_json, dict):
            auto_prompt = vlm_json.get("auto_prompt") or auto_prompt
            response_text = (
                vlm_json.get("response_describe")
                or vlm_json.get("response")
                or None
            )

        if not response_text and isinstance(vlm_raw, str):
            response_text = vlm_raw

        # Build sidecar object (merge if exists)
        sidecar_path = session_dir / f"{stem}.json"
        obj = {}
        if sidecar_path.exists():
            try:
                with open(sidecar_path, "r", encoding="utf-8") as f:
                    obj = json.load(f)
            except Exception:
                obj = {}

        if not isinstance(obj, dict):
            obj = {}

        obj["pose"] = pose
        obj["image"] = img_path.name

        if response_text:
            entries = obj.setdefault("entries", [])
            entries.append(
                {
                    "timestamp": int(time.time()),
                    "prompt": auto_prompt,
                    "response": response_text,
                }
            )

        # Store raw/full VLM payload as string for debugging / downstream
        if isinstance(vlm_json, dict):
            obj["vlm_caption"] = json.dumps(vlm_json, ensure_ascii=False)
        elif isinstance(vlm_raw, str):
            obj["vlm_caption"] = vlm_raw

        if err and "vlm_error" not in obj:
            obj["vlm_error"] = err

        tmp = str(sidecar_path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, sidecar_path)

        print(f"[receiver_vlm][json] updated: {sidecar_path}")

    except Exception as e:
        print(f"[receiver_vlm][async][error] {e}")


# ----------------- Routes -----------------

@app.get("/health")
def health():
    return {"status": "ok", "vlm_url": VLM_URL, "mode": VLM_MODE}


@app.post("/upload")
def upload():
    f = request.files.get("image")
    name = request.form.get("name", "capture")
    idx = request.form.get("index", "0")
    session_hint = (request.form.get("session") or "").strip() or None

    if not f:
        return {"error": "no file"}, 400

    session_dir = _session_dir(name, session_hint)
    stamp_short = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{name}_{stamp_short}_{idx}"

    img_path = session_dir / f"{stem}.jpg"
    f.save(img_path)

    # fire-and-forget VLM; don't block the Pi
    threading.Thread(
        target=process_vlm_async,
        args=(img_path, stem, session_dir),
        daemon=True,
    ).start()

    return {
        "ok": True,
        "saved": str(img_path),
        "session_dir": str(session_dir),
    }, 200


# ----------------- Main -----------------

if __name__ == "__main__":
    stop_event = threading.Event()
    t = threading.Thread(
        target=beacon_loop,
        args=(stop_event, LISTEN_PORT),
        daemon=True,
    )
    t.start()

    try:
        app.run(host="0.0.0.0", port=LISTEN_PORT)
    finally:
        stop_event.set()
