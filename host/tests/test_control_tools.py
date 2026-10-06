import asyncio

import pytest
from pydantic import ValidationError

from dcr_host.capabilities import DCRNodeAdapter, NodeRegistry
from dcr_host.client import DCRClient
from dcr_host.control_tools import ControlComponentArgs, ControlTools
from dcr_host.mock import MockTransport
from dcr_host.protocol import load_json


async def make_controls(*, nodes=("alpha", "beta"), permissions=None, source="synthetic",
                        timeout=.1, transport_factory=MockTransport):
    registry = NodeRegistry()
    clients = {}
    for node_id in nodes:
        client = DCRClient(transport_factory(), timeout=timeout)
        await client.connect()
        clients[node_id] = client
        registry.register(DCRNodeAdapter(client=client, node_id=node_id, source=source))
    allowed = permissions if permissions is not None else {node: {"sht45"} for node in nodes}
    return ControlTools(registry, clients, allowed), clients


async def close_clients(clients):
    await asyncio.gather(*(client.disconnect() for client in clients.values()))


@pytest.mark.asyncio
async def test_definitions_and_strict_required_nullable_value():
    controls, clients = await make_controls(nodes=("alpha",))
    try:
        names = {item["function"]["name"] for item in controls.definitions()}
        assert {"discover_capabilities", "read_capability", "inspect_node",
                "control_component"} <= names
        with pytest.raises(ValidationError):
            ControlComponentArgs.model_validate({
                "node_id": "alpha", "component": "sht45", "operation": "start",
                "expected_boot_id": "boot", "command_id": "abc",
            })
        assert ControlComponentArgs.model_validate({
            "node_id": "alpha", "component": "sht45", "operation": "start",
            "expected_boot_id": "boot", "command_id": "abc", "value": None,
        }).value is None
    finally:
        await close_clients(clients)


@pytest.mark.asyncio
async def test_inspection_is_qualified_and_exposes_components_for_two_nodes():
    controls, clients = await make_controls()
    try:
        alpha = await controls.execute("inspect_node", {"node_id": "alpha"})
        beta = await controls.execute("inspect_node", {"node_id": "beta"})
        assert alpha["ok"] and beta["ok"]
        assert alpha["result"]["components"]
        assert alpha["result"]["boot_id"]
        assert {item["id"] for item in alpha["result"]["components"]} == {
            item["id"] for item in beta["result"]["components"]}
        assert "topology" in alpha["result"]
    finally:
        await close_clients(clients)


@pytest.mark.asyncio
async def test_permission_default_deny_and_sensor_type_gate():
    controls, clients = await make_controls(nodes=("alpha",), permissions={"alpha": {"sht45"}})
    try:
        inspected = await controls.execute("inspect_node", {"node_id": "alpha"})
        boot = inspected["result"]["boot_id"]
        denied = await controls.execute("control_component", {
            "node_id": "alpha", "component": "bmp280", "operation": "stop",
            "expected_boot_id": boot, "command_id": "deny-1", "value": None})
        assert not denied["ok"] and denied["error"]["code"] == "FORBIDDEN"
        assert "alpha:bmp280" not in controls.unverified
    finally:
        await close_clients(clients)


@pytest.mark.asyncio
async def test_wrong_boot_precondition_does_not_delegate():
    controls, clients = await make_controls(nodes=("alpha",))
    try:
        inspected = await controls.execute("inspect_node", {"node_id": "alpha"})
        before = len(clients["alpha"].transport.commands)
        result = await controls.execute("control_component", {
            "node_id": "alpha", "component": "sht45", "operation": "stop",
            "expected_boot_id": "wrong-boot", "command_id": "boot-1", "value": None})
        assert not result["ok"] and result["error"]["code"] == "BOOT_MISMATCH"
        assert len(clients["alpha"].transport.commands) == before + 1
        assert clients["alpha"].transport.commands[-1]["op"] == "get_runtime_info"
    finally:
        await close_clients(clients)


@pytest.mark.asyncio
async def test_success_verifies_and_duplicate_or_collision_never_replays():
    controls, clients = await make_controls(nodes=("alpha",))
    try:
        inspected = await controls.execute("inspect_node", {"node_id": "alpha"})
        boot = inspected["result"]["boot_id"]
        args = {"node_id": "alpha", "component": "sht45", "operation": "stop",
                "expected_boot_id": boot, "command_id": "stop-1", "value": None}
        success = await controls.execute("control_component", args)
        assert success["ok"] and success["result"]["verified"] is True
        before = len(clients["alpha"].transport.commands)
        duplicate = await controls.execute("control_component", args)
        assert not duplicate["ok"] and duplicate["error"]["code"] == "DUPLICATE_COMMAND"
        assert len(clients["alpha"].transport.commands) == before
        collision = await controls.execute("control_component", {
            **args, "operation": "init", "command_id": "stop-1"})
        assert not collision["ok"] and collision["error"]["code"] == "COMMAND_ID_COLLISION"
    finally:
        await close_clients(clients)


@pytest.mark.asyncio
async def test_guarded_sensor_lifecycle_and_period_control():
    controls, clients = await make_controls(nodes=("alpha",))
    try:
        inspected = await controls.execute("inspect_node", {"node_id": "alpha"})
        boot = inspected["result"]["boot_id"]

        async def control(operation, command_id, value=None):
            return await controls.execute("control_component", {
                "node_id": "alpha", "component": "sht45", "operation": operation,
                "expected_boot_id": boot, "command_id": command_id, "value": value})

        for operation, command_id, value in (("stop", "life-stop", None),
                                              ("set_period", "life-period", 500),
                                              ("init", "life-init", None),
                                              ("start", "life-start", None)):
            result = await control(operation, command_id, value)
            assert result["ok"] and result["result"]["verified"] is True
    finally:
        await close_clients(clients)


@pytest.mark.asyncio
async def test_cached_sensor_type_is_rechecked_before_mutation():
    controls, clients = await make_controls(nodes=("alpha",))
    try:
        inspected = await controls.execute("inspect_node", {"node_id": "alpha"})
        boot = inspected["result"]["boot_id"]
        clients["alpha"].transport.components["sht45"]["type"] = "logger"
        result = await controls.execute("control_component", {
            "node_id": "alpha", "component": "sht45", "operation": "stop",
            "expected_boot_id": boot, "command_id": "type-change-1", "value": None})
        assert result["error"]["code"] == "FORBIDDEN"
        assert not any(command["op"] == "stop_component"
                       for command in clients["alpha"].transport.commands)
    finally:
        await close_clients(clients)


class PostWriteFailureTransport(MockTransport):
    def __init__(self):
        super().__init__()
        self.fail_after_send = False

    async def send(self, data):
        request = load_json(data)
        await super().send(data)
        if self.fail_after_send and request.get("op") == "stop_component":
            self.fail_after_send = False
            raise RuntimeError("simulated post-write host failure")


@pytest.mark.asyncio
async def test_unexpected_post_write_error_retains_interlock():
    controls, clients = await make_controls(nodes=("alpha",), transport_factory=PostWriteFailureTransport)
    try:
        inspected = await controls.execute("inspect_node", {"node_id": "alpha"})
        boot = inspected["result"]["boot_id"]
        clients["alpha"].transport.fail_after_send = True
        result = await controls.execute("control_component", {
            "node_id": "alpha", "component": "sht45", "operation": "stop",
            "expected_boot_id": boot, "command_id": "post-write-1", "value": None})
        assert result["error"]["code"] == "CONTROL_FAILED"
        assert result["verified"] is False
        assert "alpha:sht45" in controls.unverified
        duplicate = await controls.execute("control_component", {
            "node_id": "alpha", "component": "sht45", "operation": "stop",
            "expected_boot_id": boot, "command_id": "post-write-1", "value": None})
        assert duplicate["error"]["code"] == "DUPLICATE_COMMAND"
    finally:
        await close_clients(clients)


@pytest.mark.asyncio
async def test_verification_failure_keeps_node_component_blocked():
    controls, clients = await make_controls(nodes=("alpha",))
    try:
        inspected = await controls.execute("inspect_node", {"node_id": "alpha"})
        boot = inspected["result"]["boot_id"]
        original_tool = controls._tool

        async def wrong_health(node_id, name, args):
            if name == "get_health":
                return {"ok": True, "result": {
                    "connected": True, "boot_id": boot,
                    "components": [{"id": "sht45", "state": "RUNNING"}],
                }}
            return await original_tool(node_id, name, args)

        controls._tool = wrong_health
        result = await controls.execute("control_component", {
            "node_id": "alpha", "component": "sht45", "operation": "stop",
            "expected_boot_id": boot, "command_id": "verify-1", "value": None})
        assert not result["ok"] and result["error"]["code"] == "VERIFICATION_FAILED"
        assert result["verified"] is False
        assert "alpha:sht45" in controls.unverified
        blocked = await controls.execute("control_component", {
            "node_id": "alpha", "component": "sht45", "operation": "init",
            "expected_boot_id": boot, "command_id": "verify-2", "value": None})
        assert blocked["error"]["code"] == "VERIFICATION_REQUIRED"
    finally:
        await close_clients(clients)


@pytest.mark.asyncio
async def test_transport_unknown_outcome_stays_blocked_without_retry():
    controls, clients = await make_controls(nodes=("alpha",), timeout=.05)
    try:
        inspected = await controls.execute("inspect_node", {"node_id": "alpha"})
        boot = inspected["result"]["boot_id"]
        original_tool = controls._tool

        async def drop_mutation_response(node_id, name, args):
            if name == "stop_component":
                clients["alpha"].transport.drop_responses = True
            return await original_tool(node_id, name, args)

        controls._tool = drop_mutation_response
        result = await controls.execute("control_component", {
            "node_id": "alpha", "component": "sht45", "operation": "stop",
            "expected_boot_id": boot, "command_id": "unknown-1", "value": None})
        assert not result["ok"] and result["error"]["code"] == "DEVICE_UNAVAILABLE"
        assert result["verified"] is False
        assert "alpha:sht45" in controls.unverified
        duplicate = await controls.execute("control_component", {
            "node_id": "alpha", "component": "sht45", "operation": "stop",
            "expected_boot_id": boot, "command_id": "unknown-1", "value": None})
        assert duplicate["error"]["code"] == "DUPLICATE_COMMAND"
    finally:
        await close_clients(clients)


@pytest.mark.asyncio
async def test_command_cache_fails_closed_at_capacity_without_eviction_or_write():
    controls, clients = await make_controls(nodes=("alpha",))
    try:
        inspected = await controls.execute("inspect_node", {"node_id": "alpha"})
        boot = inspected["result"]["boot_id"]
        for index in range(256):
            filled = await controls.execute("control_component", {
                "node_id": "alpha", "component": "bmp280", "operation": "stop",
                "expected_boot_id": boot, "command_id": f"fill-{index}", "value": None})
            assert filled["error"]["code"] == "FORBIDDEN"
        before = len(clients["alpha"].transport.commands)
        full = await controls.execute("control_component", {
            "node_id": "alpha", "component": "sht45", "operation": "stop",
            "expected_boot_id": boot, "command_id": "after-full", "value": None})
        assert full["error"]["code"] == "COMMAND_CACHE_FULL"
        assert len(controls._commands) == 256
        assert len(clients["alpha"].transport.commands) == before
        assert (await controls.execute("control_component", {
            "node_id": "alpha", "component": "bmp280", "operation": "stop",
            "expected_boot_id": boot, "command_id": "fill-0", "value": None
        }))["error"]["code"] == "DUPLICATE_COMMAND"
    finally:
        await close_clients(clients)


@pytest.mark.asyncio
async def test_unknown_source_denies_mutation_and_binding_is_identity_checked():
    controls, clients = await make_controls(nodes=("alpha",), source="unknown")
    try:
        inspected = await controls.execute("inspect_node", {"node_id": "alpha"})
        boot = inspected["result"]["boot_id"]
        denied = await controls.execute("control_component", {
            "node_id": "alpha", "component": "sht45", "operation": "stop",
            "expected_boot_id": boot, "command_id": "source-1", "value": None})
        assert denied["error"]["code"] == "SOURCE_UNTRUSTED"
        wrong = DCRClient(MockTransport())
        with pytest.raises(ValueError):
            ControlTools(controls.registry, {"alpha": wrong}, {"alpha": {"sht45"}})
    finally:
        await close_clients(clients)
