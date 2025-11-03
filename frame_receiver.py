#!/usr/bin/env python3
import os, socket, struct, datetime
from pathlib import Path

HOST = "0.0.0.0"
PORT = 5001
OUT_DIR = Path("/tmp/incoming_frames")  # change if you want
OUT_DIR.mkdir(parents=True, exist_ok=True)

def recv_exact(conn, n):
    data = bytearray()
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise ConnectionError("socket closed")
        data.extend(chunk)
    return bytes(data)

def handle_client(conn, addr):
    # Simple protocol: magic(4) + name_len(2) + img_len(4) + name + img
    # Repeat until client closes.
    while True:
        header = conn.recv(4)
        if not header:
            break
        if header != b"IMG1":
            raise ValueError("bad magic")
        name_len = struct.unpack("!H", recv_exact(conn, 2))[0]
        img_len  = struct.unpack("!I", recv_exact(conn, 4))[0]
        name     = recv_exact(conn, name_len).decode("utf-8", "ignore")
        img      = recv_exact(conn, img_len)

        # Make filename (use provided name as prefix)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"{name}_{ts}.jpg" if name else f"frame_{ts}.jpg"
        fpath = OUT_DIR / fname
        with open(fpath, "wb") as f:
            f.write(img)
        # Optional: ACK
        conn.sendall(b"OK")
    conn.close()

def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen(1)
        print(f"[receiver] listening on {HOST}:{PORT}, writing to {OUT_DIR}")
        while True:
            conn, addr = s.accept()
            try:
                handle_client(conn, addr)
            except Exception as e:
                print("[receiver] error:", e)
                try: conn.close()
                except: pass

if __name__ == "__main__":
    main()
