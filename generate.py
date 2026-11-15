import argparse
import json
import re
from pathlib import Path


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_values(obj, attributes_by_id):
    """Convert exported attribute IDs into readable field names."""
    values = {}

    for attr_id, raw_value in obj["attributeValues"].items():
        if raw_value is None or raw_value == "":
            continue

        attribute = attributes_by_id[attr_id]
        if attribute["type"] == "INT":
            raw_value = int(raw_value)
        elif attribute["type"] == "DOUBLE":
            raw_value = float(raw_value)

        values[attribute["name"]] = raw_value

    return values


def part_name(name):
    """Convert a model name into a valid C++ identifier prefix."""
    return "m_" + re.sub(r"[^a-zA-Z0-9_]", "_", name)


def generate_distance_sensor(values):
    prefix = part_name(values["name"])

    return f"""constexpr uint8_t {prefix}_triggerPin = {values['triggerPin']};
constexpr uint8_t {prefix}_echoPin = {values['echoPin']};
constexpr unsigned long {prefix}_readIntervalMs = {values['readIntervalMs']}UL;

void {prefix}_init() {{
    pinMode({prefix}_triggerPin, OUTPUT);
    digitalWrite({prefix}_triggerPin, LOW);
    pinMode({prefix}_echoPin, INPUT);
}}

float {prefix}_readCm() {{
    digitalWrite({prefix}_triggerPin, LOW);
    delayMicroseconds(2);
    digitalWrite({prefix}_triggerPin, HIGH);
    delayMicroseconds(10);
    digitalWrite({prefix}_triggerPin, LOW);

    const unsigned long echoUs = pulseIn({prefix}_echoPin, HIGH, 25000UL);
    return echoUs * 0.01715f;
}}"""

def linked_objects(instance, objects, source, relation_id):
    return [
        objects[link["target"]]
        for link in instance["links"]
        if link["source"] == source["id"]
        and link["relationId"] == relation_id
    ]

def generate_line_tracker(values, channels):
    prefix = part_name(values["name"])
    positions = {"Left": "left", "Center": "centre", "Right": "right"}
    constants = [
        f"constexpr int {prefix}_darknessThreshold = {values['darknessThreshold']};"
    ]
    init_lines = []
    readers = []

    for channel in channels:
        side = positions[channel["position"]]
        pin_name = f"{prefix}_{side}Pin"
        constants.append(f"constexpr uint8_t {pin_name} = {channel['pin']};")
        init_lines.append(f"    pinMode({pin_name}, INPUT);")
        readers.append(
            f"bool {prefix}_{side}Dark() {{\n"
            f"    return analogRead({pin_name}) >= {prefix}_darknessThreshold;\n"
            f"}}"
        )

    return "\n".join([
        *constants,
        "",
        f"void {prefix}_init() {{",
        *init_lines,
        "}",
        "",
        *readers,
    ])

def generate_status_led(values):
    prefix = part_name(values["name"])
    colours = {
        "Red": "Red",
        "Green": "Green",
        "Blue": "Blue",
        "White": "White",
        "Off": "Black",
    }
    colour = colours[values["defaultColor"]]
    interval = values.get("blinkIntervalMs", 500)

    return f"""constexpr uint8_t {prefix}_pin = {values['pin']};
constexpr int {prefix}_ledCount = {values['ledCount']};
constexpr unsigned long {prefix}_blinkIntervalMs = {interval}UL;
CRGB {prefix}_pixels[{prefix}_ledCount];

void {prefix}_setColour(CRGB colour) {{
    fill_solid({prefix}_pixels, {prefix}_ledCount, colour);
    FastLED.show();
}}

void {prefix}_on() {{
    {prefix}_setColour(CRGB::{colour});
}}

void {prefix}_off() {{
    {prefix}_setColour(CRGB::Black);
}}

void {prefix}_init() {{
    FastLED.addLeds<WS2812, {prefix}_pin, GRB>(
        {prefix}_pixels, {prefix}_ledCount
    );
    {prefix}_off();
}}"""

def print_values(document):
    """Preview the distance sensor code for each conforming instance."""
    metamodel = document["metaModel"]
    classes = {cls["id"]: cls["name"] for cls in metamodel["classes"]}
    attributes = {
        attr["id"]: attr
        for cls in metamodel["classes"]
        for attr in cls["attributes"]
    }
    relations = {
        relation["name"]: relation["id"]
        for relation in metamodel["relations"]
        if relation["name"]
    }

    

    for instance in document["instanceModels"]:
        if instance["name"] == "Invalid Model":
            continue

        objects = {obj["id"]: obj for obj in instance["objects"]}
        robot, = (obj for obj in objects.values()
                  if classes[obj["classId"]] == "Robot")

        print(f"\n// {instance['name']}")

        controller, = linked_objects(
            instance, objects, robot, relations["controller"]
        )
        settings = read_values(controller, attributes)

        for field in ("cruiseSpeed", "turnSpeed", "defaultTurnAngle"):
            print(f"constexpr int controller_{field} = {settings[field]};")

        robot_name = read_values(robot, attributes)["name"]
        setup_lines = [
            "Serial.begin(9600);",
            f"Serial.println({json.dumps(robot_name)});",
        ]

        for part in linked_objects(instance, objects, robot, relations["containsParts"]):
            kind = classes[part["classId"]]
            values = read_values(part, attributes)

            if kind == "LineTracker":
                channels = [
                    read_values(channel, attributes)
                    for channel in linked_objects(instance, objects, part, relations["channels"])
                ]
                print(generate_line_tracker(values, channels))
                setup_lines.append(f"{part_name(values['name'])}_init();")
                continue

            if kind == "StatusLED":
                print(generate_status_led(values))
                setup_lines.append(f"{part_name(values['name'])}_init();")
                continue

            if kind != "DistanceSensor":
                continue

            prefix = part_name(values["name"])
            for threshold in linked_objects(instance, objects, part, relations["thresholds"]):
                fields = read_values(threshold, attributes)
                label = re.sub(r"[^a-zA-Z0-9_]", "_", fields["name"])
                print(f"constexpr double {prefix}_threshold_{label}Cm = {fields['distanceCm']};")

            print(generate_distance_sensor(values))
            setup_lines.append(f"{prefix}_init();")

            values = read_values(part, attributes)
            prefix = part_name(values["name"])

            for threshold in linked_objects(instance, objects, part, relations["thresholds"]):
                fields = read_values(threshold, attributes)
                label = re.sub(r"[^a-zA-Z0-9_]", "_", fields["name"])
                print(f"constexpr double {prefix}_threshold_{label}Cm = {fields['distanceCm']};")

            print(generate_distance_sensor(values))
            setup_lines.append(f"{prefix}_init();")

        print("\nvoid setup() {")
        for line in setup_lines:
            print(f"    {line}")
        print("}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("model", type=Path)
    args = parser.parse_args()

    print_values(load_json(args.model))


if __name__ == "__main__":
    main()
