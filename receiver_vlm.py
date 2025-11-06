#!/usr/bin/env python3
from flask import Flask, request
from pathlib import Path
import datetime as dt
import json, os, io, time, requests

# --- config ---
SAVE_ROOT = Path("/tmp/incoming_frames")
# default to PATH mode because your VLM exposes /describe (JSON {"image_path": ...})
VLM_URL   = os.environ.get("VLM_URL", "http://127.0.0.1:8080/describe")
VLM_MODE  = os.environ.get("VLM_MODE", "path")  # "path" or "upload"

# OPTIONAL: path remap (host -> container), leave empty if you mounted same path
REMAP_SRC = os.environ.get("REMAP_SRC", "/tmp/incoming_frames")
REMAP_DST = os.environ.get("REMAP_DST", "/tmp/incoming_frames")

# group multiple uploads into the same session dir if they arrive close together
SESSION_REUSE_SEC = int(os.environ.get("SESSION_REUSE_SEC", "120"))  # 2 minutes

SAVE_ROOT.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)

# in-memory session cache: name -> (session_dir, created_ts)
_sessions = {}

def _session_dir(name: str | None, session_hint: str | None) -> Path:
    """
    Determine the session directory:
    - If client sent 'session' (session_hint), honor it: /tmp/incoming_frames/<session_hint>
    - Else reuse recent session per 'name' within SESSION_REUSE_SEC
    - Else create new YYYY_MM_DD___HH_MM_SS
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
    # map host path to container path if needed
    if REMAP_SRC and REMAP_DST and p.startswith(REMAP_SRC):
        return REMAP_DST + p[len(REMAP_SRC):]
    return p

def vlm_call_upload(img_bytes: bytes, timeout=60.0) -> str:
    # This requires your VLM to expose /describe_upload (multipart). If you don’t have that, use PATH mode.
    files = {"image": ("frame.jpg", io.BytesIO(img_bytes), "image/jpeg")}
    r = requests.post(VLM_URL, files=files, timeout=timeout)
    r.raise_for_status()
    try:
        j = r.json()
        return j.get("response") or j.get("caption") or json.dumps(j, ensure_ascii=False) if isinstance(j, dict) else json.dumps(j, ensure_ascii=False)
    except ValueError:
        return r.text.strip()

def vlm_call_path(image_path: str, timeout=60.0) -> str:
    payload = {"image_path": image_path}
    r = requests.post(VLM_URL, json=payload, timeout=timeout)
    r.raise_for_status()
    try:
        j = r.json()
        return j.get("response") or j.get("caption") or json.dumps(j, ensure_ascii=False) if isinstance(j, dict) else json.dumps(j, ensure_ascii=False)
    except ValueError:
        return r.text.strip()

@app.get("/health")
def health():
    return {"status": "ok", "vlm_url": VLM_URL, "mode": VLM_MODE}

@app.post("/upload")
def upload():
    f = request.files.get("image")
    name = request.form.get("name", "capture")
    idx  = request.form.get("index", "0")
    session_hint = request.form.get("session", "").strip() or None  # optional client-provided session id

    if not f:
        return {"error":"no file"}, 400

    session_dir = _session_dir(name, session_hint)
    stem = f"{name}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}_{idx}"
    img_path = session_dir / f"{stem}.jpg"
    f.save(img_path)

    # tiny settle to avoid races
    time.sleep(0.05)

    # call VLM
    caption = None
    err = None
    try:
        if VLM_MODE.lower() == "upload":
            caption = vlm_call_upload(img_path.read_bytes())
        else:
            vlm_path = _remap_for_vlm(str(img_path))
            caption = vlm_call_path(vlm_path)
    except Exception as e:
        err = str(e)

    # sidecar JSON in the same session directory
    sidecar = session_dir / f"{stem}.json"
    sidecar.write_text(json.dumps({
        "name": name,
        "index": int(idx),
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "image": img_path.name,
        "session_dir": str(session_dir),
        "caption": caption,
        "error": err
    }, indent=2))

    status = 200 if caption and not err else 502 if err else 200
    return {"ok": bool(caption), "saved": str(img_path), "session_dir": str(session_dir), "caption": caption, "error": err}, status

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
