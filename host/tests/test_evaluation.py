from types import SimpleNamespace

from dcr_host.evaluation import assess


def test_full_cycle_requires_b_progress_while_a_stopped():
    def observation(a_state, b_sequence):
        return {"tool": "get_health", "arguments": {}, "output": {"ok": True, "result": {
            "components": [
                {"id": "sht45", "state": a_state, "healthy": a_state == "RUNNING", "outputs": {}},
                {"id": "bmp280", "state": "RUNNING", "healthy": True,
                 "outputs": {"temperature_c": {"sequence": b_sequence}}},
            ]}}}

    def mutation(name):
        return {"tool": name, "arguments": {"component": "sht45"},
                "output": {"ok": True, "result": {}}}

    trace = [observation("RUNNING", 1), mutation("stop_component"),
             observation("STOPPED", 1), observation("STOPPED", 2), mutation("init_component"),
             mutation("start_component"), observation("RUNNING", 3)]
    result = SimpleNamespace(trace=trace, outcome="model_finished", mutations_verified=True)
    mock = SimpleNamespace(components={"bmp280": {"config": {"period_ms": 1000}}})
    assert assess("full_cycle", result, mock)
    trace[3] = observation("STOPPED", 1)
    assert not assess("full_cycle", result, mock)
    trace[3] = observation("RUNNING", 2)
    trace[2] = observation("RUNNING", 1)
    assert not assess("full_cycle", result, mock)
    del trace[2]
    assert not assess("full_cycle", result, mock)
