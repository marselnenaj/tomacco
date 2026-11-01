//! `capabilities`: what this computer can do right now, in one call, so an
//! agent can plan before it builds or starts anything. `doctor` says how to
//! set up what is missing; this says what works.
use crate::{api, core::*, toolchain};
use serde_json::{Value, json};

pub fn capabilities() -> Result {
    let doctor = toolchain::doctor()?;
    let avds = api::dispatch("android.avds", &json!({})).unwrap_or(Value::Null);
    let devices = api::dispatch("device.list", &json!({})).unwrap_or(Value::Null);
    let virtual_phone = if std::env::consts::ARCH == "x86_64" {
        Some(api::dispatch("ios.virtual.status", &json!({})))
    } else {
        None
    };
    Ok(summary(&doctor, &avds, &devices, virtual_phone))
}

fn available(doctor: &Value, name: &str) -> bool {
    doctor["tools"].as_array().is_some_and(|tools| {
        tools
            .iter()
            .any(|tool| tool["name"] == name && tool["available"] == true)
    })
}

/// What is ready, from doctor, android.avds, device.list and
/// ios.virtual.status (None on a host that cannot run the virtual iPhone).
/// Each capability has `ready` and, while it is not, `missing`: the names of
/// what it lacks.
fn summary(doctor: &Value, avds: &Value, devices: &Value, virtual_phone: Option<Result>) -> Value {
    let lacking = |needs: &[(&str, bool)]| -> Vec<String> {
        needs
            .iter()
            .filter(|(_, ok)| !ok)
            .map(|(name, _)| name.to_string())
            .collect()
    };
    let capability = |needs: &[(&str, bool)]| {
        let missing = lacking(needs);
        json!({"ready":missing.is_empty(),"missing":missing})
    };
    let tool = |name: &str| available(doctor, name);
    let sdk = !s(doctor, "android_sdk").is_empty();
    let kvm = doctor["kvm"]["accessible"] == true;
    let avd_names: Vec<Value> = avds["avds"]
        .as_array()
        .map(|avds| avds.iter().map(|avd| avd["name"].clone()).collect())
        .unwrap_or_default();
    let listed = |platform: &str| -> Vec<Value> {
        devices["devices"]
            .as_array()
            .map(|devices| {
                devices
                    .iter()
                    .filter(|device| device["platform"] == platform)
                    .map(|device| {
                        let kind = if device["kind"] == "emulator" {
                            "emulator"
                        } else {
                            "physical"
                        };
                        json!({"id":device["id"],"name":device["name"],"state":device["state"],"kind":kind})
                    })
                    .collect()
            })
            .unwrap_or_default()
    };
    let mut android_build = capability(&[("java", tool("java")), ("android_sdk", sdk)]);
    android_build["gradle"] = json!(tool("gradle"));
    let mut emulator = capability(&[
        ("emulator", tool("emulator")),
        ("adb", tool("adb")),
        ("kvm", kvm),
        ("avd", !avd_names.is_empty()),
    ]);
    emulator["avds"] = json!(avd_names);
    emulator["kvm"] = json!(kvm);
    let mut android_devices = capability(&[("adb", tool("adb"))]);
    android_devices["connected"] = json!(listed("android"));
    let darwin_sdk = doctor["ios_sdk"]["exit_code"] == 0;
    let ios_build = capability(&[
        ("swift", tool("swift")),
        ("xtool", tool("xtool")),
        ("darwin_sdk", darwin_sdk),
    ]);
    let mut iphone = capability(&[("idevice_id", tool("idevice_id"))]);
    iphone["connected"] = json!(listed("ios"));
    let virtual_iphone = match virtual_phone {
        None => json!({"ready":false,"missing":["x86_64_host"],"host_supported":false}),
        Some(Err(error)) => {
            json!({"ready":false,"missing":["valid_profile"],"host_supported":true,
                "configured":true,"error":error.value()})
        }
        Some(Ok(status)) if status["configured"] != true => {
            json!({"ready":false,"missing":["profile"],"host_supported":true,"configured":false})
        }
        Some(Ok(status)) => {
            json!({"ready":true,"missing":[],"host_supported":true,"configured":true,
                "running":status["guest_running"] == true,"phase":status["phase"],
                "display_visible":status["display_visible"] == true,
                "application":status["application"]})
        }
    };
    json!({
        "android":{"build":android_build,"emulator":emulator,"devices":android_devices},
        "ios":{"build":ios_build,"devices":iphone,"virtual_iphone":virtual_iphone},
        "desktop":{"ready":doctor["desktop"]["loadable"] != false},
        "testing":crate::ios_emulator::ui_testing::matrix(),
        "next":"doctor has next_steps for what is missing"
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn doctor(tools: &[&str], sdk: &str, kvm: bool) -> Value {
        let tools: Vec<Value> = tools
            .iter()
            .map(|name| json!({"name":name,"available":true}))
            .chain([json!({"name":"gradle","available":false})])
            .collect();
        json!({"tools":tools,"android_sdk":sdk,"kvm":{"accessible":kvm},
            "ios_sdk":{"exit_code":0},"desktop":{"loadable":true}})
    }

    #[test]
    fn a_ready_machine_says_what_it_can_do_and_validates_against_its_schema() {
        let everything = doctor(
            &["java", "adb", "emulator", "swift", "xtool", "idevice_id"],
            "/sdk",
            true,
        );
        let devices = json!({"devices":[
            {"id":"emulator-5554","platform":"android","state":"device","name":"Pixel","kind":"emulator"},
            {"id":"R58","platform":"android","state":"device","name":"Galaxy"},
            {"id":"00008110","platform":"ios","state":"connected","name":"iPhone / iPad"}]});
        let status = json!({"configured":true,"guest_running":true,"phase":"launch-accepted",
            "display_visible":true,"application":"dev.example.App"});
        let value = summary(
            &everything,
            &json!({"avds":[{"name":"Tomacco"}]}),
            &devices,
            Some(Ok(status)),
        );
        assert_eq!(value["android"]["build"]["ready"], true);
        assert_eq!(value["android"]["build"]["gradle"], false);
        assert_eq!(value["android"]["emulator"]["ready"], true);
        assert_eq!(value["android"]["emulator"]["avds"], json!(["Tomacco"]));
        let android = &value["android"]["devices"]["connected"];
        assert_eq!(android[0]["kind"], "emulator");
        assert_eq!(android[1]["kind"], "physical");
        assert_eq!(value["ios"]["devices"]["connected"][0]["id"], "00008110");
        assert_eq!(value["ios"]["build"]["ready"], true);
        assert_eq!(value["ios"]["virtual_iphone"]["running"], true);
        // The testing matrix: selectors work on Android, not on the virtual iPhone.
        let selectors = &value["testing"][0];
        assert_eq!(selectors["id"], "selectors");
        assert_eq!(selectors["android"]["supported"], true);
        assert_eq!(selectors["virtual_iphone"]["supported"], false);
        let schema = &api::operation("capabilities").unwrap()["outputSchema"];
        validate(&value, schema, "result").unwrap();
    }

    #[test]
    fn a_bare_machine_names_what_each_capability_lacks() {
        let bare = doctor(&[], "", false);
        let value = summary(&bare, &Value::Null, &Value::Null, None);
        assert_eq!(
            value["android"]["emulator"]["missing"],
            json!(["emulator", "adb", "kvm", "avd"])
        );
        assert_eq!(
            value["android"]["build"]["missing"],
            json!(["java", "android_sdk"])
        );
        assert_eq!(value["ios"]["build"]["missing"], json!(["swift", "xtool"]));
        assert_eq!(value["ios"]["virtual_iphone"]["host_supported"], false);
        assert_eq!(value["android"]["devices"]["connected"], json!([]));
        let schema = &api::operation("capabilities").unwrap()["outputSchema"];
        validate(&value, schema, "result").unwrap();
        // A profile ios.virtual.status refuses, and none at all.
        let refused = summary(
            &bare,
            &Value::Null,
            &Value::Null,
            Some(Err(Error::new("ios_virtual_profile_invalid", "bad"))),
        );
        assert_eq!(
            refused["ios"]["virtual_iphone"]["error"]["code"],
            "ios_virtual_profile_invalid"
        );
        validate(&refused, schema, "result").unwrap();
        let none = summary(
            &bare,
            &Value::Null,
            &Value::Null,
            Some(Ok(json!({"configured":false}))),
        );
        assert_eq!(none["ios"]["virtual_iphone"]["missing"], json!(["profile"]));
    }
}
