# Working on this together

Start with [setup](docs/SETUP.md), run the offline checks, and make changes on a
branch. Small commits will make it easier for the rest of us to review them.

- Keep tests for normal operation and failure cases. Do not count a skipped
  hardware or integration test as a pass.
- Keep the distinction between simulated and physical devices visible in results.
- Do not bypass permissions, stale-data checks, or unknown-command handling just
  to get a demo to pass. If an action's outcome is unknown, inspect the device
  before trying it again.
- Agree on wiring, limits, and a stop procedure before a physical test. Running
  the ordinary offline tests must not flash a board or move hardware.
- Save each experiment to a new directory. Record the code revision, setup,
  failures, and measurements rather than overwriting a previous run.
- Keep credentials, device dumps, private addresses, and unreviewed recordings
  out of Git. Local output is ignored by default. Sponsor documents need separate
  permission before public redistribution.

The public repo is the shared copy, not an automatic mirror of the private test
repo. Bring tested changes across deliberately, review the diff for private
details, and rerun the checks. Do not push private history into this repository.

Please leave third-party notices intact. We have not chosen a license for our
own code yet. `.gitattributes` preserves file bytes, so avoid unrelated line-ending
or formatting changes when editing a feature.
