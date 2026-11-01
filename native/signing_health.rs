//! release.signing_health: certificate expiry, key strength and Play's
//! signing setup for the keys of an Android keystore, read with keytool.
use crate::{core::*, toolchain as tc};
use regex::Regex;
use serde_json::{Value, json};

/// Google Play has required app signing keys to stay valid past this
/// moment (22 October 2033, UTC) since 2011.
const PLAY_MINIMUM_VALIDITY: i64 = 2_013_552_000;

/// Days since 1970-01-01 of a proleptic Gregorian date.
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = year.div_euclid(400);
    let yoe = year - era * 400;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// keytool's date, `Sun Nov 01 10:00:00 UTC 2026`, as Unix seconds. The
/// call runs keytool in UTC, so the zone name is not interpreted.
fn keytool_date(text: &str) -> Option<i64> {
    let parts: Vec<&str> = text.split_whitespace().collect();
    let [_, month, day, time, .., year] = parts[..] else {
        return None;
    };
    let month = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]
    .iter()
    .position(|m| *m == month)? as i64
        + 1;
    let hms: Vec<i64> = time.split(':').filter_map(|p| p.parse().ok()).collect();
    let [h, m, s] = hms[..] else { return None };
    let days = days_from_civil(year.parse().ok()?, month, day.parse().ok()?);
    Some(days * 86_400 + h * 3600 + m * 60 + s)
}

fn iso(seconds: i64) -> String {
    let days = seconds.div_euclid(86_400);
    // Inverse of days_from_civil.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!("{year:04}-{month:02}-{day:02}")
}

fn fingerprint(value: &str) -> String {
    value
        .chars()
        .filter(char::is_ascii_hexdigit)
        .collect::<String>()
        .to_lowercase()
}

/// Each entry of `keytool -list -v`, with its first (own) certificate.
fn entries(output: &str) -> Vec<Value> {
    let field = |block: &str, name: &str| {
        Regex::new(&format!(r"(?m)^\s*{}:\s*(.*?)\s*$", regex::escape(name)))
            .unwrap()
            .captures(block)
            .map(|c| c[1].to_string())
    };
    let valid = Regex::new(r"(?m)^\s*Valid from:\s*(.*?)\s+until:\s*(.*?)\s*$").unwrap();
    let key = Regex::new(r"^(\d+)-bit\s+(\w+)").unwrap();
    output
        .split("Alias name: ")
        .skip(1)
        .map(|block| {
            let alias = block.lines().next().unwrap_or("").trim().to_string();
            let certificate = block.split("Certificate[2]:").next().unwrap_or(block);
            let mut entry = json!({
                "alias": alias,
                "entry_type": field(block, "Entry type").unwrap_or_default(),
                "subject": field(certificate, "Owner"),
                "issuer": field(certificate, "Issuer"),
                "signature_algorithm": field(certificate, "Signature algorithm name"),
                "sha256": field(certificate, "SHA256").map(|f| fingerprint(&f)),
                "sha1": field(certificate, "SHA1").map(|f| fingerprint(&f)),
            });
            if let Some(c) = valid.captures(certificate) {
                entry["valid_from"] = keytool_date(&c[1]).map(iso).into();
                entry["valid_until"] = keytool_date(&c[2]).map(iso).into();
                entry["not_after"] = keytool_date(&c[2]).into();
            }
            if let Some(text) = field(certificate, "Subject Public Key Algorithm") {
                match key.captures(&text) {
                    Some(c) => {
                        entry["key_size"] = json!(c[1].parse::<u64>().unwrap_or(0));
                        entry["key_algorithm"] = json!(c[2].to_uppercase());
                    }
                    None => entry["key_algorithm"] = json!(text),
                }
            }
            entry
        })
        .collect()
}

/// Findings for each entry at `now`, warning `warn_days` ahead, and the
/// role each key has in the Play setup given.
fn assess(
    entries: &mut [Value],
    now: i64,
    warn_days: i64,
    upload: &str,
    app_signing: &str,
) -> Vec<Value> {
    let mut findings = Vec::new();
    let mut add = |alias: &str, severity: &str, code: &str, message: String| {
        findings.push(json!({"alias":alias,"severity":severity,"code":code,"message":message}));
    };
    for entry in entries.iter_mut() {
        let alias = s(entry, "alias").to_string();
        if !s(entry, "entry_type").contains("PrivateKey") {
            add(
                &alias,
                "info",
                "no_private_key",
                format!("{alias} holds only a certificate and cannot sign"),
            );
        }
        if let Some(until) = entry["not_after"].as_i64() {
            let days = (until - now).div_euclid(86_400);
            entry["days_left"] = json!(days);
            if until <= now {
                add(
                    &alias,
                    "error",
                    "expired",
                    format!("{alias} expired on {}", s(entry, "valid_until")),
                );
            } else if days < warn_days {
                add(
                    &alias,
                    "warning",
                    "expires_soon",
                    format!(
                        "{alias} expires in {days} days, on {}",
                        s(entry, "valid_until")
                    ),
                );
            }
            if until < PLAY_MINIMUM_VALIDITY && s(entry, "entry_type").contains("PrivateKey") {
                add(
                    &alias,
                    "warning",
                    "play_minimum_validity",
                    format!(
                        "{alias} ends before 22 October 2033, which Google Play requires of an app signing key"
                    ),
                );
            }
        } else {
            add(
                &alias,
                "warning",
                "validity_unknown",
                format!("keytool showed no validity for {alias}"),
            );
        }
        let algorithm = s(entry, "key_algorithm").to_string();
        let size = entry["key_size"].as_u64().unwrap_or(0);
        if (algorithm == "RSA" || algorithm == "DSA") && size > 0 && size < 2048 {
            add(
                &alias,
                "error",
                "weak_key",
                format!("{size}-bit {algorithm} is too short; use 2048 bits or more"),
            );
        } else if algorithm == "DSA" {
            add(
                &alias,
                "warning",
                "dsa_key",
                format!("{alias} is a DSA key; apksigner and Play favour RSA or EC"),
            );
        }
        let signature = s(entry, "signature_algorithm").to_uppercase();
        if signature.starts_with("MD5") || signature.starts_with("SHA1") {
            add(
                &alias,
                "warning",
                "weak_signature",
                format!(
                    "{alias}'s certificate is signed with {}",
                    s(entry, "signature_algorithm")
                ),
            );
        }
        let sha = s(entry, "sha256");
        if !sha.is_empty() && (!upload.is_empty() || !app_signing.is_empty()) {
            let role = if sha == upload {
                "upload"
            } else if sha == app_signing {
                "app_signing"
            } else {
                "none"
            };
            entry["play_role"] = json!(role);
        }
    }
    if !upload.is_empty() || !app_signing.is_empty() {
        let roles: Vec<&str> = entries.iter().map(|e| s(e, "play_role")).collect();
        if !roles.iter().any(|r| *r == "upload" || *r == "app_signing") {
            add(
                "",
                "error",
                "not_play_key",
                "No key in this keystore matches the certificates given for Google Play".into(),
            );
        } else if roles.contains(&"app_signing") && !upload.is_empty() && upload != app_signing {
            add("", "warning", "app_signing_key_local", "This keystore holds Play's app signing key; with Play App Signing, upload with the upload key and keep this one offline".into());
        }
    }
    findings
}

pub fn health(a: &Value) -> Result {
    let keystore = path(s(a, "keystore"), false, true)?;
    let password = crate::releases::secret(s(a, "password_file"))?;
    let alias = s(a, "alias");
    let upload = fingerprint(s(a, "upload_certificate_sha256"));
    let app_signing = fingerprint(s(a, "app_signing_certificate_sha256"));
    for (name, value) in [
        ("upload_certificate_sha256", &upload),
        ("app_signing_certificate_sha256", &app_signing),
    ] {
        if !value.is_empty() && value.len() != 64 {
            return Err(Error::new(
                "invalid_argument",
                format!("{name} is a SHA-256 fingerprint: 64 hexadecimal digits, colons allowed"),
            ));
        }
    }
    let mut command = argv(&[
        &tc::find("keytool")?,
        "-J-Duser.language=en",
        "-J-Duser.country=US",
        "-J-Duser.timezone=UTC",
        "-list",
        "-v",
        "-keystore",
        &keystore.to_string_lossy(),
        "-storepass:file",
        &password.to_string_lossy(),
    ]);
    if !alias.is_empty() {
        identifier(alias, "key alias", Some("[A-Za-z0-9_.-]{1,100}"))?;
        command.extend(argv(&["-alias", alias]));
    }
    let result = tc::run(&command, 60, None, false)?;
    let output = s(&result, "stdout");
    if result["exit_code"] != 0 || !output.contains("Alias name:") {
        let text = format!("{output}{}", s(&result, "stderr"));
        let code = if text.contains("password was incorrect") || text.contains("keystore password")
        {
            "wrong_password"
        } else if text.contains("does not exist") {
            "alias_not_found"
        } else {
            "keystore_unreadable"
        };
        return Err(Error::new(code, "keytool could not read the keystore")
            .details(json!({"output":text.lines().filter(|l| !l.trim().is_empty() && !l.starts_with('\t')).take(5).collect::<Vec<_>>()})));
    }
    let mut list = entries(output);
    let warn_days = a["warn_days"].as_u64().unwrap_or(365) as i64;
    let mut findings = assess(&mut list, now() as i64, warn_days, &upload, &app_signing);
    let mut value = json!({"keystore":keystore,"warn_days":warn_days,"entries":list});
    if !s(a, "artifact").is_empty() {
        let inspected = crate::android_package::inspect(&json!({"path":s(a,"artifact")}))?;
        let signers: Vec<String> = inspected["signing"]["certificates"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|c| fingerprint(s(c, "sha256")))
            .collect();
        let keys: Vec<&str> = value["entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| s(e, "sha256"))
            .collect();
        let matched = signers.iter().any(|s| keys.contains(&s.as_str()));
        if signers.is_empty() {
            findings.push(json!({"alias":"","severity":"error","code":"artifact_unsigned","message":"The artifact has no signature this check could read"}));
        } else if !matched {
            findings.push(json!({"alias":"","severity":"error","code":"artifact_other_key","message":"The artifact is signed with a key that is not in this keystore"}));
        }
        value["artifact"] =
            json!({"path":inspected["path"],"signers":signers,"matches_keystore":matched});
    }
    value["passed"] = json!(!findings.iter().any(|f| f["severity"] == "error"));
    value["findings"] = json!(findings);
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    const OUTPUT: &str = "Keystore type: PKCS12\nKeystore provider: SUN\n\nYour keystore contains 2 entries\n\n\
Alias name: upload\nCreation date: Nov 1, 2026\nEntry type: PrivateKeyEntry\nCertificate chain length: 1\nCertificate[1]:\n\
Owner: CN=Upload, O=Example\nIssuer: CN=Upload, O=Example\nSerial number: 1a2b\n\
Valid from: Sun Nov 01 10:00:00 UTC 2026 until: Thu Mar 19 10:00:00 UTC 2054\n\
Certificate fingerprints:\n\t SHA1: 00:11\n\t SHA256: AB:CD:EF\nSignature algorithm name: SHA384withRSA\n\
Subject Public Key Algorithm: 4096-bit RSA key\nVersion: 3\n\n\
*******************************************\n\n\n\
Alias name: old\nCreation date: Jan 5, 2016\nEntry type: PrivateKeyEntry\nCertificate chain length: 1\nCertificate[1]:\n\
Owner: CN=Old\nIssuer: CN=Old\nSerial number: 5\n\
Valid from: Tue Jan 05 09:00:00 UTC 2016 until: Sat Jan 02 09:00:00 UTC 2027\n\
Certificate fingerprints:\n\t SHA1: 22:33\n\t SHA256: 12:34\nSignature algorithm name: SHA1withRSA\n\
Subject Public Key Algorithm: 1024-bit RSA key\nVersion: 3\n";

    #[test]
    fn dates_round_trip_through_unix_days() {
        assert_eq!(days_from_civil(1970, 1, 1), 0);
        assert_eq!(
            keytool_date("Sat Oct 22 00:00:00 UTC 2033"),
            Some(PLAY_MINIMUM_VALIDITY)
        );
        assert_eq!(
            iso(keytool_date("Thu Feb 29 23:59:59 UTC 2024").unwrap()),
            "2024-02-29"
        );
        assert_eq!(keytool_date("not a date"), None);
    }

    #[test]
    fn entries_and_findings_are_read_from_keytool() {
        let mut list = entries(OUTPUT);
        assert_eq!(list.len(), 2);
        assert_eq!(
            (
                s(&list[0], "alias"),
                s(&list[0], "sha256"),
                s(&list[0], "key_algorithm")
            ),
            ("upload", "abcdef", "RSA")
        );
        assert_eq!(
            (list[0]["key_size"].as_u64(), s(&list[0], "valid_until")),
            (Some(4096), "2054-03-19")
        );
        let now = keytool_date("Sun Nov 01 12:00:00 UTC 2026").unwrap();
        let findings = assess(&mut list, now, 365, "abcdef", "9999");
        let codes: Vec<(&str, &str)> = findings
            .iter()
            .map(|f| (s(f, "alias"), s(f, "code")))
            .collect();
        assert_eq!(
            codes,
            [
                ("old", "expires_soon"),
                ("old", "play_minimum_validity"),
                ("old", "weak_key"),
                ("old", "weak_signature")
            ]
        );
        assert_eq!(
            (s(&list[0], "play_role"), s(&list[1], "play_role")),
            ("upload", "none")
        );
        assert_eq!(list[1]["days_left"], 61);
        // A keystore that holds neither of Play's keys, and an expired one.
        let mut list = entries(OUTPUT);
        let later = keytool_date("Sun Jan 03 00:00:00 UTC 2027").unwrap();
        let findings = assess(&mut list, later, 365, "ffff", "");
        assert!(findings.iter().any(|f| s(f, "code") == "expired"));
        assert_eq!(s(findings.last().unwrap(), "code"), "not_play_key");
        // Play's app signing key kept locally next to a separate upload key.
        let mut list = entries(OUTPUT);
        let findings = assess(&mut list, now, 365, "ffff", "1234");
        assert_eq!(s(findings.last().unwrap(), "code"), "app_signing_key_local");
    }
}
