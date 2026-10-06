# Connection locks

`connection_locked` prevents selected wiring changes in the C runtime. The
sensor example locks both sensor-to-bus routes:

```json
"safety_rules": [
  {"id": "preserve_sht_bus", "type": "connection_locked",
   "from": "sht45.bus", "to": "i2c_bus_0.request"}
]
```

Rules load from boot configuration before component initialization. Each must
name an existing edge. The limit is 24 rules. Unknown types/keys, duplicates,
invalid endpoints, or malformed rules reject the candidate runtime.

A rule freezes all incoming wiring at its destination, including new producers
on a bus-request queue. Ordinary source fan-out elsewhere remains allowed.
Public connect/disconnect returns `DCR_ERR_SAFETY` without changing the graph.
Whole-runtime teardown has a private bypass after tasks are joined.

Locks still allow supported stop/init/start and parameter operations. They do
not keep a stopped sensor sampling, validate its measurements, or provide a
physical emergency stop. Compiled callbacks are trusted; this is not memory
isolation or tamper-proof storage.

`dcr_get_safety_rules` and the control protocol expose rule snapshots for
inspection. The remote protocol does not offer graph-edit or policy-edit commands.
`examples/locked_fanout.json` is a host-only example.

Native tests cover ordinary ports, shared bus queues, failed configuration,
lifecycle behavior, teardown, and rejected mutations. Allocation-failure tests
also check cleanup and retry paths. The host failpoint does not intercept every
libc/cJSON allocation and does not run inside the ESP32 driver.

Other proposed patterns, including bounds, dependencies, mutual exclusion, and
signal slew limits, remain unimplemented. Their behavior on stale data, faults,
and recovery needs to be decided before adding them.
