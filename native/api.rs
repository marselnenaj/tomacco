use crate::{
    apple, automation, catalog, core::*, debugger, desktop, devices, files, jobs, play_store,
    preview, profiling, projects, releases, remote, swift, toolchain, update, workspaces,
};
use serde_json::{Value, json};
use std::sync::OnceLock;
const OPERATIONS: &str = include_str!("../assets/operations.json");
pub fn manifest() -> &'static Value {
    static MANIFEST: OnceLock<Value> = OnceLock::new();
    MANIFEST
        .get_or_init(|| serde_json::from_str(OPERATIONS).expect("operation schema is valid JSON"))
}
/// One operation in the schema: its name, its JSON text, and its parsed
/// schema once something asks for it.
struct Entry {
    name: String,
    text: &'static serde_json::value::RawValue,
    schema: OnceLock<Value>,
}
/// The schema split into operations without building any of them: a call
/// needs one schema of 173, and parsing them all (224 KiB of JSON) was most
/// of the time a short `tomaccoctl call` took.
fn entries() -> &'static [Entry] {
    static ENTRIES: OnceLock<Vec<Entry>> = OnceLock::new();
    ENTRIES.get_or_init(|| {
        #[derive(serde::Deserialize)]
        struct Named<'a> {
            #[serde(borrow)]
            name: std::borrow::Cow<'a, str>,
        }
        let operations: Vec<&'static serde_json::value::RawValue> =
            serde_json::from_str(OPERATIONS).expect("operation schema is valid JSON");
        operations
            .into_iter()
            .map(|text| Entry {
                name: serde_json::from_str::<Named<'_>>(text.get())
                    .expect("every operation has a name")
                    .name
                    .into_owned(),
                text,
                schema: OnceLock::new(),
            })
            .collect()
    })
}
/// Every operation's name, in schema order.
pub fn names() -> impl Iterator<Item = &'static str> {
    entries().iter().map(|entry| entry.name.as_str())
}
/// One operation's schema; an unknown name is answered with the closest one.
pub fn operation(name: &str) -> Result<&'static Value> {
    entries()
        .iter()
        .find(|entry| entry.name == name)
        .map(|entry| {
            entry.schema.get_or_init(|| {
                serde_json::from_str(entry.text.get()).expect("operation schema is valid JSON")
            })
        })
        .ok_or_else(|| {
            let remedy = match closest(name, names()) {
                Some(near) => {
                    format!("Did you mean {near}? tomaccoctl schema --names lists every operation")
                }
                None => "Use tomaccoctl schema to discover operations".to_string(),
            };
            Error::new("operation_not_found", format!("Unknown operation: {name}")).remedy(remedy)
        })
}
pub fn arguments(name: &str, args: &Value) -> Result {
    let op = operation(name)?;
    validate(args, &op["inputSchema"], "arguments")?;
    let mut args = args.clone();
    for (key, spec) in op["inputSchema"]["properties"].as_object().unwrap() {
        if args.get(key).is_none() {
            if let Some(value) = spec.get("default") {
                args[key] = value.clone();
            }
        }
    }
    Ok(args)
}
pub fn dispatch(name: &str, args: &Value) -> Result {
    check_cancelled()?;
    let a = arguments(name, args)?;
    let _request = jobs::Request::enter(name, &a["retry"]);
    match name {
        "doctor" => toolchain::doctor().map(crate::setup::with_next_steps),
        "capabilities" => crate::capabilities::capabilities(),
        "settings.get" => toolchain::settings(),
        "settings.configure" => toolchain::configure(&a),
        "desktop.status" => desktop::status().map(redacted),
        "desktop.open" => desktop::open(&a),
        "catalog.list" => catalog::profiles(s(&a, "platform")),
        "catalog.import" => catalog::import(&a),
        "catalog.export" => catalog::export(&a),
        "catalog.remove" => catalog::remove(s(&a, "profile_id")),
        // Job records and logs hold commands, environments and tool output:
        // what leaves the process has recognised secrets masked.
        "job.list" => jobs::list(&a).map(redacted),
        "job.get" => jobs::get(s(&a, "job_id")).map(redacted),
        "job.logs" => jobs::logs(&a).map(redacted),
        "job.wait" => jobs::wait(s(&a, "job_id"), n(&a, "seconds")).map(redacted),
        "job.events" => jobs::events(s(&a, "job_id"), n(&a, "after"), n(&a, "limit")).map(redacted),
        "job.cancel" => jobs::cancel(s(&a, "job_id")),
        "job.send" => jobs::send(s(&a, "job_id"), s(&a, "command")),
        "storage.prune" => crate::storage::handle(&a),
        "diagnostics.bundle" => crate::diagnostics::bundle(&a),
        "project.inspect" => projects::inspect(s(&a, "path")),
        "project.configure" => projects::configure(&a),
        "project.create" => projects::create(&a),
        "project.plan" => projects::plan(&a),
        "project.execute" => projects::execute(&a),
        "project.run" => projects::run(&a),
        "project.watch" => crate::watch::watch(&a),
        "project.dependency_updates" => crate::dependencies::updates(&a),
        "project.analyze" => crate::analyze::analyze(&a),
        "testing.find" => automation::find(&a),
        "testing.action" => crate::recorder::testing_action(
            &crate::recorder::Store::home()?,
            &a,
            automation::action,
        ),
        "testing.lint" => automation::lint(&a),
        "testing.record" => crate::recorder::handle(&a),
        "testing.run" => automation::run(&a),
        "testing.visual_diff" => crate::visual::visual_diff(&a),
        "testing.accessibility" => crate::accessibility::accessibility(&a),
        "testing.crashes" => crate::crashes::crashes(&a),
        "testing.results" => crate::test_results::results(&a),
        "testing.setup" => crate::instrumentation::setup(&a),
        "testing.instrumentation" => crate::instrumentation::invoke(&a),
        "ios.toolkit_install"
        | "ios.flutter_setup"
        | "ios.flutter_build"
        | "ios.crash_symbolicate"
        | "ios.ipa_inspect"
        | "ios.store_ship"
        | "project.import_xcode"
        | "project.app" => crate::apple_dev::handle(name, &a),
        "ios.doctor" | "ios.prepare" | "ios.wda_prepare" | "ios.apps" | "ios.crash_reports" => {
            crate::iphone_setup::handle(name, &a)
        }
        "ios.emulator.inspect" => crate::ios_emulator::inspect(&a),
        "ios.emulator.probe" | "ios.emulator.exec" => crate::ios_emulator::probe(&a),
        "ios.emulator.prepare_research" => crate::ios_emulator::research::prepare(&a),
        "ios.emulator.research" => crate::ios_emulator::research::start(&a),
        "ios.emulator.paravirtual" => crate::ios_emulator::paravirtual::start(&a),
        "ios.emulator.control" => crate::ios_emulator::control::handle(&a),
        "ios.emulator.usb_bridge" => crate::ios_emulator::usb_bridge::start(&a),
        "ios.emulator.report" => crate::ios_emulator::research::report(&a),
        "ios.emulator.restore" => crate::ios_emulator::restore::start(&a),
        "ios.emulator.prepare_restore" => crate::ios_emulator::restore::prepare(&a),
        "ios.emulator.prepare_userland" => crate::ios_emulator::userland::prepare(&a),
        "ios.emulator.prepare_cryptexes" => crate::ios_emulator::userland::prepare_cryptexes(&a),
        "ios.emulator.build_gpu_plugin" => crate::ios_emulator::userland::build_gpu_plugin(&a),
        "ios.emulator.build_guest_tools" => crate::ios_emulator::guest_tools::run(&a, false),
        "ios.emulator.prepare_guest_tools" => crate::ios_emulator::guest_tools::run(&a, true),
        "ios.emulator.guest" => crate::ios_emulator::guest_control::call(&a),
        "ios.emulator.guest_result" => crate::ios_emulator::guest_control::result(&a),
        "ios.emulator.prepare_pmp" => crate::ios_emulator::pmp::prepare(&a),
        "ios.emulator.inspect_pmp" => crate::ios_emulator::pmp::inspect(&a),
        "ios.emulator.pmp" => crate::ios_emulator::pmp::start(&a),
        "ios.emulator.memory" => crate::ios_emulator::pmp::memory(&a),
        "ios.virtual.status" => crate::ios_emulator::virtual_phone::status(&a),
        "ios.virtual.apps" => crate::ios_emulator::virtual_phone::apps(&a),
        "ios.virtual.cleanup" => crate::ios_emulator::virtual_phone::cleanup(&a),
        "ios.virtual.crash_reports" => crate::ios_emulator::crash_reports::collect(&a),
        "ios.virtual.configure" => crate::ios_emulator::virtual_phone::configure(&a),
        "ios.virtual.start" => crate::ios_emulator::virtual_phone::start(&a),
        "ios.virtual.stop" => crate::ios_emulator::virtual_phone::stop(&a),
        name if name.starts_with("update.") => update::handle(name, &a),
        "ios.virtual.deploy" => crate::ios_emulator::virtual_phone::deploy(&a),
        "ios.virtual.debug" => crate::ios_emulator::virtual_phone::debug(&a),
        "ios.virtual.screenshot" => crate::ios_emulator::virtual_screen::screenshot(&a),
        "ios.virtual.record" => crate::ios_emulator::virtual_screen::record(&a),
        "android.input" => crate::recorder::android_input(
            &crate::recorder::Store::home()?,
            &a,
            || crate::recorder::hierarchy(s(&a, "device")),
            |a| devices::handle("android.input", a),
        ),
        "android.notifications" => crate::android_device::notification(&a),
        "android.app_files" => crate::android_device::app_files(&a),
        "android.each" => crate::android_device::each(&a),
        "android.display" => crate::android_device::display(&a),
        "android.pair" => crate::android_device::pair(&a),
        "android.screen" => crate::android_device::screen(&a),
        "android.package_inspect" => crate::android_package::inspect(&a),
        name if name.starts_with("play.") => play_store::handle(name, &a),
        name if name.starts_with("runtime.") => crate::runtime::handle(name, &a),
        name if name.starts_with("apple.") => apple::handle(name, &a),
        name if name.starts_with("release.") => releases::handle(name, &a),
        name if name.starts_with("files.") => files::handle(name, &a),
        name if name.starts_with("profile.") => profiling::handle(name, &a),
        name if name.starts_with("workspace.") => workspaces::handle(name, &a),
        name if name.starts_with("debug.") => debugger::handle(name, &a),
        name if name.starts_with("preview.") => preview::handle(name, &a),
        name if name.starts_with("remote.") => remote::handle(name, &a),
        name if name.starts_with("swift.") => swift::handle(name, &a),
        name if name.starts_with("device.")
            || name.starts_with("android.")
            || name.starts_with("ios.") =>
        {
            devices::handle(name, &a)
        }
        _ => Err(Error::new("operation_not_found", name)),
    }
}
fn redacted(mut value: Value) -> Value {
    crate::redact::value(&mut value);
    value
}
pub fn envelope(value: Result) -> Value {
    match value {
        Ok(result) => json!({"ok":true,"result":result}),
        Err(error) => json!({"ok":false,"error":error.value()}),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn one_operation_parsed_alone_is_the_one_in_the_whole_schema() {
        let all = manifest().as_array().unwrap();
        assert_eq!(names().count(), all.len());
        for (name, whole) in names().zip(all) {
            assert_eq!(s(whole, "name"), name);
            assert_eq!(operation(name).unwrap(), whole);
        }
        assert_eq!(
            operation("job.wiat").unwrap_err().code,
            "operation_not_found"
        );
    }
    #[test]
    fn all_operations_have_unique_names_and_strict_schemas() {
        let mut names = std::collections::HashSet::new();
        for op in manifest().as_array().unwrap() {
            assert!(names.insert(s(op, "name")));
            assert_eq!(op["inputSchema"]["additionalProperties"], false);
        }
        assert!(names.len() >= 76);
    }
    #[test]
    fn every_operation_has_a_title_and_all_four_tool_annotations() {
        // MCP reads a missing destructiveHint as true and a missing
        // idempotentHint as false, so each operation states all four.
        for op in manifest().as_array().unwrap() {
            let name = s(op, "name");
            assert!(!s(op, "title").is_empty(), "{name} has no title");
            let hints = op["annotations"].as_object().unwrap();
            let keys: Vec<&str> = hints.keys().map(String::as_str).collect();
            assert_eq!(
                keys,
                [
                    "readOnlyHint",
                    "destructiveHint",
                    "idempotentHint",
                    "openWorldHint"
                ],
                "{name}"
            );
            assert!(hints.values().all(Value::is_boolean), "{name}");
            if hints["readOnlyHint"] == true {
                assert_eq!(hints["destructiveHint"], false, "{name}");
                assert_eq!(hints["idempotentHint"], true, "{name}");
            }
        }
        let hint = |name: &str, key: &str| operation(name).unwrap()["annotations"][key] == true;
        assert!(hint("job.cancel", "destructiveHint"));
        assert!(hint("catalog.remove", "destructiveHint"));
        assert!(!hint("project.create", "destructiveHint"));
        assert!(hint("files.write", "idempotentHint"));
    }
    #[test]
    fn validation_rejects_ambiguous_types_and_unsupported_arguments() {
        assert!(
            arguments(
                "android.input",
                &json!({"device":"emulator-5554","action":"tap","x":true})
            )
            .is_err()
        );
        assert!(arguments("project.execute", &json!({"path":"/tmp","timeout":86401})).is_err());
        assert!(
            arguments(
                "device.install",
                &json!({"platform":"android","device":"x","paths":[]})
            )
            .is_err()
        );
        assert!(arguments("doctor", &json!({"unexpected":true})).is_err());
        assert!(arguments("catalog.list", &json!([])).is_err());
    }
    #[test]
    fn mistyped_names_and_arguments_are_answered_with_the_closest_match() {
        let error = arguments("projct.run", &json!({})).unwrap_err();
        assert_eq!(error.code, "operation_not_found");
        assert!(error.remedy.starts_with("Did you mean project.run?"));
        // The last dotted parts of exactly one operation.
        let error = arguments("virtual.start", &json!({})).unwrap_err();
        assert!(error.remedy.starts_with("Did you mean ios.virtual.start?"));
        let error = arguments("completely-different", &json!({})).unwrap_err();
        assert_eq!(error.remedy, "Use tomaccoctl schema to discover operations");
        // A misspelt required argument is named as a misspelling, not as missing.
        let error = arguments("project.inspect", &json!({"pth":"/tmp"})).unwrap_err();
        assert_eq!(error.message, "Unknown argument: arguments.pth");
        assert_eq!(error.remedy, "Did you mean arguments.path?");
        let error = arguments("job.wait", &json!({"job_id":"x","zzzzzz":1})).unwrap_err();
        assert_eq!(error.remedy, "Accepted keys of arguments: job_id, seconds");
        let error = arguments("doctor", &json!({"unexpected":true})).unwrap_err();
        assert_eq!(error.remedy, "arguments must be an empty object");
    }
    #[test]
    fn defaults_match_existing_contract() {
        let a = arguments("project.plan", &json!({"path":"/tmp"})).unwrap();
        assert_eq!(a["configuration"], "debug");
        assert_eq!(a["sign"], false);
        assert_eq!(arguments("preview.state", &json!({})).unwrap(), json!({}));
    }
}
