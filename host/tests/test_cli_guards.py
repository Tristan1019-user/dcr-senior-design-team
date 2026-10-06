import argparse

import pytest

from dcr_host.cli import run


@pytest.mark.asyncio
async def test_live_agent_rejected_before_opening_device():
    args = argparse.Namespace(command="agent", mock=False, port="NOT-A-PORT")
    with pytest.raises(ValueError, match="gated"):
        await run(args)


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), -1, 0, 3601])
async def test_watch_duration_is_bounded(seconds):
    args = argparse.Namespace(command="watch", mock=True, port=None, seconds=seconds)
    with pytest.raises(ValueError, match="duration"):
        await run(args)
