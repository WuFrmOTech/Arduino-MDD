# Elegoo V4 Robot Phase 3 Parts A and B

## Open and run the project

1. Open `Robot-Phase3-Parts-A-B.iml.json` in the [course IML editor](https://prof-rapos.github.io/iml/).
2. Regenerate both Arduino sketches from an extracted copy of this folder with Python 3:

   ```bash
   python generate.py Robot-Phase3-Parts-A-B.iml.json sketches
   ```

3. In the Arduino IDE, install FastLED, open a scenario sketch from its folder under `sketches/`, select the Arduino Uno and your robot's port, and upload. Open the Serial Monitor at 9600 baud to see the `STATE` and `LOG` messages. Follow the assignment's instruction to test with the wheels lifted first.

## What the files do

- `Robot-Phase3-Parts-A-B.iml.json` contains the metamodel and both scenario instances, including the structural links, protocols, matching ports, and the two controller state machines. The controller subclasses declare their own ports so the IML editor can resolve the connectors directly.
- `model_reader.py` resolves instance values, inherited attributes, structural links, and the parts bound to controller ports. `generate.py` produces the hardware functions. `behaviour.py` translates the model's transitions, guards and actions into Arduino code, using the bound hardware functions for commands. Both sketches are generated from this model.
- The button uses pin 2 with `INPUT_PULLUP`. The generated loop samples it every 20 ms and sends one event per sampled released-to-pressed change. The line tracker keeps its earlier dark checks and adds raw left, centre and right analog reads. The distance signal carries whole centimetres, with 0 reserved for a missing echo.
- Scenario E starts with a red LED, drives after a button press, turns right when it detects a valid reading at or below 15 cm, and stops if five seconds of driving elapse. Scenario F uses the raw centre reading and turns left when that reading is above 750. Both turns use the modeled angle multiplied by 8 ms. The 90-degree setting gives a 720 ms turn.

The V4 pin assignments are from the Phase 2 hardware model and match the pin definitions in [ELEGOO's V4 driver header](https://github.com/elegooofficial/ELEGOO-Smart-Robot-Car-Kit-V4.0-New/blob/main/DeviceDriverSet.h). The sketches were compiled for Arduino Uno with Arduino AVR Boards 1.8.8 and FastLED 3.9.15. The behaviour was checked with simulated inputs.

## Two behavioural well-formedness rules

1. **Reachability.** Every named state must have a directed path from its controller's initial pseudostate. An isolated state or isolated cycle violates this rule.
2. **Non-overlapping guards.** Two transitions from the same state with the same port and signal must have mutually exclusive guards. An empty guard is true, so it overlaps any other guard that can evaluate to true on that trigger.
