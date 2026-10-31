//! Android packages on disk: what an APK or AAB declares, read without a device.
use crate::{core::*, toolchain as tc};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs::File,
    io::{Read, Seek, SeekFrom},
    path::Path,
};

/// One entry of a ZIP file's central directory.
struct Entry {
    name: String,
    method: u16,
    compressed: u64,
    local: u64,
}

fn invalid_zip() -> Error {
    Error::new(
        "invalid_artifact",
        "Not a readable ZIP-based Android package",
    )
}

fn u16_at(b: &[u8], i: usize) -> usize {
    u16::from_le_bytes([b[i], b[i + 1]]) as usize
}

fn u32_at(b: &[u8], i: usize) -> u32 {
    u32::from_le_bytes([b[i], b[i + 1], b[i + 2], b[i + 3]])
}

/// The central directory of a ZIP file. ZIP64 archives, which Android
/// packages are not in practice, are refused.
fn central_directory(path: &Path) -> Result<(File, Vec<Entry>)> {
    let mut file = File::open(path)?;
    let size = file.seek(SeekFrom::End(0))?;
    let tail = size.min(65_557);
    file.seek(SeekFrom::Start(size - tail))?;
    let mut end = vec![0; tail as usize];
    file.read_exact(&mut end)?;
    let at = (0..end.len().saturating_sub(21))
        .rev()
        .find(|&i| end[i..i + 4] == [0x50, 0x4b, 0x05, 0x06])
        .ok_or_else(invalid_zip)?;
    let length = u32_at(&end, at + 12);
    let offset = u32_at(&end, at + 16);
    if offset == u32::MAX || length == u32::MAX || u64::from(offset) + u64::from(length) > size {
        return Err(invalid_zip());
    }
    file.seek(SeekFrom::Start(offset.into()))?;
    let mut directory = vec![0; length as usize];
    file.read_exact(&mut directory)?;
    let mut entries = Vec::new();
    let mut i = 0;
    while i + 46 <= directory.len() && directory[i..i + 4] == [0x50, 0x4b, 0x01, 0x02] {
        let name = u16_at(&directory, i + 28);
        let extra = u16_at(&directory, i + 30);
        let comment = u16_at(&directory, i + 32);
        let start = i + 46;
        let bytes = directory.get(start..start + name).ok_or_else(invalid_zip)?;
        entries.push(Entry {
            name: String::from_utf8_lossy(bytes).into_owned(),
            method: u16_at(&directory, i + 10) as u16,
            compressed: u32_at(&directory, i + 20).into(),
            local: u32_at(&directory, i + 42).into(),
        });
        i = start + name + extra + comment;
    }
    Ok((file, entries))
}

/// Entry names of a ZIP file.
pub fn zip_entries(path: &Path) -> Result<Vec<String>> {
    Ok(central_directory(path)?
        .1
        .into_iter()
        .map(|e| e.name)
        .collect())
}

/// One entry of a ZIP file, read through its local header. Only stored and
/// deflated entries occur in packages.
fn zip_read(path: &Path, wanted: &str) -> Result<Option<Vec<u8>>> {
    let (mut file, entries) = central_directory(path)?;
    let Some(entry) = entries.into_iter().find(|e| e.name == wanted) else {
        return Ok(None);
    };
    if entry.compressed > 64 << 20 {
        return Err(invalid_zip());
    }
    let mut header = [0; 30];
    file.seek(SeekFrom::Start(entry.local))?;
    file.read_exact(&mut header)?;
    if header[..4] != [0x50, 0x4b, 0x03, 0x04] {
        return Err(invalid_zip());
    }
    let skip = u16_at(&header, 26) + u16_at(&header, 28);
    file.seek(SeekFrom::Current(skip as i64))?;
    let mut data = vec![0; entry.compressed as usize];
    file.read_exact(&mut data)?;
    match entry.method {
        0 => Ok(Some(data)),
        8 => {
            let mut out = Vec::new();
            flate2::read::DeflateDecoder::new(&data[..])
                .take(64 << 20)
                .read_to_end(&mut out)?;
            Ok(Some(out))
        }
        _ => Err(invalid_zip()),
    }
}

/// Native ABIs from `lib/ABI/` in an APK, or `MODULE/lib/ABI/` in an AAB.
fn abis(entries: &[String], bundle: bool) -> BTreeSet<String> {
    entries
        .iter()
        .filter_map(|name| {
            let parts: Vec<_> = name.split('/').collect();
            let parts = if bundle { parts.get(1..)? } else { &parts[..] };
            (parts.len() >= 3 && parts[0] == "lib" && !parts[1].is_empty())
                .then(|| parts[1].to_string())
        })
        .collect()
}

/// What `aapt2 dump badging` reports about an APK.
fn badging(output: &str) -> Value {
    let quoted = regex::Regex::new(r"([A-Za-z-]+)='([^']*)'").unwrap();
    let values = regex::Regex::new(r"'([^']*)'").unwrap();
    let mut value = json!({"permissions":[],"abis":[]});
    let mut permissions = BTreeSet::new();
    let mut abis = BTreeSet::new();
    for line in output.lines() {
        let Some((key, rest)) = line.split_once(':') else {
            continue;
        };
        let fields: std::collections::HashMap<_, _> = quoted
            .captures_iter(rest)
            .map(|c| (c[1].to_string(), c[2].to_string()))
            .collect();
        let first = values.captures(rest).map(|c| c[1].to_string());
        match key {
            "package" => {
                value["package"] = json!(fields.get("name"));
                value["version_code"] = json!(
                    fields
                        .get("versionCode")
                        .and_then(|v| v.parse::<u64>().ok())
                );
                value["version_name"] = json!(fields.get("versionName"));
                value["compile_sdk"] = json!(
                    fields
                        .get("compileSdkVersion")
                        .and_then(|v| v.parse::<u64>().ok())
                );
            }
            "minSdkVersion" | "sdkVersion" => {
                if value.get("min_sdk").is_none() {
                    value["min_sdk"] = json!(first.and_then(|v| v.parse::<u64>().ok()));
                }
            }
            "targetSdkVersion" => {
                value["target_sdk"] = json!(first.and_then(|v| v.parse::<u64>().ok()));
            }
            "uses-permission" | "uses-permission-sdk-23" => {
                if let Some(name) = fields.get("name") {
                    permissions.insert(name.clone());
                }
            }
            "native-code" | "alt-native-code" => {
                abis.extend(values.captures_iter(rest).map(|c| c[1].to_string()));
            }
            "launchable-activity" => {
                value["launchable_activity"] = json!(fields.get("name"));
            }
            _ => (),
        }
    }
    value["permissions"] = json!(permissions);
    value["abis"] = json!(abis);
    value
}

/// One field of a protobuf message: a varint or a length-delimited payload.
enum Field<'a> {
    Varint(u64),
    Bytes(&'a [u8]),
}

fn varint(buffer: &[u8], at: &mut usize) -> Option<u64> {
    let mut value = 0u64;
    for shift in (0..64).step_by(7) {
        let byte = *buffer.get(*at)?;
        *at += 1;
        value |= u64::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            return Some(value);
        }
    }
    None
}

fn fields(buffer: &[u8]) -> Option<Vec<(u64, Field<'_>)>> {
    let mut at = 0;
    let mut out = Vec::new();
    while at < buffer.len() {
        let key = varint(buffer, &mut at)?;
        let field = match key & 7 {
            0 => Field::Varint(varint(buffer, &mut at)?),
            1 => {
                at += 8;
                continue;
            }
            2 => {
                let length = varint(buffer, &mut at)? as usize;
                let bytes = buffer.get(at..at.checked_add(length)?)?;
                at += length;
                Field::Bytes(bytes)
            }
            5 => {
                at += 4;
                continue;
            }
            _ => return None,
        };
        out.push((key >> 3, field));
    }
    (at == buffer.len()).then_some(out)
}

fn text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).into_owned()
}

/// An element of a manifest compiled to aapt2's protobuf XML, as in an AAB.
#[derive(Debug, Default)]
struct Element {
    name: String,
    attributes: Vec<(String, String)>,
    children: Vec<Element>,
}

impl Element {
    fn attribute(&self, name: &str) -> Option<&str> {
        self.attributes
            .iter()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.as_str())
    }
    fn all<'a>(&'a self, name: &'a str) -> impl Iterator<Item = &'a Element> + 'a {
        self.children.iter().filter(move |c| c.name == name)
    }
}

/// The value of a compiled attribute: its original text, else the compiled
/// string or number (Item.str = 2, Item.prim = 7).
fn item_value(item: &[u8]) -> Option<String> {
    for (number, field) in fields(item)? {
        match (number, field) {
            (2, Field::Bytes(string)) => {
                for (n, f) in fields(string)? {
                    if let (1, Field::Bytes(v)) = (n, f) {
                        return Some(text(v));
                    }
                }
            }
            (7, Field::Bytes(primitive)) => {
                for (n, f) in fields(primitive)? {
                    match (n, f) {
                        (6, Field::Varint(v)) => return Some((v as u32 as i32).to_string()),
                        (7, Field::Varint(v)) => return Some(format!("0x{:08x}", v as u32)),
                        (8, Field::Varint(v)) => return Some((v != 0).to_string()),
                        _ => (),
                    }
                }
            }
            _ => (),
        }
    }
    None
}

fn element(node: &[u8]) -> Option<Element> {
    let mut found = None;
    for (number, field) in fields(node)? {
        if let (1, Field::Bytes(body)) = (number, field) {
            let mut element = Element::default();
            for (n, f) in fields(body)? {
                match (n, f) {
                    (3, Field::Bytes(v)) => element.name = text(v),
                    (4, Field::Bytes(attribute)) => {
                        let (mut name, mut value, mut compiled) = (String::new(), None, None);
                        for (n, f) in fields(attribute)? {
                            match (n, f) {
                                (2, Field::Bytes(v)) => name = text(v),
                                (3, Field::Bytes(v)) if !v.is_empty() => value = Some(text(v)),
                                (6, Field::Bytes(v)) => compiled = item_value(v),
                                _ => (),
                            }
                        }
                        if let Some(value) = value.or(compiled) {
                            element.attributes.push((name, value));
                        }
                    }
                    (5, Field::Bytes(child)) => {
                        if let Some(child) = self::element(child) {
                            element.children.push(child);
                        }
                    }
                    _ => (),
                }
            }
            found = Some(element);
        }
    }
    found
}

/// The package facts of an AAB's `base/manifest/AndroidManifest.xml`.
fn bundle_manifest(bytes: &[u8]) -> Result {
    let manifest = element(bytes)
        .filter(|e| e.name == "manifest")
        .ok_or_else(|| {
            Error::new(
                "invalid_artifact",
                "The bundle's base manifest is not a compiled AndroidManifest.xml",
            )
        })?;
    let number = |v: Option<&str>| v.and_then(|v| v.parse::<u64>().ok());
    let sdk = manifest.all("uses-sdk").next();
    let permissions: BTreeSet<_> = manifest
        .all("uses-permission")
        .chain(manifest.all("uses-permission-sdk-23"))
        .filter_map(|e| e.attribute("name"))
        .collect();
    Ok(json!({
        "package": manifest.attribute("package"),
        "version_code": number(manifest.attribute("versionCode")),
        "version_name": manifest.attribute("versionName"),
        "compile_sdk": number(manifest.attribute("compileSdkVersion")),
        "min_sdk": number(sdk.and_then(|e| e.attribute("minSdkVersion"))),
        "target_sdk": number(sdk.and_then(|e| e.attribute("targetSdkVersion"))),
        "permissions": permissions,
    }))
}

/// Signers from `apksigner verify --verbose --print-certs`.
fn apk_signing(output: &str, verified: bool) -> Value {
    let scheme =
        regex::Regex::new(r"^Verified using (v[0-9.]+) scheme [^:]*: (true|false)").unwrap();
    let signer =
        regex::Regex::new(r"^Signer #(\d+) certificate (DN|SHA-256 digest|SHA-1 digest): (.*)$")
            .unwrap();
    let mut schemes = Vec::new();
    let mut certificates: Vec<Value> = Vec::new();
    for line in output.lines().map(str::trim) {
        if let Some(c) = scheme.captures(line) {
            if &c[2] == "true" {
                schemes.push(c[1].to_string());
            }
        } else if let Some(c) = signer.captures(line) {
            let index: usize = c[1].parse().unwrap_or(1);
            while certificates.len() < index {
                certificates.push(json!({}));
            }
            let key = match &c[2] {
                "DN" => "subject",
                "SHA-256 digest" => "sha256",
                _ => "sha1",
            };
            certificates[index - 1][key] = json!(c[3].trim());
        }
    }
    json!({"checked":true,"signed":verified,"schemes":schemes,"certificates":certificates})
}

/// Signers from `keytool -printcert -jarfile`, for an AAB's JAR signature.
fn jar_signing(output: &str) -> Value {
    let mut certificates: Vec<Value> = Vec::new();
    for line in output.lines().map(str::trim) {
        if let Some(owner) = line.strip_prefix("Owner:") {
            certificates.push(json!({"subject":owner.trim()}));
        } else if let Some(last) = certificates.last_mut() {
            for (prefix, key) in [("SHA256:", "sha256"), ("SHA1:", "sha1")] {
                if let Some(digest) = line.strip_prefix(prefix) {
                    last[key] = json!(digest.trim().replace(':', "").to_ascii_lowercase());
                }
            }
        }
    }
    let signed = !certificates.is_empty();
    json!({"checked":true,"signed":signed,"schemes":if signed {json!(["jar"])} else {json!([])},"certificates":certificates})
}

fn unchecked(error: Error) -> Value {
    json!({"checked":false,"error":error.value()})
}

/// Package, version, SDK levels, permissions, ABIs and signing certificate of
/// an APK (through aapt2 and apksigner) or an AAB (read directly, signature
/// through keytool), without a device.
pub fn inspect(a: &Value) -> Result {
    let file = path(s(a, "path"), false, true)?;
    let extension = file
        .extension()
        .unwrap_or_default()
        .to_string_lossy()
        .to_ascii_lowercase();
    if extension != "apk" && extension != "aab" {
        return Err(Error::new(
            "invalid_artifact",
            "Select an .apk or .aab file",
        ));
    }
    let entries = zip_entries(&file)?;
    let name = file.to_string_lossy().into_owned();
    let mut value = if extension == "apk" {
        let result = tc::run(
            &argv(&[&tc::find("aapt2")?, "dump", "badging", &name]),
            60,
            None,
            true,
        )?;
        let mut value = badging(s(&result, "stdout"));
        let mut found: BTreeSet<String> = strings(&value["abis"]).into_iter().collect();
        found.extend(abis(&entries, false));
        value["abis"] = json!(found);
        value["signing"] = match tc::find("apksigner") {
            Ok(tool) => {
                let result = tc::run(
                    &argv(&[&tool, "verify", "--verbose", "--print-certs", &name]),
                    60,
                    None,
                    false,
                )?;
                let mut signing =
                    apk_signing(s(&result, "stdout"), result["exit_code"] == json!(0));
                if result["exit_code"] != json!(0) {
                    let errors: Vec<_> =
                        format!("{}\n{}", s(&result, "stdout"), s(&result, "stderr"))
                            .lines()
                            .filter_map(|l| {
                                l.trim()
                                    .strip_prefix("ERROR:")
                                    .map(|e| e.trim().to_string())
                            })
                            .collect();
                    signing["errors"] = json!(errors);
                }
                signing
            }
            Err(error) => unchecked(error),
        };
        value
    } else {
        let manifest = zip_read(&file, "base/manifest/AndroidManifest.xml")?.ok_or_else(|| {
            Error::new(
                "invalid_artifact",
                "The bundle has no base/manifest/AndroidManifest.xml",
            )
        })?;
        let mut value = bundle_manifest(&manifest)?;
        value["abis"] = json!(abis(&entries, true));
        value["modules"] = json!(
            entries
                .iter()
                .filter_map(|e| e.strip_suffix("/manifest/AndroidManifest.xml"))
                .collect::<BTreeSet<_>>()
        );
        value["signing"] = match tc::find("keytool") {
            Ok(tool) => {
                let result = tc::run(
                    &argv(&[
                        &tool,
                        "-J-Duser.language=en",
                        "-printcert",
                        "-jarfile",
                        &name,
                    ]),
                    60,
                    None,
                    false,
                )?;
                jar_signing(s(&result, "stdout"))
            }
            Err(error) => unchecked(error),
        };
        value
    };
    value["path"] = json!(file);
    value["format"] = json!(extension);
    value["size"] = json!(file.metadata()?.len());
    Ok(value)
}

/// Installs an AAB on one device as a job: bundletool builds the APK set for
/// that device, then installs it. Without signing options bundletool signs
/// with the debug keystore in ~/.android.
pub fn install_bundle(device: &str, bundle: &Path) -> Result {
    if !zip_entries(bundle)?.iter().any(|e| e == "BundleConfig.pb") {
        return Err(Error::new(
            "invalid_artifact",
            "Not an Android App Bundle: BundleConfig.pb is missing",
        ));
    }
    let tool = tc::command("bundletool")?;
    let adb = tc::find("adb")?;
    let scratch = tempfile::Builder::new()
        .prefix("bundle-")
        .tempdir_in(home()?)?;
    std::fs::set_permissions(
        scratch.path(),
        std::os::unix::fs::PermissionsExt::from_mode(0o700),
    )?;
    let apks = scratch.path().join("device.apks");
    let mut build = tool.clone();
    build.extend([
        "build-apks".into(),
        format!("--bundle={}", bundle.display()),
        format!("--output={}", apks.display()),
        "--connected-device".into(),
        format!("--device-id={device}"),
        format!("--adb={adb}"),
    ]);
    let mut install = tool;
    install.extend([
        "install-apks".into(),
        format!("--apks={}", apks.display()),
        format!("--device-id={device}"),
        format!("--adb={adb}"),
    ]);
    let mut env = serde_json::to_value(tc::environment()?)?;
    env["ANDROID_SERIAL"] = json!(device);
    let job = crate::jobs::submit(
        "Install app bundle",
        json!({"steps":[{"argv":build},{"argv":install}],"cwd":home()?,"timeout":600,"env":env,"temporary_directories":[crate::jobs::temporary_directory(scratch.path())?]}),
    )?;
    let _ = scratch.keep();
    Ok(job)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn put(out: &mut Vec<u8>, number: u64, bytes: &[u8]) {
        let mut key = (number << 3) | 2;
        loop {
            let byte = (key & 0x7f) as u8;
            key >>= 7;
            out.push(if key > 0 { byte | 0x80 } else { byte });
            if key == 0 {
                break;
            }
        }
        let mut length = bytes.len() as u64;
        loop {
            let byte = (length & 0x7f) as u8;
            length >>= 7;
            out.push(if length > 0 { byte | 0x80 } else { byte });
            if length == 0 {
                break;
            }
        }
        out.extend_from_slice(bytes);
    }

    /// An XmlAttribute; `number` compiles it to Item.prim.int_decimal_value
    /// without the original text, as aapt2 may.
    fn attribute(name: &str, value: &str, number: bool) -> Vec<u8> {
        let mut out = Vec::new();
        put(&mut out, 1, b"http://schemas.android.com/apk/res/android");
        put(&mut out, 2, name.as_bytes());
        if number {
            let mut primitive = vec![6 << 3];
            let mut v: u64 = value.parse().unwrap();
            loop {
                let byte = (v & 0x7f) as u8;
                v >>= 7;
                primitive.push(if v > 0 { byte | 0x80 } else { byte });
                if v == 0 {
                    break;
                }
            }
            let mut item = Vec::new();
            put(&mut item, 7, &primitive);
            put(&mut out, 6, &item);
        } else {
            put(&mut out, 3, value.as_bytes());
        }
        out
    }

    fn node(name: &str, attributes: &[Vec<u8>], children: &[Vec<u8>]) -> Vec<u8> {
        let mut element = Vec::new();
        put(&mut element, 3, name.as_bytes());
        for a in attributes {
            put(&mut element, 4, a);
        }
        for c in children {
            put(&mut element, 5, c);
        }
        let mut out = Vec::new();
        put(&mut out, 1, &element);
        out
    }

    pub fn manifest() -> Vec<u8> {
        node(
            "manifest",
            &[
                attribute("versionCode", "120", true),
                attribute("versionName", "1.2.0", false),
                attribute("package", "dev.example.app", false),
            ],
            &[
                node(
                    "uses-sdk",
                    &[
                        attribute("minSdkVersion", "26", true),
                        attribute("targetSdkVersion", "36", false),
                    ],
                    &[],
                ),
                node(
                    "uses-permission",
                    &[attribute("name", "android.permission.INTERNET", false)],
                    &[],
                ),
                node(
                    "uses-permission",
                    &[attribute("name", "android.permission.CAMERA", false)],
                    &[],
                ),
                node("application", &[], &[]),
            ],
        )
    }

    fn zip(path: &Path, files: &[(&str, &[u8])]) {
        // Stored entries only: enough for the reader under test.
        let mut out = Vec::new();
        let mut central = Vec::new();
        for (name, data) in files {
            let offset = out.len() as u32;
            out.extend_from_slice(&[0x50, 0x4b, 0x03, 0x04, 20, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
            out.extend_from_slice(&0u32.to_le_bytes());
            out.extend_from_slice(&(data.len() as u32).to_le_bytes());
            out.extend_from_slice(&(data.len() as u32).to_le_bytes());
            out.extend_from_slice(&(name.len() as u16).to_le_bytes());
            out.extend_from_slice(&0u16.to_le_bytes());
            out.extend_from_slice(name.as_bytes());
            out.extend_from_slice(data);
            central
                .extend_from_slice(&[0x50, 0x4b, 0x01, 0x02, 20, 0, 20, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
            central.extend_from_slice(&0u32.to_le_bytes());
            central.extend_from_slice(&(data.len() as u32).to_le_bytes());
            central.extend_from_slice(&(data.len() as u32).to_le_bytes());
            central.extend_from_slice(&(name.len() as u16).to_le_bytes());
            central.extend_from_slice(&[0; 12]);
            central.extend_from_slice(&offset.to_le_bytes());
            central.extend_from_slice(name.as_bytes());
        }
        let start = out.len() as u32;
        out.extend_from_slice(&central);
        out.extend_from_slice(&[0x50, 0x4b, 0x05, 0x06, 0, 0, 0, 0]);
        out.extend_from_slice(&(files.len() as u16).to_le_bytes());
        out.extend_from_slice(&(files.len() as u16).to_le_bytes());
        out.extend_from_slice(&(central.len() as u32).to_le_bytes());
        out.extend_from_slice(&start.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        File::create(path).unwrap().write_all(&out).unwrap();
    }

    #[test]
    fn a_bundle_manifest_and_abis_are_read_without_tools() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("app.aab");
        let manifest = manifest();
        zip(
            &file,
            &[
                ("base/manifest/AndroidManifest.xml", &manifest),
                ("base/lib/arm64-v8a/libcore.so", b"x"),
                ("base/lib/x86_64/libcore.so", b"x"),
                ("feature/manifest/AndroidManifest.xml", b""),
                ("BundleConfig.pb", b""),
            ],
        );
        let entries = zip_entries(&file).unwrap();
        assert_eq!(entries.len(), 5);
        assert_eq!(
            abis(&entries, true).into_iter().collect::<Vec<_>>(),
            ["arm64-v8a", "x86_64"]
        );
        let read = zip_read(&file, "base/manifest/AndroidManifest.xml")
            .unwrap()
            .unwrap();
        let value = bundle_manifest(&read).unwrap();
        assert_eq!(value["package"], "dev.example.app");
        assert_eq!(value["version_code"], 120);
        assert_eq!(value["version_name"], "1.2.0");
        assert_eq!(value["min_sdk"], 26);
        assert_eq!(value["target_sdk"], 36);
        assert_eq!(
            value["permissions"],
            json!(["android.permission.CAMERA", "android.permission.INTERNET"])
        );
        assert!(zip_read(&file, "missing").unwrap().is_none());
        assert!(bundle_manifest(b"\x0a\x02\x1a\x00").is_err());
        assert!(bundle_manifest(b"\xff\xff").is_err());
    }

    #[test]
    fn badging_and_signers_are_parsed() {
        let value = badging(
            "package: name='dev.example.app' versionCode='7' versionName='1.0 beta' compileSdkVersion='36'\n\
             minSdkVersion:'24'\ntargetSdkVersion:'36'\n\
             uses-permission: name='android.permission.INTERNET'\n\
             uses-permission: name='android.permission.WRITE_EXTERNAL_STORAGE' maxSdkVersion='28'\n\
             launchable-activity: name='dev.example.app.MainActivity'  label='' icon=''\n\
             native-code: 'arm64-v8a' 'x86_64'\nalt-native-code: 'armeabi-v7a'\n",
        );
        assert_eq!(value["package"], "dev.example.app");
        assert_eq!(value["version_code"], 7);
        assert_eq!(value["version_name"], "1.0 beta");
        assert_eq!(
            (value["min_sdk"].clone(), value["target_sdk"].clone()),
            (json!(24), json!(36))
        );
        assert_eq!(value["permissions"].as_array().unwrap().len(), 2);
        assert_eq!(value["abis"], json!(["arm64-v8a", "armeabi-v7a", "x86_64"]));
        assert_eq!(value["launchable_activity"], "dev.example.app.MainActivity");
        let signing = apk_signing(
            "Verifies\nVerified using v1 scheme (JAR signing): false\nVerified using v2 scheme (APK Signature Scheme v2): true\n\
             Verified using v3 scheme (APK Signature Scheme v3): true\nNumber of signers: 1\n\
             Signer #1 certificate DN: CN=Android Debug, O=Android, C=US\n\
             Signer #1 certificate SHA-256 digest: a0d759f3\nSigner #1 certificate SHA-1 digest: 4569\n",
            true,
        );
        assert_eq!(signing["schemes"], json!(["v2", "v3"]));
        assert_eq!(signing["certificates"][0]["sha256"], "a0d759f3");
        assert_eq!(
            signing["certificates"][0]["subject"],
            "CN=Android Debug, O=Android, C=US"
        );
        let jar = jar_signing(
            "Signer #1:\n\nCertificate #1:\nOwner: CN=Android Debug, O=Android, C=US\nIssuer: CN=Android Debug\n\
             Certificate fingerprints:\n\t SHA1: AB:CD\n\t SHA256: A0:D7:59:F3\n",
        );
        assert_eq!(jar["signed"], true);
        assert_eq!(jar["certificates"][0]["sha256"], "a0d759f3");
        assert_eq!(jar_signing("Not a signed jar file")["signed"], false);
    }
}
