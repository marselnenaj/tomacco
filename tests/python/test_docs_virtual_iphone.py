"""The user-facing documents describe what landed for the virtual iPhone:
developer app deployment and its limits and the guest operations the schema
offers. Each check names the sentence a document must carry, so a
later edit that drops it fails here; every check failed before the sentence
was written. The `tomaccoctl call` examples of the README and every guide are held to the
argument validation tomaccoctl applies before every dispatch, so a documented
command the control plane refuses fails here with tomaccoctl's own message. Operations,
their arguments, tools and tool flags a guide names in prose must exist as well."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def raw(path):
    return (ROOT / path).read_text(encoding="utf-8")


def text(path):
    # One line of text, so a sentence wrapped in the file reads the same.
    return " ".join(raw(path).split())


def section(path, heading):
    """The raw text from the given heading to the next heading of the same or a higher level."""
    document = raw(path)
    start = document.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    following = re.compile(r"^#{1,%d} " % level, re.M)
    match = following.search(document, start + len(heading))
    return document[start:match.start() if match else len(document)]


def operation_entries():
    operations = json.loads(raw("assets/operations.json"))
    return operations.get("operations", operations) if isinstance(operations, dict) else operations


def operation_schema(name):
    """The input schema of the named operation in assets/operations.json, or None."""
    return next((entry["inputSchema"] for entry in operation_entries() if entry["name"] == name), None)


# Every document a person is told to follow.
GUIDES = ("README.md", "docs/README.md", "docs/getting-started.md", "docs/desktop.md", "docs/agents.md",
          "docs/android.md", "docs/iphone.md", "docs/troubleshooting.md", "docs/architecture.md", "docs/building.md",
          "docs/research-engines.md",
          "docs/virtual-ios/README.md", "docs/virtual-ios/setup.md", "docs/virtual-ios/usage.md",
          "docs/virtual-ios/troubleshooting.md", "docs/virtual-ios/how-it-works.md", "examples/README.md",
          "docs/recipes.md")
# A dotted name in a guide that looks like an operation and is a result's path instead.
RESULT_PATHS = {"workspace.ios_research.guest.detach_pending"}
OPERATION_AREAS = {entry["name"].split(".")[0] for entry in operation_entries() if "." in entry["name"]}


def call_examples(path):
    """(line number, operation, JSON arguments or None) of each `tomaccoctl call` line of a guide."""
    found = []
    for number, line in enumerate(raw(path).splitlines(), 1):
        match = re.match(r"^\s*tomaccoctl call (\S+)(?: '(\{.*\})'| @\S+)?\s*$", line)
        if match:
            found.append((number, match.group(1), match.group(2)))
    return found


def command_lines(path):
    """The lines of a guide's code blocks with continuation lines joined."""
    lines, inside, pending = [], False, ""
    for line in raw(path).splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
        elif inside:
            pending += line.strip()
            if pending.endswith("\\"):
                pending = pending[:-1] + " "
            else:
                lines.append(pending)
                pending = ""
    return lines


JOB_ID = "0123456789abcdef0123456789abcdef"
# The guide's placeholders, replaced by a value of the shape an agent passes.
PLACEHOLDERS = {"JOB_ID": JOB_ID, "BUILD_JOB": JOB_ID, "START_JOB": JOB_ID, "GUEST_JOB": JOB_ID, "SESSION_ID": JOB_ID}


def example(value):
    if isinstance(value, dict):
        return {key: example(item) for key, item in value.items()}
    if isinstance(value, list):
        return [example(item) for item in value]
    return PLACEHOLDERS.get(value, value) if isinstance(value, str) else value


def refusal(value, schema, name="arguments"):
    """The message tomaccoctl answers for arguments its validator refuses (native/core.rs
    `validate`, run against the operation's inputSchema before every dispatch), or None
    when the call is dispatched: type, enum, required keys, unknown keys, item counts,
    string lengths and patterns, numeric bounds."""
    kind = schema.get("type", "")
    valid = {"object": lambda item: isinstance(item, dict),
             "array": lambda item: isinstance(item, list),
             "string": lambda item: isinstance(item, str),
             "boolean": lambda item: isinstance(item, bool),
             "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
             "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
             "": lambda item: True}.get(kind, lambda item: False)(value)
    if not valid:
        return f"{name} must be {kind}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{name} must be one of {json.dumps(schema['enum'])}"
    if kind == "object":
        for key in schema.get("required", []):
            if key not in value:
                return f"{name}.{key} is required"
        for key, item in value.items():
            if key in schema.get("properties", {}):
                problem = refusal(item, schema["properties"][key], f"{name}.{key}")
                if problem:
                    return problem
            elif schema.get("additionalProperties") is False:
                return f"Unknown argument: {name}.{key}"
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10000):
            return f"{name} has an invalid item count"
        for index, item in enumerate(value):
            problem = refusal(item, schema.get("items", {}), f"{name}[{index}]")
            if problem:
                return problem
    elif kind == "string":
        if "\0" in value or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 100000):
            return f"Invalid string length/content: {name}"
        if "pattern" in schema and not re.fullmatch("(?:%s)" % schema["pattern"], value):
            return f"Invalid {name}: {value!r}"
    elif kind in ("integer", "number"):
        if not schema.get("minimum", float("-inf")) <= value <= schema.get("maximum", float("inf")):
            return f"{name} is out of range"
    return None


class DocumentTest(unittest.TestCase):
    longMessage = False  # the documents are long; the message names the missing sentence

    def assert_says(self, path, document, *sentences):
        for sentence in sentences:
            self.assertTrue(sentence in document, f"{path} does not say: {sentence}")

    def assert_no_longer_says(self, path, document, *sentences):
        for sentence in sentences:
            self.assertFalse(sentence in document, f"{path} still says: {sentence}")


class ReadmeTests(DocumentTest):
    def test_readme_states_the_limits(self):
        self.assert_says("README.md", text("README.md"),
                         "Physical iPhone execution is not qualified.",
                         "about 7.5-11 changed frames per second",
                         "outbound-only NAT networking behind passt",
                         "installing an app on the running phone without a reboot",
                         "Widget extensions miss chronod's deadline",
                         "Pointer authentication is bypassed",
                         "No Apple firmware, iOS image or Apple binary is part of this repository or its packages",
                         "It needs your own restore image",
                         "Image4 boot manifest",
                         "prepared on Linux with the included helper and a public research template",
                         "It is not production-ready.")

    def test_readme_keeps_its_first_commands(self):
        self.assertTrue(len(call_examples("README.md")) >= 6, "README.md lost its tomaccoctl examples")


class VirtualDeviceGuideTests(DocumentTest):
    USAGE = "docs/virtual-ios/usage.md"
    LIMITS = "docs/virtual-ios/README.md"
    SETUP = "docs/virtual-ios/setup.md"

    def test_usage_describes_deploying_an_own_app(self):
        self.assert_says(self.USAGE, text(self.USAGE),
                         "`ios.virtual.configure {add_application}`",
                         "`ios.emulator.build_guest_tools` with `applications`",
                         "phase `installing`",
                         "pins the new signing adapter in a derived kernel",
                         "The rebuild needs clang 22: name it with `ios.virtual.configure {clang}`")

    def test_usage_states_the_bundle_limits(self):
        # Frameworks and libraries ran on the phone; app extensions are accepted by
        # the bundle check but were verified offline only; everything else stays refused.
        self.assert_says(self.USAGE, text(self.USAGE),
                         "Embedded frameworks and libraries (`Frameworks/*.framework`, `Frameworks/*.dylib`)",
                         "App extensions in `PlugIns/` are accepted and signed",
                         "Not supported: ExtensionKit extensions (`Extensions/`), XPC services and other nested bundles, and bundles over 128 MiB.")

    def test_usage_states_session_and_memory_limits(self):
        self.assert_says(self.USAGE, text(self.USAGE),
                         "A session lasts at most 24 hours",
                         "under an eighth of the graphics card stays free",
                         "`guest_gpu_memory_stop: true`",
                         "The guard's trip has unit tests only.")

    def test_the_overview_states_what_it_cannot_do(self):
        self.assert_says(self.LIMITS, text(self.LIMITS),
                         "**Status: experimental.**",
                         "A boot with the `guest_network` setting",
                         "**Audio.**", "**Debugging.**", "**Hardware keyboard.**",
                         "`ios.virtual.deploy` puts a built `.app` on the",
                         "SwiftUI works, with the SDK from your own Xcode download",
                         "Pointer authentication is bypassed",
                         "XR multisampling is open",
                         "Widget extensions miss chronod's deadline",
                         "tools/prepare_ios_research_manifest.py",
                         "about 6.4 to 7.7 s", "about 0.1 s", "about 7.5 to 11 changed frames per second", "at most 1.7 GiB",
                         "i9-13900K", "Radeon RX 6800-class 16 GiB",
                         "six application processors plus a SEP", "8 GiB of guest RAM", "16 KiB translated pages")

    def test_setup_explains_how_to_prepare_the_research_manifest(self):
        self.assert_says(self.SETUP, text(self.SETUP),
                         "python3 tools/prepare_ios_research_manifest.py",
                         "--template /phone/ticket.shsh2",
                         "--device-tree /phone/dtree.restore",
                         "exactly the same 9,206 bytes",
                         "invalid inherited Apple signature",
                         "Do not regenerate the manifest for an already restored VM",
                         "This chain has not been run as one sequence on a truly fresh machine.")

    def test_the_setup_chain_has_its_steps_in_order(self):
        chain = section(self.SETUP, "## The chain")
        position = 0
        for token in ("build_ios_paravirt.py", "prepare_ios_emulator.py", "ios.emulator.build_gpu_plugin", "img4 im4p extract",
                      "prepare_ios_paravirt.py", "ios_paravirt_storage.py", "ios.emulator.prepare_userland",
                      "ios.emulator.prepare_restore", "ios.emulator.paravirtual", "ios.emulator.usb_bridge",
                      "ios.emulator.restore", "ios.emulator.prepare_cryptexes", "ios.emulator.build_guest_tools",
                      "ios.emulator.prepare_cryptexes", "prepare_ios_fileport_compat.py", "prepare_ios_workloop_compat.py",
                      "ios.emulator.prepare_guest_tools", "ios_virtual_profile.py", "ios.virtual.configure", "ios.virtual.start"):
            found = chain.find(token, position)
            self.assertTrue(found != -1, f"{self.SETUP}: {token} does not follow the previous step")
            position = found + len(token)


class IphoneGuideTests(DocumentTest):
    def test_the_guide_is_honest_about_real_phones(self):
        guide = text("docs/iphone.md")
        self.assert_says("docs/iphone.md", guide,
                         "a physical iPhone has not been qualified",
                         "omarchy-apple-dev", "omarchy-ios-kit",
                         "Tomacco ships no code from either", "Untested in Tomacco",
                         "## The Mac route".replace("## ", ""))


class EveryGuideTests(DocumentTest):
    def test_every_tomaccoctl_example_passes_argument_validation(self):
        """tomaccoctl validates a call's arguments against the operation's schema before it
        dispatches, so an example it refuses is wrong as written: the deployment sequence
        once waited with `seconds` 600 where `job.wait` allows at most 60."""
        for path in GUIDES:
            for number, name, arguments in call_examples(path):
                schema = operation_schema(name)
                self.assertTrue(schema is not None, f"{path}:{number} calls an operation the schema lacks: {name}")
                if arguments is not None:
                    problem = refusal(example(json.loads(arguments)), schema)
                    self.assertTrue(problem is None, f"{path}:{number}: tomaccoctl refuses `{name} {arguments}`: {problem}")

    def test_every_call_line_is_recognised(self):
        # A line the validation above cannot parse would otherwise escape it.
        for path in GUIDES:
            for line in command_lines(path):
                if line.startswith("tomaccoctl call "):
                    self.assertTrue(re.match(r"^tomaccoctl call (\S+)(?: '(\{.*\})'| @\S+)?$", line),
                                    f"{path}: not a checkable call: {line}")

    def test_every_operation_a_guide_names_exists(self):
        known = {entry["name"] for entry in operation_entries()}
        for path in GUIDES:
            for token in re.findall(r"`([a-z_]+(?:\.[a-z0-9_]+)+)`", raw(path)):
                if token.split(".")[0] in OPERATION_AREAS and token not in RESULT_PATHS and not token.endswith((".json", ".md", ".py")):
                    self.assertTrue(token in known, f"{path} names an operation the schema lacks: {token}")

    def test_every_argument_a_guide_names_exists(self):
        for path in GUIDES:
            for name, arguments in re.findall(r"`([a-z_]+(?:\.[a-z0-9_]+)+) \{([^}]*)\}`", raw(path)):
                schema = operation_schema(name)
                self.assertTrue(schema is not None, f"{path} names an operation the schema lacks: {name}")
                for argument in re.split(r",\s*", arguments):
                    self.assertTrue(argument in schema["properties"], f"{path}: {name} has no argument {argument}")

    def test_every_guest_action_and_operation_a_guide_names_exists(self):
        guest = set(operation_schema("ios.emulator.guest")["properties"]["operation"]["enum"])
        for path in GUIDES:
            for operation in re.findall(r"`ios\.emulator\.guest` `([a-z_]+)`", raw(path)):
                self.assertTrue(operation in guest, f"{path}: no guest operation {operation}")

    def test_every_tool_and_flag_a_guide_shows_exists(self):
        for path in GUIDES:
            document = raw(path)
            for tool in set(re.findall(r"\b(?:tools|tests)/[A-Za-z0-9_/]+\.py\b", document)):
                self.assertTrue((ROOT / tool).is_file(), f"{path} names {tool}, which does not exist")
            for line in command_lines(path):
                match = re.match(r"^(?:\S+=\S+ )*python3 (tools/\S+\.py|install\.py)\b(.*)$", line)
                if match:
                    source = raw(match.group(1))
                    for flag in re.findall(r"(?<!\S)(--[a-z0-9-]+)", match.group(2)):
                        self.assertTrue(f'"{flag}"' in source, f"{path}: {match.group(1)} has no flag {flag}")
                match = re.match(r"^tomacco\b(.*)$", line)
                if match:
                    source = raw("src/main.cpp")
                    for flag in re.findall(r"(?<!\S)--([a-z0-9-]+)", match.group(1)):
                        self.assertTrue(f'{{"{flag}"' in source, f"{path}: tomacco has no flag --{flag}")


class AgentGuideTests(DocumentTest):
    GUIDE = "docs/agents.md"
    HEADING = "### Deploying an app to the virtual iOS device"

    def test_guide_has_the_deployment_sequence_in_order(self):
        self.assert_says(self.GUIDE, raw(self.GUIDE), self.HEADING)
        deploying = section(self.GUIDE, self.HEADING)
        position = 0
        for token in ("ios.virtual.configure", "add_application", "ios.emulator.build_guest_tools",
                      "ios.virtual.start", "`installing`", "ios.virtual.status", "logs development"):
            found = deploying.find(token, position)
            self.assertTrue(found != -1, f"{self.GUIDE}: {token} does not follow the previous step")
            position = found + len(token)

    def test_guide_keeps_its_examples(self):
        guide = raw(self.GUIDE)
        self.assert_says(self.GUIDE, guide, self.HEADING)
        deploying = section(self.GUIDE, self.HEADING)
        self.assertTrue(deploying.count("\ntomaccoctl call ") >= 9,
                        f"{self.GUIDE}: the deployment sequence lost its commands")
        self.assertTrue(len(call_examples(self.GUIDE)) >= 30, f"{self.GUIDE} lost its tomaccoctl examples")

    def test_guide_names_every_guest_operation_the_schema_offers(self):
        guest = operation_schema("ios.emulator.guest")
        guide = raw(self.GUIDE)
        for operation in guest["properties"]["operation"]["enum"]:
            self.assertTrue(f"`{operation}`" in guide,
                            f"{self.GUIDE} does not list the guest operation {operation}")

    def test_guide_describes_the_diagnostic_guest_arguments(self):
        # One new operation (time_zone) and two new arguments of older ones (crash_reports, logs).
        self.assert_says(self.GUIDE, text(self.GUIDE),
                         "`crash_reports` takes any report-name prefix",
                         "`logs quiet` reads tomacco-quiet's record",
                         "`time_zone Area/City` sets the guest's zone",
                         "`guard-ended`")

    def test_guide_describes_the_running_panel(self):
        self.assert_says(self.GUIDE, text(self.GUIDE),
                         "the current run's `running_panel`")


if __name__ == "__main__":
    unittest.main()
