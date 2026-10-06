import argparse
import json
import time

import pytest

from dcr_host.capabilities import NodeRegistry
from dcr_host.control_tools import ControlTools
from dcr_host.platform_demo import run
from dcr_host.policies.simulation import SimulatedStage
from dcr_host.policy_tools import PolicyTools


def fixture(policy=None, authorized=True):
    registry = NodeRegistry()
    stage = SimulatedStage("stage", authorized=authorized)
    registry.register(stage)
    return PolicyTools(ControlTools(registry, {}, {}), {"stage": stage}, policy), stage


def test_action_and_observation_must_bind_the_same_stage():
    registry = NodeRegistry()
    registry.register(SimulatedStage("stage"))
    with pytest.raises(ValueError):
        PolicyTools(ControlTools(registry, {}, {}), {"stage": SimulatedStage("stage", authorized=True)})


@pytest.mark.asyncio
async def test_policy_target_is_verified_and_explicitly_synthetic():
    tools, stage = fixture()
    result = await tools.execute("request_stage_target", {"node_id": "stage", "target_mm": 30.0})
    assert result["ok"] and result["result"]["verified"]
    assert result["result"]["source"] == "synthetic" and result["result"]["applied_chunks"] == 3
    assert stage.snapshot(time.monotonic()).position_mm == 30


@pytest.mark.asyncio
async def test_policy_target_cannot_grant_itself_permission():
    tools, stage = fixture(authorized=False)
    result = await tools.execute("request_stage_target", {"node_id": "stage", "target_mm": 30.0})
    assert not result["ok"]
    assert stage.snapshot(time.monotonic()).position_mm == 0


@pytest.mark.asyncio
async def test_bad_policy_cannot_bypass_gate():
    class BadPolicy:
        async def predict(self, observation):
            return {"node_id": "stage", "boot_id": observation["boot_id"], "command_id": "bad",
                    "created_at": observation["now"], "expires_at": observation["now"]+1,
                    "unit": "mm", "frame": "stage", "positions": [10.0, 101.0]}
    tools, stage = fixture(BadPolicy())
    result = await tools.execute("request_stage_target", {"node_id": "stage", "target_mm": 30.0})
    assert not result["ok"] and result["error"]["code"] == "POLICY_REJECTED"
    assert stage.snapshot(time.monotonic()).position_mm == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{"node_id": "stage", "target_mm": True},
                                    {"node_id": "stage", "target_mm": 101.0},
                                    {"node_id": "physical", "target_mm": 1.0}])
async def test_bad_targets_do_not_change_simulator(args):
    tools, stage = fixture()
    result = await tools.execute("request_stage_target", args)
    assert not result["ok"]
    assert stage.snapshot(time.monotonic()).position_mm == 0


@pytest.mark.asyncio
async def test_complete_offline_demo_is_reproducible(tmp_path, capsys):
    args = argparse.Namespace(output=str(tmp_path / "demo"), mock=True, port=None,
                              mode="deterministic", cycle=True)
    assert await run(args) == 0
    result = json.loads((tmp_path / "demo" / "result.json").read_text())
    assert result["passed"] and result["cycle"]["passed"]
    assert result["offline_replay_prefix_passed"]
    assert result["invalid_policy_rejected"]
    assert not result["physical_actuation"] and not result["trained_policy"]
