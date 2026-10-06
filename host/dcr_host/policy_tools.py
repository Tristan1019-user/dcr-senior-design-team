"""Control-plane request -> policy proposal -> gate -> synthetic adapter -> proof.

No physical action adapter is supported here. Callers explicitly supply authorized
in-memory stages; a model cannot register a device or change permissions.
"""
import asyncio
import math
import time
from typing import Annotated

from pydantic import Field, StrictFloat, ValidationError

from .protocol import Name, StrictModel
from .policies.simulation import SimulatedStage, DeterministicStepPolicy


class StageTarget(StrictModel):
    node_id: Name
    target_mm: Annotated[StrictFloat, Field(ge=0, le=100, allow_inf_nan=False)]


class PolicyTools:
    def __init__(self, control, stages, policy=None):
        if not isinstance(stages, dict) or len(stages) > 4 or any(
                not isinstance(stage, SimulatedStage) or stage.node_id != node
                or not control.registry.owns_resource(node, stage)
                for node, stage in stages.items()):
            raise ValueError("Explicit synthetic stage bindings required")
        self.control, self.stages = control, dict(stages)
        self.policy = policy or DeterministicStepPolicy()
        self._lock = asyncio.Lock()
        self.instructions = control.instructions + """
There may also be a SYNTHETIC in-memory stage, not a real actuator or robot.
request_stage_target asks a separate bounded policy to propose motion; a deterministic
gate and simulator validate it. Discover the actual stage and read its capability.
Only explicitly bound simulation stages can receive target requests. Never claim
simulation movement is physical movement. Report verification, source, and limits.
Sequence counters belong to individual outputs, not a common clock."""

    @property
    def unverified(self):
        return self.control.unverified

    def record(self, kind, payload):
        self.control.record(kind, payload)

    def definitions(self):
        return self.control.definitions() + [{"type": "function", "function": {
            "name": "request_stage_target", "description": "Request a bounded SYNTHETIC stage target in mm; never moves physical hardware.",
            "parameters": StageTarget.model_json_schema(), "strict": True}}]

    async def execute(self, name, arguments):
        if name != "request_stage_target":
            return await self.control.execute(name, arguments)
        async with self._lock:
            try:
                args = StageTarget.model_validate(arguments)
            except ValidationError:
                return self.control._error("INVALID_ARGUMENT")
            stage = self.stages.get(args.node_id)
            if stage is None:
                return self.control._error("FORBIDDEN")
            applied = 0
            try:
                async with asyncio.timeout(8):
                    for _ in range(16):
                        now = time.monotonic()
                        observation = stage.snapshot(now)
                        if math.isclose(observation.position_mm, args.target_mm, abs_tol=1e-6):
                            output = {"ok": True, "result": {"verified": True, "source": "synthetic",
                                "node_id": args.node_id, "position_mm": observation.position_mm,
                                "applied_chunks": applied, "policy": await self.policy.model_info()}}
                            self.record("tool_result", {"tool": name, "args": arguments, **output})
                            return output
                        proposal = await asyncio.wait_for(self.policy.predict({
                            **observation.model_dump(mode="json"), "target_mm": args.target_mm, "now": now}), 2)
                        self.record("policy_proposal", {"source": "synthetic", "node_id": args.node_id,
                                                       "observation": observation.model_dump(mode="json"), "proposal": proposal})
                        outcome = stage.execute(proposal, observation, time.monotonic())
                        self.record("policy_execution", outcome)
                        if not outcome["ok"]:
                            return {"ok": False, "error": {"code": "POLICY_REJECTED",
                                "message": "Proposal rejected; no retry"}, "result": outcome}
                        applied += 1
                code = "POLICY_STEP_LIMIT"
            except TimeoutError:
                code = "POLICY_TIMEOUT"
            except Exception:
                code = "POLICY_FAILURE"
            output = {"ok": False, "error": {"code": code, "message": "Policy request incomplete; inspect state"},
                      "applied_chunks": applied, "source": "synthetic"}
            self.record("policy_failure", output)
            return output
