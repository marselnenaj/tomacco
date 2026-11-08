"""Private native-runtime session against a loopback WebDriverAgent double."""
import json
from pathlib import Path
import sys

from agent_fixture import Isolated, ROOT

CONTEXT = {"/wda/activeAppInfo", "/elements", "/element/active"}


class ObserveDouble(Isolated):
    def __init__(self):
        super().__init__("iphone-observe-")
        self.root = Path(self.temp.name)
        self.env["TOMACCO_TEST_ROOT"] = str(self.root)
        self.plan()
        proxy = self.traps / "iproxy"
        proxy.write_text("#!" + sys.executable + "\n" + (ROOT / "tests/fixtures/iphone_observe.py").read_text())
        proxy.chmod(0o755)
        # This optional identity lookup must also stay inside the double.
        info = self.traps / "ideviceinfo"
        info.write_text("#!/bin/sh\nexit 0\n")
        self.result("settings.configure", {"executables": {"iproxy": str(proxy), "ideviceinfo": str(info)}})
        self.session = self.result("runtime.start", {"platform": "ios", "device": "00008110-0011223344556677",
                                                    "runner_mode": "external"})["session"]

    def plan(self, **plan):
        target = self.root / "plan.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(plan))
        temporary.replace(target)

    def observe(self, **args):
        return self.result("runtime.observe", {"session": self.session, **args})

    def events(self):
        return [json.loads(line) for line in (self.root / "wda-events.jsonl").read_text().splitlines()]

    def starts(self):
        return [e for e in self.events() if e["event"] == "start"]

    def assert_safe(self):
        assert self.trapped() == "", self.trapped()
