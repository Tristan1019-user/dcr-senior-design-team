"""Explicit sensor recovery, never automatic rollback or uncertain-command retry."""
import asyncio

from pydantic import ValidationError

from .protocol import ComponentArgs, ParameterArgs


def plan(component, period_ms=None):
    state = component["state"]
    if component["type"] not in ("sht45_sensor", "bmp280_sensor"):
        raise ValueError("Recovery only supports sensor components")
    if state not in ("RUNNING", "STOPPED", "INITIALIZED", "FAULTED"):
        raise ValueError("Unsupported recovery state")
    change_period = period_ms is not None and period_ms != component["config"].get("period_ms")
    if state == "RUNNING" and component.get("healthy") and not change_period:
        return []
    actions = []
    if state in ("RUNNING", "FAULTED") or (state == "INITIALIZED" and change_period):
        actions.append("stop_component")
        state = "STOPPED"
    if change_period:
        actions.append("set_parameter")
    if state == "STOPPED":
        actions.append("init_component")
    actions.append("start_component")
    return actions


async def recover(executor, component, period_ms=None, execute=False):
    try:
        ComponentArgs.model_validate({"component": component})
        if period_ms is not None:
            ParameterArgs.model_validate({"component": component, "name": "period_ms", "value": period_ms})
    except ValidationError:
        return executor.error("INVALID_ARGUMENT", "Invalid recovery target or period")
    observed = await executor.execute("get_component", {"component": component})
    if not observed["ok"]:
        return observed
    initial = observed["result"]
    try:
        actions = plan(initial, period_ms)
    except ValueError as exc:
        return executor.error("FORBIDDEN", str(exc))
    report = {"component": component, "initial": initial, "actions": actions,
              "executed": False, "steps": [], "persistent": False}
    if not execute:
        return {"ok": True, "result": report}
    if component not in executor.allowed:
        return executor.error("FORBIDDEN", "Recovery requires an explicit component mutation allowlist")
    report["executed"] = True
    for action in actions:
        args = {"component": component}
        if action == "set_parameter":
            args.update(name="period_ms", value=period_ms)
        response = await executor.execute(action, args)
        report["steps"].append({"operation": action, "response": response})
        if not response["ok"]:
            return {"ok": False, "result": report, "error": response["error"]}
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            observed = await executor.execute("get_component", {"component": component})
            if not observed["ok"]:
                return {"ok": False, "result": report, "error": observed["error"]}
            report["last_observation"] = observed["result"]
            if component not in executor.unverified:
                break
            if asyncio.get_running_loop().time() >= deadline:
                return {"ok": False, "result": report,
                        "error": {"code": "RECOVERY_UNVERIFIED", "message": "No healthy verified state within five seconds; no retry performed"}}
            await asyncio.sleep(.25)
    final = await executor.execute("get_component", {"component": component})
    report["final"] = final.get("result")
    good = final["ok"] and final["result"].get("healthy", False)
    output = {"ok": bool(good), "result": report}
    if not good:
        output["error"] = final.get("error", {"code": "RECOVERY_UNVERIFIED", "message": "Sensor is not healthy"})
    executor.client.record("recovery", output)
    return output
