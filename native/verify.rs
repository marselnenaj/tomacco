//! `tomaccoctl verify FILE`: checks a downloaded release file against its
//! `.sha256` and its detached Ed25519 `.sig` with the release keys compiled
//! into this program, the same check `update.apply` makes before it installs.
//! A release's provenance (an in-toto statement) is checked the same way, and
//! then each file it lists that lies beside it is checked against its digest.
use crate::{core::*, product::*, update};
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use std::{
    io::Write,
    path::{Path, PathBuf},
};

/// Larger than any release file; the portable package is about 180 MB.
const FILE_LIMIT: u64 = 1024 * 1024 * 1024;
const SMALL_LIMIT: u64 = 4096;
const STATEMENT: &str = "https://in-toto.io/Statement/v1";
const USAGE: &str =
    "Usage: tomaccoctl verify FILE [--sig FILE.sig] [--sha256 FILE.sha256] [--json]";

#[derive(Debug, Default, PartialEq)]
struct Request {
    file: PathBuf,
    signature: Option<PathBuf>,
    checksum: Option<PathBuf>,
    json: bool,
}

fn parse(args: &[String]) -> Result<Request> {
    let mut request = Request::default();
    let mut file = None;
    let mut words = args.iter();
    let usage = |message: String| Error::new("invalid_request", message).remedy(USAGE.to_string());
    while let Some(word) = words.next() {
        match word.as_str() {
            "--json" => request.json = true,
            "--sig" | "--sha256" => {
                let value = words
                    .next()
                    .ok_or_else(|| usage(format!("{word} needs a file")))?;
                let slot = if word == "--sig" {
                    &mut request.signature
                } else {
                    &mut request.checksum
                };
                *slot = Some(PathBuf::from(value));
            }
            flag if flag.starts_with("--") => {
                let near = closest(flag, ["--sig", "--sha256", "--json"]);
                return Err(
                    Error::new("invalid_request", format!("Unknown option: {flag}")).remedy(
                        near.map_or(USAGE.to_string(), |near| format!("Did you mean {near}?")),
                    ),
                );
            }
            _ if file.is_some() => return Err(usage("Name one file to verify".into())),
            _ => file = Some(PathBuf::from(word)),
        }
    }
    request.file = file.ok_or_else(|| usage("Name the file to verify".into()))?;
    Ok(request)
}

fn beside(file: &Path, extension: &str) -> PathBuf {
    let mut name = file.as_os_str().to_os_string();
    name.push(extension);
    PathBuf::from(name)
}

fn read_limited(path: &Path, limit: u64) -> std::result::Result<Vec<u8>, String> {
    let size = std::fs::metadata(path)
        .map_err(|error| format!("cannot read {}: {error}", path.display()))?
        .len();
    if size > limit {
        return Err(format!("{} is larger than {limit} bytes", path.display()));
    }
    std::fs::read(path).map_err(|error| format!("cannot read {}: {error}", path.display()))
}

/// Checks `request.file` against `keys` and returns the report; `verified`
/// says whether both checks passed. The checksum is checked when its file is
/// there or named; the signature is required.
fn check(request: &Request, keys: &[[u8; 32]]) -> Result<Value> {
    let data = read_limited(&request.file, FILE_LIMIT)
        .map_err(|why| Error::new("verify_unreadable", format!("Verification failed: {why}")))?;
    let actual = digest(&data);
    let checksum_path = request
        .checksum
        .clone()
        .unwrap_or_else(|| beside(&request.file, ".sha256"));
    let checksum = if request.checksum.is_none() && !checksum_path.exists() {
        json!({"state":"absent","path":checksum_path})
    } else {
        match read_limited(&checksum_path, SMALL_LIMIT) {
            Err(why) => json!({"state":"unreadable","path":checksum_path,"reason":why}),
            Ok(text) => {
                let text = String::from_utf8_lossy(&text);
                let expected = text.split_whitespace().next().unwrap_or("").to_lowercase();
                let state = if expected == actual { "ok" } else { "mismatch" };
                json!({"state":state,"path":checksum_path,"expected":expected})
            }
        }
    };
    let signature_path = request
        .signature
        .clone()
        .unwrap_or_else(|| beside(&request.file, ".sig"));
    let signature = if keys.is_empty() {
        json!({"state":"no_key","path":signature_path,
            "reason":format!("this build of {PRODUCT_NAME} has no release key")})
    } else {
        match read_limited(&signature_path, SMALL_LIMIT) {
            Err(why) => json!({"state":"missing","path":signature_path,"reason":why}),
            Ok(signature) => match update::signing_key(keys, &data, &signature) {
                Ok(index) => json!({"state":"ok","path":signature_path,
                    "key":if index == 0 { "current" } else { "next" },
                    "key_base64":STANDARD.encode(keys[index])}),
                Err(why) => json!({"state":"invalid","path":signature_path,
                    "reason":why.replace("the package", "the file")}),
            },
        }
    };
    let mut verified =
        matches!(s(&checksum, "state"), "ok" | "absent") && signature["state"] == "ok";
    let mut report = json!({
        "file":request.file,
        "size":data.len(),
        "sha256":actual,
        "checksum":checksum,
        "signature":signature,
    });
    if let Some(provenance) = provenance(&request.file, &data) {
        verified &= provenance["subjects"].as_array().is_some_and(|subjects| {
            subjects
                .iter()
                .all(|subject| subject["state"] != "mismatch")
        });
        report["provenance"] = provenance;
    }
    report["verified"] = json!(verified);
    Ok(report)
}

/// For a release's provenance, what it says and how the files it lists that
/// lie beside it compare with their digests. None for any other file.
fn provenance(file: &Path, data: &[u8]) -> Option<Value> {
    let statement: Value = serde_json::from_slice(data).ok()?;
    if statement["_type"] != STATEMENT {
        return None;
    }
    let directory = file.parent().unwrap_or(Path::new("."));
    let subjects: Vec<Value> = statement["subject"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|subject| {
            let name = s(subject, "name");
            let expected = s(&subject["digest"], "sha256").to_lowercase();
            // Only a plain file name beside the statement is looked at.
            let plain = !name.is_empty()
                && Path::new(name).file_name().is_some_and(|base| base == name)
                && name != ".."
                && name != ".";
            let state = match plain.then(|| read_limited(&directory.join(name), FILE_LIMIT)) {
                None => "invalid",
                Some(Err(_)) => "absent",
                Some(Ok(bytes)) if digest(&bytes) == expected => "ok",
                Some(Ok(_)) => "mismatch",
            };
            json!({"name":name,"sha256":expected,"state":state})
        })
        .collect();
    let definition = &statement["predicate"]["buildDefinition"];
    let commit = definition["resolvedDependencies"]
        .as_array()
        .into_iter()
        .flatten()
        .find_map(|entry| entry["digest"]["gitCommit"].as_str())
        .unwrap_or("");
    Some(json!({
        "predicate_type":statement["predicateType"],
        "ref":definition["externalParameters"]["ref"],
        "commit":commit,
        "source_date_epoch":definition["internalParameters"]["SOURCE_DATE_EPOCH"],
        "started_on":statement["predicate"]["runDetails"]["metadata"]["startedOn"],
        "subjects":subjects,
    }))
}

/// The envelope `--json` prints: the report as the result when verified,
/// otherwise an error that carries it.
fn envelope(report: &Value) -> Result {
    if report["verified"] == true {
        return Ok(report.clone());
    }
    let mismatched: Vec<&str> = report["provenance"]["subjects"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|subject| subject["state"] == "mismatch")
        .map(|subject| s(subject, "name"))
        .collect();
    let (code, message) = if report["checksum"]["state"] == "mismatch" {
        (
            "verify_checksum_mismatch",
            "The file does not match its checksum".to_string(),
        )
    } else if report["signature"]["state"] == "ok" && !mismatched.is_empty() {
        (
            "verify_subject_mismatch",
            format!(
                "Files do not match the provenance: {}",
                mismatched.join(", ")
            ),
        )
    } else if report["checksum"]["state"] == "unreadable" {
        (
            "verify_checksum_unreadable",
            format!("The checksum: {}", s(&report["checksum"], "reason")),
        )
    } else {
        let code = match s(&report["signature"], "state") {
            "missing" => "verify_unsigned",
            "no_key" => "verify_no_key",
            _ => "verify_signature_invalid",
        };
        (
            code,
            format!("The signature: {}", s(&report["signature"], "reason")),
        )
    };
    Err(Error::new(code, message)
        .remedy("Do not install this file. Download it again from the release page")
        .details(json!({ "report": report })))
}

/// The plain-text report.
fn text(report: &Value) -> String {
    let checksum = &report["checksum"];
    let signature = &report["signature"];
    let path = |value: &Value| s(value, "path").to_string();
    let mut out = format!("{}\n", s(report, "file"));
    out += &match s(checksum, "state") {
        "ok" => format!("  SHA-256    ok        {}\n", s(report, "sha256")),
        "absent" => format!(
            "  SHA-256    -         {} (no {})\n",
            s(report, "sha256"),
            path(checksum)
        ),
        "mismatch" => format!(
            "  SHA-256    MISMATCH  {}, but {} says {}\n",
            s(report, "sha256"),
            path(checksum),
            s(checksum, "expected")
        ),
        _ => format!("  SHA-256    FAILED    {}\n", s(checksum, "reason")),
    };
    out += &match s(signature, "state") {
        "ok" => format!(
            "  Signature  ok        {PRODUCT_NAME} release key ({}), {}\n",
            s(signature, "key"),
            path(signature)
        ),
        _ => format!("  Signature  FAILED    {}\n", s(signature, "reason")),
    };
    let provenance = &report["provenance"];
    if provenance.is_object() {
        out += &format!(
            "  Provenance {} at commit {}, SOURCE_DATE_EPOCH {}, started {}\n",
            s(provenance, "ref"),
            s(provenance, "commit"),
            s(provenance, "source_date_epoch"),
            s(provenance, "started_on")
        );
        for subject in provenance["subjects"].as_array().into_iter().flatten() {
            out += &match s(subject, "state") {
                "ok" => format!("  File       ok        {}\n", s(subject, "name")),
                "absent" => format!("  File       -         {} (not here)\n", s(subject, "name")),
                "mismatch" => format!("  File       MISMATCH  {}\n", s(subject, "name")),
                _ => format!(
                    "  File       skipped   {:?} is not a file name\n",
                    s(subject, "name")
                ),
            };
        }
    }
    out += if report["verified"] == true {
        "Verified.\n"
    } else {
        "Not verified: do not install this file.\n"
    };
    out
}

/// `tomaccoctl verify FILE [--sig FILE] [--sha256 FILE] [--json]`. Exits 0
/// only when the file is verified.
pub fn command(args: &[String]) -> Result<i32> {
    let request = parse(args)?;
    let report = check(&request, &update::trusted_keys())?;
    let verified = report["verified"] == true;
    let output = if request.json {
        format!("{}\n", crate::api::envelope(envelope(&report)))
    } else {
        text(&report)
    };
    let _ = std::io::stdout().write_all(output.as_bytes());
    Ok(if verified { 0 } else { 1 })
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};

    fn words(line: &[&str]) -> Vec<String> {
        line.iter().map(|word| word.to_string()).collect()
    }

    struct Files {
        directory: tempfile::TempDir,
        current: SigningKey,
        upcoming: SigningKey,
    }
    impl Files {
        fn new() -> Self {
            Self {
                directory: tempfile::tempdir().unwrap(),
                current: SigningKey::from_bytes(&[3; 32]),
                upcoming: SigningKey::from_bytes(&[4; 32]),
            }
        }
        fn keys(&self) -> Vec<[u8; 32]> {
            vec![
                self.current.verifying_key().to_bytes(),
                self.upcoming.verifying_key().to_bytes(),
            ]
        }
        /// A file with its checksum and a signature by `key`.
        fn release(&self, name: &str, data: &[u8], key: &SigningKey) -> PathBuf {
            let file = self.directory.path().join(name);
            std::fs::write(&file, data).unwrap();
            std::fs::write(
                beside(&file, ".sha256"),
                format!("{}  {name}\n", digest(data)),
            )
            .unwrap();
            std::fs::write(
                beside(&file, ".sig"),
                STANDARD.encode(key.sign(data).to_bytes()),
            )
            .unwrap();
            file
        }
        fn check(&self, file: &Path) -> Value {
            check(
                &Request {
                    file: file.to_path_buf(),
                    ..Request::default()
                },
                &self.keys(),
            )
            .unwrap()
        }
    }

    #[test]
    fn the_command_line_names_one_file_and_optional_signature_and_checksum() {
        let request = parse(&words(&[
            "a.tar.gz", "--sig", "s", "--sha256", "c", "--json",
        ]))
        .unwrap();
        assert_eq!(
            request,
            Request {
                file: "a.tar.gz".into(),
                signature: Some("s".into()),
                checksum: Some("c".into()),
                json: true,
            }
        );
        assert!(parse(&[]).is_err());
        assert!(parse(&words(&["a", "b"])).is_err());
        assert!(parse(&words(&["a", "--sig"])).is_err());
        let error = parse(&words(&["a", "--sha265"])).unwrap_err();
        assert_eq!(error.remedy, "Did you mean --sha256?");
    }

    #[test]
    fn a_file_with_a_matching_checksum_and_signature_is_verified() {
        let files = Files::new();
        let file = files.release("p.tar.gz", b"package", &files.current);
        let report = files.check(&file);
        assert_eq!(report["verified"], true, "{report}");
        assert_eq!(report["checksum"]["state"], "ok");
        assert_eq!(report["signature"]["key"], "current");
        assert_eq!(report["sha256"], digest(b"package"));
        let shown = text(&report);
        assert!(shown.contains("SHA-256    ok"), "{shown}");
        assert!(shown.contains("release key (current)"), "{shown}");
        assert!(shown.ends_with("Verified.\n"), "{shown}");
        assert_eq!(envelope(&report).unwrap(), report);
    }

    #[test]
    fn the_next_key_is_accepted_and_an_unknown_key_refused() {
        let files = Files::new();
        let file = files.release("p.tar.gz", b"package", &files.upcoming);
        let report = files.check(&file);
        assert_eq!(report["verified"], true, "{report}");
        assert_eq!(report["signature"]["key"], "next");
        let file = files.release("q.tar.gz", b"package", &SigningKey::from_bytes(&[5; 32]));
        let report = files.check(&file);
        assert_eq!(report["verified"], false);
        assert_eq!(report["signature"]["state"], "invalid");
        let error = envelope(&report).unwrap_err();
        assert_eq!(error.code, "verify_signature_invalid");
        assert!(error.message.contains("release keys"), "{}", error.message);
        assert!(text(&report).contains("Not verified"));
    }

    #[test]
    fn a_changed_file_fails_both_checks() {
        let files = Files::new();
        let file = files.release("p.tar.gz", b"package", &files.current);
        std::fs::write(&file, b"packagf").unwrap();
        let report = files.check(&file);
        assert_eq!(report["checksum"]["state"], "mismatch");
        assert_eq!(report["signature"]["state"], "invalid");
        assert_eq!(
            envelope(&report).unwrap_err().code,
            "verify_checksum_mismatch"
        );
        assert!(text(&report).contains("MISMATCH"));
    }

    #[test]
    fn a_missing_signature_fails_and_a_missing_checksum_is_only_reported() {
        let files = Files::new();
        let file = files.release("p.tar.gz", b"package", &files.current);
        std::fs::remove_file(beside(&file, ".sha256")).unwrap();
        let report = files.check(&file);
        assert_eq!(report["checksum"]["state"], "absent");
        assert_eq!(report["verified"], true);
        // A checksum named on the command line must be there.
        let named = check(
            &Request {
                file: file.clone(),
                checksum: Some(files.directory.path().join("none.sha256")),
                ..Request::default()
            },
            &files.keys(),
        )
        .unwrap();
        assert_eq!(named["checksum"]["state"], "unreadable");
        assert_eq!(named["verified"], false);
        std::fs::remove_file(beside(&file, ".sig")).unwrap();
        let report = files.check(&file);
        assert_eq!(report["signature"]["state"], "missing");
        assert_eq!(envelope(&report).unwrap_err().code, "verify_unsigned");
    }

    #[test]
    fn signature_and_checksum_can_be_named_elsewhere() {
        let files = Files::new();
        let file = files.release("p.tar.gz", b"package", &files.current);
        let other = files.directory.path().join("other");
        std::fs::create_dir(&other).unwrap();
        for extension in [".sig", ".sha256"] {
            std::fs::rename(beside(&file, extension), other.join(extension)).unwrap();
        }
        let report = check(
            &Request {
                file,
                signature: Some(other.join(".sig")),
                checksum: Some(other.join(".sha256")),
                json: false,
            },
            &files.keys(),
        )
        .unwrap();
        assert_eq!(report["verified"], true, "{report}");
        assert_eq!(report["checksum"]["state"], "ok");
    }

    #[test]
    fn a_build_without_a_key_verifies_nothing() {
        let files = Files::new();
        let file = files.release("p.tar.gz", b"package", &files.current);
        let report = check(
            &Request {
                file,
                ..Request::default()
            },
            &[],
        )
        .unwrap();
        assert_eq!(report["verified"], false);
        assert_eq!(envelope(&report).unwrap_err().code, "verify_no_key");
    }

    /// A provenance lists the release's files: those beside it are checked,
    /// a missing one is only reported, a changed one fails.
    #[test]
    fn a_provenance_checks_the_files_it_lists() {
        let files = Files::new();
        let package = files.release("p.tar.gz", b"package", &files.current);
        let statement = |names: &[(&str, &[u8])]| {
            json!({
                "_type":STATEMENT,
                "subject":names.iter().map(|(name, data)| json!({"name":name,"digest":{"sha256":digest(data)}})).collect::<Vec<_>>(),
                "predicateType":"https://slsa.dev/provenance/v1",
                "predicate":{
                    "buildDefinition":{
                        "externalParameters":{"ref":"refs/tags/v1.2.3"},
                        "internalParameters":{"SOURCE_DATE_EPOCH":"1700000000"},
                        "resolvedDependencies":[{"uri":"git+https://x","digest":{"gitCommit":"c0ffee"}}],
                    },
                    "runDetails":{"metadata":{"startedOn":"2026-11-01T12:00:00Z"}},
                },
            })
            .to_string()
        };
        let text_of = |name: &str, names: &[(&str, &[u8])]| {
            files.release(name, statement(names).as_bytes(), &files.current)
        };
        let good = text_of(
            "t-provenance.intoto.json",
            &[("p.tar.gz", b"package"), ("q.tar.gz", b"absent")],
        );
        let report = files.check(&good);
        assert_eq!(report["verified"], true, "{report}");
        let provenance = &report["provenance"];
        assert_eq!(provenance["commit"], "c0ffee");
        assert_eq!(provenance["ref"], "refs/tags/v1.2.3");
        assert_eq!(provenance["subjects"][0]["state"], "ok");
        assert_eq!(provenance["subjects"][1]["state"], "absent");
        let shown = text(&report);
        assert!(
            shown.contains("refs/tags/v1.2.3 at commit c0ffee"),
            "{shown}"
        );
        assert!(shown.contains("File       ok        p.tar.gz"), "{shown}");
        assert!(shown.contains("q.tar.gz (not here)"), "{shown}");
        // A changed package fails the provenance, though its signature file was not consulted.
        std::fs::write(&package, b"packagf").unwrap();
        let report = files.check(&good);
        assert_eq!(report["verified"], false);
        assert_eq!(report["provenance"]["subjects"][0]["state"], "mismatch");
        assert_eq!(
            envelope(&report).unwrap_err().code,
            "verify_subject_mismatch"
        );
        // Names that leave the directory are not followed.
        let sneaky = text_of(
            "u-provenance.intoto.json",
            &[("../p.tar.gz", b"package"), ("/etc/passwd", b"")],
        );
        let report = files.check(&sneaky);
        assert_eq!(report["provenance"]["subjects"][0]["state"], "invalid");
        assert_eq!(report["provenance"]["subjects"][1]["state"], "invalid");
        // A plain package has no provenance part.
        std::fs::write(&package, b"package").unwrap();
        assert!(files.check(&package).get("provenance").is_none());
    }

    /// The signature format of the release tooling: pure Ed25519 from OpenSSL,
    /// base64 in the `.sig` file. The key existed only to make this vector.
    #[test]
    fn a_signature_made_by_openssl_verifies() {
        let directory = tempfile::tempdir().unwrap();
        let file = directory.path().join("vector");
        std::fs::write(&file, b"tomacco update test vector\n").unwrap();
        std::fs::write(beside(&file, ".sig"), "5halaIQ2vA/cDQvsEGhCr+tf5XNGpYkivRJJuyFWnJlWT7OWl++YEjodY8i9WbfMl2roqF1WHUG22i04v6s4Ag==").unwrap();
        let key: [u8; 32] = STANDARD
            .decode("rH5agwRq7RWpPm2l+XkxGfnFvQ+yRcAw8nPMhuF5yuU=")
            .unwrap()
            .try_into()
            .unwrap();
        let report = check(
            &Request {
                file,
                ..Request::default()
            },
            &[key],
        )
        .unwrap();
        assert_eq!(report["verified"], true, "{report}");
    }
}
