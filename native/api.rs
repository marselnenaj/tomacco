use crate::{
    apple, automation, catalog, core::*, debugger, desktop, devices, files, jobs, play_store,
    preview, profiling, projects, releases, remote, swift, toolchain, update, workspaces,
};
use serde_json::{Value, json};
use std::sync::OnceLock;
pub fn manifest() -> &'static Value {
    static MANIFEST: OnceLock<Value> = OnceLock::new();
    MANIFEST.get_or_init(|| {
        serde_json::from_str(include_str!("../assets/operations.json"))
            .expect("operation schema is valid JSON")
    })
}
/// Every operation's name, in schema order.
pub fn names() -> impl Iterator<Item = &'static str> {
    manifest()
        .as_array()
        .unwrap()
        .iter()
        .map(|op| s(op, "name"))
}
/// One operation's schema; an unknown name is answered with the closest one.
pub fn operation(name: &str) -> Result<&'static Value> {
    manifest()
        .as_array()
        .unwrap()
        .iter()
        .find(|op| s(op, "name") == name)
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
    match name {
        "doctor" => toolchain::doctor().map(crate::setup::with_next_steps),
        "settings.get" => toolchain::settings(),
        "settings.configure" => toolchain::configure(&a),
        "desktop.status" => desktop::status(),
        "desktop.open" => desktop::open(&a),
        "catalog.list" => catalog::profiles(s(&a, "platform")),
        "catalog.import" => catalog::import(&a),
        "catalog.export" => catalog::export(&a),
        "catalog.remove" => catalog::remove(s(&a, "profile_id")),
        "job.list" => jobs::listing(n(&a, "limit")),
        "job.get" => jobs::get(s(&a, "job_id")),
        "job.logs" => jobs::logs(&a),
        "job.wait" => jobs::wait(s(&a, "job_id"), n(&a, "seconds")),
        "job.cancel" => jobs::cancel(s(&a, "job_id")),
        "job.send" => jobs::send(s(&a, "job_id"), s(&a, "command")),
        "project.inspect" => projects::inspect(s(&a, "path")),
        "project.configure" => projects::configure(&a),
        "project.create" => projects::create(&a),
        "project.plan" => projects::plan(&a),
        "project.execute" => projects::execute(&a),
        "project.run" => projects::run(&a),
        "testing.find" => automation::find(&a),
        "testing.action" => automation::action(&a),
        "testing.run" => automation::run(&a),
        "testing.setup" => crate::instrumentation::setup(&a),
        "testing.instrumentation" => crate::instrumentation::invoke(&a),
        "ios.toolkit_install"
        | "ios.flutter_setup"
        | "ios.flutter_build"
        | "ios.crash_symbolicate"
        | "ios.store_ship"
        | "project.import_xcode"
        | "project.app" => crate::apple_dev::handle(name, &a),
        "ios.doctor" | "ios.prepare" | "ios.wda_prepare" | "ios.apps" => {
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
        "ios.virtual.configure" => crate::ios_emulator::virtual_phone::configure(&a),
        "ios.virtual.start" => crate::ios_emulator::virtual_phone::start(&a),
        "ios.virtual.stop" => crate::ios_emulator::virtual_phone::stop(&a),
        name if name.starts_with("update.") => update::handle(name, &a),
        "ios.virtual.deploy" => crate::ios_emulator::virtual_phone::deploy(&a),
        "ios.virtual.debug" => crate::ios_emulator::virtual_phone::debug(&a),
        "ios.virtual.screenshot" => crate::ios_emulator::virtual_screen::screenshot(&a),
        "ios.virtual.record" => crate::ios_emulator::virtual_screen::record(&a),
        "android.notifications" => crate::android_device::notification(&a),
        "android.display" => crate::android_device::display(&a),
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
    fn all_operations_have_unique_names_and_strict_schemas() {
        let mut names = std::collections::HashSet::new();
        for op in manifest().as_array().unwrap() {
            assert!(names.insert(s(op, "name")));
            assert_eq!(op["inputSchema"]["additionalProperties"], false);
        }
        assert!(names.len() >= 76);
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
