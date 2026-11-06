"""The phone parity matrix covers every operation and stays in step with assets/operations.json."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import build_mobile_parity as parity  # noqa: E402


def test_every_operation_is_classified():
    data = parity.build()
    names = [op["name"] for op in json.loads((ROOT / "assets" / "operations.json").read_text(encoding="utf-8"))]
    assert [row["operation"] for row in data["operations"]] == names
    assert all(row["phone"] in parity.STATUSES for row in data["operations"])
    assert sum(data["counts"].values()) == len(names)


def test_remote_and_conditional_rows_name_a_requirement_and_a_test():
    for row in parity.build()["operations"]:
        assert row["next_test"], row["operation"]
        assert row["evidence"], row["operation"]
        if row["phone"] in ("remote", "conditional"):
            assert row["requires"], row["operation"]
        assert row["verified_on_pixel"] is False, "nothing is verified on the Pixel until the device block is lifted"


def test_known_verdicts():
    verdict = {row["operation"]: row["phone"] for row in parity.build()["operations"]}
    assert verdict["android.avd_start"] == "remote"
    assert verdict["android.emulator"] == "remote"
    assert verdict["android.avd_create"] == "local"
    assert verdict["ios.virtual.start"] == "remote"
    assert verdict["ios.ipa_inspect"] == "local"
    assert verdict["preview.navigate"] == "conditional"
    assert verdict["demo.export"] == "local"
    assert verdict["project.run"] == "local"
    assert verdict["job.cancel"] == "local"
    assert verdict["remote.run"] == "local"


def test_every_workspace_route_has_a_row():
    routes = {r["route"] for r in parity.build()["ui_routes"]}
    for section in ("project", "devices", "jobs", "tools", "debug", "testing", "profiling", "files", "release", "demo"):
        assert section in routes


def test_generated_files_are_current():
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "build_mobile_parity.py"), "--check"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print("ok", name)
