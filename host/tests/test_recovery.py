import copy

import pytest

from dcr_host.client import DCRClient
from dcr_host.mock import MockTransport
from dcr_host.recovery import recover
from dcr_host.tools import ToolExecutor
from dcr_host.cli import watch_health
from dcr_host.protocol import load_json


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["STOPPED", "INITIALIZED", "FAULTED", "RUNNING"])
async def test_recovery_plan_and_execution_preserve_other_sensor(state):
    mock = MockTransport()
    mock.components["sht45"]["state"] = state
    client = DCRClient(mock)
    await client.connect()
    executor = ToolExecutor(client, ["sht45"])
    before = copy.deepcopy(mock.components["bmp280"]["config"])
    try:
        planned = await recover(executor, "sht45", 500)
        assert planned["ok"] and not planned["result"]["executed"]
        assert mock.components["sht45"]["state"] == state
        result = await recover(executor, "sht45", 500, execute=True)
        assert result["ok"]
        assert mock.components["sht45"]["state"] == "RUNNING"
        assert mock.components["sht45"]["config"]["period_ms"] == 500
        assert mock.components["bmp280"]["state"] == "RUNNING"
        assert mock.components["bmp280"]["config"] == before
        assert not executor.unverified
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_readonly_invalid_and_unknown_recovery():
    client = DCRClient(MockTransport())
    await client.connect()
    executor = ToolExecutor(client)
    try:
        assert (await recover(executor, "sht45", execute=True))["error"]["code"] == "FORBIDDEN"
        for value in [True, 100, 2001, "500"]:
            assert (await recover(executor, "sht45", value))["error"]["code"] == "INVALID_ARGUMENT"
        assert (await recover(executor, "missing"))["error"]["code"] == "NOT_FOUND"
        assert not any(c["op"] in ("stop_component", "init_component", "start_component", "set_parameter")
                       for c in client.transport.commands)
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_healthy_recovery_is_noop_and_watch_does_not_poll():
    client = DCRClient(MockTransport())
    await client.connect()
    try:
        executor = ToolExecutor(client, ["sht45"])
        result = await recover(executor, "sht45", execute=True)
        assert result["ok"] and result["result"]["actions"] == []
        count = len(client.transport.commands)
        for _ in range(10):
            assert (await watch_health(client))["connected"]
        assert len(client.transport.commands) == count
        await watch_health(client, poll=True)
        assert len(client.transport.commands) == count + 1
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_unknown_stop_outcome_never_retries_or_initializes():
    class LostStopResponse(MockTransport):
        async def send(self, data):
            if load_json(data)["op"] == "stop_component":
                self.drop_responses = True
            await super().send(data)

    client = DCRClient(LostStopResponse(), timeout=.03)
    await client.connect()
    try:
        result = await recover(ToolExecutor(client, ["sht45"]), "sht45", 500, execute=True)
        assert not result["ok"] and result["error"]["code"] == "DEVICE_UNAVAILABLE"
        ops = [c["op"] for c in client.transport.commands]
        assert ops.count("stop_component") == 1
        assert "init_component" not in ops and "set_parameter" not in ops
    finally:
        await client.disconnect()
