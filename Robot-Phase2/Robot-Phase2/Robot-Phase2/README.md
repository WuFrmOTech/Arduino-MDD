# Robot Phase 2 Sketch Generator

The generator reads `Robot-Phase2.iml.json` and produces one Arduino sketch per scenario. `generate.py` generates the sketches, and `model_reader.py` reads the exported robot model.

## Regenerate all sketches

Extract the ZIP, open a terminal in the folder containing `generate.py`, and run this command with Python 3. No additional Python packages are required.

```bash
python generate.py Robot-Phase2.iml.json sketches
```

The command creates or overwrites all seven sketches under `sketches/`, with each file at `sketches/<scenario>/<scenario>.ino`.

To compile a sketch, open its `.ino` file in the Arduino IDE, install the FastLED library if the sketch uses it, select your board, and click **Verify**.
