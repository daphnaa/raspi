#!/usr/bin/env python3
from flask import Flask, request
from pathlib import Path
import datetime

app = Flask(__name__)
OUT = Path("/tmp/incoming_frames")
OUT.mkdir(parents=True, exist_ok=True)

@app.post("/upload")
def upload():
    f = request.files.get("image")
    name = request.form.get("name","capture")
    idx  = request.form.get("index","0")
    if not f:
        return {"error":"no file"}, 400
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUT / f"{name}_{ts}_{idx}.jpg"
    f.save(path)
    return {"ok": True, "saved": str(path)}

@app.get("/health")
def health():
    return {"status":"ok"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
