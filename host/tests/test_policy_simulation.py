import math

import pytest
from pydantic import ValidationError

from dcr_host.capabilities import NodeRegistry
from dcr_host.policies.base import PolicyBackend
from dcr_host.policies.contracts import ActionChunk, ObservationBundle, SafetyGate
from dcr_host.policies.simulation import DeterministicStepPolicy, KinematicPredictor, SimulatedStage


def observation(*, node="stage", boot="stage-boot-1", now=0.0, position=10.0, sequence=0):
    return ObservationBundle(
        node_id=node,
        boot_id=boot,
        observed_at=now,
        position_mm=position,
        sequence=sequence,
    )


def action(*, node="stage", boot="stage-boot-1", command="cmd-1", created=0.0,
           expires=0.5, positions=(20.0,)):
    return ActionChunk(
        node_id=node,
        boot_id=boot,
        command_id=command,
        created_at=created,
        expires_at=expires,
        positions=positions,
    )


def test_contracts_are_strict_immutable_and_finite():
    state = observation()
    with pytest.raises(ValidationError):
        state.position_mm = 20.0
    with pytest.raises(ValidationError):
        ObservationBundle(node_id="stage", boot_id="b", observed_at=math.nan,
                           position_mm=1.0, sequence=0)
    with pytest.raises(ValidationError):
        ObservationBundle(node_id="stage", boot_id="b", observed_at=0.0,
                           position_mm=1, sequence=0)
    with pytest.raises(ValidationError):
        ActionChunk(node_id="stage", boot_id="b", command_id="x", created_at=0.0,
                    expires_at=1.0, positions=[])
    with pytest.raises(ValidationError):
        ActionChunk(node_id="stage", boot_id="b", command_id="x", created_at=0.0,
                    expires_at=1.0, positions=[math.inf])


def test_gate_accepts_explicitly_allowed_bounded_chunk():
    gate = SafetyGate(node_id="stage", allow_actions=True)
    decision = gate.check(action(positions=(20.0, 30.0)), observation(), 0.25)
    assert decision.allowed
    assert decision.flags == ()


@pytest.mark.parametrize(
    ("chunk", "state", "now", "flag"),
    [
        (action(positions=(101.0,)), observation(), 0.1, "out_of_bounds"),
        (action(positions=(36.0,)), observation(), 0.1, "delta_exceeded"),
        (action(expires=2.1), observation(), 0.1, "duration_exceeded"),
        (action(created=2.0), observation(), 1.0, "future"),
        (action(expires=0.5), observation(), 0.5, "expired"),
        (action(), observation(now=0.0), 1.1, "stale"),
        (action(boot="other-boot"), observation(), 0.1, "wrong_epoch"),
    ],
)
def test_gate_denies_limits_identity_time_and_freshness(chunk, state, now, flag):
    decision = SafetyGate(node_id="stage", allow_actions=True).check(chunk, state, now)
    assert not decision.allowed
    assert flag in decision.flags


def test_gate_requires_explicit_allow_and_never_uses_model_metadata():
    gate = SafetyGate(node_id="stage")
    decision = gate.check(action(), observation(), 0.1, allow=True)
    assert not decision.allowed
    assert decision.flags == ("disallowed_no_allow",)


def test_gate_rechecks_unit_frame_and_source_on_forged_models():
    forged_action = ActionChunk.model_construct(**{
        **action().model_dump(), "unit": "in", "frame": "raw"})
    forged_observation = ObservationBundle.model_construct(**{
        **observation().model_dump(), "source": "physical"})
    decision = SafetyGate(node_id="stage", allow_actions=True).check(
        forged_action, forged_observation, 0.1)
    assert not decision.allowed
    assert "unknown" in decision.flags


@pytest.mark.asyncio
async def test_simulated_stage_is_a_non_dcr_registry_adapter():
    stage = SimulatedStage(node_id="stage", clock=lambda: 3.0)
    registry = NodeRegistry()
    registry.register(stage)
    discovered = await registry.discover()
    assert discovered["nodes"][0]["source"] == "synthetic"
    assert discovered["nodes"][0]["capabilities"][0]["local_id"] == "position.read"
    observed = await registry.observe("stage", "position.read")
    assert observed["ok"]
    assert observed["result"]["observations"][0]["capability_id"] == "stage:position.read"
    assert observed["result"]["observations"][0]["unit"] == "mm"


def test_policy_is_provider_neutral_and_produces_small_bounded_proposal():
    policy = DeterministicStepPolicy(step_mm=10.0)
    assert isinstance(policy, PolicyBackend)
    proposal = policy.propose(observation(position=10.0), target_mm=80.0, now=0.25)
    assert proposal.positions == (20.0,)
    assert proposal.node_id == "stage"
    assert proposal.boot_id == "stage-boot-1"
    assert proposal.expires_at - proposal.created_at <= 2.0


def test_invalid_policy_target_is_not_actuated():
    stage = SimulatedStage(node_id="stage", authorized=True)
    before = stage.snapshot(0.0)
    policy = DeterministicStepPolicy()
    with pytest.raises(ValueError):
        policy.propose(before, target_mm=math.nan, now=0.1)
    with pytest.raises(ValueError):
        policy.propose(before, target_mm=101.0, now=0.1)
    after = stage.snapshot(0.1)
    assert after.position_mm == before.position_mm
    assert after.sequence == before.sequence


def test_stage_rejects_replay_and_stale_observation_without_mutation():
    stage = SimulatedStage(node_id="stage", authorized=True)
    policy = DeterministicStepPolicy(step_mm=10.0)
    before = stage.snapshot(0.0)
    chunk = policy.propose(before, target_mm=30.0, now=0.1)
    accepted = stage.execute(chunk, before, 0.2)
    assert accepted["ok"]
    current = stage.snapshot(0.3)
    replay = stage.execute(chunk, current, 0.3)
    assert replay["ok"] is False
    assert "replay" in replay["flags"]
    stale = stage.execute(policy.propose(current, target_mm=50.0, now=0.4), before, 0.4)
    assert stale["ok"] is False
    assert "stale" in stale["flags"]
    final = stage.snapshot(0.4)
    assert final.position_mm == current.position_mm
    assert final.sequence == current.sequence


def test_stage_revalidates_forged_action_instance_before_mutation():
    stage = SimulatedStage(node_id="stage", authorized=True)
    state = stage.snapshot(0.0)
    forged = ActionChunk.model_construct(**{
        **action(command="forged").model_dump(), "unit": "in", "positions": (20.0,)})
    result = stage.execute(forged, state, 0.1)
    assert result["ok"] is False
    assert "unknown" in result["flags"]
    assert stage.snapshot(0.1).position_mm == state.position_mm


def test_entire_chunk_is_atomic_and_replay_capacity_fails_closed():
    stage = SimulatedStage(node_id="stage", authorized=True, replay_capacity=1)
    state = stage.snapshot(0.0)
    invalid = action(command="bad", positions=(20.0, 200.0))
    rejected = stage.execute(invalid, state, 0.1)
    assert rejected["ok"] is False
    assert "out_of_bounds" in rejected["flags"]
    unchanged = stage.snapshot(0.1)
    assert unchanged.position_mm == state.position_mm
    assert unchanged.sequence == state.sequence

    accepted = stage.execute(action(command="good", positions=(20.0, 25.0)), unchanged, 0.2)
    assert accepted["ok"]
    full = stage.execute(action(command="later", positions=(30.0,)), stage.snapshot(0.3), 0.3)
    assert full["ok"] is False
    assert "replay_cache_full" in full["flags"]


def test_chunk_waypoints_also_obey_adjacent_delta_without_partial_apply():
    stage = SimulatedStage(node_id="stage", initial_position_mm=50.0, authorized=True)
    state = stage.snapshot(0.0)
    jump = action(command="jump", positions=(25.0, 75.0))
    result = stage.execute(jump, state, 0.1)
    assert result["ok"] is False
    assert "delta_exceeded" in result["flags"]
    current = stage.snapshot(0.1)
    assert current.position_mm == 50.0
    assert current.sequence == state.sequence


def test_wrong_epoch_and_future_proposals_do_not_change_stage():
    stage = SimulatedStage(node_id="stage", authorized=True)
    state = stage.snapshot(0.0)
    wrong = stage.execute(action(boot="other-boot", command="wrong"), state, 0.1)
    future = stage.execute(action(command="future", created=2.0, expires=2.5), state, 1.0)
    assert wrong["ok"] is False and "wrong_epoch" in wrong["flags"]
    assert future["ok"] is False and "future" in future["flags"]
    assert stage.snapshot(0.1).sequence == 0


@pytest.mark.asyncio
async def test_base_policy_predict_is_explicitly_synthetic_not_learned():
    policy = DeterministicStepPolicy(step_mm=5.0)
    result = await policy.predict({**observation(now=1.0).model_dump(), "target_mm": 20.0, "now": 1.0})
    assert result["node_id"] == "stage"
    assert result["positions"] == [15.0]
    info = await policy.model_info()
    assert info == {"name": "deterministic_step", "source": "synthetic",
                    "trained": False, "world_model": False}


@pytest.mark.asyncio
async def test_json_policy_output_roundtrips_through_stage():
    stage = SimulatedStage(node_id="stage", authorized=True)
    state = stage.snapshot(0.0)
    policy = DeterministicStepPolicy(step_mm=5.0)
    payload = await policy.predict({**state.model_dump(), "target_mm": 20.0, "now": 0.1})
    result = stage.execute(payload, state, 0.1)
    assert result["ok"]


def test_kinematic_predictor_is_analytical_and_does_not_actuate():
    stage = SimulatedStage(node_id="stage", authorized=True)
    state = stage.snapshot(0.0)
    proposal = DeterministicStepPolicy().propose(state, target_mm=20.0, now=0.1)
    prediction = KinematicPredictor().predict(state, proposal, 0.1)
    assert prediction["source"] == "synthetic"
    assert prediction["trained"] is False
    assert prediction["predicted_position_mm"] == 10.0
    assert stage.snapshot(0.1).position_mm == state.position_mm


def test_simulated_stage_requires_explicit_authorization():
    stage = SimulatedStage(node_id="stage")
    state = stage.snapshot(0.0)
    proposal = DeterministicStepPolicy().propose(state, target_mm=20.0, now=0.1)
    result = stage.execute(proposal, state, 0.1)
    assert result["ok"] is False
    assert "disallowed_no_allow" in result["flags"]
    assert stage.snapshot(0.1).position_mm == state.position_mm
