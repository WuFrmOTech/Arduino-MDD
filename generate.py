"""Generate Arduino sketches from the course modeling tool export.

Usage:  python generate.py <model.iml.json> <output-dir> [--strict]
"""
import argparse
import json
import re
import sys
from pathlib import Path

from model_reader import read_models

PIN = "uint8_t"
MS = "unsigned long"

# Documented defaults, used only when the model leaves the value out.
DEFAULTS = {"blinkIntervalMs": 500, "darknessThreshold": 500}
# Values that are simply left out of the sketch when the model has no value.
OPTIONAL = {"readIntervalMs", "defaultSpeed"}
MIN_REPORT_MS = 200
CHANNELS = ("left", "centre", "right")


def identifier(name):
    return "m_" + re.sub(r"[^a-zA-Z0-9_]", "_", name)


def string(value):
    return json.dumps(value, ensure_ascii=False)


def constant(prefix, key, value, cpp_type, note=""):
    return f"constexpr {cpp_type} {prefix}_{key} = {value};{note}"


def pin(value):
    if type(value) is int and 0 <= value <= 255:
        return value
    if isinstance(value, str) and re.fullmatch(r"A[0-5]", value):
        return value
    raise ValueError(f"Invalid pin: {value!r}")


def output(label, expression):
    return f"Serial.print(F({string(label + ' = ')})); Serial.println({expression});"


# ---------------------------------------------------------------- templates
def distance(name):
    return f"""
void {name}_init() {{
  pinMode({name}_triggerPin, OUTPUT);
  digitalWrite({name}_triggerPin, LOW);
  pinMode({name}_echoPin, INPUT);
}}

float {name}_readCm() {{
  digitalWrite({name}_triggerPin, LOW);
  delayMicroseconds(2);
  digitalWrite({name}_triggerPin, HIGH);
  delayMicroseconds(10);
  digitalWrite({name}_triggerPin, LOW);
  const unsigned long echoUs = pulseIn({name}_echoPin, HIGH, 25000UL);
  return echoUs * 0.01715f;  // 0 when there is no valid echo
}}
"""


def tracker(name, sides):
    modes = "\n".join(f"  pinMode({name}_{side}Pin, INPUT);" for side in sides)
    readers = "\n".join(
        f"bool {name}_{side}Dark() {{\n"
        f"  return analogRead({name}_{side}Pin) >= {name}_darknessThreshold;\n"
        "}" for side in sides
    )
    return f"\nvoid {name}_init() {{\n{modes}\n}}\n\n{readers}\n"


def driver(name):
    actions = {
        "forward": ("HIGH", "HIGH"),
        "backward": ("LOW", "LOW"),
        "turnLeft": ("LOW", "HIGH"),
        "turnRight": ("HIGH", "LOW"),
    }
    functions = "\n".join(
        f"void {name}_{action}(uint8_t speed) {{\n"
        f"  {name}_drive({left}, {right}, speed);\n"
        "}" for action, (left, right) in actions.items()
    )
    return f"""
void {name}_stop() {{
  analogWrite({name}_leftPwmPin, 0);
  analogWrite({name}_rightPwmPin, 0);
}}

void {name}_init() {{
  digitalWrite({name}_enablePin, LOW);
  pinMode({name}_enablePin, OUTPUT);
  pinMode({name}_leftPwmPin, OUTPUT);
  pinMode({name}_rightPwmPin, OUTPUT);
  pinMode({name}_leftDirectionPin, OUTPUT);
  pinMode({name}_rightDirectionPin, OUTPUT);
  {name}_stop();
  digitalWrite({name}_enablePin, HIGH);
}}

void {name}_drive(uint8_t leftDirection, uint8_t rightDirection, uint8_t speed) {{
  {name}_stop();
  digitalWrite({name}_leftDirectionPin, leftDirection);
  digitalWrite({name}_rightDirectionPin, rightDirection);
  analogWrite({name}_leftPwmPin, speed);
  analogWrite({name}_rightPwmPin, speed);
}}

{functions}
"""


def led(name):
    return f"""
CRGB {name}_pixels[{name}_count];

void {name}_setColour(CRGB colour) {{
  fill_solid({name}_pixels, {name}_count, colour);
  FastLED.show();
}}

void {name}_on() {{
  {name}_setColour(CRGB({name}_red, {name}_green, {name}_blue));
}}

void {name}_off() {{
  {name}_setColour(CRGB::Black);
}}

void {name}_init() {{
  FastLED.addLeds<WS2812, {name}_dataPin, GRB>({name}_pixels, {name}_count);
  {name}_off();
}}
"""


# Each handler supplies a template, the pin fields it consumes and its settings.
PARTS = {
    "DistanceSensor": (distance, ("triggerPin", "echoPin"), {"readIntervalMs": MS}),
    "LineTracker": (tracker, tuple(f"{s}Pin" for s in CHANNELS),
                    {"darknessThreshold": "int", "readIntervalMs": MS}),
    "MotorDriver": (driver, ("enablePin", "leftPwmPin", "rightPwmPin",
                             "leftDirectionPin", "rightDirectionPin"),
                    {"defaultSpeed": PIN}),
    "StatusLED": (led, ("dataPin",), {"count": "int", "red": PIN, "green": PIN,
                                      "blue": PIN, "blinkIntervalMs": MS}),
}


def tracker_sides(part):
    return [side for side in CHANNELS if f"{side}Pin" in part]


# ---------------------------------------------------------- default behaviour
# Phase 3: replace default_loop() with generated control logic. Nothing else in
# the generator depends on what loop() contains.
def default_loop(parts):
    lines = []
    for part in parts:
        name = identifier(part["name"])
        kind = part["type"]
        if kind == "StatusLED":
            lines.extend([
                f"static bool {name}_lit = false;",
                f"static unsigned long {name}_lastBlink = 0;",
                f"if (now - {name}_lastBlink >= {name}_blinkIntervalMs) {{",
                f"  {name}_lastBlink = now;",
                f"  {name}_lit = !{name}_lit;",
                f"  if ({name}_lit) {name}_on();",
                f"  else {name}_off();",
                "}",
            ])
        elif kind in ("DistanceSensor", "LineTracker"):
            if part.get("readIntervalMs") is not None:
                interval = (f"({name}_readIntervalMs > {MIN_REPORT_MS}UL ? "
                            f"{name}_readIntervalMs : {MIN_REPORT_MS}UL)")
            else:
                interval = f"{MIN_REPORT_MS}UL"
            lines.extend([
                f"static unsigned long {name}_lastReport = 0;",
                f"if (now - {name}_lastReport >= {interval}) {{",
                f"  {name}_lastReport = now;",
            ])
            if kind == "DistanceSensor":
                lines.append("  " + output(part["name"] + " cm", f"{name}_readCm()"))
            else:
                lines.append(f"  Serial.print(F({string(part['name'] + ' ')}));")
                sides = tracker_sides(part)
                for index, side in enumerate(sides):
                    last = index == len(sides) - 1
                    method = "println" if last else "print"
                    suffix = "" if last else " "
                    lines.append(f"  Serial.print(F({string(side + '=')}));")
                    lines.append(
                        f'  Serial.{method}({name}_{side}Dark() ? '
                        f'F("dark{suffix}") : F("light{suffix}"));'
                    )
            lines.append("}")
    return ["const unsigned long now = millis();", *lines] if lines else []


def function(name, lines):
    return "\n".join([f"void {name}() {{", *("  " + line for line in lines), "}"])


# ------------------------------------------------------------------ visitor
def generate(robot):
    parts = robot["parts"]
    names = [identifier(part["name"]) for part in parts]
    if len(names) != len(set(names)):
        raise ValueError(f"{robot['name']}: part names collide after C++ identifier conversion")
    headers = [f"// Generated by {Path(sys.argv[0]).name} - do not edit by hand.",
               "#include <Arduino.h>"]
    if any(part["type"] == "StatusLED" for part in parts):
        headers.append("#include <FastLED.h>")
    constants = []
    templates = []
    setup = ["Serial.begin(9600);", f"Serial.println(F({string(robot['name'])}));"]

    # Every numeric controller setting the model stores becomes a constant.
    for key, value in robot.get("controller", {}).items():
        if key == "name" or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        cpp_type = "int" if isinstance(value, int) else "double"
        ckey = re.sub(r"[^a-zA-Z0-9_]", "_", key)
        constants.append(constant("controller", ckey, value, cpp_type))
        setup.append(output(key, f"controller_{ckey}"))

    for part, name in zip(parts, names):
        template, pins, types = PARTS[part["type"]]
        setup.append(f"{name}_init();")
        setup.append(f"Serial.println(F({string(part['name'] + ' [' + part['type'] + ']')}));")
        if part["type"] == "LineTracker":
            pins = [f"{side}Pin" for side in tracker_sides(part)]
        for key in pins:
            constants.append(constant(name, key, pin(part[key]), PIN))
            setup.append(output(key, f"{name}_{key}"))
        for key, cpp_type in types.items():
            value, note = part.get(key), ""
            if value is None:
                if key in DEFAULTS:
                    value, note = DEFAULTS[key], "  // default: not set in the model"
                elif key in OPTIONAL:
                    continue
                else:
                    raise ValueError(f"{part['name']}: missing value for {key}")
            if type(value) is not int or value < 0 or (key == "count" and value == 0):
                raise ValueError(f"{part['name']}.{key}: invalid value {value!r}")
            constants.append(constant(name, key, value, cpp_type, note))
            setup.append(output(key, f"{name}_{key}"))
        if part.get("mount") is not None:
            setup.append(output("mount", f"F({string(part['mount'])})"))
        threshold_names = set()
        for threshold in part.get("thresholds", []):
            key = "threshold_" + identifier(threshold["name"])[2:] + "Cm"
            if key in threshold_names:
                raise ValueError(f"{part['name']}: threshold names collide in C++")
            threshold_names.add(key)
            constants.append(constant(name, key, threshold["distanceCm"], "double"))
            setup.append(output(threshold["name"] + " cm", f"{name}_{key}"))
        code = template(name, tracker_sides(part)) if part["type"] == "LineTracker" else template(name)
        templates.append(code.strip())
    return "\n\n".join([
        "\n".join(headers), "\n".join(constants), *templates,
        function("setup", setup), function("loop", default_loop(parts)),
    ]) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("model", type=Path, help="the .iml.json export")
    parser.add_argument("output", type=Path, help="folder that receives one sketch folder per model")
    parser.add_argument("--strict", action="store_true",
                        help="stop at the first model that does not conform")
    args = parser.parse_args()
    try:
        robots, errors = read_models(args.model, strict=args.strict)
        generated = {}
        for robot in robots:
            scenario = robot["scenario"]
            if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", scenario):
                raise ValueError(f"Invalid sketch name: {scenario!r}")
            if scenario.lower() in generated:
                raise ValueError(f"Duplicate sketch name: {scenario!r}")
            generated[scenario.lower()] = (scenario, generate(robot))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"error: {exc}\n")
    for message in errors:
        print(f"SKIPPED (does not conform): {message}", file=sys.stderr)
    for scenario, sketch in generated.values():
        directory = args.output / scenario
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{scenario}.ino"
        path.write_text(sketch, encoding="utf-8")
        print(path)
    if not generated:
        parser.exit(1, "error: no conforming models found\n")


if __name__ == "__main__":
    main()
