"""project.inspect and project.run for React Native, Expo, Capacitor and Kotlin Multiplatform
projects, with stand-in tools and fixture projects."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_client import StudioError, dispatch, jobs
from test_product import Isolated

RN_APP_GRADLE = """apply plugin: "com.android.application"
apply plugin: "com.facebook.react"
android {
    namespace "com.demo"
    defaultConfig {
        applicationId "com.demo"
        minSdkVersion rootProject.ext.minSdkVersion
    }
}
"""


class FrameworkTests(Isolated):
    def project(self, name, package, files=()):
        root = self.root / name
        root.mkdir()
        (root / "package.json").write_text(json.dumps(package))
        for relative, text in files:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return root

    def react_native(self, installed=True):
        root = self.project("RnApp", {"dependencies": {"react-native": "0.80.0"}}, [
            ("android/settings.gradle", "include ':app'\n"),
            ("android/build.gradle", "buildscript { ext { minSdkVersion = 24 } }\n"),
            ("android/app/build.gradle", RN_APP_GRADLE),
            ("android/gradlew", "#!/bin/sh\nsleep 30\n"),
        ])
        if installed:
            (root / "node_modules/react-native").mkdir(parents=True)
        return root

    def test_react_native_is_inspected_and_run_with_gradle_then_metro(self):
        for tool in ("npx", "node", "adb"):
            self.tool(tool)
        root = self.react_native(installed=False)
        inspected = dispatch("project.inspect", {"path": str(root)})
        self.assertIn("react-native", inspected["kinds"])
        self.assertIn("android", inspected["platforms"])
        self.assertEqual(inspected["javascript"]["framework"], "react-native")
        self.assertFalse(inspected["javascript"]["dependencies_installed"])
        self.assertEqual(inspected["android"]["application_ids"], ["com.demo"])
        self.assertEqual(inspected["android"]["app_modules"][0]["min_sdk"], 24)
        args = {"path": str(root), "platform": "android", "device": "emulator-5554"}
        with self.assertRaises(StudioError) as missing:
            dispatch("project.run", args)
        self.assertEqual(missing.exception.code, "dependencies_missing")
        (root / "node_modules/react-native").mkdir(parents=True)
        job = dispatch("project.run", args)
        steps = job["plan"]["steps"]
        self.assertEqual(steps[0]["argv"][-2:], ["--console=plain", ":app:installDebug"])
        self.assertEqual(steps[1]["argv"][1], "_serve_then")
        self.assertTrue(steps[1]["interactive"])
        serve = json.loads(steps[1]["argv"][2])
        self.assertEqual(serve["serve"][1:], ["react-native", "start", "--port", "8081"])
        self.assertEqual(serve["then"][0][1:], ["-s", "emulator-5554", "reverse", "tcp:8081", "tcp:8081"])
        self.assertIn('"bundle_id":"com.demo"', serve["then"][1][-1])
        jobs.cancel(job["id"])
        jobs.wait(job["id"], 10)
        release = dispatch("project.run", {**args, "configuration": "release"})
        self.assertEqual(release["plan"]["steps"][0]["argv"][-1], ":app:installRelease")
        self.assertEqual(release["plan"]["steps"][1]["argv"][1:3], ["call", "android.launch"])

    def test_react_native_without_android_or_node_says_what_to_do(self):
        self.tool("npx")
        root = self.project("Bare", {"dependencies": {"react-native": "0.80.0"}})
        with self.assertRaises(StudioError) as missing:
            dispatch("project.run", {"path": str(root), "platform": "android", "device": "emulator-5554"})
        self.assertEqual(missing.exception.code, "android_project_missing")
        root = self.react_native()
        with patch.dict(os.environ, {"PATH": "/nonexistent"}):
            with self.assertRaises(StudioError) as missing:
                dispatch("project.run", {"path": str(root), "platform": "android", "device": "emulator-5554"})
        self.assertEqual(missing.exception.code, "tool_missing")

    def test_expo_runs_expo_run_android_on_the_device(self):
        root = self.project("ExpoApp", {"dependencies": {"expo": "54.0.0", "react-native": "0.81.0"}})
        inspected = dispatch("project.inspect", {"path": str(root)})
        self.assertEqual(inspected["javascript"]["framework"], "expo")
        self.assertEqual(inspected["javascript"]["android_project"], None)
        self.assertIn("android", inspected["platforms"])
        args = {"path": str(root), "platform": "android", "device": "emulator-5554"}
        with patch.dict(os.environ, {"PATH": "/nonexistent"}):
            with self.assertRaises(StudioError) as missing:
                dispatch("project.run", args)
        self.assertEqual(missing.exception.code, "tool_missing")
        self.assertIn("npx", str(missing.exception))
        self.tool("npx", "import time;time.sleep(30)\n")
        with self.assertRaises(StudioError) as missing:
            dispatch("project.run", args)
        self.assertEqual(missing.exception.code, "dependencies_missing")
        (root / "node_modules/expo").mkdir(parents=True)
        job = dispatch("project.run", args)
        step = job["plan"]["steps"][0]
        self.assertEqual(step["argv"][1:], ["expo", "run:android", "--device", "emulator-5554"])
        self.assertTrue(step["interactive"])
        self.assertEqual(job["plan"]["env"]["ANDROID_SERIAL"], "emulator-5554")
        jobs.cancel(job["id"])
        jobs.wait(job["id"], 10)
        release = dispatch("project.run", {**args, "configuration": "release"})
        self.assertEqual(release["plan"]["steps"][0]["argv"][5:], ["--variant", "release", "--no-bundler"])
        self.assertFalse(release["plan"]["steps"][0]["interactive"])

    def test_capacitor_builds_syncs_installs_and_launches(self):
        log = self.root / "calls.log"
        record = f"import sys\nopen({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        self.tool("npx", record)
        self.tool("npm", record)
        self.tool("adb", "import json,sys\nargs=sys.argv[1:]\nprint('priority=0\\nio.ionic.starter/.MainActivity' if 'resolve-activity' in args[-1] else json.dumps(args))\n")
        root = self.project("CapApp", {"scripts": {"build": "vite build"}, "dependencies": {"@capacitor/core": "7.0.0"}},
                            [("capacitor.config.json", '{"appId":"io.ionic.starter","webDir":"dist"}')])
        inspected = dispatch("project.inspect", {"path": str(root)})
        self.assertIn("capacitor", inspected["kinds"])
        self.assertEqual(inspected["javascript"]["app_id"], "io.ionic.starter")
        args = {"path": str(root), "platform": "android", "device": "emulator-5554"}
        with self.assertRaises(StudioError) as missing:
            dispatch("project.run", args)
        self.assertEqual(missing.exception.code, "android_project_missing")
        for relative, text in (("android/settings.gradle", "include ':app'\n"),
                               ("android/app/build.gradle", "apply plugin: 'com.android.application'\nandroid { namespace 'io.ionic.starter' }\n"),
                               ("android/gradlew", f"#!/bin/sh\necho gradle \"$@\" >> {str(log)!r}\n")):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(text)
        with self.assertRaises(StudioError) as missing:
            dispatch("project.run", args)
        self.assertEqual(missing.exception.code, "dependencies_missing")
        (root / "node_modules/@capacitor/cli").mkdir(parents=True)
        job = dispatch("project.run", args)
        done = jobs.wait(job["id"], 30)
        self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"])["text"])
        self.assertEqual(log.read_text().splitlines(), ["run build", "cap sync android", "gradle --console=plain :app:installDebug"])
        self.assertIn("io.ionic.starter/.MainActivity", jobs.logs(job["id"])["text"])


if __name__ == "__main__":
    unittest.main()
