# How it works

```text
Scripted task or model
          |
Python tools: device identity, permissions, freshness, input checks
          |
          +-- Serial adapter --> ESP32 runtime --> I2C workers --> Sensors
          |
          +-- Policy checks --> Simulated position stage
```

## On the ESP32

DCR is C11 code with an ESP-IDF/FreeRTOS platform layer. Components are compiled
into a registry. JSON selects their instances, settings, and connections. It is
dynamic configuration, not downloading and executing arbitrary new code.

Each component has a lifecycle and typed ports. Signals expose a latest value;
queues deliver bounded sequences with backpressure. The runtime validates the
graph before starting it, and joins component tasks before freeing their memory.

Two bus workers own the two I2C controllers. Sensors send requests to those
workers and get responses through private queues. This keeps peripheral handles
out of the sensor components and allows controlled shutdown. Most inputs allow
one producer; bus-request queues explicitly allow several clients.

There are eight components in the sensor configuration: two bus workers, two
sensors, and four numeric loggers. Connection locks prevent selected wiring
changes. They do not guarantee that a sensor stays healthy or replace hardware
safety mechanisms.

## On the host

The Python client exchanges bounded JSON lines over serial. Device adapters
translate supported outputs into capabilities such as temperature readings.
Observations include units, age, sequence, health, and a boot identifier so a
stale value or reboot is not mistaken for a fresh measurement.

Read-only access is the default. Sensor control needs explicit permission and
only exposes supported lifecycle/configuration operations. A lost response can
mean an action happened even though the host never saw the reply. In that case,
further writes are blocked pending inspection rather than automatically retried.
The current command cache is in memory, so durable restart recovery is still work
to do.

## Where the model fits

An OpenAI-compatible adapter presents a small set of tools to the model. The
model can request a read or an allowed operation; it cannot grant itself more
permission. Host code checks the arguments and the returned device state.
This is also how the scripted workflow runs, making comparison possible.

For the stage demo, a separate policy interface proposes bounded steps. Checks
cover units, coordinate frame, age, limits, and replay before the simulated stage
accepts them. The current policy is analytical, not learned. A real actuator
would need its own adapter, local limits, feedback, and physical tests.

## Design references

- [C runtime internals](architecture.md)
- [Serial protocol](control-protocol-v1.md)
- [Sensor wiring, status codes, and firmware](SENSOR_DEMO.md)
- [Connection locks](CONNECTION_LOCKS.md)
- [Current limits and next steps](STATUS.md)
