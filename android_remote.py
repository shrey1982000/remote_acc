"""
android_remote.py  -  PC-side client (WebSocket relay mode)
============================================================
Connects to relay server via WebSocket. Works over the internet,
no ADB needed.

Controls:
  Left click          -> tap
  Left click + drag   -> swipe
  Right click         -> long press

Requirements:
    pip install av pillow websockets

Usage:
    python android_remote.py
"""

import asyncio
import av
import json
import math
import queue
import socket
import struct
import sys
import threading
import tkinter as tk
from PIL import Image, ImageTk
import websockets

# ── Configuration ──────────────────────────────────────────────────────────────
WINDOW_W           = 405
RENDER_FPS         = 60
SWIPE_THRESHOLD    = 10
SWIPE_DURATION     = 300
RENDER_INTERVAL_MS = int(1000 / RENDER_FPS)

TYPE_TAP        = 0x01
TYPE_SWIPE      = 0x02
TYPE_LONG_PRESS = 0x04
# ───────────────────────────────────────────────────────────────────────────────


class WsBinaryStream:
    """
    Wraps the WebSocket video channel into a file-like object for PyAV.
    Android sends: [4-byte size][NAL bytes]
    We prepend Annex-B start code so FFmpeg can parse H.264.
    """
    ANNEXB_START = b"\x00\x00\x00\x01"

    def __init__(self, ws_recv_queue: queue.Queue):
        self._q   = ws_recv_queue
        self._buf = bytearray()
        self._pos = 0

    def _pull(self):
        """Block until we have a full framed NAL from the queue."""
        # Accumulate raw bytes from websocket messages
        while True:
            chunk = self._q.get()  # blocks
            if chunk is None:
                raise OSError("stream closed")
            self._buf += chunk

            # Try to consume as many framed NALs as possible
            while len(self._buf) - self._pos >= 4:
                size = struct.unpack_from(">I", self._buf, self._pos)[0]
                if len(self._buf) - self._pos - 4 >= size:
                    nal = bytes(self._buf[self._pos + 4 : self._pos + 4 + size])
                    self._pos += 4 + size
                    self._buf += self.ANNEXB_START + nal
                    # Compact
                    if self._pos > 500_000:
                        self._buf = self._buf[self._pos:]
                        self._pos = 0
                    return
                else:
                    break  # need more data

    def read(self, n: int) -> bytes:
        while len(self._buf) - self._pos < n:
            self._pull()
        data = bytes(self._buf[self._pos : self._pos + n])
        self._pos += n
        if self._pos > 1_000_000:
            self._buf = self._buf[self._pos:]
            self._pos = 0
        return data

    def readable(self):  return True
    def writable(self):  return False
    def seekable(self):  return False


class AndroidRemote:
    def __init__(self, relay_url: str, room_id: str):
        self.relay_url = relay_url  # e.g. ws://something.up.railway.app
        self.room_id   = room_id
        self.running   = True

        self._video_ws = None
        self._input_ws = None
        self._input_loop: asyncio.AbstractEventLoop = None

        self._frame_lock  = threading.Lock()
        self._frame       = None
        self._frame_dirty = False
        self._photo       = None
        self._canvas_item = None
        self._drag_x      = None
        self._drag_y      = None
        self._window_h    = 720

        self._video_queue: queue.Queue = queue.Queue()

        # ── UI ──────────────────────────────────────────────────────────────
        self.root = tk.Tk()
        self.root.title(f"Android Remote  –  Room: {room_id}")
        self.root.resizable(False, False)

        self.tk_canvas = tk.Canvas(
            self.root, width=WINDOW_W, height=self._window_h,
            bg="black", highlightthickness=0, cursor="crosshair"
        )
        self.tk_canvas.pack()
        self.tk_canvas.bind("<ButtonPress-1>",   self._on_lmb_press)
        self.tk_canvas.bind("<ButtonRelease-1>", self._on_lmb_release)
        self.tk_canvas.bind("<Button-3>",        self._on_rmb)

        self.status_var = tk.StringVar(value="Connecting …")
        tk.Label(self.root, textvariable=self.status_var,
                 fg="white", bg="#222").pack(fill="x")
        self.root.configure(bg="#222")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        threading.Thread(target=self._run_async_loop, daemon=True).start()
        self.root.after(RENDER_INTERVAL_MS, self._render_tick)
        self.root.mainloop()

    # ── Async networking ──────────────────────────────────────────────────────

    def _run_async_loop(self):
        loop = asyncio.new_event_loop()
        self._input_loop = loop
        loop.run_until_complete(self._connect())

    async def _connect(self):
        try:
            self._set_status("Connecting video channel …")
            video_ws = await websockets.connect(self.relay_url)
            await video_ws.send(json.dumps({"role": "P", "channel": "V", "room": self.room_id}))
            self._video_ws = video_ws

            self._set_status("Connecting input channel …")
            input_ws = await websockets.connect(self.relay_url)
            await input_ws.send(json.dumps({"role": "P", "channel": "I", "room": self.room_id}))
            self._input_ws = input_ws

            self._set_status(f"Connected — waiting for Android to join room '{self.room_id}' …")

            # Run video receiver and keep input ws alive concurrently
            await asyncio.gather(
                self._receive_video(),
                self._keep_input_alive(),
            )
        except Exception as e:
            self._set_status(f"Connection error: {e}")

    async def _receive_video(self):
        """Push incoming websocket binary messages into the queue for PyAV thread."""
        try:
            async for message in self._video_ws:
                if not self.running:
                    break
                if isinstance(message, bytes):
                    self._video_queue.put(message)
        except Exception as e:
            if self.running:
                self._set_status(f"Video stream error: {e}")
        finally:
            self._video_queue.put(None)  # signal EOF

    async def _keep_input_alive(self):
        """Keep the input websocket open (it only sends, doesn't receive much)."""
        try:
            async for _ in self._input_ws:
                pass
        except: pass

    # ── PyAV decode (runs in its own thread) ─────────────────────────────────

    def start_decode_thread(self):
        threading.Thread(target=self._decode_video, daemon=True).start()

    def _decode_video(self):
        try:
            stream = WsBinaryStream(self._video_queue)

            # First 8 bytes = phone dimensions
            header = stream.read(8)
            phone_w, phone_h = struct.unpack(">II", header)
            aspect   = phone_h / phone_w
            window_h = int(WINDOW_W * aspect)
            self.root.after(0, lambda: self._resize_canvas(window_h))
            self._window_h = window_h
            self._set_status(f"Phone: {phone_w}x{phone_h}  |  LClick=tap  Drag=swipe  RClick=long press")

            container = av.open(stream, format="h264")
            for packet in container.demux():
                if not self.running:
                    break
                for frame in packet.decode():
                    img = frame.to_image()
                    with self._frame_lock:
                        self._frame       = img
                        self._frame_dirty = True

        except Exception as e:
            if self.running:
                self._set_status(f"Decode error: {e}")
                import traceback; traceback.print_exc()

    # ── Render ────────────────────────────────────────────────────────────────

    def _resize_canvas(self, new_h: int):
        self._window_h = new_h
        self.tk_canvas.config(height=new_h)
        self.root.geometry(f"{WINDOW_W}x{new_h + 30}")
        # Start decode thread once we know dimensions
        self.start_decode_thread()

    def _render_tick(self):
        if not self.running:
            return
        with self._frame_lock:
            if self._frame_dirty and self._frame is not None:
                snapshot          = self._frame
                self._frame_dirty = False
            else:
                snapshot = None
        if snapshot is not None:
            display     = snapshot.resize((WINDOW_W, self._window_h), Image.Resampling.BILINEAR)
            self._photo = ImageTk.PhotoImage(display)
            if self._canvas_item is None:
                self._canvas_item = self.tk_canvas.create_image(0, 0, image=self._photo, anchor="nw")
            else:
                self.tk_canvas.itemconfig(self._canvas_item, image=self._photo)
        self.root.after(RENDER_INTERVAL_MS, self._render_tick)

    # ── Input senders ─────────────────────────────────────────────────────────

    def _send_input(self, data: bytes):
        if not self._input_ws or not self._input_loop:
            return
        asyncio.run_coroutine_threadsafe(
            self._input_ws.send(data), self._input_loop
        )

    def _send_tap(self, rx: float, ry: float):
        self._send_input(struct.pack(">Bff", TYPE_TAP, rx, ry))
        self._set_status(f"Tap  ({rx:.3f}, {ry:.3f})")

    def _send_swipe(self, rx1, ry1, rx2, ry2):
        self._send_input(struct.pack(">Bffffi", TYPE_SWIPE, rx1, ry1, rx2, ry2, SWIPE_DURATION))
        self._set_status(f"Swipe  ({rx1:.2f},{ry1:.2f}) -> ({rx2:.2f},{ry2:.2f})")

    def _send_long_press(self, rx: float, ry: float):
        self._send_input(struct.pack(">Bff", TYPE_LONG_PRESS, rx, ry))
        self._set_status(f"Long press  ({rx:.3f}, {ry:.3f})")

    # ── Mouse handlers ────────────────────────────────────────────────────────

    def _on_lmb_press(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_lmb_release(self, event):
        if self._drag_x is None:
            return
        dist = math.hypot(event.x - self._drag_x, event.y - self._drag_y)
        if dist < SWIPE_THRESHOLD:
            self._send_tap(self._drag_x / WINDOW_W, self._drag_y / self._window_h)
        else:
            self._send_swipe(
                self._drag_x / WINDOW_W, self._drag_y / self._window_h,
                event.x      / WINDOW_W, event.y      / self._window_h,
            )
        self._drag_x = self._drag_y = None

    def _on_rmb(self, event):
        self._send_long_press(event.x / WINDOW_W, event.y / self._window_h)

    # ── Misc ──────────────────────────────────────────────────────────────────

    def _set_status(self, msg: str):
        try:
            self.root.after(0, lambda: self.status_var.set(msg))
        except: pass

    def _on_close(self):
        self.running = False
        self._video_queue.put(None)
        try: self._video_ws and asyncio.run_coroutine_threadsafe(self._video_ws.close(), self._input_loop)
        except: pass
        try: self._input_ws and asyncio.run_coroutine_threadsafe(self._input_ws.close(), self._input_loop)
        except: pass
        self.root.destroy()
        sys.exit(0)


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Android Remote – WebSocket Relay mode")
    print("=" * 45)

    relay_host = input("Relay server address (e.g. something.up.railway.app): ").strip()
    room_id    = input("Room ID: ").strip()

    if not relay_host or not room_id:
        print("Error: both fields required.")
        sys.exit(1)

    # Build WebSocket URL
    relay_url = f"ws://{relay_host}"
    print(f"\nConnecting to {relay_url}  room={room_id!r}\n")

    try:
        AndroidRemote(relay_url, room_id)
    except Exception:
        import traceback
        traceback.print_exc()
        input("\nPress Enter to close …")
