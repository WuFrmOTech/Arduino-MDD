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
        self.meta = meta
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

    def class_members(self, class_id, key):
        members = {}
        while class_id:
            for item in self.classes[class_id].get(key, []):
                members.setdefault(item["name"], item)
            class_id = self.parents.get(class_id)
        return members

    def fields(self, obj):
        fields = {}
        for a in self.class_members(obj["classId"], "attributes").values():
            raw = obj["attributeValues"].get(a["id"], a.get("defaultValue"))
            if raw in (None, ""):
                if a["lowerBound"]:
                    raise ValueError(f"{obj['name']} requires {a['name']}")
                continue
            if a["type"] == "INT":
                raw = int(raw)
            elif a["type"] == "DOUBLE":
                raw = float(raw)
                if not math.isfinite(raw):
                    raise ValueError(f"{obj['name']}.{a['name']} must be finite")
            elif a["type"] == "BOOLEAN":
                text = str(raw).lower()
                if text not in ("true", "false"):
                    raise ValueError(f"{obj['name']}.{a['name']} requires true or false")
                raw = text == "true"
            fields[a["name"]] = raw
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
        elif kind == "Button":
            values["buttonPin"] = values.pop("pin")
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
        read = self.targets(controller, "reads")
        linked_parts = [(part, self.class_members(part["classId"], "ports"), self.part(part))
                        for part in parts]
        parts = [part for _, _, part in linked_parts]
        pins = [v for p in parts for k,v in p.items() if k.endswith("Pin")]
        if len(set(pins)) != len(pins):
            raise ValueError("Hardware pin assignments must be unique within a robot")
        scenario = re.sub(r"[^a-zA-Z0-9_]", "", self.model["name"])
        fields = self.fields(controller)
        robot = {"scenario": scenario, "name": self.fields(root)["name"],
                 "controller": fields, "parts": parts}
        behaviour = self.meta.get("behaviours", {}).get(controller["classId"])
        if behaviour and behaviour["states"]:
            ports = self.class_members(controller["classId"], "ports")
            bindings = {}
            for name, port in ports.items():
                if port["protocolId"] in ("sys-timing", "sys-log"):
                    continue
                matches = [data for part, candidates, data in linked_parts
                           if part in read + driven
                           for candidate in candidates.values()
                           if candidate["protocolId"] == port["protocolId"]
                           and candidate["conjugated"] != port["conjugated"]]
                if len(matches) > 1:
                    raise ValueError(f"Port {name} matches more than one linked part")
                if matches:
                    bindings[name] = matches[0]
            robot["behaviour"] = behaviour
            robot["binding"] = bindings
            robot["ports"] = ports
            robot["protocols"] = self.meta.get("protocols", [])
            robot["controller_name"] = controller["name"]
        return robot


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
