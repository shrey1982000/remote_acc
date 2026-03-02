"""
relay_server.py  -  WebSocket relay for Android Remote
=======================================================
Works on Railway free tier (HTTP/WebSocket only).

Each client connects via WebSocket and sends a JSON handshake first:
  {"role": "A" or "P", "channel": "V" or "I", "room": "<room_id>"}

After handshake, raw bytes are piped between Android and PC.
"""

import asyncio
import json
import os
import websockets

PORT = int(os.environ.get("PORT", 8000))

# rooms[room_id][channel] = {"A": ws|None, "P": ws|None}
rooms: dict = {}
rooms_lock: asyncio.Lock = None  # initialized in main()


def get_or_create_slot(room_id: str, channel: str) -> dict:
    if room_id not in rooms:
        rooms[room_id] = {
            "V": {"A": None, "P": None},
            "I": {"A": None, "P": None},
        }
    return rooms[room_id][channel]


async def pipe_ws(src, dst, label: str):
    try:
        async for message in src:
            await dst.send(message)
    except Exception as e:
        print(f"[relay] pipe closed: {label} ({e})")


async def handler(websocket):
    global rooms_lock
    addr = websocket.remote_address
    role = channel = room_id = None
    try:
        raw = await asyncio.wait_for(websocket.recv(), timeout=15)
        hs  = json.loads(raw)
        role    = hs["role"]
        channel = hs["channel"]
        room_id = hs["room"].strip()

        if role not in ("A", "P") or channel not in ("V", "I") or not room_id:
            await websocket.close(1008, "bad handshake")
            return

        print(f"[relay] {addr}  role={role}  ch={channel}  room={room_id!r}")

        async with rooms_lock:
            slot = get_or_create_slot(room_id, channel)
            if slot[role] is not None:
                await websocket.close(1008, "duplicate connection")
                return
            slot[role] = websocket

        peer_role = "P" if role == "A" else "A"

        # Wait up to 120s for peer
        for _ in range(1200):
            async with rooms_lock:
                peer = slot[peer_role]
            if peer is not None:
                break
            await asyncio.sleep(0.1)
        else:
            print(f"[relay] timeout waiting for peer {room_id!r}/{channel}")
            async with rooms_lock:
                slot[role] = None
            await websocket.close(1001, "peer timeout")
            return

        print(f"[relay] pairing {room_id!r}/{channel}")

        async with rooms_lock:
            a_ws = slot["A"]
            p_ws = slot["P"]
            slot["A"] = None
            slot["P"] = None

        await asyncio.gather(
            pipe_ws(a_ws, p_ws, f"{room_id}/{channel} A->P"),
            pipe_ws(p_ws, a_ws, f"{room_id}/{channel} P->A"),
        )

        async with rooms_lock:
            r = rooms.get(room_id)
            if r and all(r[ch]["A"] is None and r[ch]["P"] is None for ch in ("V", "I")):
                del rooms[room_id]
                print(f"[relay] room {room_id!r} cleaned up")

    except Exception as e:
        print(f"[relay] error {addr}: {e}")
        if role and channel and room_id:
            async with rooms_lock:
                try:
                    rooms[room_id][channel][role] = None
                except: pass
        try: await websocket.close()
        except: pass


async def main():
    global rooms_lock
    rooms_lock = asyncio.Lock()
    print(f"[relay] WebSocket relay on 0.0.0.0:{PORT}")
    async with websockets.serve(handler, "0.0.0.0", PORT, max_size=10_000_000):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
