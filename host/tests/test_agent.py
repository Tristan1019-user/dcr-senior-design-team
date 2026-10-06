import asyncio

import pytest

from dcr_host.agent import Agent
from dcr_host.client import DCRClient
from dcr_host.mock import MockTransport
from dcr_host.models.base import ModelBackendTimeoutError, ModelReply, ToolCall
from dcr_host.tools import ToolExecutor


class ScriptedBackend:
    def __init__(self, steps):
        self.steps = iter(steps)
    async def generate(self, messages, tools):
        step = next(self.steps)
        if isinstance(step, Exception):
            raise step
        return step


def call(n, tool, args):
    return ModelReply(text="", tool_calls=[ToolCall(id=str(n), name=tool, arguments=args)])


async def setup(steps, **kwargs):
    client = DCRClient(MockTransport())
    await client.connect()
    agent = Agent(ScriptedBackend(steps), ToolExecutor(client, ["sht45"]), **kwargs)
    return client, agent


async def test_mutation_requires_separate_verification():
    for verify in (False, True):
        steps = [call(1, "get_components", {}), call(2, "stop_component", {"component": "sht45"})]
        if verify:
            steps += [call(3, "get_component", {"component": "sht45"})]
        steps += [ModelReply(text="Stopped successfully")]
        client, agent = await setup(steps)
        try:
            result = await agent.run("stop sht45")
            assert result.mutations_verified == verify
            assert result.outcome == ("model_finished" if verify else "unverified_mutation")
            assert client.transport.components["bmp280"]["state"] == "RUNNING"
        finally:
            await client.disconnect()


async def test_error_recovery_and_repeat_block():
    steps = [call(1, "get_components", {}), call(2, "start_component", {"component": "sht45"}),
             call(3, "get_component", {"component": "sht45"}), ModelReply(text="Already running; no change needed")]
    client, agent = await setup(steps)
    try:
        result = await agent.run("start sht45")
        assert result.outcome == "model_finished"
        assert result.trace[1]["output"]["error"]["code"] == "INVALID_STATE"
    finally:
        await client.disconnect()
    client, agent = await setup([call(1, "shell", {}), call(2, "shell", {})])
    try:
        assert (await agent.run("anything")).outcome == "repeated_invalid_action"
    finally:
        await client.disconnect()


async def test_limits_and_backend_failure():
    for steps, kwargs, expected in [
        ([call(1, "get_components", {})], {"max_turns": 1}, "turn_limit"),
        ([call(1, "get_components", {}), call(2, "get_health", {})], {"max_tool_steps": 1}, "step_limit"),
        ([ModelBackendTimeoutError("timeout")], {}, "model_failure"),
    ]:
        client, agent = await setup(steps, **kwargs)
        try:
            assert (await agent.run("inspect")).outcome == expected
        finally:
            await client.disconnect()


async def test_retry_after_verified_prerequisite_repair():
    args = {"component": "sht45"}
    steps = [call(1, "get_components", {}), call(2, "stop_component", args),
             call(3, "init_component", args), call(4, "get_component", args),
             call(5, "init_component", args), call(6, "get_component", args),
             ModelReply(text="Initialized and verified; not started")]
    client, agent = await setup(steps)
    try:
        result = await agent.run("initialize stopped sensor")
        assert result.outcome == "model_finished"
        assert result.trace[2]["output"]["error"]["code"] == "VERIFICATION_REQUIRED"
        assert result.trace[4]["output"]["ok"]
        assert result.mutations_verified
    finally:
        await client.disconnect()


async def test_no_partial_batch_mutation():
    client, agent = await setup([ModelReply(text="", tool_calls=[
        ToolCall(id="1", name="stop_component", arguments={"component": "sht45"}),
        ToolCall(id="2", name="stop_component", arguments={"component": "bmp280"})])])
    try:
        result = await agent.run("stop")
        assert result.outcome == "multiple_calls_rejected"
        assert all(c["state"] == "RUNNING" for c in client.transport.components.values())
    finally:
        await client.disconnect()


async def test_cancel_does_not_execute_later_steps():
    class Slow:
        async def generate(self, *args):
            await asyncio.sleep(100)
    client, agent = await setup([])
    agent.backend = Slow()
    task = asyncio.create_task(agent.run("inspect"))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(client.transport.commands) == 2  # Handshake only.
    await client.disconnect()
