//! Test impact selection for a Gradle build: from the files a change
//! touches (git diff against a base, or a given list), the unit and
//! instrumented test classes and testing.run scenarios likely affected,
//! by simple rules over modules, packages and names. It reads the build and
//! source files without running Gradle; it can miss a test that reaches the
//! changed code by reflection, dependency injection or a generated name, so
//! a full run before a release still matters.
use crate::{core::*, gradle_model, jobs, projects, toolchain as tc};
use regex::Regex;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};

/// Test source files read per build, and bytes read per file.
const MAX_TEST_FILES: usize = 20_000;
const MAX_FILE_BYTES: u64 = 1024 * 1024;
const MAX_CHANGED: usize = 5_000;

#[derive(Clone, Copy, PartialEq, Debug)]
enum Kind {
    Unit,
    Instrumented,
    Main,
}

struct Module {
    path: String,
    dir: PathBuf,
    android: bool,
    script: String,
}

struct TestFile {
    module: usize,
    kind: Kind,
    class: String,
    simple: String,
    package: String,
    text: String,
}

/// The source set a path inside a module belongs to, and the rest after
/// `src/SET/`.
fn source_set(relative: &Path) -> Option<(Kind, String, PathBuf)> {
    let mut parts = relative.components();
    if parts.next()?.as_os_str() != "src" {
        return None;
    }
    let set = parts.next()?.as_os_str().to_string_lossy().into_owned();
    let rest: PathBuf = parts.collect();
    let kind = if set.starts_with("androidTest")
        || set.contains("InstrumentedTest")
        || set.contains("DeviceTest")
    {
        Kind::Instrumented
    } else if set.starts_with("test") || set.ends_with("Test") {
        Kind::Unit
    } else {
        Kind::Main
    };
    Some((kind, set, rest))
}

fn is_code(path: &Path) -> bool {
    path.extension().is_some_and(|e| e == "kt" || e == "java")
}

/// The package a source file declares, or the one its directories name.
fn package_of(rest: &Path, text: Option<&str>) -> String {
    if let Some(found) = text.and_then(|t| {
        Regex::new(r"(?m)^\s*package\s+([\w.]+)")
            .unwrap()
            .captures(t)
            .map(|c| c[1].to_string())
    }) {
        return found;
    }
    let parts: Vec<String> = rest
        .parent()
        .unwrap_or(Path::new(""))
        .components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .collect();
    let start = parts
        .iter()
        .position(|p| p == "java" || p == "kotlin")
        .map_or(0, |i| i + 1);
    parts[start.min(parts.len())..].join(".")
}

fn read_limited(file: &Path) -> Option<String> {
    let meta = fs::metadata(file).ok()?;
    if meta.len() > MAX_FILE_BYTES {
        return None;
    }
    fs::read_to_string(file).ok()
}

fn is_android(script: &str) -> bool {
    script.contains("com.android.")
        || Regex::new(r"libs\.plugins\.android")
            .unwrap()
            .is_match(script)
        || !gradle_model::blocks(script, "android").is_empty()
}

fn normalized(name: &str) -> String {
    name.chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == ':')
        .collect::<String>()
        .to_lowercase()
}

/// For each module, the modules that depend on it, directly or not, from
/// `project(":x")` and `projects.x` in their build files.
fn dependents(modules: &[Module]) -> Vec<BTreeSet<usize>> {
    let by_name: BTreeMap<String, usize> = modules
        .iter()
        .enumerate()
        .map(|(i, m)| (normalized(&m.path), i))
        .collect();
    let project = Regex::new(r#"project\(\s*(?:path\s*[:=]\s*)?["'](:[\w.:-]+)["']"#).unwrap();
    let accessor = Regex::new(r"\bprojects\.([\w.]+)").unwrap();
    let mut direct: Vec<BTreeSet<usize>> = vec![BTreeSet::new(); modules.len()];
    for (index, module) in modules.iter().enumerate() {
        let mut names: Vec<String> = project
            .captures_iter(&module.script)
            .map(|c| normalized(&c[1]))
            .collect();
        names.extend(
            accessor
                .captures_iter(&module.script)
                .map(|c| normalized(&format!(":{}", c[1].replace('.', ":")))),
        );
        for name in names {
            if let Some(&used) = by_name.get(&name) {
                if used != index {
                    direct[used].insert(index);
                }
            }
        }
    }
    (0..modules.len())
        .map(|start| {
            let mut seen = BTreeSet::new();
            let mut queue = vec![start];
            while let Some(at) = queue.pop() {
                for &next in &direct[at] {
                    if seen.insert(next) {
                        queue.push(next);
                    }
                }
            }
            seen.remove(&start);
            seen
        })
        .collect()
}

/// The changed files, relative to the project: given, or from git.
fn changed_files(root: &Path, a: &Value) -> Result<(Vec<String>, String)> {
    let given = strings(&a["files"]);
    if !given.is_empty() {
        let files = given
            .into_iter()
            .map(|f| {
                let p = PathBuf::from(&f);
                p.strip_prefix(root)
                    .map(|r| r.to_string_lossy().into_owned())
                    .unwrap_or(f)
            })
            .collect();
        return Ok((files, "files".into()));
    }
    let base = match s(a, "base") {
        "" => "HEAD",
        base => base,
    };
    identifier(
        base,
        "git revision",
        Some(r"[A-Za-z0-9][A-Za-z0-9_./~^@{}-]{0,199}"),
    )?;
    let git = tc::find("git")?;
    let run = |args: &[&str]| -> Result<Vec<String>> {
        let mut command = vec![
            git.clone(),
            "-C".into(),
            root.to_string_lossy().into_owned(),
        ];
        command.extend(args.iter().map(|a| a.to_string()));
        let output = tc::run(&command, 60, None, true).map_err(|error| {
            Error::new(
                "git_failed",
                format!("git {} failed in {}", args.join(" "), root.display()),
            )
            .remedy("Give files, the changed paths, when the project is not a git checkout or the base is unknown")
            .details(error.value())
        })?;
        Ok(s(&output, "stdout")
            .lines()
            .filter(|l| !l.trim().is_empty())
            .map(str::to_string)
            .collect())
    };
    let mut files = run(&["diff", "--name-only", "--relative", base, "--"])?;
    files.extend(run(&["ls-files", "--others", "--exclude-standard"])?);
    files.sort();
    files.dedup();
    Ok((files, base.to_string()))
}

#[derive(Default)]
struct Selection {
    run_all: Vec<String>,
    /// (module, kind) -> class -> reasons
    classes: BTreeMap<(usize, u8), BTreeMap<String, BTreeSet<String>>>,
    /// (module, kind) -> reasons for running all of them
    whole: BTreeMap<(usize, u8), BTreeSet<String>>,
    touched: BTreeSet<usize>,
}

fn kind_key(kind: Kind) -> u8 {
    if kind == Kind::Instrumented { 1 } else { 0 }
}

fn plan(
    root: &Path,
    gradle_dir: &Path,
    changed: &[String],
) -> (Selection, Vec<Module>, Vec<Value>) {
    let mut modules: Vec<Module> = gradle_model::modules_of(gradle_dir)
        .into_iter()
        .filter_map(|(name, dir)| {
            let script = gradle_model::build_file(&dir)
                .and_then(|p| gradle_model::read_text(&p))
                .map(|t| gradle_model::strip_comments(&t))?;
            Some(Module {
                path: if name.is_empty() {
                    ":".into()
                } else {
                    format!(":{name}")
                },
                android: is_android(&script),
                dir,
                script,
            })
        })
        .collect();
    // The deepest module first, so a file belongs to its own module.
    modules.sort_by_key(|m| std::cmp::Reverse(m.dir.components().count()));
    let reverse = dependents(&modules);
    let mut tests = Vec::new();
    for (index, module) in modules.iter().enumerate() {
        let base = glob::Pattern::escape(&module.dir.to_string_lossy());
        for pattern in [
            "src/*/java/**/*.kt",
            "src/*/java/**/*.java",
            "src/*/kotlin/**/*.kt",
            "src/*/kotlin/**/*.java",
        ] {
            for file in glob_paths(&format!("{base}/{pattern}")).unwrap_or_default() {
                if tests.len() >= MAX_TEST_FILES {
                    break;
                }
                let Ok(relative) = file.strip_prefix(&module.dir) else {
                    continue;
                };
                // A file of a nested module is that module's.
                if modules[..index]
                    .iter()
                    .any(|m| file.starts_with(&m.dir) && m.dir != module.dir)
                {
                    continue;
                }
                let Some((kind, _, rest)) = source_set(relative) else {
                    continue;
                };
                if kind == Kind::Main {
                    continue;
                }
                let text = read_limited(&file).unwrap_or_default();
                let package = package_of(&rest, Some(&text));
                let simple = file
                    .file_stem()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .into_owned();
                tests.push(TestFile {
                    module: index,
                    kind,
                    class: if package.is_empty() {
                        simple.clone()
                    } else {
                        format!("{package}.{simple}")
                    },
                    simple,
                    package,
                    text,
                });
            }
        }
    }
    let mut selection = Selection::default();
    let mut unmapped = Vec::new();
    let build_wide = Regex::new(r"(^|/)(settings\.gradle(\.kts)?|gradle\.properties|libs\.versions\.toml|gradlew(\.bat)?)$|(^|/)(gradle|buildSrc|build-logic)/").unwrap();
    for relative in changed.iter().take(MAX_CHANGED) {
        let file = root.join(relative);
        let in_gradle = file.starts_with(gradle_dir);
        if in_gradle
            && build_wide.is_match(
                &file
                    .strip_prefix(gradle_dir)
                    .unwrap_or(&file)
                    .to_string_lossy(),
            )
        {
            selection
                .run_all
                .push(format!("{relative}: build setup changed"));
            continue;
        }
        let Some(index) = modules.iter().position(|m| file.starts_with(&m.dir)) else {
            unmapped.push(json!({"file":relative,"reason":"outside the Gradle build"}));
            continue;
        };
        let module = &modules[index];
        let inside = file
            .strip_prefix(&module.dir)
            .unwrap_or(&file)
            .to_path_buf();
        let name = inside.to_string_lossy();
        if name == "build.gradle" || name == "build.gradle.kts" || name == "proguard-rules.pro" {
            if module.path == ":" {
                selection
                    .run_all
                    .push(format!("{relative}: root build file changed"));
                continue;
            }
            for target in std::iter::once(index).chain(reverse[index].iter().copied()) {
                selection.touched.insert(target);
                for kind in [0, 1] {
                    selection
                        .whole
                        .entry((target, kind))
                        .or_default()
                        .insert(format!("{relative}: build file of {} changed", module.path));
                }
            }
            continue;
        }
        let Some((kind, _, rest)) = source_set(&inside) else {
            unmapped
                .push(json!({"file":relative,"module":module.path,"reason":"not in a source set"}));
            continue;
        };
        if kind != Kind::Main {
            if is_code(&file) && file.is_file() {
                let text = read_limited(&file);
                let package = package_of(&rest, text.as_deref());
                let simple = file.file_stem().unwrap_or_default().to_string_lossy();
                let class = if package.is_empty() {
                    simple.to_string()
                } else {
                    format!("{package}.{simple}")
                };
                // A helper without tests of its own still changes the tests that use it.
                let own = tests.iter().any(|t| t.class == class && t.module == index);
                selection
                    .classes
                    .entry((index, kind_key(kind)))
                    .or_default()
                    .entry(class)
                    .or_default()
                    .insert(format!("{relative}: test changed"));
                if !own {
                    unmapped.push(json!({"file":relative,"module":module.path,"reason":"test source without a test class of that name"}));
                }
            } else if !file.exists() {
                unmapped.push(
                    json!({"file":relative,"module":module.path,"reason":"deleted test file"}),
                );
            } else {
                selection
                    .whole
                    .entry((index, kind_key(kind)))
                    .or_default()
                    .insert(format!("{relative}: test resources changed"));
            }
            continue;
        }
        let affected: Vec<usize> = std::iter::once(index)
            .chain(reverse[index].iter().copied())
            .collect();
        selection.touched.extend(affected.iter().copied());
        if !is_code(&file) {
            let resources =
                rest.starts_with("res") || rest.to_string_lossy() == "AndroidManifest.xml";
            let reason = if resources {
                format!("{relative}: resources or manifest changed")
            } else {
                format!("{relative}: non-code source changed")
            };
            for &target in &affected {
                selection
                    .whole
                    .entry((target, 1))
                    .or_default()
                    .insert(reason.clone());
                if !resources {
                    selection
                        .whole
                        .entry((target, 0))
                        .or_default()
                        .insert(reason.clone());
                }
            }
            continue;
        }
        let text = read_limited(&file);
        let package = package_of(&rest, text.as_deref());
        let simple = file
            .file_stem()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned();
        let names = [
            format!("{simple}Test"),
            format!("{simple}Tests"),
            format!("{simple}IT"),
            format!("{simple}Spec"),
            format!("Test{simple}"),
        ];
        let word = Regex::new(&format!(r"\b{}\b", regex::escape(&simple))).unwrap();
        let imported = Regex::new(&format!(
            r"(?m)^\s*import\s+{}\.(?:{}\b|\*)",
            regex::escape(&package),
            regex::escape(&simple)
        ))
        .unwrap();
        let mut matched = false;
        for test in tests.iter().filter(|t| affected.contains(&t.module)) {
            let reason = if names.contains(&test.simple)
                && (test.package == package
                    || imported.is_match(&test.text)
                    || test.module == index)
            {
                format!("{relative}: named after {simple}")
            } else if word.is_match(&test.text)
                && (test.package == package || imported.is_match(&test.text))
            {
                format!("{relative}: uses {simple}")
            } else {
                continue;
            };
            matched = true;
            selection
                .classes
                .entry((test.module, kind_key(test.kind)))
                .or_default()
                .entry(test.class.clone())
                .or_default()
                .insert(reason);
        }
        if !matched {
            // Top-level functions and the like: the tests of its package.
            for test in tests
                .iter()
                .filter(|t| t.module == index && t.package == package && !package.is_empty())
            {
                matched = true;
                selection
                    .classes
                    .entry((test.module, kind_key(test.kind)))
                    .or_default()
                    .entry(test.class.clone())
                    .or_default()
                    .insert(format!("{relative}: same package {package}"));
            }
        }
        if !matched {
            unmapped.push(
                json!({"file":relative,"module":module.path,"reason":"no test names or uses it"}),
            );
        }
    }
    (selection, modules, unmapped)
}

/// The application IDs of the app modules a change touches.
fn affected_apps(gradle_dir: &Path, modules: &[Module], selection: &Selection) -> BTreeSet<String> {
    let model = gradle_model::read(gradle_dir);
    let mut ids = BTreeSet::new();
    for app in model["app_modules"].as_array().into_iter().flatten() {
        let path = match s(app, "module") {
            "" => ":",
            p => p,
        };
        let touched = !selection.run_all.is_empty()
            || modules
                .iter()
                .enumerate()
                .any(|(i, m)| m.path == path && selection.touched.contains(&i));
        if !touched {
            continue;
        }
        if let Some(id) = app["application_id"].as_str() {
            ids.insert(id.to_string());
        }
        for variant in app["variants"].as_array().into_iter().flatten() {
            if let Some(id) = variant["application_id"].as_str() {
                ids.insert(id.to_string());
            }
        }
    }
    ids
}

fn scenarios(at: &Path, apps: &BTreeSet<String>, run_all: bool) -> Result<Vec<Value>> {
    let files: Vec<PathBuf> = if at.is_dir() {
        let mut files: Vec<PathBuf> = fs::read_dir(at)?
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| p.is_file() && p.extension().is_some_and(|e| e == "json"))
            .collect();
        files.sort();
        files
    } else {
        vec![at.to_path_buf()]
    };
    let mut found = Vec::new();
    for file in files {
        let Ok(scenario) = read_json(&file, None) else {
            continue;
        };
        let used: BTreeSet<String> = scenario["steps"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|step| step["bundle_id"].as_str().map(str::to_string))
            .collect();
        let reason = if run_all {
            Some("build setup changed".to_string())
        } else if used.is_empty() {
            (!apps.is_empty()).then(|| "names no app; an app changed".to_string())
        } else {
            used.intersection(apps)
                .next()
                .map(|id| format!("uses {id}, whose module or its dependencies changed"))
        };
        if let Some(reason) = reason {
            found.push(json!({"path":file,"name":scenario["name"],"reason":reason}));
        }
    }
    Ok(found)
}

fn task_prefix(module: &Module) -> String {
    if module.path == ":" {
        ":".into()
    } else {
        format!("{}:", module.path)
    }
}

pub fn impact(a: &Value) -> Result {
    let project = projects::inspect(s(a, "path"))?;
    let root = PathBuf::from(s(&project, "path"));
    let Some(gradle_dir) = project["gradle_directory"].as_str().map(PathBuf::from) else {
        return Err(Error::new(
            "action_unsupported",
            "Test impact selection reads Gradle builds; this project has none",
        )
        .remedy("Run the project's tests with project.execute"));
    };
    let (changed, base) = changed_files(&root, a)?;
    let suffix = if s(a, "configuration") == "release" {
        "Release"
    } else {
        "Debug"
    };
    let (selection, modules, unmapped) = plan(&root, &gradle_dir, &changed);
    let run_all = !selection.run_all.is_empty();
    let mut unit = Vec::new();
    let mut instrumented = Vec::new();
    let mut unit_steps: Vec<Vec<String>> = Vec::new();
    let mut instrumented_steps: Vec<Vec<String>> = Vec::new();
    let keys: BTreeSet<(usize, u8)> = selection
        .classes
        .keys()
        .chain(selection.whole.keys())
        .copied()
        .collect();
    for (index, kind) in keys {
        let module = &modules[index];
        if kind == 1 && !module.android {
            continue;
        }
        let whole = selection.whole.get(&(index, kind));
        let classes = selection
            .classes
            .get(&(index, kind))
            .cloned()
            .unwrap_or_default();
        let list = if kind == 0 {
            &mut unit
        } else {
            &mut instrumented
        };
        list.push(json!({
            "module":module.path,
            "all":whole.is_some(),
            "reasons":whole.map(|w| w.iter().cloned().collect::<Vec<_>>()).unwrap_or_default(),
            "classes":classes.iter().map(|(class, reasons)| json!({"class":class,"reasons":reasons})).collect::<Vec<_>>(),
        }));
        if run_all {
            continue;
        }
        let prefix = task_prefix(module);
        if kind == 0 {
            let task = if module.android {
                format!("{prefix}test{suffix}UnitTest")
            } else {
                format!("{prefix}test")
            };
            let mut step = vec![task];
            if whole.is_none() {
                for class in classes.keys() {
                    step.extend(["--tests".to_string(), class.clone()]);
                }
            }
            unit_steps.push(step);
        } else if module.android {
            let mut step = vec![format!("{prefix}connected{suffix}AndroidTest")];
            if whole.is_none() {
                step.push(format!(
                    "-Pandroid.testInstrumentationRunnerArguments.class={}",
                    classes.keys().cloned().collect::<Vec<_>>().join(",")
                ));
            }
            instrumented_steps.push(step);
        }
    }
    if run_all {
        unit_steps = vec![vec![format!("test{suffix}UnitTest")]];
        instrumented_steps = vec![vec![format!("connected{suffix}AndroidTest")]];
    }
    // Unit tests run in one Gradle call; each module's instrumented tests in
    // its own, as the class filter applies to every module it runs.
    let gradle = projects::gradle_command(&project)?;
    let unit_command: Vec<Vec<String>> = if unit_steps.is_empty() {
        Vec::new()
    } else {
        let mut command = gradle.clone();
        command.push("--console=plain".into());
        command.extend(unit_steps.into_iter().flatten());
        vec![command]
    };
    let instrumented_commands: Vec<Vec<String>> = instrumented_steps
        .into_iter()
        .map(|step| {
            let mut command = gradle.clone();
            command.push("--console=plain".into());
            command.extend(step);
            command
        })
        .collect();
    let mut value = json!({
        "path":root,"base":base,"changed_files":changed.len(),"changed":changed.iter().take(200).collect::<Vec<_>>(),
        "run_all":run_all,"run_all_reasons":selection.run_all,
        "unit":unit,"instrumented":instrumented,"unmapped":unmapped,
        "commands":{"unit":unit_command,"instrumented":instrumented_commands},
        "heuristic":"Module, package and name rules over the build and source files; Gradle was not run",
    });
    if !s(a, "scenarios").is_empty() {
        let at = path(s(a, "scenarios"), false, true)?;
        let apps = affected_apps(&gradle_dir, &modules, &selection);
        value["scenarios"] = json!(scenarios(&at, &apps, run_all)?);
        value["affected_apps"] = json!(apps);
    }
    let run = s(a, "run");
    if run == "unit" || run == "instrumented" {
        let commands = if run == "unit" {
            unit_command
        } else {
            instrumented_commands
        };
        if commands.is_empty() {
            value["job"] = Value::Null;
            value["note"] = json!(format!("No {run} tests are affected; nothing ran"));
            return Ok(value);
        }
        let mut env = serde_json::to_value(tc::environment()?)?;
        if run == "instrumented" && !s(a, "device").is_empty() {
            env["ANDROID_SERIAL"] = json!(identifier(s(a, "device"), "device identifier", None)?);
        }
        let label = format!("{} · affected {run} tests", s(&project, "name"));
        let spec = json!({
            "project":root,"cwd":gradle_dir,"steps":commands.iter().map(|c| json!({"argv":c})).collect::<Vec<_>>(),
            "env":env,"artifacts":["**/build/reports/**/index.html"],"exclusive":root,"label":label,
            "timeout":a["timeout"].as_u64().unwrap_or(1800),"single_active":true,"resource":"build",
            "cleanup":[projects::test_report_cleanup(&root, now() - 2.0)?],
            "always_artifacts":[root.join(projects::TEST_REPORT_HTML)],
            "operation_key":{"operation":"testing.impact","arguments":a,"project":gradle_dir},
        });
        value["job"] = jobs::submit(&label, spec)?;
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(root: &Path, relative: &str, text: &str) {
        let file = root.join(relative);
        fs::create_dir_all(file.parent().unwrap()).unwrap();
        fs::write(file, text).unwrap();
    }

    /// An app module that uses a core library, each with tests.
    fn fixture() -> tempfile::TempDir {
        let root = tempfile::tempdir().unwrap();
        let r = root.path();
        write(
            r,
            "settings.gradle.kts",
            "include(\":app\", \":core\", \":other\")",
        );
        write(
            r,
            "build.gradle.kts",
            "plugins { id(\"com.android.application\") apply false }",
        );
        write(
            r,
            "app/build.gradle.kts",
            "plugins { id(\"com.android.application\") }\nandroid { namespace = \"dev.example.app\"\n defaultConfig { applicationId = \"dev.example.app\" } }\ndependencies { implementation(project(\":core\")) }",
        );
        write(r, "core/build.gradle.kts", "plugins { kotlin(\"jvm\") }");
        write(
            r,
            "other/build.gradle.kts",
            "plugins { id(\"com.android.library\") }\nandroid { namespace = \"dev.example.other\" }",
        );
        write(
            r,
            "core/src/main/kotlin/dev/example/core/Counter.kt",
            "package dev.example.core\nclass Counter",
        );
        write(
            r,
            "core/src/main/kotlin/dev/example/core/Format.kt",
            "package dev.example.core\nfun format(x: Int) = x.toString()",
        );
        write(
            r,
            "core/src/test/kotlin/dev/example/core/CounterTest.kt",
            "package dev.example.core\nclass CounterTest { val c = Counter() }",
        );
        write(
            r,
            "core/src/test/kotlin/dev/example/core/MathTest.kt",
            "package dev.example.core\nclass MathTest",
        );
        write(
            r,
            "app/src/main/java/dev/example/app/MainActivity.kt",
            "package dev.example.app\nclass MainActivity",
        );
        write(
            r,
            "app/src/test/java/dev/example/app/ScreenTest.kt",
            "package dev.example.app\nimport dev.example.core.Counter\nclass ScreenTest { val c = Counter() }",
        );
        write(
            r,
            "app/src/androidTest/java/dev/example/app/MainActivityTest.kt",
            "package dev.example.app\nclass MainActivityTest",
        );
        write(
            r,
            "other/src/test/java/dev/example/other/OtherTest.kt",
            "package dev.example.other\nclass OtherTest { val counter = \"Counter\" }",
        );
        write(
            r,
            "scenarios/login.json",
            "{\"name\":\"Login\",\"steps\":[{\"action\":\"launch\",\"bundle_id\":\"dev.example.app\"}]}",
        );
        write(
            r,
            "scenarios/elsewhere.json",
            "{\"name\":\"Elsewhere\",\"steps\":[{\"action\":\"launch\",\"bundle_id\":\"dev.example.web\"}]}",
        );
        root
    }

    fn classes(list: &Value) -> Vec<String> {
        list.as_array()
            .unwrap()
            .iter()
            .flat_map(|m| {
                m["classes"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|c| s(c, "class").to_string())
            })
            .collect()
    }

    #[test]
    fn a_library_change_selects_its_tests_and_its_dependents_users() {
        let root = fixture();
        let value = impact(&json!({"path":root.path(),"files":["core/src/main/kotlin/dev/example/core/Counter.kt","README.md"],
            "scenarios":root.path().join("scenarios")}))
        .unwrap();
        assert_eq!(value["run_all"], false);
        let mut unit = classes(&value["unit"]);
        unit.sort();
        // Not MathTest (same package, but neither named after nor using it),
        // not OtherTest (other module, no dependency, only the word in a string).
        assert_eq!(
            unit,
            ["dev.example.app.ScreenTest", "dev.example.core.CounterTest"]
        );
        assert_eq!(value["instrumented"], json!([]));
        assert_eq!(value["unmapped"][0]["file"], "README.md");
        let unit_command = strings(&value["commands"]["unit"][0]);
        let joined = unit_command.join(" ");
        assert!(
            joined.contains(":app:testDebugUnitTest --tests dev.example.app.ScreenTest"),
            "{joined}"
        );
        assert!(
            joined.contains(":core:test --tests dev.example.core.CounterTest"),
            "{joined}"
        );
        assert_eq!(value["scenarios"].as_array().unwrap().len(), 1);
        assert_eq!(value["scenarios"][0]["name"], "Login");
        assert_eq!(value["affected_apps"], json!(["dev.example.app"]));
    }

    #[test]
    fn package_fallback_resources_build_files_and_tests_themselves() {
        let root = fixture();
        let value = impact(&json!({"path":root.path(),"files":["core/src/main/kotlin/dev/example/core/Format.kt"]})).unwrap();
        let mut unit = classes(&value["unit"]);
        unit.sort();
        assert_eq!(
            unit,
            ["dev.example.core.CounterTest", "dev.example.core.MathTest"]
        );
        let resources = impact(&json!({"path":root.path(),"files":["app/src/main/res/values/strings.xml"],"configuration":"release"})).unwrap();
        assert_eq!(resources["instrumented"][0]["module"], ":app");
        assert_eq!(resources["instrumented"][0]["all"], true);
        assert_eq!(
            strings(&resources["commands"]["instrumented"][0])
                .last()
                .unwrap(),
            ":app:connectedReleaseAndroidTest"
        );
        let test = impact(&json!({"path":root.path(),"files":["app/src/androidTest/java/dev/example/app/MainActivityTest.kt"]})).unwrap();
        assert_eq!(
            classes(&test["instrumented"]),
            ["dev.example.app.MainActivityTest"]
        );
        assert!(
            strings(&test["commands"]["instrumented"][0])
                .last()
                .unwrap()
                .ends_with("class=dev.example.app.MainActivityTest")
        );
        let named = impact(&json!({"path":root.path(),"files":["app/src/main/java/dev/example/app/MainActivity.kt"]})).unwrap();
        assert_eq!(
            classes(&named["instrumented"]),
            ["dev.example.app.MainActivityTest"]
        );
        let module =
            impact(&json!({"path":root.path(),"files":["core/build.gradle.kts"]})).unwrap();
        let modules: Vec<&str> = module["unit"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| s(m, "module"))
            .collect();
        assert_eq!(modules, [":app", ":core"]);
        let all = impact(&json!({"path":root.path(),"files":["gradle/libs.versions.toml"],"scenarios":root.path().join("scenarios")})).unwrap();
        assert_eq!(all["run_all"], true);
        assert_eq!(
            strings(&all["commands"]["unit"][0]).last().unwrap(),
            "testDebugUnitTest"
        );
        assert_eq!(all["scenarios"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn changed_files_come_from_git_against_the_base() {
        let Ok(git) = tc::find("git") else { return };
        let root = fixture();
        let git_run = |args: &[&str]| {
            let status = std::process::Command::new(&git)
                .args([
                    "-c",
                    "user.name=T",
                    "-c",
                    "user.email=t@example.invalid",
                    "-C",
                ])
                .arg(root.path())
                .args(args)
                .output()
                .unwrap();
            assert!(status.status.success(), "{status:?}");
        };
        git_run(&["init", "-q"]);
        git_run(&["add", "."]);
        git_run(&["commit", "-q", "-m", "start"]);
        write(
            root.path(),
            "core/src/main/kotlin/dev/example/core/Counter.kt",
            "package dev.example.core\nclass Counter(val x: Int)",
        );
        write(
            root.path(),
            "core/src/main/kotlin/dev/example/core/New.kt",
            "package dev.example.core\nclass New",
        );
        let value = impact(&json!({"path":root.path()})).unwrap();
        assert_eq!(value["base"], "HEAD");
        assert_eq!(
            value["changed"],
            json!([
                "core/src/main/kotlin/dev/example/core/Counter.kt",
                "core/src/main/kotlin/dev/example/core/New.kt"
            ])
        );
        assert!(classes(&value["unit"]).contains(&"dev.example.core.CounterTest".to_string()));
        assert_eq!(
            impact(&json!({"path":root.path(),"base":"no-such-ref"}))
                .unwrap_err()
                .code,
            "git_failed"
        );
    }
}
