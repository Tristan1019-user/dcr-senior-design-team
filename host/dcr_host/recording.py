"""Append-only bounded session records. Replay is offline, never a device writer."""
import json
import time
import asyncio
from pathlib import Path

from .protocol import load_json
from .protocol import PREFIX, encode
from .transport import Transport, TransportError


class Recorder:
    def __init__(self, path, max_bytes=20_000_000):
        self.path, self.max_bytes = Path(path), max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("x", encoding="utf-8")
        self.bytes = 0

    def record(self, kind, payload):
        # Callers pass protocol/model identities and tool results, never headers/config.
        line = json.dumps({"time": time.time(), "kind": kind, "payload": payload}, allow_nan=False) + "\n"
        self.bytes += len(line.encode())
        if self.bytes > self.max_bytes:
            raise RuntimeError("Recording size limit reached")
        self.file.write(line)
        self.file.flush()

    def close(self):
        self.file.close()


def replay(path):
    with Path(path).open("rb") as file:
        while line := file.readline(65537):
            if len(line) > 65536:
                raise ValueError("Recording line too large")
            yield load_json(line)


class ReplayTransport(Transport):
    """Replay a recorded command sequence against a client, with no hardware path."""
    def __init__(self, path, node_id=None):
        self.path = path
        self.node_id = node_id
        self.connected = False
        self.queue = asyncio.Queue(maxsize=1024)
        self.rows = []
        self.index = 0

    async def connect(self):
        if self.connected:
            raise TransportError("Replay already connected")
        self.rows = []
        if Path(self.path).stat().st_size > 20_000_000:
            raise TransportError("Replay file limit exceeded")
        for row in replay(self.path):
            if self.node_id is not None:
                if row["kind"] not in ("node.request", "node.device"):
                    continue
                payload = row["payload"]
                if payload.get("node_id") != self.node_id:
                    continue
                row = {"kind": row["kind"].removeprefix("node."), "payload": payload["data"]}
            if row["kind"] in ("request", "device"):
                if len(self.rows) >= 10000:
                    raise TransportError("Replay record limit exceeded")
                self.rows.append(row)
        if not self.rows:
            raise TransportError("No matching recorded node traffic")
        self.index = 0
        self.connected = True
        try:
            while not self.queue.empty():
                self.queue.get_nowait()
            self._emit()
        except asyncio.CancelledError:
            self.connected = False
            raise
        except Exception as exc:
            self.connected = False
            raise TransportError("Replay initialization failed") from exc

    def _emit(self, request_id=None, recorded_id=None):
        while self.index < len(self.rows) and self.rows[self.index]["kind"] == "device":
            data = dict(self.rows[self.index]["payload"])
            self.index += 1
            if recorded_id is not None and data.get("id") == recorded_id:
                data["id"] = request_id
            self.queue.put_nowait(PREFIX + encode(data))

    async def send(self, data):
        if not self.connected or self.index >= len(self.rows):
            raise TransportError("Replay exhausted or disconnected")
        request = load_json(data)
        expected = self.rows[self.index]["payload"]
        if any(request.get(k) != expected.get(k) for k in ("v", "op", "args")):
            raise TransportError("Replay command differs from recording; nothing executed")
        self.index += 1
        try:
            self._emit(request["id"], expected["id"])
        except asyncio.CancelledError:
            self.connected = False
            raise
        except Exception as exc:
            self.connected = False
            raise TransportError("Replay emission failed") from exc

    async def receive(self):
        if not self.connected:
            raise TransportError("Replay disconnected")
        item = await self.queue.get()
        if item is None:
            raise TransportError("Replay disconnected")
        return item

    async def disconnect(self):
        self.connected = False
        if not self.queue.full():
            self.queue.put_nowait(None)
