"""
relay_server.py  –  TCP relay for Android Remote
=================================================
Deploy this on Railway (or any server with a public IP).

Each session has a room ID. Android and PC both connect to this server
using the same room ID. The server pairs them up and pipes bytes both ways.

Each "room" needs TWO channel pairs:
  - video   channel  (Android → PC)
  - input   channel  (PC → Android)

Connection handshake (first 64 bytes sent by client, null-padded):
  [1 byte role: 'A'=Android, 'P'=PC] [1 byte channel: 'V'=video, 'I'=input] [62 bytes room ID (ASCII, null padded)]

Once both sides of a channel connect, relay just pipes bytes.
"""

import os
import socket
import threading
from collections import defaultdict

PORT = int(os.environ.get("PORT", 7000))

# rooms[room_id][channel] = {'A': socket | None, 'P': socket | None}
rooms: dict[str, dict[str, dict]] = defaultdict(lambda: {
    'V': {'A': None, 'P': None, 'lock': threading.Lock()},
    'I': {'A': None, 'P': None, 'lock': threading.Lock()},
})
rooms_lock = threading.Lock()


def pipe(src: socket.socket, dst: socket.socket, label: str):
    """Forward bytes from src to dst until either closes."""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try: src.close()
        except: pass
        try: dst.close()
        except: pass
    print(f"[relay] pipe closed: {label}")


def handle_client(conn: socket.socket, addr):
    try:
        header = b""
        while len(header) < 64:
            chunk = conn.recv(64 - len(header))
            if not chunk:
                conn.close()
                return
            header += chunk

        role    = chr(header[0])    # 'A' or 'P'
        channel = chr(header[1])    # 'V' or 'I'
        room_id = header[2:].rstrip(b'\x00').decode('ascii', errors='replace').strip()

        if role not in ('A', 'P') or channel not in ('V', 'I') or not room_id:
            print(f"[relay] bad header from {addr}: role={role} ch={channel} room={room_id!r}")
            conn.close()
            return

        print(f"[relay] {addr}  role={role}  channel={channel}  room={room_id!r}")

        with rooms_lock:
            slot = rooms[room_id][channel]

        with slot['lock']:
            if slot[role] is not None:
                # Duplicate connection — reject
                print(f"[relay] duplicate {role}/{channel} for room {room_id!r}, rejecting")
                conn.close()
                return
            slot[role] = conn

        # Wait for the peer to connect (poll — simple and good enough)
        peer_role = 'P' if role == 'A' else 'A'
        print(f"[relay] waiting for peer {peer_role}/{channel} in room {room_id!r}")
        import time
        for _ in range(300):  # wait up to 30 seconds
            with slot['lock']:
                peer = slot[peer_role]
            if peer is not None:
                break
            time.sleep(0.1)
        else:
            print(f"[relay] timeout waiting for peer in room {room_id!r}")
            conn.close()
            with slot['lock']:
                slot[role] = None
            return

        print(f"[relay] pairing {channel} channel in room {room_id!r}")

        with slot['lock']:
            a_sock = slot['A']
            p_sock = slot['P']
            # Clear slot so room can be reused
            slot['A'] = None
            slot['P'] = None

        # Pipe in both directions concurrently
        label = f"{room_id}/{channel}"
        t = threading.Thread(target=pipe, args=(p_sock, a_sock, f"{label} P→A"), daemon=True)
        t.start()
        pipe(a_sock, p_sock, f"{label} A→P")
        t.join()

        # Clean up room if empty
        with rooms_lock:
            r = rooms.get(room_id)
            if r:
                all_empty = all(
                    r[ch]['A'] is None and r[ch]['P'] is None
                    for ch in ('V', 'I')
                )
                if all_empty:
                    del rooms[room_id]
                    print(f"[relay] room {room_id!r} cleaned up")

    except Exception as e:
        print(f"[relay] error handling {addr}: {e}")
        try: conn.close()
        except: pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(50)
    print(f"[relay] listening on 0.0.0.0:{PORT}")

    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()