use crate::{core::*, gradle_model, javascript, jobs, swift, toolchain as tc};
use serde_json::{Value, json};
use std::{collections::BTreeSet, fs, path::Path};

/// A project's configuration file.
pub fn config_path(root: &Path) -> std::path::PathBuf {
    root.join("tomacco.json")
}
pub fn inspect(value: &str) -> Result {
    let root = path(value, true, true)?;
    let config = read_json(&config_path(&root), Some(json!({})))?;
    if !config.is_object() {
        return Err(Error::new(
            "invalid_project",
            "tomacco.json must be an object",
        ));
    }
    let mut kinds = Vec::new();
    let mut platforms = BTreeSet::new();
    if root.join("pubspec.yaml").is_file()
        && fs::read_to_string(root.join("pubspec.yaml"))?.contains("flutter:")
    {
        kinds.push("flutter");
        for p in ["android", "ios", "web", "linux"] {
            if root.join(p).is_dir() {
                platforms.insert(p);
            }
        }
    }
    if ["xtool.yml", "xtool.yaml", "xtool.json"]
        .iter()
        .any(|p| root.join(p).is_file())
    {
        kinds.push("xtool");
        platforms.insert("ios");
    }
    let gradle = [root.clone(), root.join("android")]
        .into_iter()
        .find(|dir| {
            [
                "settings.gradle",
                "settings.gradle.kts",
                "build.gradle",
                "build.gradle.kts",
            ]
            .iter()
            .any(|p| dir.join(p).is_file())
        });
    let mut multiplatform = None;
    if let Some(dir) = &gradle {
        kinds.push("gradle");
        platforms.insert("android");
        multiplatform = gradle_model::multiplatform(dir);
        if let Some(mp) = &multiplatform {
            kinds.push("kotlin-multiplatform");
            if mp["compose_multiplatform"] == true {
                kinds.push("compose-multiplatform");
            }
            if mp["ios"]["targets"]
                .as_array()
                .is_some_and(|t| !t.is_empty())
            {
                platforms.insert("ios");
            }
        }
    }
    if root.join("Package.swift").is_file() {
        kinds.push("swift");
        if !kinds.contains(&"xtool") {
            platforms.extend(["linux", "android"]);
        }
    } else if root.join("Swift/Package.swift").is_file() {
        kinds.push("swift");
    }
    if root.join("package.json").is_file() {
        let package = read_json(&root.join("package.json"), None)?;
        for (name, key) in [
            ("react-native", "react-native"),
            ("expo", "expo"),
            ("capacitor", "@capacitor/core"),
        ] {
            if package["dependencies"].get(key).is_some()
                || package["devDependencies"].get(key).is_some()
            {
                kinds.push(name);
            }
        }
        if kinds.contains(&"expo") {
            platforms.insert("android");
        }
        kinds.push("web");
        platforms.insert("web");
    }
    if fs::read_dir(&root)?.filter_map(|e| e.ok()).any(|e| {
        e.path()
            .extension()
            .is_some_and(|e| e == "xcodeproj" || e == "xcworkspace")
    }) {
        kinds.push("xcode");
        platforms.insert("ios");
    }
    let tasks = config.get("tasks").cloned().unwrap_or(json!({}));
    let tasks = tasks.as_object().ok_or_else(|| {
        Error::new(
            "invalid_project",
            "tasks must be an object of command arrays",
        )
    })?;
    if kinds.is_empty() && tasks.is_empty() {
        return Err(Error::new(
            "project_unrecognized",
            format!("No supported project in {}", root.display()),
        )
        .remedy(
            "Open a Flutter, Gradle, Swift, xtool, web or Xcode project, or add tomacco.json tasks",
        ));
    }
    let mut names: Vec<_> = tasks.keys().cloned().collect();
    names.sort();
    let mut value = json!({"path":root,"name":root.file_name().unwrap_or_default().to_string_lossy(),"kinds":kinds,"platforms":platforms,"gradle_directory":gradle,"android":gradle.as_deref().map(gradle_model::read),"tasks":names,"configuration":config});
    if let Some(dir) = &gradle {
        value["test_setup"] = gradle_model::test_setup(dir);
        let performance = crate::macrobench::setup(dir);
        if !performance.is_null() {
            value["performance"] = performance;
        }
    }
    if let Some(mp) = multiplatform {
        value["multiplatform"] = mp;
    }
    if let Some(framework) = javascript::framework(&kinds) {
        value["javascript"] = javascript::describe(&root, framework, gradle.as_deref());
    }
    Ok(value)
}
fn kind(project: &Value, name: &str) -> bool {
    project["kinds"]
        .as_array()
        .is_some_and(|a| a.iter().any(|v| v == name))
}
fn command_array(value: &Value) -> bool {
    value.as_array().is_some_and(|a| {
        !a.is_empty()
            && a.iter()
                .all(|v| v.as_str().is_some_and(|s| !s.contains('\0')))
    })
}
/// The Gradle command of a project: its wrapper, or gradle.
pub(crate) fn gradle_command(project: &Value) -> Result<Vec<String>> {
    gradle(project)
}
fn gradle(project: &Value) -> Result<Vec<String>> {
    let wrapper = Path::new(s(project, "gradle_directory")).join("gradlew");
    if wrapper.is_file() {
        Ok(if tc::executable(&wrapper) {
            vec![wrapper.to_string_lossy().into_owned()]
        } else {
            vec!["/bin/sh".into(), wrapper.to_string_lossy().into_owned()]
        })
    } else {
        Ok(vec![tc::find("gradle")?])
    }
}
pub fn configure(a: &Value) -> Result {
    let project = inspect(s(a, "path"))?;
    let root = Path::new(s(&project, "path"));
    let mut config = project["configuration"].clone();
    let id = s(a, "application_id");
    if !id.is_empty() {
        identifier(
            id,
            "application ID",
            Some(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+"),
        )?;
        config["applicationId"] = json!(id);
    }
    if let Some(tasks) = a["tasks"].as_object() {
        for (name, command) in tasks {
            identifier(name, "task name", None)?;
            if !command_array(command) {
                return Err(Error::new(
                    "invalid_task",
                    "Each task must be a non-empty command array",
                ));
            }
        }
        config["tasks"] = a["tasks"].clone();
    }
    if !id.is_empty() && kind(&project, "xtool") {
        let target = [root.join("xtool.yml"), root.join("xtool.yaml")]
            .into_iter()
            .find(|p| p.is_file())
            .ok_or_else(|| {
                Error::new(
                    "config_unsupported",
                    "Update the bundle identifier in this project's xtool configuration manually",
                )
            })?;
        let value = fs::read_to_string(&target)?;
        let re = regex::Regex::new(r"(?m)^bundleID:.*$").unwrap();
        if !re.is_match(&value) {
            return Err(Error::new(
                "invalid_project",
                "xtool configuration has no top-level bundleID",
            ));
        }
        atomic_write(
            &target,
            re.replace_all(&value, format!("bundleID: {id}")).as_bytes(),
        )?;
    }
    atomic_json(&config_path(root), &config)?;
    inspect(&root.to_string_lossy())
}
pub fn plan(a: &Value) -> Result {
    let project = inspect(s(a, "path"))?;
    let root = Path::new(s(&project, "path"));
    let mut cwd = root.to_path_buf();
    let mut args = Vec::new();
    let mut artifacts = Vec::<String>::new();
    // Reports kept even when the job fails, as failing tests do.
    let mut always = Vec::<String>::new();
    let action = s(a, "action");
    let platform = s(a, "platform");
    let configuration = s(a, "configuration");
    let format = s(a, "format");
    let suffix = if configuration == "release" {
        "Release"
    } else {
        "Debug"
    };
    if action == "task" {
        let entry = &project["configuration"]["tasks"][s(a, "task")];
        if !command_array(entry) {
            return Err(Error::new(
                "invalid_task",
                "A configured task must be a non-empty argv array",
            )
            .remedy("Define tasks in tomacco.json"));
        }
        args = strings(entry);
    } else if kind(&project, "kotlin-multiplatform") && platform == "ios" {
        return Err(kotlin_ios_error());
    } else if kind(&project, "flutter") {
        let tool = tc::find("flutter")?;
        match action {
            // The JSON reporter's file is what testing.results reads.
            "test" => {
                args = argv(&[
                    &tool,
                    "test",
                    "--reporter",
                    "expanded",
                    &format!("--file-reporter=json:{FLUTTER_TEST_REPORT}"),
                ]);
                always = argv(&[FLUTTER_TEST_REPORT]);
            }
            "lint" => args = argv(&[&tool, "analyze", "--no-pub"]),
            _ => {
                if platform == "ios" && !cfg!(target_os = "macos") {
                    return Err(Error::new("macos_required","project.execute builds Flutter iOS apps only on macOS").remedy("On Linux call ios.flutter_build (it needs ios.toolkit_install), or use remote.run with an SSH Mac workspace"));
                }
                if action == "instrumented-test" {
                    let device = s(a, "device");
                    if device.is_empty() {
                        return Err(Error::new(
                            "device_required",
                            "Flutter integration tests run on a device; name it with device",
                        )
                        .remedy("Pass device, a serial from device.list"));
                    }
                    identifier(device, "device identifier", None)?;
                    if !root.join("integration_test").is_dir() {
                        return Err(Error::new(
                            "integration_test_missing",
                            "This Flutter project has no integration_test directory",
                        )
                        .remedy("Add the integration_test package as a dev dependency and its tests in integration_test/, as Flutter's integration testing guide describes"));
                    }
                    args = argv(&[
                        &tool,
                        "test",
                        "integration_test",
                        "-d",
                        device,
                        "--reporter",
                        "expanded",
                        &format!("--file-reporter=json:{FLUTTER_INTEGRATION_REPORT}"),
                    ]);
                    always = argv(&[FLUTTER_INTEGRATION_REPORT]);
                }
                if action == "build" {
                    let target = match platform {
                        "android" => {
                            if format == "aab" {
                                "appbundle"
                            } else {
                                "apk"
                            }
                        }
                        "ios" => "ipa",
                        "web" => "web",
                        "linux" => "linux",
                        _ => return Err(Error::new("platform_unsupported", platform)),
                    };
                    args = argv(&[&tool, "build", target]);
                    if platform != "web" {
                        args.push(format!("--{configuration}"));
                    }
                    if platform == "ios" && !b(a, "sign") {
                        args.push("--no-codesign".into());
                    }
                    artifacts = argv(&[
                        "build/app/outputs/flutter-apk/*.apk",
                        "build/app/outputs/bundle/**/*.aab",
                        "build/ios/ipa/*.ipa",
                        "build/web/index.html",
                    ]);
                }
            }
        }
    } else if kind(&project, "xtool") && platform == "ios" {
        match action {
            "build" => {
                args = argv(&[
                    &tc::find("xtool")?,
                    "dev",
                    "build",
                    "--configuration",
                    configuration,
                ]);
                if b(a, "sign") {
                    args.push("--sign".into());
                }
                if format == "ipa" {
                    args.push("--ipa".into());
                }
                artifacts = argv(&[
                    ".xtool/**/*.ipa",
                    ".build/**/*.ipa",
                    "xtool/**/*.ipa",
                    "*.ipa",
                ]);
            }
            "test" => args = argv(&[&tc::find("swift")?, "test"]),
            _ => (),
        }
    } else if kind(&project, "gradle") && platform == "android" {
        cwd = Path::new(s(&project, "gradle_directory")).to_path_buf();
        let target = match action {
            "build" => format!(
                "{}{suffix}",
                if format == "aab" {
                    "bundle"
                } else {
                    "assemble"
                }
            ),
            "test" => format!("test{suffix}UnitTest"),
            "lint" => format!("lint{suffix}"),
            "instrumented-test" => format!("connected{suffix}AndroidTest"),
            _ => String::new(),
        };
        if !target.is_empty() {
            let setup = &project["test_setup"];
            let mut tasks = vec![target];
            let mut properties = Vec::new();
            let managed = s(a, "managed_device");
            let shards = a["shards"].as_u64().unwrap_or(0);
            if !managed.is_empty() {
                if action != "instrumented-test" {
                    return Err(Error::new(
                        "invalid_argument",
                        "managed_device runs instrumented tests; use it with action instrumented-test",
                    ));
                }
                if b(a, "coverage") {
                    return Err(Error::new(
                        "invalid_argument",
                        "coverage is not offered for a Gradle Managed Device run",
                    )
                    .remedy("Run instrumented-test with coverage on a connected device instead (device)"));
                }
                tasks = vec![managed_device_task(setup, managed, suffix)?];
                if shards > 1 {
                    properties.push(format!(
                        "-Pandroid.experimental.androidTest.numManagedDeviceShards={shards}"
                    ));
                }
            } else if shards > 1 {
                return Err(Error::new(
                    "invalid_argument",
                    "shards splits a Gradle Managed Device run; give managed_device too",
                )
                .remedy("For connected devices, testing.run with shard:true splits scenarios across devices"));
            }
            if b(a, "coverage") && ["test", "instrumented-test"].contains(&action) {
                tasks.extend(coverage_tasks(setup, action, suffix)?);
            }
            args = gradle(&project)?;
            args.push("--console=plain".into());
            args.extend(properties);
            args.extend(tasks);
            artifacts = argv(&[
                "**/build/outputs/apk/**/*.apk",
                "**/build/outputs/bundle/**/*.aab",
                "**/build/reports/**/index.html",
            ]);
            if b(a, "coverage") {
                artifacts.extend(argv(&[
                    "**/build/reports/coverage/**/report.xml",
                    "**/build/reports/jacoco/**/*.xml",
                    "**/build/reports/kover/**/*.xml",
                ]));
            }
        }
    } else if kind(&project, "web") && platform == "web" {
        let package = read_json(&root.join("package.json"), None)?;
        if package["scripts"].get(action).is_some() {
            args = argv(&[&tc::find("npm")?, "run", action]);
            artifacts = argv(&[
                "dist/index.html",
                "build/index.html",
                "build/web/index.html",
            ]);
        }
    } else if kind(&project, "swift")
        && ["android", "linux"].contains(&platform)
        && ["build", "test"].contains(&action)
    {
        let package = if root.join("Package.swift").is_file() {
            root.to_path_buf()
        } else {
            root.join("Swift")
        };
        let args = crate::api::arguments(
            "swift.plan",
            &json!({"path":package,"platform":platform,"action":action,"configuration":configuration}),
        )?;
        let mut value = swift::plan(&args)?;
        value["project"] = project["path"].clone();
        value["label"] = json!(format!(
            "{} · {action} · Swift · {platform}",
            s(&project, "name")
        ));
        return Ok(value);
    } else if kind(&project, "xcode") {
        return Err(
            Error::new("macos_required", "Xcode projects require a macOS toolchain")
                .remedy("Configure a Mac workspace with remote.configure, then use remote.run"),
        );
    }
    if args.is_empty() {
        return Err(Error::new(
            "action_unsupported",
            format!(
                "{action}/{platform} is not supported for {}",
                project["kinds"]
            ),
        )
        .remedy("Use an explicitly configured project task for framework-specific commands"));
    }
    let mut env = serde_json::to_value(tc::environment()?)?;
    // Gradle's connected tests run on the device ANDROID_SERIAL names.
    if !s(a, "device").is_empty() && action == "instrumented-test" && kind(&project, "gradle") {
        env["ANDROID_SERIAL"] = json!(identifier(s(a, "device"), "device identifier", None)?);
    }
    let mut value = json!({"project":project["path"],"cwd":cwd,"steps":[{"argv":args}],"env":env,"artifacts":artifacts,"exclusive":root,"label":format!("{} · {action} · {platform} · {configuration}",s(&project,"name"))});
    if b(a, "coverage")
        && !(kind(&project, "gradle")
            && platform == "android"
            && ["test", "instrumented-test"].contains(&action))
    {
        return Err(Error::new(
            "action_unsupported",
            "coverage is offered for Gradle test and instrumented-test runs",
        )
        .remedy("Run flutter test --coverage as a tomacco.json task for a Flutter project"));
    }
    // A test run's HTML report and its test history entries, also when
    // tests fail.
    if ["test", "instrumented-test"].contains(&action) {
        value["cleanup"] = json!([test_report_cleanup(root, now() - 2.0)?]);
        always.push(root.join(TEST_REPORT_HTML).to_string_lossy().into_owned());
    }
    if !always.is_empty() {
        value["always_artifacts"] = json!(always);
    }
    Ok(value)
}
/// The Gradle task that runs instrumented tests on a Gradle Managed Device
/// or device group the build files declare: `NAMEDebugAndroidTest`,
/// `GROUPGroupDebugAndroidTest`, or `allDevicesDebugAndroidTest` for `all`.
fn managed_device_task(setup: &Value, name: &str, suffix: &str) -> Result<String> {
    identifier(
        name,
        "managed device name",
        Some(r"[A-Za-z][A-Za-z0-9_]{0,99}"),
    )?;
    let modules = setup["modules"].as_array().cloned().unwrap_or_default();
    let devices: Vec<&str> = modules
        .iter()
        .flat_map(|m| m["managed_devices"].as_array().into_iter().flatten())
        .map(|d| s(d, "name"))
        .collect();
    let groups: Vec<&str> = modules
        .iter()
        .flat_map(|m| m["managed_device_groups"].as_array().into_iter().flatten())
        .filter_map(Value::as_str)
        .collect();
    if name == "all" && !devices.is_empty() {
        return Ok(format!("allDevices{suffix}AndroidTest"));
    }
    if devices.contains(&name) {
        return Ok(format!("{name}{suffix}AndroidTest"));
    }
    if groups.contains(&name) {
        return Ok(format!("{name}Group{suffix}AndroidTest"));
    }
    Err(Error::new(
        "managed_device_not_found",
        format!("The build files declare no Gradle Managed Device or group named {name}"),
    )
    .remedy(if devices.is_empty() && groups.is_empty() {
        "Declare one under android { testOptions { managedDevices { localDevices { create(\"pixel2api30\") { device = \"Pixel 2\"; apiLevel = 30; systemImageSource = \"aosp\" } } } } }, as the Android Gradle plugin documents".to_string()
    } else {
        "Use a name from project.inspect's test_setup: managed_devices, managed_device_groups, or all".to_string()
    })
    .details(json!({"managed_devices":devices,"managed_device_groups":groups})))
}

/// The Gradle tasks that write the coverage report of a test run, for what
/// the build files enable.
fn coverage_tasks(setup: &Value, action: &str, suffix: &str) -> Result<Vec<String>> {
    let modules = setup["modules"].as_array().cloned().unwrap_or_default();
    let variant = suffix.to_lowercase();
    let enables = |key: &str| {
        modules.iter().any(|m| {
            m[key]
                .as_array()
                .is_some_and(|types| types.iter().any(|t| t == variant.as_str()))
        })
    };
    let mut tasks = Vec::new();
    if action == "test" {
        if enables("unit_test_coverage") {
            tasks.push(format!("create{suffix}UnitTestCoverageReport"));
        }
        if modules.iter().any(|m| b(m, "kover")) {
            if modules.iter().any(|m| b(m, "kover") && b(m, "android")) {
                tasks.push(format!("koverXmlReport{suffix}"));
            } else {
                tasks.push("koverXmlReport".into());
            }
        }
        if modules.iter().any(|m| b(m, "jacoco_plugin")) {
            tasks.push("jacocoTestReport".into());
        }
    } else if action == "instrumented-test" {
        if enables("android_test_coverage") {
            tasks.push(format!("create{suffix}AndroidTestCoverageReport"));
        } else if enables("test_coverage_enabled") {
            tasks.push(format!("create{suffix}CoverageReport"));
        }
    }
    if tasks.is_empty() {
        let setting = if action == "test" {
            "enableUnitTestCoverage = true"
        } else {
            "enableAndroidTestCoverage = true"
        };
        return Err(Error::new(
            "coverage_not_configured",
            format!("The build files enable no coverage report for {action} in the {variant} build type"),
        )
        .remedy(format!("Add {setting} to android {{ buildTypes {{ {variant} {{ }} }} }} of the module, or apply the jacoco plugin or Kover, then run again")));
    }
    Ok(tasks)
}

/// The job cleanup that writes a test job's HTML report and records its
/// results in the test history, whether the tests passed or not.
pub(crate) fn test_report_cleanup(root: &Path, since: f64) -> Result<Value> {
    Ok(
        json!({"argv":[own_executable()?, "_test_report", root, format!("{since:.3}")],"timeout":120}),
    )
}

/// Where a test job's HTML report goes, relative to the project.
pub const TEST_REPORT_HTML: &str = "build/tomacco/test-report.html";

/// Where a Flutter test job's JSON reporter writes, relative to the project.
pub const FLUTTER_TEST_REPORT: &str = "build/tomacco/flutter_test.json";
/// Where a Flutter integration test job's JSON reporter writes.
pub const FLUTTER_INTEGRATION_REPORT: &str = "build/tomacco/integration_test.json";

pub fn execute(a: &Value) -> Result {
    let mut value = plan(a)?;
    // Flutter's file reporter writes into build/tomacco.
    if value["steps"][0]["argv"].as_array().is_some_and(|args| {
        args.iter().any(|arg| {
            arg.as_str()
                .is_some_and(|a| a.starts_with("--file-reporter="))
        })
    }) {
        fs::create_dir_all(Path::new(s(&value, "cwd")).join("build/tomacco"))?;
    }
    let label = s(&value, "label").to_string();
    value["timeout"] = a["timeout"].clone();
    value["single_active"] = json!(true);
    value["resource"] = json!("build");
    value["operation_key"] =
        json!({"operation":"project.execute","arguments":a,"project":value["cwd"]});
    jobs::submit(&label, value)
}
pub fn run(a: &Value) -> Result {
    let project = inspect(s(a, "path"))?;
    let device = identifier(s(a, "device"), "device identifier", None)?;
    let platform = s(a, "platform");
    let config = s(a, "configuration");
    let root = s(&project, "path");
    let mut env = serde_json::to_value(tc::environment()?)?;
    if device == crate::virtual_run::DEVICE {
        return crate::virtual_run::submit(&project, a, env);
    }
    if kind(&project, "flutter") {
        if platform == "ios" && !cfg!(target_os = "macos") {
            return Err(Error::new(
                "macos_required",
                "Flutter iOS run requires macOS",
            ));
        }
        return jobs::submit(
            &format!("Run {}", s(&project, "name")),
            json!({"steps":[{"argv":[tc::find("flutter")?,"run","-d",device,format!("--{config}"),"--machine"],"interactive":true,"protocol":"flutter-machine"}],"cwd":root,"env":env,"timeout":86400,"exclusive":root,"single_active":true}),
        );
    }
    if kind(&project, "xtool") && platform == "ios" {
        tc::find("go-ios")?;
        identifier(device, "iPhone UDID", Some("[A-Fa-f0-9-]{16,64}"))?;
        let launch = json!({"path":root,"device":device,"bundle_id":s(a,"bundle_id")});
        return jobs::submit(
            &format!("Deploy {}", s(&project, "name")),
            json!({"steps":[{"argv":[tc::find("xtool")?,"dev","run","--udid",device,"--configuration",config]},{"argv":[crate::core::own_executable()?,"_iphone_launch",launch.to_string()]}],"cwd":root,"env":env,"exclusive":root,"single_active":true,"resource":"build"}),
        );
    }
    if kind(&project, "kotlin-multiplatform") && platform == "ios" {
        return Err(kotlin_ios_error());
    }
    let framework = if platform == "android" {
        javascript::framework(
            &project["kinds"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>(),
        )
    } else {
        None
    };
    if framework == Some("expo") {
        return javascript::run_expo(&project, a, env);
    }
    if framework == Some("react-native") {
        javascript::prepare_react_native(&project)?;
    }
    let before = if framework == Some("capacitor") {
        javascript::capacitor_steps(&project)?
    } else {
        Vec::new()
    };
    if kind(&project, "gradle") && platform == "android" {
        let (task, id) = android_target(&project, a)?;
        let mut args = gradle(&project)?;
        args.extend(["--console=plain".into(), task]);
        env["ANDROID_SERIAL"] = json!(device);
        let launch = argv(&[
            &crate::core::own_executable()?.to_string_lossy(),
            "call",
            "android.launch",
            &json!({"device":device,"bundle_id":id}).to_string(),
        ]);
        let mut steps = before;
        steps.push(json!({"argv":args}));
        // A React Native debug build loads its JavaScript from Metro.
        let serves = framework == Some("react-native") && !release_build(a);
        if serves {
            steps.push(javascript::metro_step(&project, device, launch)?);
        } else {
            steps.push(json!({"argv":launch}));
        }
        let mut spec = json!({"steps":steps,"cwd":project["gradle_directory"],"env":env,"artifacts":["**/build/outputs/apk/**/*.apk"],"exclusive":root,"single_active":true});
        if serves {
            spec["timeout"] = json!(86400);
        } else {
            // A Metro session runs for hours, so only a plain install takes a build slot.
            spec["resource"] = json!("build");
        }
        return jobs::submit(&format!("Deploy {}", s(&project, "name")), spec);
    }
    Err(Error::new(
        "action_unsupported",
        "Use build, device.install and device.app for this project type",
    ))
}
fn kotlin_ios_error() -> Error {
    Error::new(
        "macos_required",
        "The iOS target of a Kotlin Multiplatform project builds only on macOS",
    )
    .remedy(gradle_model::KMP_IOS_NEEDS)
}
/// Whether a run installs a release build: the release configuration, or a
/// variant whose build type is release.
fn release_build(a: &Value) -> bool {
    let variant = s(a, "variant");
    if variant.is_empty() {
        s(a, "configuration") == "release"
    } else {
        variant.ends_with("Release") || variant == "release"
    }
}
fn capitalized(value: &str) -> String {
    let mut chars = value.chars();
    chars
        .next()
        .map(|c| c.to_uppercase().collect::<String>() + chars.as_str())
        .unwrap_or_default()
}
/// The Gradle install task and the application ID of an Android run: the
/// requested variant, or the configuration when the app has no flavors. The
/// ID is `bundle_id`, else the variant's ID from the build files when a
/// variant is named, else tomacco.json's applicationId, else the build files',
/// else a Capacitor config's appId.
fn android_target(project: &Value, a: &Value) -> Result<(String, String)> {
    let configuration = s(a, "configuration");
    let apps = project["android"]["app_modules"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let app = match apps.len() {
        1 => Some(&apps[0]),
        _ => apps.iter().find(|m| m["module"] == ":app"),
    };
    let variants: Vec<&Value> = app
        .and_then(|m| m["variants"].as_array())
        .map(|v| v.iter().collect())
        .unwrap_or_default();
    let names = |filter: &dyn Fn(&&Value) -> bool| {
        variants
            .iter()
            .filter(|v| filter(v))
            .filter_map(|v| v["name"].as_str())
            .collect::<Vec<_>>()
            .join(", ")
    };
    let requested = s(a, "variant");
    let variant = if requested.is_empty() {
        if app.is_some_and(|m| m["flavors"].as_array().is_some_and(|f| !f.is_empty())) {
            return Err(Error::new(
                "variant_required",
                "This app has product flavors; name the build variant to install",
            )
            .remedy(format!(
                "Pass variant, one of: {}",
                names(&|v| v["build_type"] == configuration)
            )));
        }
        configuration.to_string()
    } else {
        identifier(requested, "build variant", Some("[a-z][A-Za-z0-9]{0,99}"))?;
        if !variants.is_empty() && !variants.iter().any(|v| v["name"] == requested) {
            return Err(Error::new(
                "invalid_variant",
                format!("The build files have no variant {requested}"),
            )
            .remedy(format!("Use one of: {}", names(&|_| true))));
        }
        requested.to_string()
    };
    let module = app.map(|m| s(m, "module")).unwrap_or_default();
    let task = if module.is_empty() {
        format!("install{}", capitalized(&variant))
    } else {
        format!("{module}:install{}", capitalized(&variant))
    };
    let from_build = variants
        .iter()
        .find(|v| v["name"] == variant.as_str())
        .and_then(|v| v["application_id"].as_str())
        .unwrap_or_default();
    let configured = s(&project["configuration"], "applicationId");
    let id = [
        s(a, "bundle_id"),
        if requested.is_empty() { "" } else { from_build },
        configured,
        from_build,
        s(&project["javascript"], "app_id"),
    ]
    .into_iter()
    .find(|id| !id.is_empty())
    .ok_or_else(|| {
        let known: Vec<_> = apps
            .iter()
            .map(|m| format!("{} {}", s(m, "module"), m["application_id"]))
            .collect();
        Error::new(
            "application_id_unknown",
            "The build files do not state this app's application ID",
        )
        .remedy(if known.len() > 1 {
            format!(
                "Several app modules ({}): pass bundle_id, or set applicationId with project.configure",
                known.join(", ")
            )
        } else {
            "Pass bundle_id, or set applicationId with project.configure".to_string()
        })
    })?;
    identifier(
        id,
        "application ID",
        Some(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+"),
    )?;
    Ok((task, id.to_string()))
}
fn write(root: &Path, relative: &str, content: &str) -> Result<()> {
    let file = root.join(relative);
    fs::create_dir_all(file.parent().unwrap())?;
    fs::write(file, content)?;
    Ok(())
}
pub fn create(a: &Value) -> Result {
    let target = path(s(a, "path"), false, false)?;
    let name = identifier(
        s(a, "name"),
        "project name",
        Some("[A-Za-z][A-Za-z0-9_]{0,49}"),
    )?;
    let organization = identifier(
        s(a, "organization"),
        "organization",
        Some(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+"),
    )?;
    let kind = s(a, "kind");
    if b(a, "build") && kind != "xtool" {
        return Err(Error::new(
            "invalid_argument",
            "build:true is for kind xtool; other kinds build with project.execute",
        ));
    }
    if b(a, "performance") && kind != "compose" {
        return Err(Error::new(
            "invalid_argument",
            "performance:true is for kind compose",
        ));
    }
    if target.exists() {
        return Err(Error::new(
            "path_exists",
            format!("Choose a new directory: {}", target.display()),
        ));
    }
    fs::create_dir_all(target.parent().unwrap())?;
    let bundle = format!("{organization}.{}", name.to_lowercase());
    if kind == "flutter" {
        return jobs::submit(
            "Create Flutter project",
            json!({"steps":[{"argv":[tc::find("flutter")?,"create","--org",organization,"--project-name",name.to_lowercase(),"--platforms","android,ios,web,linux",target]}],"cwd":target.parent(),"env":tc::environment()?}),
        );
    }
    if kind == "xtool" {
        let directory = target.file_name().unwrap_or_default().to_string_lossy();
        identifier(
            &directory,
            "xtool directory name",
            Some("[A-Za-z][A-Za-z0-9_]{0,49}"),
        )?;
        let xtool = tc::find("xtool")?;
        let mut steps = vec![
            json!({"argv":[xtool,"new",directory,"--skip-setup"]}),
            json!({"argv":[crate::core::own_executable()?,"call","project.configure",json!({"path":target,"application_id":bundle}).to_string()]}),
        ];
        if b(a, "build") {
            steps.push(json!({"argv":[xtool,"dev","build"],"cwd":target}));
        }
        return jobs::submit(
            "Create iOS project",
            json!({"steps":steps,"cwd":target.parent(),"env":tc::environment()?,"metadata":{"project":target,"next":{"operation":"project.app","arguments":{"path":target}}}}),
        );
    }
    let sdk = tc::android_sdk()?.ok_or_else(|| {
        Error::new("sdk_missing", "Configure the Android SDK first")
            .remedy("Set android_sdk with settings.configure")
    })?;
    tc::find("gradle")?;
    let api = glob_paths(&format!(
        "{}/platforms/android-*",
        glob::Pattern::escape(&sdk.to_string_lossy())
    ))?
    .iter()
    .filter_map(|p| {
        p.file_name()?
            .to_str()?
            .strip_prefix("android-")?
            .parse::<u64>()
            .ok()
    })
    .max()
    .ok_or_else(|| {
        Error::new(
            "sdk_platform_missing",
            "Install an Android platform using android.sdk_install",
        )
    })?;
    if kind == "compose" && api < 36 {
        return Err(Error::new(
            "sdk_platform_missing",
            format!("The Compose starter needs Android platform 36 or newer; the newest installed is {api}"),
        )
        .remedy("Install platforms;android-36 using android.sdk_install"));
    }
    // Stage a complete project beside the destination, then publish it atomically.
    let staging = tempfile::Builder::new()
        .prefix(".tomacco-project-")
        .tempdir_in(target.parent().unwrap())?;
    let root = staging.path();
    write(
        root,
        "settings.gradle.kts",
        &format!(
            "pluginManagement {{ repositories {{ google(); mavenCentral(); gradlePluginPortal() }} }}\ndependencyResolutionManagement {{ repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS); repositories {{ google(); mavenCentral() }} }}\nrootProject.name = \"{name}\"\ninclude(\":app\")\n"
        ),
    )?;
    let manifest = format!(
        "<manifest xmlns:android=\"http://schemas.android.com/apk/res/android\"><application android:label=\"{name}\" android:theme=\"@android:style/Theme.Material.Light.NoActionBar\" android:allowBackup=\"false\"><activity android:name=\".MainActivity\" android:exported=\"true\"><intent-filter><action android:name=\"android.intent.action.MAIN\"/><category android:name=\"android.intent.category.LAUNCHER\"/></intent-filter></activity></application></manifest>\n"
    );
    write(root, "app/src/main/AndroidManifest.xml", &manifest)?;
    let package = bundle.replace('.', "/");
    if kind == "compose" {
        // Kotlin with Jetpack Compose, through AGP's built-in Kotlin support.
        let fill = |text: &str| {
            text.replace("__BUNDLE__", &bundle)
                .replace("__NAME__", name)
                .replace("__API__", &api.to_string())
        };
        let performance = b(a, "performance");
        let mut root_plugins = String::from(
            "plugins {\n    alias(libs.plugins.android.application) apply false\n    alias(libs.plugins.kotlin.compose) apply false\n",
        );
        let mut catalog =
            include_str!("../assets/templates/compose/libs.versions.toml").to_string();
        let mut app = fill(include_str!("../assets/templates/compose/build.gradle.kts"));
        if performance {
            root_plugins.push_str("    alias(libs.plugins.android.test) apply false\n    alias(libs.plugins.androidx.baselineprofile) apply false\n");
            catalog = catalog
                .replace(
                    "\n[libraries]",
                    "benchmark = \"1.5.0\"\nbaselineprofile = \"1.5.0\"\nprofileinstaller = \"1.4.1\"\nuiautomator = \"2.4.0\"\n\n[libraries]",
                )
                .replace(
                    "\n[plugins]",
                    "androidx-benchmark-macro-junit4 = { group = \"androidx.benchmark\", name = \"benchmark-macro-junit4\", version.ref = \"benchmark\" }\nandroidx-profileinstaller = { group = \"androidx.profileinstaller\", name = \"profileinstaller\", version.ref = \"profileinstaller\" }\nandroidx-uiautomator = { group = \"androidx.test.uiautomator\", name = \"uiautomator\", version.ref = \"uiautomator\" }\n\n[plugins]",
                );
            catalog.push_str("android-test = { id = \"com.android.test\", version.ref = \"agp\" }\nandroidx-baselineprofile = { id = \"androidx.baselineprofile\", version.ref = \"baselineprofile\" }\n");
            app = app
                .replace(
                    "    alias(libs.plugins.kotlin.compose)\n",
                    "    alias(libs.plugins.kotlin.compose)\n    alias(libs.plugins.androidx.baselineprofile)\n",
                )
                .replace(
                    "dependencies {\n",
                    "dependencies {\n    implementation(libs.androidx.profileinstaller)\n    \"baselineProfile\"(project(\":baselineprofile\"))\n",
                );
            let settings = fs::read_to_string(root.join("settings.gradle.kts"))?;
            write(
                root,
                "settings.gradle.kts",
                &settings.replace(
                    "include(\":app\")",
                    "include(\":app\")\ninclude(\":baselineprofile\")",
                ),
            )?;
            write(
                root,
                "baselineprofile/build.gradle.kts",
                &fill(include_str!(
                    "../assets/templates/compose-performance/build.gradle.kts"
                )),
            )?;
            write(
                root,
                "baselineprofile/src/main/AndroidManifest.xml",
                "<manifest xmlns:android=\"http://schemas.android.com/apk/res/android\" />\n",
            )?;
            write(
                root,
                &format!(
                    "baselineprofile/src/main/java/{package}/baselineprofile/BaselineProfileGenerator.kt"
                ),
                &fill(include_str!(
                    "../assets/templates/compose-performance/BaselineProfileGenerator.kt"
                )),
            )?;
            write(
                root,
                &format!(
                    "baselineprofile/src/main/java/{package}/baselineprofile/StartupBenchmarks.kt"
                ),
                &fill(include_str!(
                    "../assets/templates/compose-performance/StartupBenchmarks.kt"
                )),
            )?;
        }
        root_plugins.push_str("}\n");
        write(root, "build.gradle.kts", &root_plugins)?;
        write(root, "gradle/libs.versions.toml", &catalog)?;
        write(root, "app/build.gradle.kts", &app)?;
        write(
            root,
            "gradle.properties",
            "org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8\nandroid.useAndroidX=true\n",
        )?;
        write(
            root,
            &format!("app/src/main/java/{package}/MainActivity.kt"),
            &fill(include_str!("../assets/templates/compose/MainActivity.kt")),
        )?;
        write(
            root,
            &format!("app/src/androidTest/java/{package}/CounterTest.kt"),
            &fill(include_str!("../assets/templates/compose/CounterTest.kt")),
        )?;
    } else {
        write(
            root,
            "build.gradle.kts",
            "plugins { id(\"com.android.application\") version \"9.1.1\" apply false }\n",
        )?;
        let mut gradle = format!(
            "plugins {{ id(\"com.android.application\") }}\nandroid {{\n namespace = \"{bundle}\"\n compileSdk = {api}\n defaultConfig {{ applicationId = \"{bundle}\"; minSdk = 26; targetSdk = {api}; versionCode = 1; versionName = \"1.0\" }}\n}}\n"
        );
        if kind == "swift-android" {
            gradle.push_str(include_str!("../assets/templates/swift.gradle.kts"));
        }
        write(root, "app/build.gradle.kts", &gradle)?;
        write(
            root,
            "gradle.properties",
            "org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8\n",
        )?;
        let mut java = include_str!("../assets/templates/MainActivity.java")
            .replace("__BUNDLE__", &bundle)
            .replace("__NAME__", name);
        if kind != "swift-android" {
            java=java.replace("    static { System.loadLibrary(\"TomaccoCore\"); }\n    private static native int nextCount(int current);\n","").replace("count = nextCount(count);","count++;");
        }
        write(
            root,
            &format!("app/src/main/java/{package}/MainActivity.java"),
            &java,
        )?;
    }
    let mut config = json!({"applicationId":bundle,"tasks":{}});
    write(
        root,
        ".gitignore",
        ".gradle/\nbuild/\napp/build/\nlocal.properties\nSwift/.build/\nSwift/.swiftpm/\n",
    )?;
    if kind == "swift-android" {
        write(
            root,
            "Swift/Package.swift",
            include_str!("../assets/templates/Swift/Package.swift"),
        )?;
        let jni = format!(
            "Java_{}_MainActivity_nextCount",
            bundle.replace('_', "_1").replace('.', "_")
        );
        write(
            root,
            "Swift/Sources/TomaccoCore/Counter.swift",
            &include_str!("../assets/templates/Swift/Sources/TomaccoCore/Counter.swift")
                .replace("__JNI__", &jni),
        )?;
        write(
            root,
            "Swift/Tests/TomaccoCoreTests/CounterTests.swift",
            include_str!("../assets/templates/Swift/Tests/TomaccoCoreTests/CounterTests.swift"),
        )?;
        config["swift"] = json!({"package":"Swift","sdk":"swift-6.4.0-RELEASE_android","api":26,"abis":["arm64-v8a","x86_64"]});
        config["tasks"] = json!({"swift-test":["swift","test","--package-path","Swift"]});
    }
    atomic_json(&root.join("tomacco.json"), &config)?;
    fs::rename(root, &target)?;
    Ok(json!({"created":target,"application_id":bundle,"platform":"android","api":api}))
}

#[cfg(test)]
mod configuration_tests {
    use super::*;

    #[test]
    fn a_project_is_configured_by_its_tomacco_json() {
        let project = tempfile::tempdir().unwrap();
        let current = project.path().join("tomacco.json");
        assert_eq!(config_path(project.path()), current);
        fs::write(&current, br#"{"tasks":{"check":["true"]}}"#).unwrap();
        let inspected = inspect(&project.path().to_string_lossy()).unwrap();
        assert_eq!(inspected["configuration"]["tasks"]["check"][0], "true");
    }

    fn fixture(files: &[(&str, &str)]) -> tempfile::TempDir {
        let project = tempfile::tempdir().unwrap();
        for (relative, text) in files {
            let file = project.path().join(relative);
            fs::create_dir_all(file.parent().unwrap()).unwrap();
            fs::write(file, text).unwrap();
        }
        project
    }

    #[test]
    fn inspect_reads_the_app_id_and_sdk_levels_from_the_build_files() {
        let project = fixture(&[
            ("settings.gradle.kts", "include(\":app\")\n"),
            (
                "app/build.gradle.kts",
                "plugins { id(\"com.android.application\") }\nandroid { namespace = \"dev.x\"; compileSdk = 36\n defaultConfig { applicationId = \"dev.x.app\"; minSdk = 26; targetSdk = 36 } }\n",
            ),
        ]);
        let inspected = inspect(&project.path().to_string_lossy()).unwrap();
        let app = &inspected["android"]["app_modules"][0];
        assert_eq!(app["application_id"], "dev.x.app");
        assert_eq!(app["min_sdk"], 26);
        assert_eq!(app["target_sdk"], 36);
        assert_eq!(app["compile_sdk"], 36);
        assert_eq!(
            inspected["android"]["application_ids"],
            json!(["dev.x.app"])
        );
        let (task, id) = android_target(&inspected, &json!({"configuration":"debug"})).unwrap();
        assert_eq!(
            (task.as_str(), id.as_str()),
            (":app:installDebug", "dev.x.app")
        );
        let (task, id) = android_target(
            &inspected,
            &json!({"configuration":"release","bundle_id":"dev.other"}),
        )
        .unwrap();
        assert_eq!(
            (task.as_str(), id.as_str()),
            (":app:installRelease", "dev.other")
        );
    }

    #[test]
    fn a_flavored_app_needs_a_variant_and_runs_the_variants_id() {
        let project = fixture(&[
            ("settings.gradle", "include ':app'\n"),
            (
                "app/build.gradle",
                "apply plugin: 'com.android.application'\nandroid {\n defaultConfig { applicationId 'dev.f' }\n buildTypes { debug { applicationIdSuffix '.debug' } }\n flavorDimensions 'tier'\n productFlavors {\n  free { applicationIdSuffix '.free' }\n  paid { }\n }\n}\n",
            ),
            ("tomacco.json", "{\"applicationId\":\"dev.f\"}"),
        ]);
        let inspected = inspect(&project.path().to_string_lossy()).unwrap();
        let error = android_target(&inspected, &json!({"configuration":"debug"})).unwrap_err();
        assert_eq!(error.code, "variant_required");
        assert!(error.remedy.contains("freeDebug, paidDebug"));
        let error = android_target(
            &inspected,
            &json!({"configuration":"debug","variant":"proDebug"}),
        )
        .unwrap_err();
        assert_eq!(error.code, "invalid_variant");
        let (task, id) = android_target(
            &inspected,
            &json!({"configuration":"debug","variant":"freeDebug"}),
        )
        .unwrap();
        assert_eq!(
            (task.as_str(), id.as_str()),
            (":app:installFreeDebug", "dev.f.free.debug")
        );
    }

    #[test]
    fn without_an_id_in_the_build_files_the_run_says_how_to_give_one() {
        let project = fixture(&[("build.gradle", "plugins { id 'my.android.application' }\n")]);
        let inspected = inspect(&project.path().to_string_lossy()).unwrap();
        let error = android_target(&inspected, &json!({"configuration":"debug"})).unwrap_err();
        assert_eq!(error.code, "application_id_unknown");
        assert!(error.remedy.contains("bundle_id"));
        let (task, _) = android_target(
            &inspected,
            &json!({"configuration":"debug","bundle_id":"dev.y"}),
        )
        .unwrap();
        assert_eq!(task, "installDebug");
    }

    #[test]
    fn a_kotlin_multiplatform_project_runs_android_and_explains_ios() {
        let project = fixture(&[
            ("settings.gradle.kts", "include(\":composeApp\")\n"),
            (
                "composeApp/build.gradle.kts",
                "plugins { kotlin(\"multiplatform\"); id(\"com.android.application\"); id(\"org.jetbrains.compose\") }\nkotlin { androidTarget(); iosArm64(); iosSimulatorArm64() }\nandroid { namespace = \"dev.kmp\"; defaultConfig { applicationId = \"dev.kmp.app\" } }\n",
            ),
        ]);
        let path = project.path().to_string_lossy().to_string();
        let inspected = inspect(&path).unwrap();
        assert_eq!(
            inspected["kinds"],
            json!(["gradle", "kotlin-multiplatform", "compose-multiplatform"])
        );
        assert_eq!(inspected["platforms"], json!(["android", "ios"]));
        assert_eq!(inspected["multiplatform"]["ios"]["builds_on_linux"], false);
        let (task, id) = android_target(&inspected, &json!({"configuration":"debug"})).unwrap();
        assert_eq!(
            (task.as_str(), id.as_str()),
            (":composeApp:installDebug", "dev.kmp.app")
        );
        let error = plan(&json!({"path":path,"platform":"ios","action":"build"})).unwrap_err();
        assert_eq!(error.code, "macos_required");
        assert!(error.remedy.contains("remote.configure"));
        let error =
            run(&json!({"path":path,"platform":"ios","device":"00008110-0011223344556677"}))
                .unwrap_err();
        assert_eq!(error.code, "macos_required");
    }

    #[test]
    fn gradle_tests_get_coverage_tasks_managed_devices_and_a_report_cleanup() {
        let project = fixture(&[
            ("settings.gradle.kts", "include(\":app\")"),
            ("gradlew", "#!/bin/sh\n"),
            (
                "app/build.gradle.kts",
                "plugins { id(\"com.android.application\") }\nandroid {\n namespace = \"dev.example\"\n defaultConfig { applicationId = \"dev.example\" }\n buildTypes { debug { enableUnitTestCoverage = true } }\n testOptions { managedDevices { localDevices { create(\"pixel2api30\") { device = \"Pixel 2\"; apiLevel = 30 } }\n groups { create(\"phones\") { } } } }\n}",
            ),
        ]);
        let path = project.path();
        let argv = |a: Value| -> Result<Vec<String>> {
            let mut a = a;
            a["path"] = json!(path);
            a["platform"] = json!("android");
            for (key, value) in [
                ("configuration", "debug"),
                ("format", "apk"),
                ("managed_device", ""),
                ("device", ""),
            ] {
                if a.get(key).is_none() {
                    a[key] = json!(value);
                }
            }
            plan(&a).map(|p| strings(&p["steps"][0]["argv"]))
        };
        let tail = |args: Vec<String>| {
            let start = args.iter().position(|a| a == "--console=plain").unwrap() + 1;
            args[start..].join(" ")
        };
        assert_eq!(
            tail(argv(json!({"action":"test","coverage":true})).unwrap()),
            "testDebugUnitTest createDebugUnitTestCoverageReport"
        );
        assert_eq!(
            argv(json!({"action":"instrumented-test","coverage":true}))
                .unwrap_err()
                .code,
            "coverage_not_configured"
        );
        assert_eq!(
            tail(
                argv(
                    json!({"action":"instrumented-test","managed_device":"pixel2api30","shards":2})
                )
                .unwrap()
            ),
            "-Pandroid.experimental.androidTest.numManagedDeviceShards=2 pixel2api30DebugAndroidTest"
        );
        assert_eq!(
            tail(argv(json!({"action":"instrumented-test","managed_device":"phones"})).unwrap()),
            "phonesGroupDebugAndroidTest"
        );
        assert_eq!(
            tail(argv(json!({"action":"instrumented-test","managed_device":"all"})).unwrap()),
            "allDevicesDebugAndroidTest"
        );
        let missing =
            argv(json!({"action":"instrumented-test","managed_device":"tablet"})).unwrap_err();
        assert_eq!(missing.code, "managed_device_not_found");
        assert_eq!(
            argv(json!({"action":"test","managed_device":"pixel2api30"}))
                .unwrap_err()
                .code,
            "invalid_argument"
        );
        assert_eq!(
            argv(json!({"action":"instrumented-test","shards":2}))
                .unwrap_err()
                .code,
            "invalid_argument"
        );
        assert_eq!(
            argv(json!({"action":"build","coverage":true}))
                .unwrap_err()
                .code,
            "action_unsupported"
        );
        let planned = plan(&json!({"path":path,"platform":"android","action":"test","configuration":"debug","format":"apk","managed_device":"","device":""})).unwrap();
        assert_eq!(strings(&planned["cleanup"][0]["argv"])[1], "_test_report");
        assert!(
            strings(&planned["always_artifacts"])
                .iter()
                .any(|a| a.ends_with(TEST_REPORT_HTML))
        );
        let built = plan(&json!({"path":path,"platform":"android","action":"build","configuration":"debug","format":"apk","managed_device":"","device":""})).unwrap();
        assert!(built.get("cleanup").is_none());
        let inspected = inspect(&path.to_string_lossy()).unwrap();
        assert_eq!(
            inspected["test_setup"]["modules"][0]["managed_devices"][0]["name"],
            "pixel2api30"
        );
    }
}
