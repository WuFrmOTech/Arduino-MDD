"""Compile IML controller states, transitions, guards and actions into Arduino C++."""
import json
import re

IDENT = r"[A-Za-z_]\w*"
TOKEN = re.compile(r'"(?:[^"\\\n]|\\.)*"|[0-9]+|[A-Za-z_]\w*|==|!=|<=|>=|&&|\|\||[^\s]', re.ASCII)
PRECEDENCE = {operator: level for level, operators in enumerate(
    (("||",), ("&&",), ("==", "!="), ("<", "<=", ">", ">="), ("+", "-"), ("*", "/", "%")), 1
) for operator in operators}
CPP_TYPES = {"INT": "int", "BOOLEAN": "bool"}
MOTOR_COMMANDS = {"stop": 0, "forward": 1, "backward": 1, "turnLeft": 1, "turnRight": 1}
LED_COMMANDS = {"off": "_off", "red": "_showRed", "green": "_showGreen", "yellow": "_showYellow"}
READERS = {"DistanceSensor": ("_readCmInt",), "LineTracker": ("_leftRaw", "_centreRaw", "_rightRaw"),
           "Button": ()}
SYSTEM = {
    "sys-timing": (("informIn", "out", ("INT",)), ("cancelTimer", "out", ()), ("timeout", "in", ())),
    "sys-log": (("log", "out", ("STRING",)),),
}


class Expression:
    """Read one expression using the precedence of the supported C++ operators."""

    def __init__(self, tokens, names):
        self.tokens = tokens
        self.names = names
        self.index = 0

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else ""

    def take(self):
        value = self.peek()
        self.index += 1
        return value

    def expect(self, token):
        if self.take() != token:
            raise ValueError(f"expected {token!r}")

    def parse(self, minimum=1):
        token = self.take()
        if token in ("!", "-", "+"):
            left = f"({token}{self.parse(7)})"
        elif token == "(":
            left = self.parse()
            self.expect(")")
        elif token.isdecimal():
            left = str(int(token))
            if int(left) > 2147483647:
                raise ValueError("integer literal is out of range")
        elif token in ("true", "false"):
            left = token
        elif token in self.names:
            left = self.names[token]
        else:
            raise ValueError(f"invalid operand {token!r}")

        while (precedence := PRECEDENCE.get(self.peek(), 0)) >= minimum:
            operator = self.take()
            right = self.parse(precedence + 1)
            left = f"({left} {operator} {right})"
        return left


def checked_name(name):
    if not re.fullmatch(IDENT, name, re.ASCII) or name in ("true", "false"):
        raise ValueError(f"invalid model identifier {name!r}")
    return name


class Behaviour:
    def __init__(self, robot, identifier, string):
        self.robot = robot
        self.identifier = identifier
        self.string = string
        self.attributes = {checked_name(key): "attr_" + key
                           for key, value in robot["controller"].items() if type(value) in (int, bool)}
        self.bindings = robot["binding"]
        self.ports = robot["ports"]
        self.signals = {}
        self.events = {}
        self.timer_port = None
        protocols = {p["id"]: p["signals"] for p in robot["protocols"]}
        for index, (port_name, port) in enumerate(self.ports.items()):
            checked_name(port_name)
            protocol = port["protocolId"]
            if protocol == "sys-timing":
                if self.timer_port is not None:
                    raise ValueError("only one Timing port is supported")
                self.timer_port = port_name
            if protocol in SYSTEM:
                definitions = [{"name": name, "direction": direction, "params": [
                    {"name": f"arg{i}", "type": kind} for i, kind in enumerate(types)]}
                    for name, direction, types in SYSTEM[protocol]]
            else:
                definitions = protocols[protocol]
            for signal in definitions:
                name = checked_name(signal["name"])
                endpoint = f"{port_name}.{name}"
                if endpoint in self.signals:
                    raise ValueError(f"duplicate signal {endpoint}")
                params = [(checked_name(p["name"]), p["type"]) for p in signal["params"]]
                if len({name for name, _ in params}) != len(params):
                    raise ValueError(f"duplicate parameter on {endpoint}")
                incoming = (signal["direction"] == "in") != port["conjugated"]
                self.signals[endpoint] = (incoming, params)
                if incoming:
                    self.events[endpoint] = f"event_{index}_{name}"

    def guard(self, text, names):
        if not text.strip():
            return "true"
        try:
            expression = Expression(TOKEN.findall(text), names)
            code = expression.parse()
            expression.expect("")
            return code
        except ValueError as error:
            raise ValueError(f"unsupported guard {text!r}: {error}") from error

    def actions(self, text, names):
        if not text.strip():
            return []
        code = []
        current = []
        for token in TOKEN.findall(text):
            if token != ";":
                current.append(token)
                continue
            if not current:
                continue
            try:
                code.append(self.action(current, names))
            except (KeyError, ValueError, TypeError) as error:
                raise ValueError(f"unsupported action in {text!r}: {' '.join(current)}: {error}") from error
            current = []
        if current:
            raise ValueError(f"unsupported action in {text!r}: missing semicolon")
        return code

    def action(self, tokens, names):
        expression = Expression(tokens, names)
        first = expression.take()
        if expression.peek() == "=":
            expression.take()
            target = self.attributes[first]
            value = expression.parse()
            expression.expect("")
            return f"{target} = {value};"

        expression.expect(".")
        signal = expression.take()
        endpoint = f"{first}.{signal}"
        incoming, params = self.signals[endpoint]
        if incoming:
            raise ValueError(f"{endpoint} is a received signal")
        expression.expect("(")
        args = []
        for i, (_, kind) in enumerate(params):
            if i:
                expression.expect(",")
            if kind == "STRING":
                value = json.loads(expression.take())
                if not isinstance(value, str):
                    raise ValueError("expected a quoted string")
                args.append(self.string(value))
            else:
                args.append(expression.parse())
        expression.expect(")")
        expression.expect("")
        protocol = self.ports[first]["protocolId"]
        if protocol == "sys-timing":
            if signal == "informIn":
                return f"startTimer({args[0]});"
            return "timerActive = false;"
        if protocol == "sys-log":
            return f"Serial.print(F({self.string('LOG ' + self.robot['controller_name'] + ': ')})); Serial.println(F({args[0]}));"
        part = self.bindings[first]
        name = self.identifier(part["name"])
        if (part["type"] == "MotorDriver" and MOTOR_COMMANDS.get(signal) == len(args)
                and all(kind == "INT" for _, kind in params)):
            argument = f"clampMotorSpeed({args[0]})" if args else ""
            return f"{name}_{signal}({argument});"
        if part["type"] == "StatusLED" and signal in LED_COMMANDS and not args:
            return f"{name}{LED_COMMANDS[signal]}();"
        raise ValueError(f"no hardware command for {endpoint}")

    def render(self):
        model = self.robot["behaviour"]
        states = {s["id"]: s for s in model["states"] if s["kind"] == "simple"}
        initial, = [s["id"] for s in model["states"] if s["kind"] == "initial"]
        if len(states) + 1 != len(model["states"]):
            raise ValueError("only an initial pseudostate and simple states are supported")
        keys = {key: "State_" + checked_name(state["name"]) for key, state in states.items()}
        if len(set(keys.values())) != len(keys) or "State_init" in keys.values():
            raise ValueError("duplicate or reserved state name")
        start, = [t for t in model["transitions"] if t["source"] == initial]
        if start["trigger"] or start["guard"]:
            raise ValueError("the initial transition must be unconditional")
        transitions = [t for t in model["transitions"] if t["source"] != initial]
        for t in transitions:
            if t["source"] not in states or t["target"] not in states or t["trigger"] not in self.events:
                raise ValueError(f"invalid transition {t}")

        blocks = ["enum ControllerState { State_init, " + ", ".join(keys.values()) + " };",
                  "ControllerState state = State_init;"]
        for name, cpp in self.attributes.items():
            value = self.robot["controller"][name]
            cpp_type, literal = ("bool", str(value).lower()) if type(value) is bool else ("int", str(value))
            blocks.append(f"{cpp_type} {cpp} = {literal};")
        if self.timer_port is not None:
            blocks.append("""bool timerActive = false;
unsigned long timerStartedAt = 0;
unsigned long timerDuration = 0;

void startTimer(long duration) {
  timerStartedAt = millis();
  timerDuration = duration < 0 ? 0UL : static_cast<unsigned long>(duration);
  timerActive = true;
}""")
        if any(part["type"] == "MotorDriver" for part in self.bindings.values()):
            blocks.append("""uint8_t clampMotorSpeed(long speed) {
  if (speed < 0) return 0;
  if (speed > 255) return 255;
  return static_cast<uint8_t>(speed);
}""")

        for state_id, state in states.items():
            for action in ("entry", "exit"):
                blocks.append(make_function(f"{action}_{keys[state_id]}",
                                            self.actions(state[action], self.attributes)))
        button_setup = []
        for index, part in enumerate(self.bindings.values()):
            if part["type"] == "Button":
                blocks.append(f"bool buttonWasPressed_{index} = false;")
                name = self.identifier(part["name"])
                button_setup.append(f"buttonWasPressed_{index} = {name}_isPressed();")
        start_code = button_setup + self.actions(start["effect"], self.attributes)
        start_code += self.enter_state("init", states[start["target"]]["name"], keys[start["target"]])
        blocks.append(make_function("startController", start_code))

        for endpoint, function in self.events.items():
            if not any(t["trigger"] == endpoint for t in transitions) and not self.delivered(endpoint):
                continue
            params = self.signals[endpoint][1]
            scope = self.attributes | {name: "param_" + name for name, _ in params}
            signature = ", ".join(f"{CPP_TYPES[kind]} param_{name}" for name, kind in params)
            # A protocol can carry parameters that this particular controller never reads.
            lines = [f"(void)param_{name};" for name, _ in params]
            for t in transitions:
                if t["trigger"] != endpoint:
                    continue
                source, target = t["source"], t["target"]
                guard = self.guard(t["guard"], scope)
                lines += [f"if (state == {keys[source]} && ({guard})) {{", f"  exit_{keys[source]}();"]
                lines += ["  " + line for line in self.actions(t["effect"], scope)]
                lines += ["  " + line for line in self.enter_state(
                    states[source]["name"], states[target]["name"], keys[target])]
                lines += ["  return;", "}"]
            blocks.append(make_function(function, lines, signature))
        return "\n\n".join(blocks), self.loop()

    def enter_state(self, old, new, target):
        code = [f"state = {target};"]
        if old != new:
            log = f"STATE {self.robot['controller_name']} {old} -> {new}"
            code.append(f"Serial.println(F({self.string(log)}));")
        return [*code, f"entry_{target}();"]

    def delivered(self, endpoint):
        port, signal = endpoint.split(".")
        return port in self.bindings or (port == self.timer_port and signal == "timeout")

    def loop(self):
        lines = ["const unsigned long now = millis();"]
        if self.timer_port is not None:
            timer_event = self.events[f"{self.timer_port}.timeout"]
            lines += ["if (timerActive && now - timerStartedAt >= timerDuration) {",
                      "  timerActive = false;", f"  {timer_event}();", "}"]
        for index, (port, part) in enumerate(self.bindings.items()):
            readers = READERS.get(part["type"])
            if readers is None:
                continue
            event, = [event for event in self.events if event.split(".")[0] == port]
            if [kind for _, kind in self.signals[event][1]] != ["INT"] * len(readers):
                raise ValueError(f"{event} needs {len(readers)} INT parameters")
            if not part.get("readIntervalMs"):
                raise ValueError(f"{part['name']} requires a positive readIntervalMs")
            if part["type"] == "LineTracker" and any(side not in part for side in (
                    "leftPin", "centrePin", "rightPin")):
                raise ValueError("line readings require all three line channels")
            function = self.events[event]
            name = self.identifier(part["name"])
            last_poll = f"sensorLastAt_{index}"
            lines += [f"static unsigned long {last_poll} = 0;",
                      f"if (now - {last_poll} >= {name}_readIntervalMs) {{",
                      f"  {last_poll} = now;"]
            if part["type"] == "Button":
                previous = f"buttonWasPressed_{index}"
                lines += [f"  const bool pressed = {name}_isPressed();",
                          f"  if (pressed && !{previous}) {function}();", f"  {previous} = pressed;"]
            else:
                # Read each input in protocol order before delivering the event.
                lines += [f"  const int reading{i} = {name}{suffix}();" for i, suffix in enumerate(readers)]
                arguments = ", ".join(f"reading{i}" for i in range(len(readers)))
                lines.append(f"  {function}({arguments});")
            lines.append("}")
        return lines


def make_function(name, lines, params=""):
    return "\n".join([f"void {name}({params}) {{", *("  " + line for line in lines), "}"])
