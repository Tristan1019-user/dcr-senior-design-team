# Architecture and contracts

## Boundaries

- `dcr/src/runtime.c`: immutable registry resolution, owned instances, command API, inspection.
- `lifecycle.c`: legal state transitions, task trampoline, watchdog, cooperative stop/join.
- `port.c`: typed endpoint resolution, signal fan-out and per-consumer queues.
- `validator.c`: required-input validation and deferred-start warnings.
- `config.c`: bounded strict JSON parsing, schema/option checks, construction through public APIs.
- `platform/host`: desktop threads/synchronization; `platform/esp32`: FreeRTOS tasks, mutexes, binary semaphores and queues.
- `platform/common`: bounded filesystem read. `components/common`: portable demo components.

The core includes no ESP-IDF or FreeRTOS headers. PAL functions isolate memory,
logging, monotonic time, files, task handles, locks, wakeups and queues. Firmware
bus adapters use SDK hardware drivers; the runtime cannot prevent
an arbitrary compiled C component from bypassing the contract.

## Ownership and concurrency

The caller owns the immutable registry/descriptors for the runtime's lifetime.
The runtime copies component configuration JSON and owns instances, port handles,
connection records, signal values, notification events and input queues.
Component init owns its resources via `user_data`; it must publish partial state
there before returning an error. Stop must accept NULL and release partially
initialized state too. Stop is called exactly once per init attempt.

Callbacks use `dcr_port(instance, name)` once at run entry, then cache the handle.
Undeclared names fault the instance and return NULL; this is a recoverable checked
error rather than the spec's optional debug-process assertion. Components do not
call each other. Handles must not outlive the instance; no ISR may invoke normal
port APIs. Future interrupt components must hand ISR work to their task using
ISR-safe SDK primitives.

One external control-plane owner serializes topology/lifecycle/config calls.
All topology changes require no component task handles, including exited-but-not-
joined tasks. Port access and watchdog state updates are mutex-protected. This
is not a general concurrent control-plane API and does not attempt live graph
rewiring under running callbacks.

## Signal and queue behavior

Each output owns a latest typed value and monotonically increasing sequence.
SIGNAL consumers reference that shared output, each with an independent update
cursor and wakeup event. Reads are non-destructive; wait consumes only that
consumer's notification/cursor, not the shared value. Updates may coalesce.

Each ordinary QUEUE input owns an eight-item queue. FreeRTOS uses `xQueueCreate`;
the Windows PAL uses a bounded lock-protected ring. Fan-out reserves capacity in
all destination queues before any publication. A full queue blocks the producer
up to the requested timeout; timeout produces no partial fan-out or silent drop.
Thus the slowest ordered consumer intentionally applies backpressure to the
whole output, including signal consumers. Components decide how to handle timeout.

Publication is serialized per runtime; waits release the lock. Wakeups are per
input, with 10 ms bounded waits to check cooperative stop/heartbeat. This is a
correctness-first implementation, not a measured hard-real-time scheduler.
Port type comes only from compiled descriptors (BOOL, INT32, UINT32, FLOAT,
BUS_REQUEST). The structured bus type is not emitted as a scalar telemetry value.

The canonical signal name is the first explicit name or `instance.port`; later
different names on the same output normalize to it, rather than creating another
signal. Named aliases are not separately addressable in this MVP.

## Lifecycle

`UNLOADED -> INITIALIZED -> RUNNING -> STOPPED`, then explicit init/start.
Failed init and unexpected run return produce FAULTED. Faulted tasks must be
joined before re-init. Stop requests cancellation, wakes waits, joins the task,
then calls stop to free resources. If join times out, memory/resources remain
owned; callers must not destroy/free the runtime. There is no unsafe forced task
deletion. Bus workers must outlive their active clients; shutdown has to quiesce
clients before workers and retain resources after a client join timeout. Re-init
discards queued input values, releases queued reply references, and resets its
signal cursor to the source's current sequence.

Cooperative sleep and port waits refresh heartbeat. A run callback busy elsewhere
must call `dcr_heartbeat`. The monitor marks overdue tasks FAULTED and requests
stop, without pretending a wedged task has released hardware. A watchdog fault
is not an independent hardware safety mechanism and does not stop dependent
actuators automatically. Only the connection-lock policy described below is
implemented; other proposed safety-rule types remain future work.

## Shared buses: approved interpretation

Ordinary inputs have one driver (R07). A descriptor may explicitly mark a QUEUE
INPUT `multi_producer=true`; only this resource-request input accepts several
OUTPUT sources. It owns one queue; the resource component is its single consumer.
The connection API rejects requesters above the resource owner's priority (R23).

The I2C adapter uses a dedicated worker for each controller. Sensors publish a
BUS_REQUEST with operation, address, up to 32 data bytes, transaction ID and private
reply-queue handle. Each request output has exactly one worker destination. The
worker serializes transactions and replies without a requester lookup table.
The ESP-IDF handles remain inside the platform bus adapter, not the sensor code.

Each sensor allocates its response queue at initialization. A successfully queued
request owns a retained reference; the worker sends its reply before releasing
that reference. Closing the client queue rejects late replies, but storage stays
alive until every queued/in-flight reference is released. Drains also release
references. A worker must never use a reply pointer after releasing its request.
`dcr_bus_request_release` is called exactly once per dequeued request, not on a
producer's unqueued local copy. This is a trusted C API contract, not memory-safe
sandboxing of arbitrary third-party components.

The mock backend tests contention and lifecycle behavior; earlier physical runs
are summarized in [STATUS.md](STATUS.md). SPI/general UART and arbitrary
hardware resources still need adapters and their own tests. R24 is not a claim
that every resource type has been implemented.

Serialization is per request. An I2C register read uses one combined SDK
transaction, but a command/wait/read sequence is not a multi-request lock or
reservation of a sensor address. The physical demo has one client per sensor.
Future multiple clients of the same peripheral need a protocol-level owner or
an explicitly bounded grouped-transaction contract.

## Bounds and schema choices

24 instances, 64 connections, 12 ports/type, names of 1-47 ASCII letters/digits,
underscore or hyphen; JSON <=64 KiB and <=32 nesting levels. Unknown/duplicate
schema keys are rejected. Component config is opaque to the runtime; its callback
validates values. No tasks or init callbacks run before structural validation.
Parser/staging memory is necessarily allocated while parsing; the spec's
"unknown type before any allocation" is interpreted as before any instance,
channel or hardware allocation, not literally before JSON parsing.

Priority 1..platform maximum; stack bytes 2048..65536; optional core defaults to
unbound. Stack precedence: explicit instance, explicit global setting, descriptor,
then 3072 bytes. This resolves conflicting descriptor-default vs global-default
wording without silently overwriting type-specific stack needs.

`dcr_get_topology` is a state/connection snapshot, not a reloadable configuration
export. `dcr_build_json` creates a new runtime; a malformed candidate does not
mutate an existing runtime. If an init fails, it returns the inspectable runtime
with faulted instances and starts no tasks. Caller must destroy it when finished.

## Immutable connection policy

`safety_rules` supports `connection_locked` only. Validated boot rules retain
the protected port references and block public disconnect or connect to the
destination, including additional bus-queue producers. Public component deletion
cannot remove connected endpoints. Whole-runtime teardown bypasses locks only
after tasks have joined. Rules are inspectable but not remotely editable.
See [CONNECTION_LOCKS.md](CONNECTION_LOCKS.md) for semantics and explicit limits.
