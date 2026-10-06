import asyncio
import json

import pytest
from pydantic import ValidationError

from dcr_host.client import DCRClient
from dcr_host.mock import MockTransport
from dcr_host.protocol import PREFIX, ProtocolError, decode_device, encode, load_json, validate_args
from dcr_host.recording import Recorder, ReplayTransport, replay
from dcr_host.tools import ToolExecutor, definitions
from dcr_host.transport import SerialTransport, TransportError


@pytest.mark.parametrize("raw", [b'{"v":1,"v":1}', b'{"x":NaN}', b'\xff', b'{', b'[' * 2000])
def test_invalid_json(raw):
    with pytest.raises(ProtocolError):
        load_json(raw)


@pytest.mark.parametrize("args", [{"component": "sht45", "name": "period_ms", "value": True},
                                 {"component": "sht45", "name": "period_ms", "value": 1.5},
                                 {"component": "sht45", "name": "pin", "value": 500},
                                 {"component": "sht45", "name": "period_ms", "value": 249},
                                 {"component": "../x", "name": "period_ms", "value": 500},
                                 {"component": "sht45", "name": "period_ms", "value": 500, "extra": 1}])
def test_strict_args(args):
    with pytest.raises(ValidationError):
        validate_args("set_parameter", args)


def test_framing_version_and_log_separation():
    assert decode_device(b'I (100) hello\n') is None
    with pytest.raises(ProtocolError):
        decode_device(PREFIX + b'{"v":true,"id":1,"ok":true,"result":{}}\n')
    with pytest.raises(ProtocolError):
        decode_device(PREFIX + b'{"v":2,"id":1,"ok":true,"result":{}}\n')
    with pytest.raises(ProtocolError):
        decode_device(PREFIX + b'x' * 9000)


async def test_independent_lifecycle_parameter_and_verification():
    mock = MockTransport()
    client = DCRClient(mock)
    await client.connect()
    tools = ToolExecutor(client, ["sht45"])
    try:
        assert not (await tools.execute("stop_component", {"component": "sht45"}))["ok"]
        assert (await tools.execute("get_health", {}))["ok"]
        before = mock.components["bmp280"]["outputs"]["temperature_c"]["sequence"]
        assert (await tools.execute("stop_component", {"component": "sht45"}))["ok"]
        assert set(tools.unverified) == {"sht45"}
        stopped = await tools.execute("get_component", {"component": "sht45"})
        assert stopped["result"]["state"] == "STOPPED"
        assert not stopped["result"]["healthy"]
        assert not tools.unverified
        assert (await tools.execute("start_component", {"component": "sht45"}))["error"]["code"] == "INVALID_STATE"
        assert (await tools.execute("set_parameter", {"component": "sht45", "name": "period_ms", "value": 500}))["ok"]
        assert (await tools.execute("init_component", {"component": "sht45"}))["error"]["code"] == "VERIFICATION_REQUIRED"
        assert (await tools.execute("get_component", {"component": "sht45"}))["ok"]
        assert (await tools.execute("init_component", {"component": "sht45"}))["ok"]
        assert (await tools.execute("get_component", {"component": "sht45"}))["ok"]
        assert (await tools.execute("start_component", {"component": "sht45"}))["ok"]
        await asyncio.sleep(1.1)
        health = await tools.execute("get_health", {})
        assert all(c["healthy"] for c in health["result"]["components"])
        assert not tools.unverified
        assert mock.components["bmp280"]["outputs"]["temperature_c"]["sequence"] > before
        assert not (await tools.execute("stop_component", {"component": "bmp280"}))["ok"]
    finally:
        await client.disconnect()


async def test_fault_stale_and_unrelated_preservation():
    now = [0.0]
    mock = MockTransport(clock=lambda: now[0])
    client = DCRClient(mock)
    await client.connect()
    client.state.clock = lambda: now[0]
    try:
        mock.faults.add("sht45")
        now[0] = 10.0
        health = await client.refresh()
        by_id = {c["id"]: c for c in health["components"]}
        assert not by_id["sht45"]["healthy"]
        assert by_id["bmp280"]["healthy"]
        mock.faults.clear()
        now[0] = 11.0
        await client.refresh()
        mock.stale.add("sht45")
        now[0] = 20.0
        health = await client.refresh()
        by_id = {c["id"]: c for c in health["components"]}
        assert not by_id["sht45"]["healthy"]
        assert by_id["bmp280"]["healthy"]
    finally:
        await client.disconnect()


async def test_timeout_no_retry_reconnect():
    mock = MockTransport()
    client = DCRClient(mock, timeout=.1)
    await client.connect()
    mock.drop_responses = True
    before = len(mock.commands)
    with pytest.raises(TransportError):
        await client.call("stop_component", {"component": "sht45"})
    assert len(mock.commands) == before + 1
    assert not client.state.connected
    await client.disconnect()
    mock.drop_responses = False
    await client.connect()
    assert (await client.call("get_component", {"component": "sht45"}))["state"] == "STOPPED"
    await client.disconnect()


async def test_nonexistent_invalid_tool_and_read_only():
    client = DCRClient(MockTransport())
    await client.connect()
    tools = ToolExecutor(client)
    try:
        assert (await tools.execute("get_component", {"component": "sensor_xyz"}))["error"]["code"] == "NOT_FOUND"
        assert (await tools.execute("shell", {"command": "anything"}))["error"]["code"] == "UNKNOWN_TOOL"
        assert (await tools.execute("get_components", {"extra": 1}))["error"]["code"] == "INVALID_ARGUMENT"
        assert (await tools.execute("stop_component", {"component": "sht45"}))["error"]["code"] == "FORBIDDEN"
    finally:
        await client.disconnect()


async def test_wrong_id_is_not_correlated():
    mock = MockTransport()
    client = DCRClient(mock, timeout=.1)
    await client.connect()
    mock.drop_responses = True
    mock.queue.put_nowait(PREFIX + encode({"v": 1, "id": 999, "ok": True, "result": {}}))
    with pytest.raises(TransportError):
        await client.call("get_components", {})
    await client.disconnect()


async def test_malformed_event_fails_closed_and_reboot_invalidates_state():
    mock = MockTransport()
    client = DCRClient(mock)
    await client.connect()
    mock.queue.put_nowait(PREFIX + b'{"v":1,"event":"unknown"}\n')
    await asyncio.sleep(.02)
    assert not client.state.connected
    await client.disconnect()
    await client.connect()
    mock.boot_id = "different-boot"
    await asyncio.sleep(.1)
    assert not client.state.connected
    await client.disconnect()


async def test_record_replay_never_executes(tmp_path):
    path = tmp_path / "record.jsonl"
    record = Recorder(path)
    client = DCRClient(MockTransport(), recorder=record)
    await client.connect()
    await client.disconnect()
    record.close()
    rows = list(replay(path))
    assert rows[0]["kind"] == "request"
    assert any(r["kind"] == "device" for r in rows)
    with pytest.raises(FileExistsError):
        Recorder(path)
    replay_client = DCRClient(ReplayTransport(path))
    await replay_client.connect()
    assert replay_client.state.components.keys() == client.state.components.keys()
    with pytest.raises(TransportError):
        await replay_client.call("stop_component", {"component": "sht45"})
    await replay_client.disconnect()


def test_tool_schemas_no_transport_or_gpio():
    names = {t["function"]["name"] for t in definitions()}
    assert "start_component" in names
    assert not names & {"send", "write", "gpio", "shell", "flash"}
    assert all(not t["function"]["parameters"]["additionalProperties"] for t in definitions())


async def test_serial_chunks_and_no_reset(monkeypatch):
    class FakeSerial:
        def __init__(self, **kwargs):
            self.dtr = self.rts = None
            self.chunks = [b"@D", b'CR {"x":1}', b"\n"]
            self.opened = False
        def open(self):
            assert self.dtr is False and self.rts is False
            self.opened = True
        def close(self):
            self.opened = False
        def read_until(self, *args):
            return self.chunks.pop(0)
        def write(self, data):
            return len(data)
    monkeypatch.setattr("dcr_host.transport.serial.Serial", FakeSerial)
    transport = SerialTransport("not-real")
    await transport.connect()
    assert await transport.receive() == b'@DCR {"x":1}\n'
    await transport.send(b"x\n")
    await transport.disconnect()
    with pytest.raises(TransportError):
        await transport.send(b"x\n")
