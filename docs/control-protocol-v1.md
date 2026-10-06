# DCR control protocol v1 (implementation contract)

UTF-8 JSON Lines, maximum 8192 bytes per frame excluding newline. Firmware
protocol lines start with `@DCR `; host requests are bare JSON + newline. Human
logs never have that prefix. Host never parses human logs as state. Single
control owner, one outstanding host request. No automatic mutation retries.

Request: `{"v":1,"id":1,"op":"get_components","args":{}}`.
IDs are integers 1..2147483647. Exactly v/id/op/args, no duplicate or unknown
keys. Unknown versions/ops/args rejected before mutation.
Success: `{"v":1,"id":1,"ok":true,"result":{...}}`.
Failure: `{"v":1,"id":1,"ok":false,"error":{"code":"INVALID_STATE","message":"..."}}`.
Unparseable/uncorrelatable frames may receive id:null error; never execute them.

Ops and exact args:
- get_runtime_info {} -> {protocol:1, boot_id:string, uptime_ms:int, capabilities:[op...]}
- get_components {} -> {components:[component...]}
- get_component {component:string} -> component
- get_topology {} -> {components:[...], connections:[{from,to,signal}]}
- get_configuration {} -> {components:[{id,type,config}], persistent:false}
- init_component/start_component/stop_component {component:string} -> component
- set_parameter {component:string,name:"period_ms",value:int} -> component

Component shape:
`{id,type,state,fault,config,outputs}`. state is UNLOADED/INITIALIZED/RUNNING/
STOPPED/FAULTED. fault is a string ("ok" for none). outputs is a dict keyed by
compiled output port name; each output is
`{value:number|bool|null,sequence:int,timestamp_ms:int|null,age_ms:int|null}`.
No successful publication -> null value/time/age, sequence zero. Device age is
computed from monotonic uint32 elapsed time. Snapshot cannot turn a stale value
fresh. Init invalidates the target's old output samples.
Status port numeric enum follows SENSOR_DEMO.md. Status=0 alone is not healthy:
all required numeric outputs must be fresh and the component RUNNING.
Snapshots are not atomic physical multi-port acquisitions.

Firmware async event:
`{"v":1,"event":"snapshot","boot_id":"...","components":[component...]}`.
At most 4 Hz, contains latest values (not guaranteed delivery of every sample).
Device sensors continue if no host/model exists. Host history is bounded and
represents observed samples, not guaranteed lossless device history.
Boot-ID changes invalidate cache and pending state; require a new handshake.

Mutation scope: only sht45 and bmp280 sensor IDs; no logger/task control exposed.
stop requires RUNNING/INITIALIZED/FAULTED with bounded join, never force-delete.
init requires STOPPED (FAULTED recovery requires stop first).
start requires INITIALIZED, never silently initializes.
set_parameter requires STOPPED and name=period_ms, integer250..2000. No pin/bus
edits; update copies existing config, only changes period. Explicit sequence:
stop -> set_parameter -> init -> start. Edits RAM-only, reboot reloads flash.

Device returns INVALID_STATE, NOT_FOUND, INVALID_ARGUMENT, UNKNOWN_OPERATION,
UNSUPPORTED_VERSION, FORBIDDEN, TIMEOUT, INTERNAL or FRAME_TOO_LARGE errors.
Request overflows are discarded through newline. Malformed frames, duplicates,
nonfinite values, boolean IDs/periods, fractional integer fields must fail closed.
No arbitrary shell, raw bus, GPIO, firmware, topology edit, or code operation.

Host tools use the same operation names/schemas, default to read-only, and
require an explicit mutation allowlist. A timeout/disconnect means mutation
outcome may be unknown; reconnect/inspect, do not resend automatically.
