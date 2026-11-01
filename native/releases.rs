//! Release signing with file-based credentials and atomic publication.
use crate::{core::*, jobs, toolchain as tc};
use serde_json::{Value, json};
use std::{
    fs,
    io::Read,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
};
pub(crate) fn secret(value: &str) -> Result<PathBuf> {
    let path = secret_path(value, "the password or token file")?;
    let m = fs::metadata(&path)?;
    if !m.is_file() || m.len() > 65536 {
        return Err(Error::new("invalid_secret", "Use a small password file"));
    }
    if m.permissions().mode() & 0o077 != 0 {
        return Err(Error::new(
            "secret_permissions",
            "Password files must be readable only by their owner",
        )
        .remedy(format!("chmod 600 {}", path.display())));
    }
    let bytes = fs::read(&path)?;
    let password = bytes.strip_suffix(b"\n").unwrap_or(&bytes);
    if password.len() < 6 || password.contains(&b'\n') || password.contains(&0) {
        return Err(Error::new(
            "invalid_secret",
            "Use one password of at least six bytes",
        ));
    }
    Ok(path)
}
fn no_existing(value: &str) -> Result<PathBuf> {
    let p = output_path(value)?;
    if p.exists() {
        return Err(Error::new(
            "path_exists",
            "Choose a new release output path",
        ));
    }
    fs::create_dir_all(p.parent().unwrap())?;
    Ok(p)
}
pub fn handle(op: &str, a: &Value) -> Result {
    match op {
        "release.verify" => verify(&path(s(a, "path"), false, true)?),
        "release.version" => crate::release_version::version(a),
        "release.notes" => crate::release_notes::notes(a),
        "release.keystore" | "release.sign" => {
            secret(s(a, "password_file"))?;
            identifier(s(a, "alias"), "key alias", Some("[A-Za-z0-9_.-]{1,100}"))?;
            let output = no_existing(s(a, "output"))?;
            if op == "release.sign" {
                path(s(a, "keystore"), false, true)?;
                let input = path(s(a, "path"), false, true)?;
                if ![Some("apk"), Some("aab")].contains(&input.extension().and_then(|x| x.to_str()))
                {
                    return Err(Error::new("unsupported_artifact", "Select an APK or AAB"));
                }
            }
            let dir = home()?
                .join("releases")
                .join(uuid::Uuid::new_v4().simple().to_string());
            private_dir(&dir)?;
            let mut spec = a.clone();
            spec["output"] = json!(output);
            spec["password_file"] = json!(secret(s(a, "password_file"))?);
            if !s(a, "key_password_file").is_empty() {
                spec["key_password_file"] = json!(secret(s(a, "key_password_file"))?);
            }
            if op == "release.sign" {
                spec["path"] = json!(path(s(a, "path"), false, true)?);
                spec["keystore"] = json!(path(s(a, "keystore"), false, true)?);
            }
            let scratch = tempfile::Builder::new()
                .prefix(".tomacco-sign-")
                .tempdir_in(output.parent().unwrap())?;
            fs::set_permissions(scratch.path(), fs::Permissions::from_mode(0o700))?;
            spec["scratch"] = json!(scratch.path());
            spec["operation"] = json!(op);
            spec["report"] = json!(dir.join("report.json"));
            atomic_json(&dir.join("request.json"), &spec)?;
            let job = jobs::submit(
                if op == "release.keystore" {
                    "Create Android signing identity"
                } else {
                    "Sign Android release"
                },
                json!({"steps":[{"argv":[crate::core::own_executable()?,"_release",dir.join("request.json")],"artifacts":[output,dir.join("report.json")]}],"cwd":dir,"env":tc::environment()?,"timeout":600,"exclusive":format!("release:{}",output.display()),"always_artifacts":["report.json"],"temporary_directories":[jobs::temporary_directory(scratch.path())?]}),
            )?;
            let _ = scratch.keep();
            Ok(job)
        }
        _ => Err(Error::new("operation_not_found", op)),
    }
}
fn publish(source: &Path, target: &Path, private: bool) -> Result<()> {
    fs::set_permissions(
        source,
        fs::Permissions::from_mode(if private { 0o600 } else { 0o644 }),
    )?;
    fs::File::open(source)?.sync_all()?;
    fs::hard_link(source, target).map_err(|e| {
        if target.exists() {
            Error::new(
                "path_exists",
                "The output appeared while signing; it was preserved",
            )
        } else {
            e.into()
        }
    })?;
    fs::File::open(target.parent().unwrap())?.sync_all()?;
    Ok(())
}
pub fn worker(file: &str) -> Result<()> {
    let a = read_json(&path(file, false, true)?, None)?;
    let result = execute(&a);
    let report = match &result {
        Ok(v) => json!({"passed":true,"result":v,"created":now()}),
        Err(e) => json!({"passed":false,"error":e.value(),"created":now()}),
    };
    atomic_json(&path(s(&a, "report"), false, false)?, &report)?;
    result.map(|_| ())
}
fn execute(a: &Value) -> Result {
    let pass = secret(s(a, "password_file"))?;
    let output = no_existing(s(a, "output"))?;
    let scratch = path(s(a, "scratch"), true, true)?;
    let password = pass.to_string_lossy().into_owned();
    let alias = s(a, "alias");
    let key_password = scratch.join("key-password");
    let key_source = if !s(a, "key_password_file").is_empty() {
        secret(s(a, "key_password_file"))?
    } else {
        pass.clone()
    };
    atomic_write(&key_password, &fs::read(key_source)?)?;
    if s(a, "operation") == "release.keystore" {
        let key = scratch.join("identity.p12");
        let argv = vec![
            tc::find("keytool")?,
            "-genkeypair".into(),
            "-noprompt".into(),
            "-keystore".into(),
            key.to_string_lossy().into_owned(),
            "-storetype".into(),
            "PKCS12".into(),
            "-alias".into(),
            alias.into(),
            "-keyalg".into(),
            "RSA".into(),
            "-keysize".into(),
            "4096".into(),
            "-validity".into(),
            n(a, "validity_days").to_string(),
            "-dname".into(),
            s(a, "distinguished_name").into(),
            "-storepass:file".into(),
            password.clone(),
            "-keypass:file".into(),
            password,
        ];
        let result = tc::run(&argv, 120, None, true)?;
        publish(&key, &output, true)?;
        return Ok(json!({"keystore":artifact(&output,0.0)?,"alias":alias,"diagnostics":result}));
    }
    let input = path(s(a, "path"), false, true)?;
    let keystore = path(s(a, "keystore"), false, true)?;
    let signed = scratch.join(format!(
        "signed.{}",
        input.extension().unwrap().to_string_lossy()
    ));
    if input.extension().is_some_and(|x| x == "apk") {
        let aligned = scratch.join("aligned.apk");
        tc::run(
            &argv(&[
                &tc::find("zipalign")?,
                "-P",
                "16",
                "-f",
                "4",
                &input.to_string_lossy(),
                &aligned.to_string_lossy(),
            ]),
            120,
            None,
            true,
        )?;
        tc::run(
            &argv(&[
                &tc::find("apksigner")?,
                "sign",
                "--ks",
                &keystore.to_string_lossy(),
                "--ks-key-alias",
                alias,
                "--ks-pass",
                &format!("file:{password}"),
                "--key-pass",
                &format!("file:{}", key_password.display()),
                "--v4-signing-enabled",
                "false",
                "--out",
                &signed.to_string_lossy(),
                &aligned.to_string_lossy(),
            ]),
            120,
            None,
            true,
        )?;
    } else {
        tc::run(
            &argv(&[
                &tc::find("jarsigner")?,
                "-J-Duser.language=en",
                "-keystore",
                &keystore.to_string_lossy(),
                "-storepass:file",
                &password,
                "-keypass:file",
                &key_password.to_string_lossy(),
                "-sigalg",
                "SHA256withRSA",
                "-digestalg",
                "SHA-256",
                "-signedjar",
                &signed.to_string_lossy(),
                &input.to_string_lossy(),
                alias,
            ]),
            120,
            None,
            true,
        )?;
    }
    let verification = verify(&signed)?;
    publish(&signed, &output, false)?;
    Ok(json!({"artifact":artifact(&output,0.0)?,"verification":verification,"alias":alias}))
}
fn verify(file: &Path) -> Result {
    let mut head = [0; 4];
    fs::File::open(file)?.read_exact(&mut head)?;
    if head != *b"PK\x03\x04" {
        return Err(Error::new(
            "invalid_artifact",
            "Not a ZIP-based Android package",
        ));
    }
    let output = if file.extension().is_some_and(|x| x == "apk") {
        tc::run(
            &argv(&[
                &tc::find("apksigner")?,
                "verify",
                "--verbose",
                "--print-certs",
                &file.to_string_lossy(),
            ]),
            120,
            None,
            true,
        )?
    } else if file.extension().is_some_and(|x| x == "aab") {
        let result = tc::run(
            &argv(&[
                &tc::find("jarsigner")?,
                "-J-Duser.language=en",
                "-verify",
                "-certs",
                &file.to_string_lossy(),
            ]),
            120,
            None,
            true,
        )?;
        if !s(&result, "stdout").contains("jar verified.") {
            return Err(Error::new(
                "unsigned_artifact",
                "Bundle signature could not be verified",
            )
            .details(result));
        }
        result
    } else {
        return Err(Error::new("unsupported_artifact", "Select an APK or AAB"));
    };
    Ok(json!({"verified":true,"artifact":artifact(file,0.0)?,"details":output}))
}
