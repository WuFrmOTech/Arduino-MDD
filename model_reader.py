"""Resolve class, attribute and relationship IDs in the modeling tool export."""
import json
import math
import re

COLOURS = {
    "Red": (255, 0, 0), "Green": (0, 255, 0), "Blue": (0, 0, 255),
    "White": (255, 255, 255), "Off": (0, 0, 0),
}


class Instance:
    def __init__(self, model, meta):
        self.model = model
        self.classes = {c["id"]: c for c in meta["classes"]}
        self.attributes = {a["id"]: a for c in meta["classes"] for a in c["attributes"]}
        self.relations = {r["id"]: r for r in meta["relations"]}
        self.objects = {o["id"]: o for o in model["objects"]}
        self.parents = {r["source"]: r["target"] for r in meta["relations"]
                        if r["kind"] == "INHERITANCE"}

    def kind(self, obj):
        return self.classes[obj["classId"]]["name"]

    def matches(self, obj, class_id):
        current = obj["classId"]
        while current:
            if current == class_id:
                return True
            current = self.parents.get(current)
        return False

    def fields(self, obj):
        fields = {}
        current = obj["classId"]
        while current:
            for a in self.classes[current]["attributes"]:
                raw = obj["attributeValues"].get(a["id"], a.get("defaultValue"))
                if raw in (None, ""):
                    if a["lowerBound"]:
                        raise ValueError(f"{obj['name']} requires {a['name']}")
                    continue
                if a["type"] == "INT":
                    raw = int(raw)
                if a["type"] == "DOUBLE":
                    raw = float(raw)
                    if not math.isfinite(raw):
                        raise ValueError(f"{obj['name']}.{a['name']} must be finite")
                fields[a["name"]] = raw
            current = self.parents.get(current)
        return fields

    def targets(self, obj, relation):
        return [self.objects[l["target"]] for l in self.model["links"]
                if l["source"] == obj["id"] and self.relations[l["relationId"]]["name"] == relation]

    def validate_links(self):
        for link in self.model["links"]:
            relation = self.relations[link["relationId"]]
            for end in ("source", "target"):
                if not self.matches(self.objects[link[end]], relation[end]):
                    raise ValueError(f"Incorrect {end} type for {relation['name']}")

    def part(self, obj):
        kind = self.kind(obj)
        values = self.fields(obj)
        if kind == "DistanceSensor":
            values["mount"] = values.pop("mountPosition")
            thresholds = [self.fields(t) for t in self.targets(obj, "thresholds")]
            distances = {}
            for threshold in thresholds:
                label = threshold["name"]
                key = label.casefold()
                if key in distances or threshold["distanceCm"] <= 0:
                    raise ValueError("Thresholds need unique names and positive distances")
                distances[key] = threshold["distanceCm"]
            stop = distances.get("stop", distances.get("danger"))
            if stop is not None and "warning" in distances and stop >= distances["warning"]:
                raise ValueError("The stop distance must be below the warning distance")
            values["thresholds"] = thresholds
        elif kind == "MotorDriver":
            motors = [self.fields(m) for m in self.targets(obj, "motors")]
            if sorted(m["side"] for m in motors) != ["Left", "Right"]:
                raise ValueError("A motor driver needs one Left motor and one Right motor")
            for motor in motors:
                side = motor["side"].lower()
                values[side + "PwmPin"] = motor["powerPin"]
                values[side + "DirectionPin"] = motor["directionPin"]
        elif kind == "LineTracker":
            for channel in self.targets(obj, "channels"):
                attrs = self.fields(channel)
                side = {"Left":"left", "Center":"centre", "Right":"right"}[attrs["position"]]
                key = side + "Pin"
                if key in values:
                    raise ValueError("Line-channel positions must be unique")
                values[key] = attrs["pin"]
            if not self.targets(obj, "channels"):
                raise ValueError("A line tracker needs at least one channel")
        elif kind == "StatusLED":
            values["dataPin"] = values.pop("pin")
            values["count"] = values.pop("ledCount")
            values["red"], values["green"], values["blue"] = COLOURS[values.pop("defaultColor")]
        else:
            raise ValueError(f"Unsupported part class {kind}")
        return {"type": kind, **values}

    def robot(self):
        self.validate_links()
        root, = [o for o in self.objects.values() if self.kind(o) == "Robot"]
        controller, = self.targets(root, "controller")
        parts = self.targets(root, "containsParts")
        owned = {o["id"] for o in parts}
        driven = self.targets(controller, "drives")
        if not driven:
            raise ValueError("The controller needs at least one outgoing drives link")
        if any(o["id"] not in owned for o in driven + self.targets(controller, "reads")):
            raise ValueError("The controller must link to parts belonging to its robot")
        parts = [self.part(o) for o in parts]
        pins = [v for p in parts for k,v in p.items() if k.endswith("Pin")]
        if len(set(pins)) != len(pins):
            raise ValueError("Hardware pin assignments must be unique within a robot")
        scenario = re.sub(r"[^a-zA-Z0-9_]", "", self.model["name"])
        return {"scenario": scenario, "name": self.fields(root)["name"],
                "controller": self.fields(controller), "parts": parts}


def read_models(path):
    document = json.loads(path.read_text(encoding="utf-8"))
    robots = []
    for model in document["instanceModels"]:
        if model["name"] == "Invalid Model":
            continue  # The supplied negative example is deliberately excluded.
        try:
            robots.append(Instance(model, document["metaModel"]).robot())
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"{model['name']}: {exc}") from exc
    return robots
