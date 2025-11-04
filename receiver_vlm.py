#!/usr/bin/env python3
from flask import Flask, request
from pathlib import Path
import datetime, json, os, io, time, requests

# --- config ---
SAVE_DIR = Path("/tmp/incoming_frames")
VLM_URL  = os.environ.get("VLM_URL", "http://127.0.0.1:8080/describe")  # adjust to your VLM endpoint
VLM_MODE = os.environ.get("VLM_MODE", "upload")  # "upload" or "path"
SAVE_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

def vlm_call_upload(img_bytes: bytes, timeout=30.0):
    files = {"image": ("frame.jpg", io.BytesIO(img_bytes), "image/jpeg")}
    r = requests.post(VLM_URL, files=files, timeout=timeout)
    r.raise_for_status()
    try:
        j = r.json()
        if isinstance(j, dict):
            return j.get("response") or j.get("caption") or json.dumps(j, ensure_ascii=False)
        return json.dumps(j, ensure_ascii=False)
    except ValueError:
        return r.text.strip()

def vlm_call_path(image_path: str, timeout=30.0):
    payload = {"image_path": image_path}
    r = requests.post(VLM_URL, json=payload, timeout=timeout)
    r.raise_for_status()
    try:
        j = r.json()
        if isinstance(j, dict):
            return j.get("response") or j.get("caption") or json.dumps(j, ensure_ascii=False)
        return json.dumps(j, ensure_ascii=False)
    except ValueError:
        return r.text.strip()

@app.get("/health")
def health():
    return {"status":"ok", "vlm_url": VLM_URL, "mode": VLM_MODE}

@app.post("/upload")
def upload():
    f = request.files.get("image")
    name = request.form.get("name", "capture")
    idx  = request.form.get("index", "0")
    if not f:
        return {"error":"no file"}, 400

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{name}_{ts}_{idx}"
    img_path = SAVE_DIR / f"{stem}.jpg"
    f.save(img_path)

    # call VLM locally on Jetson
    caption = None
    try:
        if VLM_MODE == "upload":
            caption = vlm_call_upload(img_path.read_bytes())
        else:  # "path"
            caption = vlm_call_path(str(img_path))
    except Exception as e:
        caption = None

    # sidecar JSON
    sidecar = SAVE_DIR / f"{stem}.json"
    obj = {
        "name": name, "index": int(idx), "timestamp": ts,
        "image": img_path.name, "caption": caption
    }
    sidecar.write_text(json.dumps(obj, indent=2))

    return {"ok": True, "saved": str(img_path), "caption": caption}

if __name__ == "__main__":
    # run:  receiver_vlm.py  (or wrap with systemd below)
    app.run(host="0.0.0.0", port=5001)
