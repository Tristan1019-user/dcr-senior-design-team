"""Bounded byte-line transports; no model can access these through tool schemas."""
import asyncio
from abc import ABC, abstractmethod

import serial

from .protocol import MAX_FRAME


class TransportError(Exception):
    pass


class Transport(ABC):
    @abstractmethod
    async def connect(self): ...

    @abstractmethod
    async def disconnect(self): ...

    @abstractmethod
    async def send(self, data: bytes): ...

    @abstractmethod
    async def receive(self) -> bytes: ...


class SerialTransport(Transport):
    def __init__(self, port: str, baudrate: int = 115200):
        self.port, self.baudrate = port, baudrate
        self._serial = None

    async def connect(self):
        if self._serial is not None:
            raise TransportError("Already connected")
        def open_port():
            obj = serial.Serial(port=None, baudrate=self.baudrate, timeout=0.1, write_timeout=1)
            obj.dtr = obj.rts = False
            obj.port = self.port
            try:
                obj.open()
            except serial.SerialException as exc:
                raise TransportError("Cannot open serial device") from exc
            return obj
        self._serial = await asyncio.to_thread(open_port)

    async def disconnect(self):
        obj, self._serial = self._serial, None
        if obj:
            await asyncio.to_thread(obj.close)

    async def send(self, data):
        obj = self._serial
        if not obj:
            raise TransportError("Disconnected")
        try:
            count = await asyncio.to_thread(obj.write, data)
            if count != len(data):
                raise TransportError("Partial write; command outcome unknown")
        except (serial.SerialException, OSError) as exc:
            raise TransportError("Serial write failed; command outcome unknown") from exc

    async def receive(self):
        buf = bytearray()
        while self._serial:
            try:
                chunk = await asyncio.to_thread(self._serial.read_until, b"\n", MAX_FRAME + 9)
            except (serial.SerialException, OSError, AttributeError) as exc:
                raise TransportError("Serial disconnected") from exc
            buf.extend(chunk)
            if len(buf) > MAX_FRAME + 8:
                raise TransportError("Oversized serial frame; reconnect required")
            if buf.endswith(b"\n"):
                return bytes(buf)
        raise TransportError("Disconnected")
