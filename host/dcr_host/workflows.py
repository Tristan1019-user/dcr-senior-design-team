"""Bounded, independently checked workflows above node-qualified tools."""
import asyncio
import time
import uuid


class WorkflowFailure(RuntimeError):
    pass


_NO_ACTION_ERRORS = frozenset((
    "INVALID_ARGUMENT", "FORBIDDEN", "INSPECTION_REQUIRED", "VERIFICATION_REQUIRED",
    "INVALID_STATE", "NOT_FOUND", "BOOT_MISMATCH", "RECONNECT_REQUIRED",
))


def _result(output):
    if not output.get("ok"):
        raise WorkflowFailure(output.get("error", {}).get("code", "FAILED"))
    return output["result"]


def _component(health, name):
    found = [item for item in health["components"] if item["id"] == name]
    if len(found) != 1:
        raise WorkflowFailure("COMPONENT_NOT_FOUND")
    return found[0]


async def sensor_cycle(executor, node_id, component="sht45", companion_node=None,
                       companion_capability="bmp280.temperature_c"):
    """Stop/init/start one authorized sensor; prove an independent output advances.

    This is a regression scenario, not a generic physical transaction. Recovery
    only restores this initially-running sensor when its boot and state are known.
    Unknown write outcomes are never replayed; the executor retains its interlock.
    """
    companion_node = companion_node or node_id
    if companion_node == node_id and companion_capability.split(":")[-1].split(".")[0] == component:
        raise ValueError("Companion must be independent of the stopped component")
    run_id = uuid.uuid4().hex
    started = time.monotonic()
    report = {"workflow": "sensor_isolation_cycle/v1", "run_id": run_id,
              "node_id": node_id, "component": component, "companion_node": companion_node,
              "passed": False, "steps": [], "recovery": "not_needed"}
    initial, original = None, None
    recording_failed = False
    unknown_outcome = False
    primary_exception = None
    reraise_primary = False
    outcome_record_error = None

    def record(kind, payload):
        nonlocal recording_failed
        if recording_failed:
            return
        try:
            executor.record(kind, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            recording_failed = True
            raise

    async def step(tool, arguments):
        nonlocal unknown_outcome
        mutation = tool == "control_component"
        if mutation:
            # A raised timeout/exception may follow a wire write. Only the
            # response below can prove that no mutation needs interlocking.
            unknown_outcome = True
        output = await executor.execute(tool, arguments)
        report["steps"].append({"tool": tool, "arguments": arguments, "output": output})
        record("workflow_step", report["steps"][-1])
        if mutation:
            error_code = output.get("error", {}).get("code") if isinstance(output, dict) else None
            if isinstance(output, dict) and output.get("ok") and \
                    isinstance(output.get("result"), dict) and \
                    output["result"].get("verified") is True:
                unknown_outcome = False
            elif isinstance(output, dict) and not output.get("ok") and \
                    error_code in _NO_ACTION_ERRORS:
                unknown_outcome = False
        return _result(output)

    async def inspect():
        return await step("inspect_node", {"node_id": node_id})

    async def command(operation, suffix):
        result = await step("control_component", {"node_id": node_id, "component": component,
            "operation": operation, "expected_boot_id": initial["boot_id"],
            "command_id": run_id + suffix, "value": None})
        if result.get("verified") is not True:
            raise WorkflowFailure("UNVERIFIED_COMMAND")

    async def companion():
        data = await step("read_capability", {"node_id": companion_node,
                                               "capability_id": companion_capability})
        sample = data["observations"][0]
        if not sample["fresh"] or not sample["healthy"] or sample["sequence"] is None:
            raise WorkflowFailure("COMPANION_UNHEALTHY")
        return sample

    try:
        record("workflow_start", {k: v for k, v in report.items() if k != "steps"})
        async with asyncio.timeout(45):
            initial = await inspect()
            original = _component(initial, component)
            if original["state"] != "RUNNING" or not original["healthy"]:
                raise WorkflowFailure("INITIAL_SENSOR_NOT_HEALTHY")
            healthy_before = {c["id"] for c in initial["components"] if c["healthy"]}
            await companion()
            await command("stop", "s")
            stopped = await inspect()
            if _component(stopped, component)["state"] != "STOPPED":
                raise WorkflowFailure("STOP_NOT_OBSERVED")
            first = await companion()
            deadline = time.monotonic() + 5
            while True:
                await asyncio.sleep(.25)
                last = await companion()
                if last["boot_id"] != first["boot_id"]:
                    raise WorkflowFailure("COMPANION_REBOOTED")
                if last["sequence"] > first["sequence"]:
                    break
                if time.monotonic() >= deadline:
                    raise WorkflowFailure("COMPANION_DID_NOT_ADVANCE")
            still_stopped = await inspect()
            if _component(still_stopped, component)["state"] != "STOPPED":
                raise WorkflowFailure("STOPPED_WINDOW_LOST")
            await command("init", "i")
            await command("start", "r")
            final = await inspect()
            if final["boot_id"] != initial["boot_id"]:
                raise WorkflowFailure("NODE_REBOOTED")
            if not healthy_before <= {c["id"] for c in final["components"] if c["healthy"]}:
                raise WorkflowFailure("HEALTH_NOT_RESTORED")
            if {c["id"]: c["config"] for c in initial["components"]} != {
                    c["id"]: c["config"] for c in final["components"]}:
                raise WorkflowFailure("CONFIGURATION_CHANGED")
            report.update(passed=True, boot_id=final["boot_id"], configuration_unchanged=True,
                          healthy_before=sorted(healthy_before),
                          companion_sequences=[first["sequence"], last["sequence"]],
                          companion_source=last["source"], final_health=final)
    except asyncio.CancelledError as exc:
        primary_exception = exc
        reraise_primary = True
        raise
    except (WorkflowFailure, TimeoutError) as exc:
        primary_exception = exc
        report["error"] = str(exc) if isinstance(exc, WorkflowFailure) else "WORKFLOW_TIMEOUT"
        if recording_failed or unknown_outcome:
            report["recovery"] = "manual_inspection_required"
        elif initial and original and original["state"] == "RUNNING":
            report["recovery"] = "inspection_required"
            try:
                async with asyncio.timeout(12):
                    current = await inspect()
                    if current["boot_id"] != initial["boot_id"]:
                        raise WorkflowFailure("RECOVERY_BOOT_CHANGED")
                    state = _component(current, component)["state"]
                    if state == "STOPPED":
                        await command("init", "recoveryi")
                        state = "INITIALIZED"
                    if state == "INITIALIZED":
                        await command("start", "recoveryr")
                    restored = await inspect()
                    report["recovery"] = "restored" if _component(restored, component)["healthy"] else "manual_inspection_required"
            except asyncio.CancelledError:
                raise
            except (WorkflowFailure, TimeoutError):
                report["recovery"] = "manual_inspection_required"
            except Exception as recovery_error:
                report["recovery"] = "manual_inspection_required"
                report["recovery_error_type"] = type(recovery_error).__name__
    except Exception as exc:
        # An unexpected executor/shape error may follow a wire write. Do not
        # attempt recovery; preserve the original exception for the caller.
        primary_exception = exc
        reraise_primary = True
        report["error"] = "WORKFLOW_EXCEPTION"
        report["error_type"] = type(exc).__name__
        report["recovery"] = "manual_inspection_required"
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if not recording_failed and not isinstance(primary_exception, asyncio.CancelledError):
            try:
                record("workflow_outcome", report)
            except asyncio.CancelledError:
                raise
            except Exception as record_error:
                outcome_record_error = record_error
    if outcome_record_error is not None:
        if primary_exception is not None:
            raise primary_exception from outcome_record_error
        raise outcome_record_error
    if reraise_primary:
        raise primary_exception
    return report
