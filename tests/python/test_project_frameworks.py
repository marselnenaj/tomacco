"""project.inspect and project.run for React Native, Expo, Capacitor and Kotlin Multiplatform
projects, project.dependency_updates and project.analyze, with stand-in tools and fixture projects."""
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

    def test_kotlin_multiplatform_android_plans_with_gradle_and_ios_needs_a_mac(self):
        root = self.root / "Kmp"
        for relative, text in (("settings.gradle.kts", 'include(":composeApp")\n'),
                               ("composeApp/build.gradle.kts", 'plugins { kotlin("multiplatform"); id("com.android.application") }\nkotlin { androidTarget(); iosArm64() }\nandroid { defaultConfig { applicationId = "dev.kmp" } }\n'),
                               ("gradlew", "#!/bin/sh\n")):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(text)
        inspected = dispatch("project.inspect", {"path": str(root)})
        self.assertIn("kotlin-multiplatform", inspected["kinds"])
        self.assertEqual(inspected["multiplatform"]["ios"]["targets"], ["iosArm64"])
        plan = dispatch("project.plan", {"path": str(root), "platform": "android"})
        self.assertEqual(plan["steps"][0]["argv"][-1], "assembleDebug")
        with self.assertRaises(StudioError) as refused:
            dispatch("project.plan", {"path": str(root), "platform": "ios"})
        self.assertEqual(refused.exception.code, "macos_required")


class DependencyUpdateTests(Isolated):
    def metadata(self, repository, group, artifact, versions):
        directory = repository / group.replace(".", "/") / artifact
        directory.mkdir(parents=True)
        listed = "".join(f"<version>{v}</version>" for v in versions)
        (directory / "maven-metadata.xml").write_text(
            f"<metadata><groupId>{group}</groupId><artifactId>{artifact}</artifactId><versioning><versions>{listed}</versions></versioning></metadata>")

    def test_a_gradle_build_is_compared_with_repository_metadata(self):
        root = self.root / "App"
        for relative, text in (("settings.gradle.kts", 'include(":app")\n'),
                               ("gradle/libs.versions.toml", '[versions]\ncore = "1.12.0"\n[libraries]\ncore = { module = "androidx.core:core-ktx", version.ref = "core" }\nui = { group = "androidx.compose.ui", name = "ui" }\nmissing = "com.nowhere:thing:1.0"\n[plugins]\napp = { id = "com.android.application", version = "9.1.1" }\n'),
                               ("app/build.gradle.kts", 'dependencies { implementation("com.squareup.okio:okio:3.9.0") }\n')):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(text)
        google, central = self.root / "google", self.root / "central"
        self.metadata(google, "androidx.core", "core-ktx", ["1.12.0", "1.13.0", "1.14.0-alpha01"])
        self.metadata(google, "com.android.application", "com.android.application.gradle.plugin", ["9.0.0", "9.1.1"])
        self.metadata(central, "com.squareup.okio", "okio", ["3.9.0", "3.10.0"])
        before = sorted(p.relative_to(root) for p in root.rglob("*"))
        job = dispatch("project.dependency_updates", {"path": str(root), "repositories": [google.as_uri(), central.as_uri()]})
        done = jobs.wait(job["id"], 60)
        self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"])["text"])
        report_path = next(a["path"] for a in done["result"]["artifacts"] if a["path"].endswith("report.json"))
        report = json.loads(Path(report_path).read_text())
        entries = {d["name"]: d for d in report["dependencies"]}
        self.assertEqual(entries["core"]["status"], "update_available")
        self.assertEqual(entries["core"]["latest_stable"], "1.13.0")
        self.assertEqual(entries["core"]["latest"], "1.14.0-alpha01")
        self.assertEqual(entries["core"]["source"], "gradle/libs.versions.toml:4")
        self.assertEqual(entries["core"]["repository"], google.as_uri())
        self.assertEqual(entries["app"]["status"], "up_to_date")
        self.assertEqual(entries["com.squareup.okio:okio"]["latest_stable"], "3.10.0")
        self.assertEqual(entries["com.squareup.okio:okio"]["repository"], central.as_uri())
        self.assertEqual(entries["ui"]["status"], "managed")
        self.assertEqual(entries["missing"]["status"], "not_found")
        self.assertEqual(report["summary"], {"managed": 1, "not_found": 1, "up_to_date": 1, "update_available": 2})
        self.assertEqual(sorted(p.relative_to(root) for p in root.rglob("*")), before)

    def test_offline_and_unsupported_projects_are_refused_at_once(self):
        root = self.root / "App"
        root.mkdir()
        (root / "build.gradle").write_text("dependencies { implementation 'a.b:c:1.0' }\n")
        with self.assertRaises(StudioError) as refused:
            dispatch("project.dependency_updates", {"path": str(root), "repositories": ["https://repository.invalid/m2"]})
        self.assertEqual(refused.exception.code, "offline")
        with self.assertRaises(StudioError) as refused:
            dispatch("project.dependency_updates", {"path": str(root), "repositories": ["http://plain.example/m2"]})
        self.assertEqual(refused.exception.code, "invalid_argument")
        web = self.root / "Web"
        web.mkdir()
        (web / "package.json").write_text("{}")
        with self.assertRaises(StudioError) as refused:
            dispatch("project.dependency_updates", {"path": str(web)})
        self.assertEqual(refused.exception.code, "action_unsupported")

    def test_pub_npm_and_swiftpm_dependencies_are_checked_in_one_report(self):
        root = self.root / "Mixed"
        root.mkdir()
        (root / "pubspec.yaml").write_text("name: mixed\nflutter:\n")
        (root / "package.json").write_text(json.dumps({"dependencies": {"react": "^18.2.0", "left-pad": "1.0.0"}}))
        (root / "Package.resolved").write_text(json.dumps({"version": 2, "pins": [
            {"identity": "swift-log", "kind": "remoteSourceControl", "location": "https://example.invalid/swift-log.git",
             "state": {"revision": "abc", "version": "1.5.0"}},
            {"identity": "fork", "kind": "remoteSourceControl", "location": "https://example.invalid/fork.git",
             "state": {"branch": "main", "revision": "def"}}]}))
        calls = self.root / "calls.log"
        record = f"open({str(calls)!r},'a').write(' '.join(sys.argv[1:])+'\\n')\n"
        self.tool("flutter", "import json,sys\n" + record + "assert sys.argv[1:]==['pub','outdated','--json']\n"
                  "print(json.dumps({'packages':[{'package':'http','kind':'direct','current':{'version':'1.1.0'},'upgradable':{'version':'1.2.2'},"
                  "'resolvable':{'version':'1.2.2'},'latest':{'version':'1.2.2'}}]}))\n")
        self.tool("npm", "import json,sys\n" + record + "assert sys.argv[1:]==['outdated','--json']\n"
                  "print(json.dumps({'react':{'current':'18.2.0','wanted':'18.3.1','latest':'19.1.0','type':'dependencies'}}))\nsys.exit(1)\n")
        self.tool("git", "import sys\n" + record + "assert sys.argv[1:4]==['ls-remote','--tags','--refs']\n"
                  "print('a\\trefs/tags/1.5.0\\nb\\trefs/tags/1.6.0')\n")
        before = sorted(p.relative_to(root) for p in root.rglob("*"))
        job = dispatch("project.dependency_updates", {"path": str(root)})
        done = jobs.wait(job["id"], 60)
        self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"])["text"])
        report_path = next(a["path"] for a in done["result"]["artifacts"] if a["path"].endswith("report.json"))
        report = json.loads(Path(report_path).read_text())
        self.assertEqual([e["name"] for e in report["ecosystems"]], ["pub", "npm", "swiftpm"])
        entries = {(d["ecosystem"], d["name"]): d for d in report["dependencies"]}
        self.assertEqual(entries[("pub", "http")]["status"], "update_available")
        self.assertEqual(entries[("pub", "http")]["latest"], "1.2.2")
        self.assertEqual(entries[("npm", "react")]["status"], "update_available")
        self.assertEqual(entries[("npm", "react")]["latest"], "19.1.0")
        self.assertEqual(entries[("npm", "left-pad")]["status"], "up_to_date")
        self.assertEqual(entries[("swiftpm", "swift-log")]["latest_stable"], "1.6.0")
        self.assertEqual(entries[("swiftpm", "fork")]["status"], "unversioned")
        self.assertEqual(report["summary"], {"unversioned": 1, "up_to_date": 1, "update_available": 3})
        self.assertEqual(calls.read_text().count("ls-remote"), 1)
        self.assertEqual(sorted(p.relative_to(root) for p in root.rglob("*")), before)
        # A missing tool leaves the others checked, and is named.
        (self.root / "npm").unlink()
        with patch.dict(os.environ, {"PATH": "/nonexistent"}):
            job = dispatch("project.dependency_updates", {"path": str(root)})
            done = jobs.wait(job["id"], 60)
        report = json.loads(Path(next(a["path"] for a in done["result"]["artifacts"] if a["path"].endswith("report.json"))).read_text())
        npm = next(e for e in report["ecosystems"] if e["name"] == "npm")
        self.assertIn("npm", npm["error"])
        self.assertEqual(sum(1 for d in report["dependencies"] if d["ecosystem"] == "pub"), 1)


class AnalyzeTests(Isolated):
    def report(self, job):
        done = jobs.wait(job["id"], 60)
        path = next(a["path"] for a in done["result"]["artifacts"] if a["path"].endswith("report.json"))
        return done, json.loads(Path(path).read_text())

    def test_a_flutter_project_is_analyzed_with_flutters_dart(self):
        root = self.root / "FlutterApp"
        (root / "lib").mkdir(parents=True)
        (root / "pubspec.yaml").write_text("flutter:\n")
        sdk = self.root / "flutter/bin"
        sdk.mkdir(parents=True)
        for name in ("flutter", "dart"):
            (sdk / name).write_text("#!" + sys.executable + "\n" + (
                "import json,os,sys\nassert sys.argv[1:] == ['analyze','--format=json','.'], sys.argv\n"
                "print('Analyzing FlutterApp...')\n"
                "f=os.path.join(os.getcwd(),'lib/main.dart')\n"
                "print(json.dumps({'version':1,'diagnostics':[{'code':'undefined_identifier','severity':'ERROR','type':'COMPILE_TIME_ERROR',"
                "'location':{'file':f,'range':{'start':{'offset':1,'line':4,'column':3},'end':{'offset':2,'line':4,'column':7}}},'problemMessage':\"Undefined name 'nope'.\"}]}))\n"
                "sys.exit(3)\n" if name == "dart" else ""))
            (sdk / name).chmod(0o755)
        dispatch("settings.configure", {"executables": {"flutter": str(sdk / "flutter")}})
        with patch.dict(os.environ, {"PATH": "/nonexistent"}):
            job = dispatch("project.analyze", {"path": str(root)})
            done, report = self.report(job)
        self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"])["text"])
        self.assertEqual(report["passed"], False)
        self.assertEqual(report["totals"], {"issues": 1, "error": 1, "warning": 0, "info": 0})
        issue = report["issues"][0]
        self.assertEqual((issue["tool"], issue["file"], issue["line"], issue["column"], issue["severity"], issue["code"]),
                         ("dart", "lib/main.dart", 4, 3, "error", "undefined_identifier"))
        self.assertEqual(report["tools"][0]["exit_code"], 3)
        self.assertIn("1 errors, 0 warnings", jobs.logs(job["id"])["text"])

    def test_a_typescript_project_runs_its_own_eslint_and_tsc(self):
        root = self.root / "Web"
        bin_dir = root / "node_modules/.bin"
        bin_dir.mkdir(parents=True)
        (root / "package.json").write_text("{}")
        with self.assertRaises(StudioError) as refused:
            dispatch("project.analyze", {"path": str(root)})
        self.assertEqual(refused.exception.code, "nothing_to_analyze")
        (root / "tsconfig.json").write_text("{}")
        scripts = {
            "eslint": "import json,os,sys\nassert sys.argv[1:] == ['--format','json','.']\n"
                      "print(json.dumps([{'filePath':os.path.join(os.getcwd(),'src/a.ts'),'messages':[{'ruleId':'semi','severity':1,'message':'Missing semicolon.','line':2,'column':9}]}]))\nsys.exit(0)\n",
            "tsc": "import sys\nassert sys.argv[1:] == ['--noEmit','--pretty','false']\n"
                   "print(\"src/a.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'.\")\nsys.exit(2)\n",
        }
        for name, source in scripts.items():
            (bin_dir / name).write_text("#!" + sys.executable + "\n" + source)
            (bin_dir / name).chmod(0o755)
        with self.assertRaises(StudioError) as refused:
            dispatch("project.analyze", {"path": str(root), "tools": ["dart"]})
        self.assertEqual(refused.exception.code, "tool_missing")
        before = sorted(p.relative_to(root) for p in root.rglob("*"))
        done, report = self.report(dispatch("project.analyze", {"path": str(root)}))
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual([t["tool"] for t in report["tools"]], ["eslint", "tsc"])
        self.assertEqual([(i["tool"], i["file"], i["line"], i["severity"], i["code"]) for i in report["issues"]],
                         [("eslint", "src/a.ts", 2, "warning", "semi"), ("tsc", "src/a.ts", 3, "error", "TS2322")])
        self.assertEqual(sorted(p.relative_to(root) for p in root.rglob("*")), before)
        done, report = self.report(dispatch("project.analyze", {"path": str(root), "tools": ["eslint"]}))
        self.assertEqual(report["passed"], True)
        self.assertEqual(report["totals"]["warning"], 1)


FLUTTER_TEST = r"""import json,os,sys
args=sys.argv[1:]
report=next(a.split(':',1)[1] for a in args if a.startswith('--file-reporter=json:'))
test=os.path.join(os.getcwd(),'test','home_test.dart')
failures=os.path.join(os.getcwd(),'test','failures')
os.makedirs(failures,exist_ok=True)
for kind in ('masterImage','testImage','isolatedDiff','maskedDiff'):
    open(os.path.join(failures,'home_'+kind+'.png'),'wb').write(b'png')
error='Golden "goldens/home.png": Pixel test failed, 1.20%, 480px diff detected.\nFailure feedback can be found at '+failures
events=[{'type':'start','protocolVersion':'0.1.1','time':0},
        {'type':'suite','suite':{'id':0,'platform':'vm','path':test},'time':0},
        {'type':'testStart','test':{'id':1,'name':'home renders','suiteID':0,'line':5,'url':'file://'+test},'time':10},
        {'type':'testDone','testID':1,'result':'success','skipped':False,'hidden':False,'time':60},
        {'type':'testStart','test':{'id':2,'name':'home matches golden','suiteID':0,'line':9,'url':'file://'+test},'time':70},
        {'type':'error','testID':2,'error':error,'stackTrace':'','isFailure':True,'time':90},
        {'type':'testDone','testID':2,'result':'failure','skipped':False,'hidden':False,'time':100},
        {'type':'done','success':False,'time':120}]
with open(report,'w') as f:
    for e in events: f.write(json.dumps(e)+'\n')
print('00:01 +1 -1: Some tests failed.')
sys.exit(1)
"""


class FlutterTestResultTests(Isolated):
    def flutter_project(self):
        root = self.root / "FlutterApp"
        (root / "test").mkdir(parents=True)
        (root / "pubspec.yaml").write_text("flutter:\n")
        return root

    def test_flutter_tests_are_read_with_their_golden_diffs(self):
        self.tool("flutter", FLUTTER_TEST)
        root = self.flutter_project()
        plan = dispatch("project.plan", {"path": str(root), "action": "test"})
        self.assertEqual(plan["steps"][0]["argv"][1:], ["test", "--reporter", "expanded", "--file-reporter=json:build/tomacco/flutter_test.json"])
        job = dispatch("project.execute", {"path": str(root), "action": "test"})
        done = jobs.wait(job["id"], 30)
        self.assertEqual(done["state"], "failed", jobs.logs(job["id"])["text"])
        self.assertTrue(any(a["path"].endswith("build/tomacco/flutter_test.json") for a in done["result"]["artifacts"]))
        results = dispatch("testing.results", {"path": str(root), "job_id": job["id"]})
        self.assertEqual(results["totals"]["tests"], 2)
        self.assertEqual(results["totals"]["failed"], 1)
        self.assertEqual(results["failures"][0]["name"], "home matches golden")
        golden = results["golden_failures"][0]
        self.assertEqual(golden["golden"], "goldens/home.png")
        self.assertEqual(golden["message"], "Pixel test failed, 1.20%, 480px diff detected.")
        self.assertEqual(sorted(golden["images"]), ["isolatedDiff", "maskedDiff", "masterImage", "testImage"])
        self.assertTrue(golden["images"]["isolatedDiff"].endswith("test/failures/home_isolatedDiff.png"))


class WatchTests(Isolated):
    def test_a_source_change_rebuilds_and_relaunches_and_build_output_does_not(self):
        import time
        from host_patience import PATIENCE
        builds = self.root / "builds.log"
        self.tool("adb", "import json,sys\nargs=sys.argv[1:]\nprint('priority=0\\ndev.example.app/.MainActivity' if 'resolve-activity' in args[-1] else json.dumps(args))\n")
        root = self.root / "App"
        for relative, text in (("settings.gradle", "include ':app'\n"),
                               ("app/build.gradle", "apply plugin: 'com.android.application'\nandroid { namespace 'dev.example.app'\n"
                                                    "defaultConfig { applicationId 'dev.example.app' } }\n"),
                               ("app/src/main/java/Main.kt", "fun main() {}\n"),
                               ("gradlew", f"#!/bin/sh\necho \"$@\" >> {str(builds)!r}\n")):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(text)
        (root / "gradlew").chmod(0o755)
        with self.assertRaises(StudioError):
            dispatch("project.watch", {"path": str(root), "platform": "android", "device": "emulator-5554", "ignore": ["a/b"]})
        job = dispatch("project.watch", {"path": str(root), "platform": "android", "device": "emulator-5554", "debounce_ms": 200})

        def log_has(text):
            deadline = time.monotonic() + 30 * PATIENCE
            while text not in jobs.logs(job["id"])["text"]:
                self.assertLess(time.monotonic(), deadline, jobs.logs(job["id"])["text"])
                time.sleep(.1)
        log_has("cycle 0: built, installed and launched")
        (root / "app/build/outputs").mkdir(parents=True)
        (root / "app/build/outputs/app.apk").write_text("apk")
        (root / "app/src/main/java/Main.kt").write_text("fun main() { println(1) }\n")
        log_has("cycle 1: 1 file changed (app/src/main/java/Main.kt): built, installed and launched")
        self.assertEqual(builds.read_text().splitlines(), ["--console=plain :app:installDebug"] * 2)
        self.assertNotIn("cycle 2", jobs.logs(job["id"])["text"])
        children = [j for j in jobs.listing(50) if j["id"] != job["id"]]
        self.assertTrue(children and all(j["state"] == "succeeded" for j in children))
        jobs.cancel(job["id"])
        self.assertEqual(jobs.wait(job["id"], 10 * PATIENCE)["state"], "cancelled")

    def test_a_flutter_change_is_hot_reloaded_in_one_session(self):
        import time
        from host_patience import PATIENCE
        commands = self.root / "commands.log"
        self.tool("flutter", f"""import json,sys
print(json.dumps([{{'event':'app.start','params':{{'appId':'fixture-app'}}}}]),flush=True)
print(json.dumps([{{'event':'app.started','params':{{'appId':'fixture-app'}}}}]),flush=True)
for line in sys.stdin:
    value=json.loads(line)[0]
    open({str(commands)!r},'a').write(value['method']+'\\n')
    print(json.dumps([{{'id':value['id'],'result':{{'code':0}}}}]),flush=True)
""")
        root = self.root / "FlutterApp"
        (root / "lib").mkdir(parents=True)
        (root / "android").mkdir()
        (root / "pubspec.yaml").write_text("flutter:\n")
        (root / "lib/main.dart").write_text("void main() {}\n")
        job = dispatch("project.watch", {"path": str(root), "platform": "android", "device": "emulator-5554", "debounce_ms": 200})

        def wait_for(check, what):
            deadline = time.monotonic() + 30 * PATIENCE
            while not check():
                self.assertLess(time.monotonic(), deadline, (what, jobs.logs(job["id"])["text"]))
                time.sleep(.1)
        wait_for(lambda: "cycle 0: started Flutter session" in jobs.logs(job["id"])["text"], "session")
        session = jobs.logs(job["id"])["text"].split("started Flutter session ")[1].split()[0]
        wait_for(lambda: jobs.get(session).get("phase") == "ready", "ready")
        (root / "lib/main.dart").write_text("void main() { print(1); }\n")
        wait_for(lambda: f"cycle 1: 1 file changed (lib/main.dart): Hot Reload sent to Flutter session {session}"
                 in jobs.logs(job["id"])["text"], "reload")
        wait_for(lambda: commands.exists() and commands.read_text() == "app.restart\n", "restart")
        jobs.cancel(job["id"])
        jobs.wait(job["id"], 10 * PATIENCE)
        # The session the watch started ends with it.
        self.assertEqual(jobs.wait(session, 10 * PATIENCE)["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
