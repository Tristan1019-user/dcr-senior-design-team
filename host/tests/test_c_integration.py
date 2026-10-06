"""Real C runtime/protocol with synthetic sensors; never physical evidence."""
import asyncio
from pathlib import Path

import pytest

from dcr_host.client import DCRClient
from dcr_host.protocol import ProtocolError, load_json
from dcr_host.tools import ToolExecutor
from dcr_host.transport import Transport


def test_numeric_overflow_rejected():
    with pytest.raises(ProtocolError):
        load_json('{"value":1e999}')


class ProcessTransport(Transport):
    def __init__(self, executable):
        self.executable = executable

    async def connect(self):
        self.process = await asyncio.create_subprocess_exec(str(self.executable),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)

    async def send(self, data):
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def receive(self):
        line = await self.process.stdout.readline()
        if not line:
            raise ConnectionError("C emulator exited")
        return line

    async def disconnect(self):
        self.process.stdin.close()
        await asyncio.wait_for(self.process.wait(), 5)
        assert self.process.returncode == 0


@pytest.mark.asyncio
async def test_real_c_protocol_independent_lifecycle():
    executable = Path(__file__).resolve().parents[2] / "build-host/Debug/dcr_control_demo.exe"
    if not executable.exists():
        pytest.skip("Build the native dcr_control_demo target first")
    client = DCRClient(ProcessTransport(executable), timeout=5)
    tools = ToolExecutor(client, ["sht45"])
    try:
        await client.connect()
        await asyncio.sleep(1.1)
        health = await tools.execute("get_health", {})
        assert health["ok"]
        assert {c["id"] for c in health["result"]["components"]} == {
            "i2c_bus_0", "i2c_bus_1", "sht45", "bmp280",
            "sht_temperature_c", "humidity_pct", "bmp_temperature_c", "pressure_pa",
        }
        assert all(c["healthy"] for c in health["result"]["components"])
        b_before = client.state.inspect("bmp280")["outputs"]["temperature_c"]["sequence"]
        for op, args in [
            ("stop_component", {"component": "sht45"}),
            ("set_parameter", {"component": "sht45", "name": "period_ms", "value": 500}),
            ("init_component", {"component": "sht45"}),
            ("start_component", {"component": "sht45"}),
        ]:
            assert (await tools.execute(op, args))["ok"]
            if op == "stop_component":
                await asyncio.sleep(1.1)
                b = await tools.execute("get_component", {"component": "bmp280"})
                assert b["result"]["healthy"]
                assert b["result"]["outputs"]["temperature_c"]["sequence"] > b_before
            if op == "start_component":
                await asyncio.sleep(.6)
            assert (await tools.execute("get_component", {"component": "sht45"}))["ok"]
        assert not tools.unverified
        assert client.state.inspect("sht45")["healthy"]
        assert client.state.inspect("sht45")["config"]["period_ms"] == 500
    finally:
        await client.disconnect()
