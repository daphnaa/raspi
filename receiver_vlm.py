#!/usr/bin/env python3
from flask import Flask, request
from pathlib import Path
import datetime as dt
import json, os, io, time, re, requests
import threading


# --- config ---
SAVE_ROOT = Path("/tmp/incoming_frames")
VLM_URL   = os.environ.get("VLM_URL", "http://127.0.0.1:8080/describe")
VLM_MODE  = os.environ.get("VLM_MODE", "path")  # "path" or "upload"

# אם נתיב ההרצה בתוך הקונטיינר שונה, אפשר למפות כאן (host->container)
REMAP_SRC = os.environ.get("REMAP_SRC", "/tmp/incoming_frames")
REMAP_DST = os.environ.get("REMAP_DST", "/tmp/incoming_frames")

SESSION_REUSE_SEC = int(os.environ.get("SESSION_REUSE_SEC", "120"))

SAVE_ROOT.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)

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
            x_mm  = int(gd["x"]); y_mm = int(gd["y"]); z_mm = int(gd["z"])
            yaw_u = int(gd["yaw"])
            return {"x": x_mm/1000.0, "y": y_mm/1000.0, "z": z_mm/1000.0, "yaw": yaw_u/1_000_000.0}
        except Exception:
            pass
    m = _RX_UNDERSCORE.match(s)
    if m:
        gd = m.groupdict()
        try:
            return {"x": float(gd["x"]), "y": float(gd["y"]), "z": float(gd["z"]), "yaw": float(gd["yaw"])}
        except Exception:
            pass
    # default pose if none encoded
    return {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}

# --- session dir management ---
_sessions = {}
def _session_dir(name: str | None, session_hint: str | None) -> Path:
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

# --- VLM calls ---
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

def process_vlm_async(img_path: Path, stem: str, session_dir: Path):
    try:
        # small delay so file is fully flushed
        time.sleep(0.05)

        vlm_json, vlm_raw = None, None
        err = None
        try:
            if VLM_MODE.lower() == "upload":
                vlm_json, vlm_raw = vlm_call_upload(img_path.read_bytes())
            else:
                vlm_json, vlm_raw = vlm_call_path(_remap_for_vlm(str(img_path)))
        except Exception as e:
            err = str(e)

        pose = _parse_pose_from_name(img_path) if "_parse_pose_from_name" in globals() else None
        if not isinstance(pose, dict):
            pose = {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}

        auto_prompt = "Describe the objects in the image"
        response_text = None
        if isinstance(vlm_json, dict):
            auto_prompt = vlm_json.get("auto_prompt") or auto_prompt
            response_text = vlm_json.get("response_describe") or vlm_json.get("response") or None
        if not response_text and isinstance(vlm_raw, str):
            response_text = vlm_raw

        vlm_caption = None
        if isinstance(vlm_json, dict):
            vlm_caption = json.dumps(vlm_json, ensure_ascii=False)
        elif isinstance(vlm_raw, str):
            vlm_caption = vlm_raw

        entries = []
        if response_text:
            entries.append({
                "timestamp": int(time.time()),
                "prompt": auto_prompt,
                "response": response_text
            })

        sidecar_path = session_dir / f"{stem}.json"

        # merge if exists
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
        if entries:
            obj.setdefault("entries", []).extend(entries)
        if vlm_caption:
            obj["vlm_caption"] = vlm_caption
        if err and "vlm_error" not in obj:
            obj["vlm_error"] = err

        tmp = str(sidecar_path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, sidecar_path)

        print(f"[receiver_vlm][json] updated: {sidecar_path}")

    except Exception as e:
        print(f"[receiver_vlm][async][error] {e}")

# --- routes ---
@app.get("/health")
def health():
    return {"status":"ok", "vlm_url": VLM_URL, "mode": VLM_MODE}

@app.post("/upload")
def upload():
    f = request.files.get("image")
    name = request.form.get("name", "capture")
    idx  = request.form.get("index", "0")
    session_hint = request.form.get("session", "").strip() or None

    if not f:
        return {"error": "no file"}, 400

    session_dir = _session_dir(name, session_hint)
    stamp_short = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{name}_{stamp_short}_{idx}"
    img_path = session_dir / f"{stem}.jpg"
    f.save(img_path)

    # kick off async VLM + JSON update
    threading.Thread(
        target=process_vlm_async,
        args=(img_path, stem, session_dir),
        daemon=True
    ).start()

    # return immediately → Pi never times out
    return {
        "ok": True,
        "saved": str(img_path),
        "session_dir": str(session_dir)
    }, 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
