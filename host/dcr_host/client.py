"""One serialized control owner. Never retry a command with an unknown outcome."""
import asyncio
import contextlib
from pydantic import ValidationError

from .protocol import (DeviceError, Failure, ProtocolError, Snapshot, decode_device,
                       encode, validate_args)
from .state import RuntimeState
from .transport import TransportError


class DCRClient:
    def __init__(self, transport, timeout=3.0, recorder=None):
        self.transport, self.timeout, self.recorder = transport, timeout, recorder
        self.state = RuntimeState()
        self._counter = 0
        self._pending = {}
        self._lock = asyncio.Lock()
        self._reader = None
        self._error = None

    def record(self, kind, payload):
        if self.recorder:
            self.recorder.record(kind, payload)

    async def connect(self):
        if self._reader:
            raise TransportError("Client already connected")
        self.state.clear()
        self._error = None
        await self.transport.connect()
        self._reader = asyncio.create_task(self._read())
        try:
            info = await self.call("get_runtime_info", {})
            if type(info.get("protocol")) is not int or info["protocol"] != 1 or not info.get("boot_id") or not isinstance(info["boot_id"], str):
                raise ProtocolError("Invalid runtime handshake")
            self.state.boot_id = info["boot_id"]
            self.state.connected = True
            await self.call("get_components", {})
        except BaseException:
            await self.disconnect()
            raise

    async def disconnect(self):
        self.state.connected = False
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        await self.transport.disconnect()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(TransportError("Disconnected; command outcome unknown"))

    async def _read(self):
        try:
            while True:
                msg = decode_device(await self.transport.receive())
                if msg is None:
                    continue
                self.record("device", msg.model_dump())
                if isinstance(msg, Snapshot):
                    if self.state.boot_id is None:
                        continue
                    if msg.boot_id != self.state.boot_id:
                        raise TransportError("Device restarted; reconnect and inspect before further commands")
                    self.state.update(msg.components)
                else:
                    future = self._pending.get(msg.id)
                    if future and not future.done():
                        future.set_result(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = exc
            self.state.connected = False
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(TransportError("Receive failed; reconnect and inspect"))

    async def call(self, op, args):
        args = validate_args(op, args)
        async with self._lock:
            if self._error or not self._reader:
                raise TransportError("Client unavailable; reconnect required")
            self._counter += 1
            if self._counter > 2147483647:
                raise ProtocolError("Request ID exhausted")
            request = {"v": 1, "id": self._counter, "op": op, "args": args}
            future = asyncio.get_running_loop().create_future()
            self._pending[self._counter] = future
            try:
                self.record("request", request)
                await self.transport.send(encode(request))
                try:
                    response = await asyncio.wait_for(future, self.timeout)
                except TimeoutError as exc:
                    self._error = exc
                    self.state.connected = False
                    raise TransportError("Request timed out; outcome unknown, reconnect and inspect") from exc
                if isinstance(response, Failure):
                    raise DeviceError(response.error.code, response.error.message)
                result = response.result
                if op == "get_components":
                    self.state.update(result["components"])
                elif op in ("get_component", "start_component", "stop_component", "init_component", "set_parameter"):
                    self.state.update([result])
                return result
            except asyncio.CancelledError:
                self.state.connected = False
                self._error = TransportError("Cancelled request; reconnect and inspect")
                raise
            except TransportError as exc:
                self.state.connected = False
                self._error = exc
                raise
            except (ValidationError, KeyError, TypeError) as exc:
                self.state.connected = False
                self._error = exc
                raise TransportError("Malformed operation result; reconnect and inspect") from exc
            finally:
                self._pending.pop(self._counter, None)

    async def refresh(self):
        await self.call("get_components", {})
        return self.state.health()
