//! android.size_report: what makes an APK or AAB big, an estimate of the
//! download per ABI and screen density, and the change from an earlier build.
use crate::{android_package::zip_sizes, core::*, toolchain as tc};
use serde_json::{Map, Value, json};
use std::{collections::BTreeMap, path::Path};

const DENSITIES: &[&str] = &[
    "ldpi", "mdpi", "tvdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi", "nodpi", "anydpi",
];

/// One file of the package: its path inside the module, module, category,
/// ABI and density, and its sizes.
#[derive(Clone, Debug, PartialEq)]
struct File {
    name: String,
    module: String,
    category: &'static str,
    abi: Option<String>,
    density: Option<String>,
    compressed: u64,
    uncompressed: u64,
}

fn density_of(path: &str) -> Option<String> {
    let folder = path.strip_prefix("res/")?.split('/').next()?;
    folder
        .split('-')
        .skip(1)
        .find(|q| {
            DENSITIES.contains(q)
                || (q.ends_with("dpi") && q[..q.len() - 3].chars().all(|c| c.is_ascii_digit()))
        })
        .map(String::from)
}

fn classify(entry: &str, bundle: bool) -> Option<File> {
    if entry.ends_with('/') {
        return None;
    }
    let (module, inner) = if bundle {
        match entry.split_once('/') {
            Some((m, rest)) if !["META-INF", "BUNDLE-METADATA"].contains(&m) => {
                (m.to_string(), rest.to_string())
            }
            _ => (String::new(), entry.to_string()),
        }
    } else {
        ("base".to_string(), entry.to_string())
    };
    let path = inner.strip_prefix("root/").unwrap_or(&inner);
    let mut abi = None;
    let category = if path.ends_with(".dex") || (bundle && inner.starts_with("dex/")) {
        "dex"
    } else if let Some(rest) = path.strip_prefix("lib/") {
        abi = rest.split_once('/').map(|(a, _)| a.to_string());
        "native"
    } else if path.starts_with("res/") || path == "resources.arsc" || path == "resources.pb" {
        "resources"
    } else if path.starts_with("assets/") {
        "assets"
    } else if path == "AndroidManifest.xml" || inner.starts_with("manifest/") {
        "manifest"
    } else if entry.starts_with("META-INF/") {
        "signature"
    } else if bundle && module.is_empty() {
        "bundle_metadata"
    } else {
        "other"
    };
    Some(File {
        name: entry.to_string(),
        module,
        category,
        abi,
        density: density_of(path),
        compressed: 0,
        uncompressed: 0,
    })
}

fn files(path: &Path) -> Result<(String, Vec<File>)> {
    let format = match path.extension().and_then(|e| e.to_str()) {
        Some("apk") => "apk",
        Some("aab") => "aab",
        _ => {
            return Err(Error::new(
                "unsupported_artifact",
                "Select an .apk or .aab file",
            ));
        }
    };
    let list = zip_sizes(path)?;
    let entries = list
        .into_iter()
        .filter_map(|(name, compressed, uncompressed)| {
            let mut file = classify(&name, format == "aab")?;
            file.compressed = compressed;
            file.uncompressed = uncompressed;
            Some(file)
        })
        .collect();
    Ok((format.to_string(), entries))
}

/// Sizes summed by category, ABI and density, and the files-based estimate
/// of what one device downloads: everything except other ABIs' libraries
/// and other densities' resources.
fn summary(files: &[File]) -> Value {
    let mut categories: BTreeMap<&str, (u64, u64, u64)> = BTreeMap::new();
    let mut abis: BTreeMap<String, u64> = BTreeMap::new();
    let mut densities: BTreeMap<String, u64> = BTreeMap::new();
    let mut modules: BTreeMap<String, u64> = BTreeMap::new();
    let (mut compressed, mut uncompressed) = (0, 0);
    for f in files {
        let c = categories.entry(f.category).or_default();
        c.0 += 1;
        c.1 += f.compressed;
        c.2 += f.uncompressed;
        compressed += f.compressed;
        uncompressed += f.uncompressed;
        if let Some(abi) = &f.abi {
            *abis.entry(abi.clone()).or_default() += f.compressed;
        }
        if let Some(density) = &f.density {
            *densities.entry(density.clone()).or_default() += f.compressed;
        }
        if !f.module.is_empty() {
            *modules.entry(f.module.clone()).or_default() += f.compressed;
        }
    }
    let specific: Vec<&String> = densities
        .keys()
        .filter(|d| !["nodpi", "anydpi"].contains(&d.as_str()))
        .collect();
    let all_abis: u64 = abis.values().sum();
    let all_densities: u64 = specific.iter().map(|d| densities[*d]).sum();
    let common = compressed - all_abis - all_densities;
    let mut estimate = Map::new();
    let abi_list: Vec<Option<&String>> = if abis.is_empty() {
        vec![None]
    } else {
        abis.keys().map(Some).collect()
    };
    let density_list: Vec<Option<&String>> = if specific.is_empty() {
        vec![None]
    } else {
        specific.iter().copied().map(Some).collect()
    };
    for abi in &abi_list {
        for density in &density_list {
            let key = format!(
                "{}/{}",
                abi.map(String::as_str).unwrap_or("any"),
                density.map(String::as_str).unwrap_or("any")
            );
            let bytes = common
                + abi.map(|a| abis[a]).unwrap_or(0)
                + density.map(|d| densities[d]).unwrap_or(0);
            estimate.insert(key, json!(bytes));
        }
    }
    json!({
        "entries":files.len(),
        "compressed":compressed,
        "uncompressed":uncompressed,
        "categories":categories.iter().map(|(k,(n,c,u))|(k.to_string(),json!({"files":n,"compressed":c,"uncompressed":u}))).collect::<Map<_,_>>(),
        "abis":abis,
        "densities":densities,
        "modules":modules,
        "estimate":{"method":"files","note":"Compressed sizes in this file, without other ABIs' libraries and other densities' resources; Play recompresses, so the real download differs","by_abi_density":estimate},
    })
}

fn largest(files: &[File], top: usize) -> Vec<Value> {
    let mut sorted: Vec<&File> = files.iter().collect();
    sorted.sort_by(|a, b| b.compressed.cmp(&a.compressed).then(a.name.cmp(&b.name)));
    sorted
        .into_iter()
        .take(top)
        .map(|f| json!({"name":f.name,"category":f.category,"compressed":f.compressed,"uncompressed":f.uncompressed}))
        .collect()
}

/// new minus old, number by number, through nested objects; a side that
/// lacks a key counts as 0.
fn delta(new: &Value, old: &Value) -> Value {
    if new.is_object() || old.is_object() {
        let empty = Map::new();
        let a = new.as_object().unwrap_or(&empty);
        let b = old.as_object().unwrap_or(&empty);
        let mut out = Map::new();
        for key in a.keys().chain(b.keys()) {
            if !out.contains_key(key) {
                let value = delta(
                    a.get(key).unwrap_or(&Value::Null),
                    b.get(key).unwrap_or(&Value::Null),
                );
                if !value.is_null() {
                    out.insert(key.clone(), value);
                }
            }
        }
        return Value::Object(out);
    }
    if new.is_number() || old.is_number() {
        return json!(new.as_i64().unwrap_or(0) - old.as_i64().unwrap_or(0));
    }
    Value::Null
}

/// What changed between two builds: totals, per group, and the files that
/// grew, shrank, appeared or went away the most.
fn compare(
    new: &[File],
    old: &[File],
    new_summary: &Value,
    old_summary: &Value,
    top: usize,
) -> Value {
    let before: BTreeMap<&str, &File> = old.iter().map(|f| (f.name.as_str(), f)).collect();
    let after: BTreeMap<&str, &File> = new.iter().map(|f| (f.name.as_str(), f)).collect();
    let mut changes = Vec::new();
    for (name, f) in &after {
        let was = before.get(name).map(|o| o.compressed);
        if was != Some(f.compressed) {
            changes.push((f.compressed as i64 - was.unwrap_or(0) as i64, json!({"name":name,"change":if was.is_some(){"changed"}else{"added"},"compressed":f.compressed,"before":was})));
        }
    }
    for (name, f) in &before {
        if !after.contains_key(name) {
            changes.push((
                -(f.compressed as i64),
                json!({"name":name,"change":"removed","compressed":0,"before":f.compressed}),
            ));
        }
    }
    changes.sort_by(|a, b| {
        b.0.abs()
            .cmp(&a.0.abs())
            .then(a.1["name"].as_str().cmp(&b.1["name"].as_str()))
    });
    let total = changes.len();
    let files: Vec<Value> = changes
        .into_iter()
        .take(top)
        .map(|(d, mut v)| {
            v["delta"] = json!(d);
            v
        })
        .collect();
    let mut summary = delta(new_summary, old_summary);
    if let Some(map) = summary.as_object_mut() {
        map.remove("estimate");
        map.insert(
            "estimate".into(),
            delta(
                &new_summary["estimate"]["by_abi_density"],
                &old_summary["estimate"]["by_abi_density"],
            ),
        );
    }
    json!({"summary":summary,"changed_files":total,"files":files})
}

/// `bundletool get-size total` CSV: one row per device group.
fn size_rows(csv: &str) -> Vec<Value> {
    let mut lines = csv.lines().map(str::trim).filter(|l| !l.is_empty());
    let Some(header) = lines.next() else {
        return Vec::new();
    };
    let keys: Vec<String> = header.split(',').map(|k| k.trim().to_lowercase()).collect();
    lines
        .map(|line| {
            let mut row = Map::new();
            for (key, value) in keys.iter().zip(line.split(',')) {
                let value = value.trim();
                row.insert(
                    key.clone(),
                    value
                        .parse::<u64>()
                        .map(Value::from)
                        .unwrap_or_else(|_| json!(value)),
                );
            }
            Value::Object(row)
        })
        .collect()
}

/// The download size bundletool computes for each ABI and density from the
/// APKs it builds of a bundle.
fn bundletool_sizes(bundle: &Path) -> Result {
    let tool = tc::command("bundletool")?;
    let scratch = tempfile::Builder::new()
        .prefix("size-")
        .tempdir_in(home()?)?;
    let apks = scratch.path().join("all.apks");
    let mut build = tool.clone();
    build.extend([
        "build-apks".into(),
        format!("--bundle={}", bundle.display()),
        format!("--output={}", apks.display()),
    ]);
    tc::run(&build, 300, Some(scratch.path()), true)?;
    let mut size = tool;
    size.extend([
        "get-size".into(),
        "total".into(),
        format!("--apks={}", apks.display()),
        "--dimensions=ABI,SCREEN_DENSITY".into(),
    ]);
    let result = tc::run(&size, 120, Some(scratch.path()), true)?;
    let rows = size_rows(s(&result, "stdout"));
    if rows.is_empty() {
        return Err(
            Error::new("bundletool_failed", "bundletool reported no sizes").details(result),
        );
    }
    Ok(json!({"method":"bundletool get-size total","rows":rows}))
}

fn report(path: &Path, top: usize, use_bundletool: bool) -> Result<(Value, Vec<File>)> {
    let (format, entries) = files(path)?;
    let summary = summary(&entries);
    let mut value = json!({"path":path,"format":format,"file_size":std::fs::metadata(path)?.len(),"summary":summary,"largest":largest(&entries,top)});
    if format == "aab" && use_bundletool {
        value["download"] = match bundletool_sizes(path) {
            Ok(sizes) => sizes,
            Err(error) => json!({"method":"unavailable","error":error.value()}),
        };
    }
    Ok((value, entries))
}

pub fn size_report(a: &Value) -> Result {
    let file = path(s(a, "path"), false, true)?;
    let top = a["top"].as_u64().unwrap_or(15) as usize;
    let use_bundletool = a.get("bundletool").and_then(Value::as_bool).unwrap_or(true);
    let (mut value, entries) = report(&file, top, use_bundletool)?;
    if !s(a, "baseline").is_empty() {
        let old = path(s(a, "baseline"), false, true)?;
        let (base, old_entries) = report(&old, top, use_bundletool)?;
        let mut diff = compare(
            &entries,
            &old_entries,
            &value["summary"],
            &base["summary"],
            top,
        );
        diff["baseline"] = json!(old);
        diff["file_size"] = json!(
            value["file_size"].as_i64().unwrap_or(0) - base["file_size"].as_i64().unwrap_or(0)
        );
        if let (Some(new_rows), Some(old_rows)) = (
            value["download"]["rows"].as_array(),
            base["download"]["rows"].as_array(),
        ) {
            let key =
                |r: &Value| format!("{}/{}/{}", s(r, "sdk"), s(r, "abi"), s(r, "screen_density"));
            let old: BTreeMap<String, &Value> = old_rows.iter().map(|r| (key(r), r)).collect();
            diff["download"] = json!(new_rows.iter().filter_map(|r| {
                let was = old.get(&key(r))?;
                Some(json!({"sdk":r["sdk"],"abi":r["abi"],"screen_density":r["screen_density"],"max":r["max"].as_i64().unwrap_or(0)-was["max"].as_i64().unwrap_or(0)}))
            }).collect::<Vec<_>>());
        }
        value["baseline"] = base["summary"].clone();
        value["delta"] = diff;
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn file(name: &str, bundle: bool, size: u64) -> File {
        let mut f = classify(name, bundle).unwrap();
        f.compressed = size;
        f.uncompressed = size * 2;
        f
    }

    #[test]
    fn entries_are_classified_by_category_abi_and_density() {
        let f = classify("lib/arm64-v8a/libcore.so", false).unwrap();
        assert_eq!(
            (f.category, f.abi.as_deref()),
            ("native", Some("arm64-v8a"))
        );
        let f = classify("base/res/drawable-xxhdpi-v4/a.png", true).unwrap();
        assert_eq!(
            (f.module.as_str(), f.category, f.density.as_deref()),
            ("base", "resources", Some("xxhdpi"))
        );
        assert_eq!(
            classify("res/mipmap-anydpi-v26/ic.xml", false)
                .unwrap()
                .density
                .as_deref(),
            Some("anydpi")
        );
        assert_eq!(
            classify("res/layout/main.xml", false).unwrap().density,
            None
        );
        assert_eq!(
            classify("base/dex/classes.dex", true).unwrap().category,
            "dex"
        );
        assert_eq!(
            classify("feature/manifest/AndroidManifest.xml", true)
                .unwrap()
                .category,
            "manifest"
        );
        assert_eq!(
            classify("BundleConfig.pb", true).unwrap().category,
            "bundle_metadata"
        );
        assert_eq!(
            classify("META-INF/CERT.RSA", false).unwrap().category,
            "signature"
        );
        assert!(classify("res/", false).is_none());
    }

    #[test]
    fn the_estimate_leaves_out_other_abis_and_densities() {
        let files = vec![
            file("classes.dex", false, 1000),
            file("lib/arm64-v8a/a.so", false, 500),
            file("lib/x86_64/a.so", false, 600),
            file("res/drawable-xxhdpi/a.png", false, 70),
            file("res/drawable-mdpi/a.png", false, 30),
            file("res/drawable-nodpi/b.png", false, 5),
        ];
        let s = summary(&files);
        assert_eq!(s["compressed"], 2205);
        assert_eq!(s["categories"]["native"]["compressed"], 1100);
        assert_eq!(
            s["estimate"]["by_abi_density"]["arm64-v8a/xxhdpi"],
            1000 + 5 + 500 + 70
        );
        assert_eq!(
            s["estimate"]["by_abi_density"]["x86_64/mdpi"],
            1000 + 5 + 600 + 30
        );
        assert_eq!(largest(&files, 2)[0]["name"], "classes.dex");
    }

    #[test]
    fn a_comparison_lists_the_biggest_changes_first() {
        let old = vec![
            file("classes.dex", false, 1000),
            file("assets/old.bin", false, 50),
        ];
        let new = vec![
            file("classes.dex", false, 1300),
            file("lib/arm64-v8a/a.so", false, 400),
        ];
        let diff = compare(&new, &old, &summary(&new), &summary(&old), 10);
        assert_eq!(diff["summary"]["compressed"], 650);
        assert_eq!(diff["summary"]["categories"]["assets"]["compressed"], -50);
        assert_eq!(diff["files"][0]["name"], "lib/arm64-v8a/a.so");
        assert_eq!(diff["files"][0]["change"], "added");
        assert_eq!(diff["files"][1]["delta"], 300);
        assert_eq!(diff["files"][2]["change"], "removed");
        assert_eq!(diff["changed_files"], 3);
    }

    #[test]
    fn bundletool_csv_becomes_rows() {
        let rows = size_rows(
            "SDK,ABI,SCREEN_DENSITY,MIN,MAX\n21-,arm64-v8a,XXHDPI,1200,1300\n21-,x86_64,MDPI,1100,1150\n",
        );
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["abi"], "arm64-v8a");
        assert_eq!(rows[1]["max"], 1150);
    }
}
