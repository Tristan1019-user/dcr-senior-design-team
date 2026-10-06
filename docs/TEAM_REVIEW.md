# What I want us to discuss

I started with the ESP32 and two sensors because they gave me a way to test the
runtime and host together without needing a robot first. I think the useful
next step is to make the same interface work across different kinds of hardware,
then test whether a model helps coordinate them.

We already have something runnable, so we do not need to start over. But I do
not want us to mistake a working sensor demo for the entire project.

## My suggested next experiment

Choose one actuator with feedback and a documented controller interface. Give it
one repeatable task that uses an observation from the sensor board. Run the task
with a script first, then with a model using the same available commands.

Measure completion, time, incorrect requests, and what happens when a sensor or
connection fails. The device still needs local limits and a way to stop without
depending on Python or the model.

## Decisions for the group

| Question | My starting point |
|---|---|
| What hardware can we actually use? | Borrow a feedback-equipped stage, gripper, or isolated robot before buying anything. |
| What task should we demonstrate? | One measurable task involving two different controllers. |
| Where does AI help? | Compare against the scripted baseline; measure rather than assume. |
| What happens if a command times out? | Read the device state before retrying; make recovery survive a host restart. |
| What is required versus extra? | Agree on the course/sponsor deliverable before adding vision or learned motion. |

## Possible work split

- Device side: runtime, drivers, wiring, local limits, and fault handling.
- Host side: device adapters, model tools, permissions, and recovery.
- Experiments: task definition, repeatable tests, measurements, and comparisons.

These are areas we can divide up, not assignments I have made for anyone.
Everyone should be able to run the offline demo and explain which parts are real.

## Larger direction

After one real action works reliably, add a second controller type and a second
model backend. Vision and learned policies could come later, once there is a task
and useful data to train on. The host already separates task planning from the
action-policy interface, but the only current policy is an analytical simulation.

I would rather show one complete task, including a failure and recovery, than a
larger demo that only works when nothing goes wrong.
