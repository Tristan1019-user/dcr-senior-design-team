import pytest

from dcr_host.capabilities import Capability, Observation, NodeRegistry


class Adapter:
    node_id, source = "external", "synthetic"

    def __init__(self, **changes):
        self.resource_identity = self
        self.changes = changes

    async def discover(self):
        return [Capability(local_id="position", semantic_kind="position.read", component="stage",
                           unit="mm", origin="fixture", source=self.source)]

    async def observe(self, capability_id=None):
        fields = dict(node_id=self.node_id, capability_id="external:position", boot_id="boot",
                      value=2.0, unit="mm", age_ms=0, fresh=True, healthy=True,
                      sequence=1, source_timestamp_ms=0, source=self.source)
        fields.update(self.changes)
        return [Observation(**fields)]


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"unit": "degC"}, {"capability_id": "external:invented"},
                                     {"fresh": False}, {"value": None}, {"age_ms": None}])
async def test_unfiltered_adapter_results_obey_catalog(changes):
    registry = NodeRegistry()
    registry.register(Adapter(**changes))
    result = await registry.observe("external")
    assert not result["ok"] and result["error"]["code"] == "invalid"


@pytest.mark.asyncio
async def test_unknown_capability_cannot_be_asserted_by_adapter():
    registry = NodeRegistry()
    adapter = Adapter()
    registry.register(adapter)
    assert registry.owns_resource("external", adapter)
    assert not registry.owns_resource("external", object())
    result = await registry.observe("external", "invented")
    assert not result["ok"] and result["error"]["code"] == "not_found"
