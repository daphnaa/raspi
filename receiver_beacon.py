#!/usr/bin/env python3
import socket, json, time

BCAST_PORT = 50001          # discovery port (Pi listens here)
INTERVAL   = 5.0            # seconds
UPLOAD_PORT = 5001          # where your /upload is listening

def main():
    hostname = socket.gethostname()

    # auto-detect local IP (best-effort)
    ip = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    if not ip:
        print("[beacon] WARN: could not detect IP, will still broadcast without it")

    print(f"[beacon] advertising as capture receiver on {ip or 'UNKNOWN'}:{UPLOAD_PORT}")

    while True:
        payload = {
            "type": "capture_receiver",
            "url": f"http://{ip}:{UPLOAD_PORT}" if ip else "",
            "host": hostname,
        }
        data = json.dumps(payload).encode("utf-8")

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(0.2)
            s.sendto(data, ("255.255.255.255", BCAST_PORT))
        except Exception:
            pass
        finally:
            try:
                s.close()
            except Exception:
                pass

        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
