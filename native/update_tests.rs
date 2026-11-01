use super::*;
use ed25519_dalek::{Signer, SigningKey};
use std::{cell::RefCell, collections::HashMap, os::unix::fs::PermissionsExt};

const SEED: [u8; 32] = [7; 32];

fn test_key() -> [u8; 32] {
    SigningKey::from_bytes(&SEED).verifying_key().to_bytes()
}

fn sign(package: &[u8]) -> Vec<u8> {
    STANDARD
        .encode(SigningKey::from_bytes(&SEED).sign(package).to_bytes())
        .into_bytes()
}

fn github(key: Option<[u8; 32]>) -> Endpoints {
    Endpoints {
        api: API.into(),
        api_host: API_HOST.into(),
        download_hosts: DOWNLOAD_HOSTS.iter().map(|h| h.to_string()).collect(),
        download_prefix: format!(
            "https://{GITHUB_HOST}/{REPO_OWNER}/{REPO_NAME}/releases/download/"
        ),
        loopback: false,
        public_keys: key.into_iter().collect(),
    }
}

/// Answers from a table of URLs, and records every request and its token.
#[derive(Default)]
struct Fake {
    pages: HashMap<String, (u16, Vec<u8>, Option<String>)>,
    offline: bool,
    requests: RefCell<Vec<(String, Option<String>)>>,
}
impl Fake {
    fn page(mut self, url: &str, status: u16, body: &[u8]) -> Self {
        self.pages
            .insert(url.to_string(), (status, body.to_vec(), None));
        self
    }
    fn redirect(mut self, url: &str, to: &str) -> Self {
        self.pages
            .insert(url.to_string(), (302, Vec::new(), Some(to.to_string())));
        self
    }
    fn count(&self) -> usize {
        self.requests.borrow().len()
    }
}
impl Transport for Fake {
    fn fetch(
        &self,
        request: &Fetch,
        sink: &mut dyn Write,
    ) -> std::result::Result<Reply, NetFailure> {
        self.requests.borrow_mut().push((
            request.url.to_string(),
            request.token.map(ToString::to_string),
        ));
        if self.offline {
            return Err(NetFailure::Offline);
        }
        let Some((status, body, location)) = self.pages.get(request.url) else {
            return Ok(Reply {
                status: 404,
                location: None,
            });
        };
        sink.write_all(body).map_err(|_| NetFailure::TooLarge)?;
        Ok(Reply {
            status: *status,
            location: location.clone(),
        })
    }
}

const LATEST: &str = "https://api.github.com/repos/marselnenaj/tomacco/releases/latest";
const DOWNLOADS: &str = "https://github.com/marselnenaj/tomacco/releases/download/v2.0.0";

fn release_json(tag: &str, draft: bool, prerelease: bool, assets: &[&str]) -> Vec<u8> {
    let assets: Vec<Value> = assets
        .iter()
        .map(|name| {
            json!({"name":name,"size":10,
                "browser_download_url":format!("{DOWNLOADS}/{name}"),
                "url":format!("https://api.github.com/repos/marselnenaj/tomacco/releases/assets/{}",name.len())})
        })
        .collect();
    json!({"tag_name":tag,"draft":draft,"prerelease":prerelease,
        "html_url":"https://github.com/marselnenaj/tomacco/releases/tag/v2.0.0",
        "body":"What changed","assets":assets})
    .to_string()
    .into_bytes()
}

fn tarball(entries: &[(&str, &[u8])]) -> Vec<u8> {
    let mut builder = tar::Builder::new(flate2::write::GzEncoder::new(
        Vec::new(),
        flate2::Compression::fast(),
    ));
    for (name, data) in entries {
        let mut header = tar::Header::new_gnu();
        header.set_size(data.len() as u64);
        header.set_mode(0o755);
        header.set_entry_type(tar::EntryType::Regular);
        header.set_cksum();
        builder.append_data(&mut header, name, *data).unwrap();
    }
    builder.into_inner().unwrap().finish().unwrap()
}

/// A tar entry named exactly as given: the tar crate's writer refuses `..`.
fn raw_tarball(name: &str, kind: u8, link: &str) -> Vec<u8> {
    let mut block = [0u8; 512];
    block[..name.len()].copy_from_slice(name.as_bytes());
    block[100..107].copy_from_slice(b"0000755");
    block[108..115].copy_from_slice(b"0000000");
    block[116..123].copy_from_slice(b"0000000");
    block[124..135].copy_from_slice(b"00000000000");
    block[136..147].copy_from_slice(b"00000000000");
    block[148..156].copy_from_slice(b"        ");
    block[156] = kind;
    block[157..157 + link.len()].copy_from_slice(link.as_bytes());
    let sum: u32 = block.iter().map(|byte| u32::from(*byte)).sum();
    block[148..154].copy_from_slice(format!("{sum:06o}").as_bytes());
    block[154] = 0;
    let mut tar = block.to_vec();
    tar.extend_from_slice(&[0u8; 1024]);
    let mut encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::fast());
    encoder.write_all(&tar).unwrap();
    encoder.finish().unwrap()
}

fn notify() -> Mode {
    mode_from(&json!({}), None)
}

fn run_check(fake: &Fake, mode: &Mode, manual: bool, state: &Value) -> Option<Value> {
    check_with(
        fake,
        &github(None),
        None,
        mode,
        manual,
        state,
        1_000_000.0,
        "1.0.0",
    )
}

#[test]
fn off_makes_no_request_and_only_the_user_can_ask() {
    let fake = Fake::default().page(LATEST, 200, &release_json("v2.0.0", false, false, &[]));
    let off = mode_from(&json!({"updates":{"mode":"off"}}), None);
    assert_eq!(off.effective, "off");
    assert!(run_check(&fake, &off, false, &json!({})).is_none());
    assert_eq!(fake.count(), 0);
    // Only an explicit manual check reaches the network.
    assert!(run_check(&fake, &off, true, &json!({})).is_some());
    assert_eq!(fake.count(), 1);
}

#[test]
fn forced_off_never_asks_even_the_user() {
    assert_eq!(forced_by(Some("off"), None), Some("build"));
    assert_eq!(forced_by(None, Some("off")), Some("environment"));
    assert_eq!(forced_by(None, Some("notify")), None);
    let fake = Fake::default();
    let forced = mode_from(&json!({"updates":{"mode":"auto"}}), Some("environment"));
    assert_eq!(forced.effective, "off");
    assert!(run_check(&fake, &forced, true, &json!({})).is_none());
    assert!(run_check(&fake, &forced, false, &json!({})).is_none());
    assert_eq!(fake.count(), 0);
    assert_eq!(
        admit(&json!({"manual":true}), &forced, &json!({}), "1.0.0")
            .unwrap_err()
            .code,
        "update_forced_off"
    );
}

#[test]
fn notify_is_the_default_and_checks_once_a_day() {
    assert_eq!(notify().effective, "notify");
    assert_eq!(
        mode_from(&json!({"updates":{"mode":"nonsense"}}), None).effective,
        "notify"
    );
    let fake = Fake::default();
    let time = 1_000_000.0;
    let recent = json!({"checked_at":time - 3600.0,"check":{"state":"up_to_date"}});
    assert!(run_check(&fake, &notify(), false, &recent).is_none());
    let old = json!({"checked_at":time - CHECK_INTERVAL,"check":{"state":"up_to_date"}});
    assert!(run_check(&fake, &notify(), false, &old).is_some());
    // A failed check is asked again sooner, but not at every start.
    let failed = json!({"checked_at":time - 3600.0,"check":{"state":"offline"}});
    assert!(run_check(&fake, &notify(), false, &failed).is_none());
    let failed = json!({"checked_at":time - RETRY_INTERVAL,"check":{"state":"offline"}});
    assert!(run_check(&fake, &notify(), false, &failed).is_some());
}

#[test]
fn versions_compare_semantically_and_never_downgrade() {
    let version = |text| Version::parse(text).unwrap();
    assert!(applies("1.9.0", &version("1.10.0")));
    assert!(applies("1.0.0-rc.1", &version("1.0.0")));
    assert!(!applies("1.0.0", &version("1.0.0")));
    assert!(!applies("2.0.0", &version("1.9.9")));
    assert!(!applies("not a version", &version("9.0.0")));
}

#[test]
fn a_downgrade_is_refused() {
    let fake = Fake::default().page(LATEST, 200, &release_json("v2.0.0", false, false, &[]));
    let scratch = tempfile::tempdir().unwrap();
    let error = download_and_verify(
        &fake,
        &github(Some(test_key())),
        None,
        "3.0.0",
        scratch.path(),
        &mut None,
        "stable",
    )
    .err()
    .unwrap();
    assert_eq!(error.code, "update_not_newer");
    assert_eq!(fake.count(), 1);
}

#[test]
fn a_private_repository_is_no_release_yet_rather_than_an_error() {
    let fake = Fake::default().page(LATEST, 404, b"{}");
    let found = run_check(&fake, &notify(), true, &json!({})).unwrap();
    assert_eq!(found["state"], "no_release");
    assert_eq!(found["message"], "No release is available yet.");
}

#[test]
fn offline_is_recorded_not_raised() {
    let fake = Fake {
        offline: true,
        ..Fake::default()
    };
    let found = run_check(&fake, &notify(), false, &json!({})).unwrap();
    assert_eq!(found["state"], "offline");
    assert_eq!(found["update_available"], false);
}

#[test]
fn drafts_and_prereleases_are_not_releases() {
    for body in [
        release_json("v2.0.0", true, false, &[]),
        release_json("v2.0.0", false, true, &[]),
        release_json("v2.0.0-rc.1", false, false, &[]),
    ] {
        assert!(parse_release(&body).unwrap().is_none());
    }
    assert!(parse_release(&release_json("nightly", false, false, &[])).is_err());
    assert!(parse_release(b"not json").is_err());
}

const RELEASES: &str = "https://api.github.com/repos/marselnenaj/tomacco/releases?per_page=30";

fn release_list(entries: &[(&str, bool, bool)]) -> Vec<u8> {
    let list: Vec<Value> = entries
        .iter()
        .map(|(tag, draft, prerelease)| {
            serde_json::from_slice(&release_json(tag, *draft, *prerelease, &[])).unwrap()
        })
        .collect();
    Value::from(list).to_string().into_bytes()
}

fn channel(name: &str) -> Mode {
    mode_from(&json!({"updates":{"channel":name}}), None)
}

#[test]
fn the_channel_is_stable_by_default_and_unknown_values_count_as_stable() {
    assert_eq!(notify().channel, "stable");
    assert_eq!(channel("prerelease").channel, "prerelease");
    assert_eq!(channel("nightly").channel, "stable");
}

#[test]
fn the_prerelease_channel_takes_the_highest_version_drafts_left_out() {
    let body = release_list(&[
        ("v2.1.0-rc.1", true, true),
        ("v2.0.0", false, false),
        ("v2.1.0-beta.2", false, true),
        ("v2.1.0-beta.10", false, true),
        ("nightly", false, true),
        ("v1.9.0", false, false),
    ]);
    let chosen = select_release(&body, true).unwrap().unwrap();
    assert_eq!(chosen.version.to_string(), "2.1.0-beta.10");
    // A final release outranks its own pre-releases.
    let body = release_list(&[("v2.1.0-rc.1", false, true), ("v2.1.0", false, false)]);
    assert_eq!(
        select_release(&body, true)
            .unwrap()
            .unwrap()
            .version
            .to_string(),
        "2.1.0"
    );
    // The same list for the stable channel: only full releases.
    let body = release_list(&[("v2.1.0-rc.1", false, true), ("v2.0.0", false, false)]);
    assert_eq!(
        select_release(&body, false)
            .unwrap()
            .unwrap()
            .version
            .to_string(),
        "2.0.0"
    );
    // A tag with a pre-release part counts as one even when GitHub does not mark it.
    let body = release_list(&[("v2.1.0-rc.1", false, false)]);
    assert!(select_release(&body, false).unwrap().is_none());
    assert!(select_release(&body, true).unwrap().is_some());
    assert!(select_release(&release_list(&[]), true).unwrap().is_none());
    assert!(select_release(b"{}", true).is_err());
}

#[test]
fn each_channel_asks_its_own_endpoint_and_records_itself() {
    let fake = Fake::default()
        .page(LATEST, 200, &release_json("v2.0.0", false, false, &[]))
        .page(
            RELEASES,
            200,
            &release_list(&[("v2.1.0-rc.1", false, true), ("v2.0.0", false, false)]),
        );
    let stable = run_check(&fake, &notify(), true, &json!({})).unwrap();
    assert_eq!(
        (stable["latest"].as_str(), stable["channel"].as_str()),
        (Some("2.0.0"), Some("stable"))
    );
    assert_eq!(stable["prerelease"], false);
    let early = run_check(&fake, &channel("prerelease"), true, &json!({})).unwrap();
    assert_eq!(
        (early["latest"].as_str(), early["channel"].as_str()),
        (Some("2.1.0-rc.1"), Some("prerelease"))
    );
    assert_eq!(early["prerelease"], true);
    let asked: Vec<String> = fake.requests.borrow().iter().map(|r| r.0.clone()).collect();
    assert_eq!(asked, vec![LATEST.to_string(), RELEASES.to_string()]);
}

#[test]
fn a_changed_channel_makes_the_recorded_check_stale() {
    let fake = Fake::default().page(
        RELEASES,
        200,
        &release_list(&[("v2.1.0-rc.1", false, true)]),
    );
    // Checked an hour ago on the stable channel: not due again for stable,
    // due at once for the pre-release channel.
    let state = json!({"checked_at":1_000_000.0 - 3600.0,
        "check":{"state":"up_to_date","channel":"stable"}});
    assert!(run_check(&fake, &notify(), false, &state).is_none());
    assert!(run_check(&fake, &channel("prerelease"), false, &state).is_some());
    // A check recorded before channels existed was a stable one.
    let old = json!({"checked_at":1_000_000.0 - 3600.0,"check":{"state":"up_to_date"}});
    assert!(run_check(&fake, &notify(), false, &old).is_none());
    // Off still sends nothing.
    let off = mode_from(
        &json!({"updates":{"mode":"off","channel":"prerelease"}}),
        None,
    );
    assert!(run_check(&fake, &off, false, &state).is_none());
}

#[test]
fn auto_installs_only_what_the_current_channel_found() {
    let state =
        json!({"check":{"latest":"2.1.0-rc.1","update_available":true,"channel":"prerelease"}});
    let auto =
        |channel: &str| mode_from(&json!({"updates":{"mode":"auto","channel":channel}}), None);
    assert!(admit(&json!({"auto":true}), &auto("prerelease"), &state, "2.0.0").is_ok());
    assert_eq!(
        admit(&json!({"auto":true}), &auto("stable"), &state, "2.0.0")
            .unwrap_err()
            .code,
        "update_not_available"
    );
}

#[test]
fn a_prerelease_installs_through_the_prerelease_channel_only() {
    let version = "2.1.0-rc.1";
    let name = package_name(&Version::parse(version).unwrap());
    let package = b"package";
    let signature = sign(package);
    let signature_name = format!("{name}.sig");
    let list = Value::from(vec![
        serde_json::from_slice::<Value>(&release_json(
            &format!("v{version}"),
            false,
            true,
            &[name.as_str(), signature_name.as_str()],
        ))
        .unwrap(),
    ])
    .to_string();
    let fake = Fake::default()
        .page(RELEASES, 200, list.as_bytes())
        .page(&format!("{DOWNLOADS}/{name}"), 200, package)
        .page(&format!("{DOWNLOADS}/{signature_name}"), 200, &signature);
    let scratch = tempfile::tempdir().unwrap();
    let verified = download_and_verify(
        &fake,
        &github(Some(test_key())),
        None,
        "2.0.0",
        scratch.path(),
        &mut None,
        "prerelease",
    )
    .unwrap();
    assert_eq!(verified.version.to_string(), version);
    // The stable channel asks for the latest release, which this fixture lacks.
    let error = download_and_verify(
        &fake,
        &github(Some(test_key())),
        None,
        "2.0.0",
        scratch.path(),
        &mut None,
        "stable",
    )
    .err()
    .unwrap();
    assert_eq!(error.code, "update_no_release");
}

#[test]
fn configure_takes_a_known_channel_only() {
    let mut config = json!({"updates":{"mode":"auto"}});
    configure(&mut config, &json!({"channel":"prerelease"})).unwrap();
    assert_eq!(
        config["updates"],
        json!({"mode":"auto","channel":"prerelease"})
    );
    assert!(configure(&mut config, &json!({"channel":"nightly"})).is_err());
    assert_eq!(config["updates"]["channel"], "prerelease");
}

#[test]
fn a_release_without_this_systems_package_does_not_apply() {
    let name = package_name(&Version::parse("2.0.0").unwrap());
    let fake = Fake::default().page(LATEST, 200, &release_json("v2.0.0", false, false, &[&name]));
    let found = run_check(&fake, &notify(), true, &json!({})).unwrap();
    assert_eq!(found["state"], "update_available");
    assert_eq!(found["latest"], "2.0.0");
    assert_eq!(found["notes"], "What changed");
    let fake = Fake::default().page(LATEST, 200, &release_json("v2.0.0", false, false, &[]));
    let found = run_check(&fake, &notify(), true, &json!({})).unwrap();
    assert_eq!(found["state"], "no_package");
    assert_eq!(found["update_available"], false);
    // Another architecture's package is no package for this computer.
    let other = if std::env::consts::ARCH == "aarch64" {
        "x86_64"
    } else {
        "aarch64"
    };
    let foreign = name.replace(std::env::consts::ARCH, other);
    let fake = Fake::default().page(
        LATEST,
        200,
        &release_json("v2.0.0", false, false, &[&foreign]),
    );
    let found = run_check(&fake, &notify(), true, &json!({})).unwrap();
    assert_eq!(found["state"], "no_package");
    assert_eq!(found["update_available"], false);
}

#[test]
fn only_github_over_tls_is_accepted() {
    let endpoints = github(None);
    let hosts = endpoints.download_hosts.clone();
    let good = "https://objects.githubusercontent.com/x";
    assert_eq!(
        permitted_url(good, &hosts, &endpoints).unwrap(),
        "objects.githubusercontent.com"
    );
    for bad in [
        "http://github.com/x",
        "https://github.com.evil.example/x",
        "https://evil.example/github.com/",
        "https://github.com@evil.example/x",
        "https://github.com:8443/x",
        "https://user:secret@github.com/x",
        "https://evil.example\\@github.com/x",
        "https://github.com/a b",
        "ftp://github.com/x",
        "file:///etc/passwd",
        "https://github.com/a/../../b",
        "https://github.com/%2e%2e/b",
    ] {
        assert_eq!(
            permitted_url(bad, &hosts, &endpoints).unwrap_err().code,
            "update_wrong_host",
            "{bad}"
        );
    }
}

#[test]
fn a_redirect_to_another_host_is_refused_and_the_token_stays_with_the_api() {
    let name = "package";
    let asset = json!({"name":name,"url":"https://api.github.com/repos/marselnenaj/tomacco/releases/assets/9",
        "browser_download_url":format!("{DOWNLOADS}/{name}")});
    let endpoints = github(Some(test_key()));
    let fake = Fake::default().redirect(
        &format!("{DOWNLOADS}/{name}"),
        "https://evil.example/package",
    );
    let error = fetch_asset(&fake, &endpoints, None, &asset, 100, &mut Vec::new()).unwrap_err();
    assert_eq!(error.code, "update_wrong_host");
    assert_eq!(fake.count(), 1);
    // A download address the release names on another host is never asked for.
    let foreign = json!({"name":name,"browser_download_url":"https://evil.example/package"});
    let fake = Fake::default();
    let error = fetch_asset(&fake, &endpoints, None, &foreign, 100, &mut Vec::new()).unwrap_err();
    assert_eq!(error.code, "update_wrong_host");
    assert_eq!(fake.count(), 0);
    // With a token: the API asset, then storage without it.
    let storage = "https://objects.githubusercontent.com/signed";
    let fake = Fake::default()
        .redirect(s(&asset, "url"), storage)
        .page(storage, 200, b"data");
    let mut body = Vec::new();
    fetch_asset(
        &fake,
        &endpoints,
        Some("secret-token"),
        &asset,
        100,
        &mut body,
    )
    .unwrap();
    assert_eq!(body, b"data");
    let requests = fake.requests.borrow();
    assert_eq!(requests[0].1.as_deref(), Some("secret-token"));
    assert_eq!(requests[1].1, None);
}

#[test]
fn the_token_is_not_on_the_command_line() {
    let request = Fetch {
        url: "https://api.github.com/x",
        accept: "application/json",
        token: Some("secret-token"),
        limit: 10,
        timeout: 5,
    };
    let arguments = curl_arguments(&request, Path::new("/tmp/h"), false);
    assert!(!arguments.iter().any(|part| part.contains("secret-token")));
    assert!(
        arguments
            .windows(2)
            .any(|w| w[0] == "--proto" && w[1] == "=https")
    );
    assert!(arguments.iter().all(|part| part != "--location"));
    assert!(curl_configuration(&request).contains("secret-token"));
}

#[test]
fn a_download_over_its_limit_stops() {
    let endpoints = github(Some(test_key()));
    let asset = json!({"name":"p","browser_download_url":format!("{DOWNLOADS}/p")});
    let fake = Fake::default().page(&format!("{DOWNLOADS}/p"), 200, &[0u8; 200]);
    let error = fetch_asset(&fake, &endpoints, None, &asset, 100, &mut Vec::new()).unwrap_err();
    assert_eq!(error.code, "update_too_large");
}

fn signed_release(version: &str, package: &[u8], signature: Option<&[u8]>) -> Fake {
    let name = package_name(&Version::parse(version).unwrap());
    let signature_name = format!("{name}.sig");
    let mut assets = vec![name.as_str()];
    if signature.is_some() {
        assets.push(&signature_name);
    }
    let mut fake = Fake::default()
        .page(
            LATEST,
            200,
            &release_json(&format!("v{version}"), false, false, &assets),
        )
        .page(&format!("{DOWNLOADS}/{name}"), 200, package);
    if let Some(signature) = signature {
        fake = fake.page(&format!("{DOWNLOADS}/{signature_name}"), 200, signature);
    }
    fake
}

fn verify_release(fake: &Fake) -> Result<Verified> {
    let scratch = tempfile::tempdir().unwrap();
    download_and_verify(
        fake,
        &github(Some(test_key())),
        None,
        "1.0.0",
        scratch.path(),
        &mut None,
        "stable",
    )
}

#[test]
fn a_correctly_signed_package_verifies() {
    let package = tarball(&[("a", b"b")]);
    let fake = signed_release("2.0.0", &package, Some(&sign(&package)));
    let verified = verify_release(&fake).ok().unwrap();
    assert_eq!(verified.version.to_string(), "2.0.0");
    assert_eq!(verified.package, package);
}

/// A rotation: this binary trusts the current key and the next one. A
/// package signed by the next key installs; one signed by any other is refused.
#[test]
fn during_a_rotation_the_next_key_is_accepted_and_an_unknown_key_refused() {
    let next = SigningKey::from_bytes(&[8; 32]);
    let keys = vec![test_key(), next.verifying_key().to_bytes()];
    let endpoints = Endpoints {
        public_keys: keys.clone(),
        ..github(None)
    };
    let package = tarball(&[("a", b"b")]);
    let by = |key: &SigningKey| STANDARD.encode(key.sign(&package).to_bytes()).into_bytes();
    let verify = |signature: &[u8]| {
        let scratch = tempfile::tempdir().unwrap();
        download_and_verify(
            &signed_release("2.0.0", &package, Some(signature)),
            &endpoints,
            None,
            "1.0.0",
            scratch.path(),
            &mut None,
            "stable",
        )
    };
    assert_eq!(verify(&by(&next)).ok().unwrap().package, package);
    assert_eq!(verify(&sign(&package)).ok().unwrap().package, package);
    assert_eq!(verify_signature(&keys, &package, &by(&next)).unwrap(), 1);
    assert_eq!(
        verify_signature(&keys, &package, &sign(&package)).unwrap(),
        0
    );
    let unknown = verify(&by(&SigningKey::from_bytes(&[9; 32])))
        .err()
        .unwrap();
    assert_eq!(unknown.code, "update_signature_invalid");
    assert!(
        unknown.message.contains("release keys"),
        "{}",
        unknown.message
    );
}

/// The keys compiled in are valid, the current one comes first, and an empty
/// next key adds none.
#[test]
fn the_compiled_keys_decode_and_the_current_one_comes_first() {
    let keys = release_keys();
    assert_eq!(
        keys.first(),
        decode_key(RELEASE_SIGNING_PUBLIC_KEY_BASE64).as_ref()
    );
    let expected = [
        RELEASE_SIGNING_PUBLIC_KEY_BASE64,
        NEXT_RELEASE_SIGNING_PUBLIC_KEY_BASE64,
    ];
    for text in expected.iter().filter(|text| !text.is_empty()) {
        assert!(decode_key(text).is_some(), "{text} is not a 32-byte key");
    }
    assert_eq!(
        keys.len(),
        expected.iter().filter(|t| !t.is_empty()).count()
    );
    assert!(keys.windows(2).all(|pair| pair[0] != pair[1]));
}

#[test]
fn an_unsigned_package_is_refused_before_it_is_downloaded() {
    let package = tarball(&[("a", b"b")]);
    let fake = signed_release("2.0.0", &package, None);
    let error = verify_release(&fake).err().unwrap();
    assert_eq!(error.code, "update_unsigned");
    assert!(error.message.contains("no signature"));
    assert_eq!(fake.count(), 1);
}

#[test]
fn a_wrong_or_malformed_signature_is_refused() {
    let package = tarball(&[("a", b"b")]);
    let foreign = STANDARD
        .encode(SigningKey::from_bytes(&[9; 32]).sign(&package).to_bytes())
        .into_bytes();
    for signature in [&foreign[..], b"not base64 !", b"c2hvcnQ="] {
        let fake = signed_release("2.0.0", &package, Some(signature));
        let error = verify_release(&fake).err().unwrap();
        assert_eq!(error.code, "update_signature_invalid");
        assert!(error.message.contains("Nothing was installed"));
    }
}

#[test]
fn a_tampered_package_is_refused() {
    let package = tarball(&[("a", b"b")]);
    let signature = sign(&package);
    let mut tampered = package;
    let last = tampered.len() - 1;
    tampered[last] ^= 1;
    let fake = signed_release("2.0.0", &tampered, Some(&signature));
    assert_eq!(
        verify_release(&fake).err().unwrap().code,
        "update_signature_invalid"
    );
}

#[test]
fn a_build_without_the_key_refuses_and_says_so() {
    let fake = Fake::default();
    let scratch = tempfile::tempdir().unwrap();
    let error = download_and_verify(
        &fake,
        &github(None),
        None,
        "1.0.0",
        scratch.path(),
        &mut None,
        "stable",
    )
    .err()
    .unwrap();
    assert_eq!(error.code, "update_signing_key_missing");
    assert!(error.message.contains("no release signing key"));
    assert_eq!(fake.count(), 0);
}

#[test]
fn the_checksum_must_match_when_it_is_published() {
    verify_checksum(b"x", format!("{}  name\n", digest(b"x")).as_bytes()).unwrap();
    assert_eq!(
        verify_checksum(b"y", format!("{}  name\n", digest(b"x")).as_bytes())
            .unwrap_err()
            .code,
        "update_checksum_mismatch"
    );
}

#[test]
fn unpacking_writes_files_with_their_modes() {
    let scratch = tempfile::tempdir().unwrap();
    unpack(&tarball(&[("top/bin/tool", b"run")]), scratch.path()).unwrap();
    let tool = scratch.path().join("top/bin/tool");
    assert_eq!(fs::read(&tool).unwrap(), b"run");
    assert_eq!(
        fs::metadata(&tool).unwrap().permissions().mode() & 0o777,
        0o755
    );
}

#[test]
fn unsafe_tar_entries_are_refused() {
    for package in [
        raw_tarball("../escaped", b'0', ""),
        raw_tarball("top/../../escaped", b'0', ""),
        raw_tarball("/tmp/escaped", b'0', ""),
        raw_tarball("top/link", b'2', "/etc/passwd"),
        raw_tarball("top/link", b'2', "../../outside"),
        raw_tarball("top/hard", b'1', "/etc/passwd"),
        raw_tarball("top/device", b'3', ""),
        raw_tarball("top/fifo", b'6', ""),
    ] {
        let parent = tempfile::tempdir().unwrap();
        let destination = parent.path().join("unpacked");
        fs::create_dir(&destination).unwrap();
        let error = unpack(&package, &destination).unwrap_err();
        assert_eq!(error.code, "update_unsafe_package", "{}", error.message);
        assert!(!parent.path().join("escaped").exists());
    }
}

#[test]
fn nothing_is_written_through_a_link_inside_the_package() {
    let parent = tempfile::tempdir().unwrap();
    let outside = parent.path().join("outside");
    fs::create_dir(&outside).unwrap();
    let destination = parent.path().join("unpacked");
    fs::create_dir(&destination).unwrap();
    std::os::unix::fs::symlink(&outside, destination.join("link")).unwrap();
    let error = unpack(&tarball(&[("link/file", b"x")]), &destination).unwrap_err();
    assert_eq!(error.code, "update_unsafe_package");
    assert!(!outside.join("file").exists());
    // A link already in place is not replaced by, or written through as, a file.
    let error = unpack(&tarball(&[("link", b"x")]), &destination).unwrap_err();
    assert_eq!(error.code, "update_unsafe_package");
}

#[test]
fn a_contained_link_is_kept() {
    assert!(link_stays_inside(Path::new("a/b"), Path::new("c")));
    assert!(link_stays_inside(Path::new("a/b"), Path::new("../c")));
    assert!(!link_stays_inside(Path::new("a/b"), Path::new("../../c")));
    assert!(!link_stays_inside(Path::new("a"), Path::new("/c")));
}

#[test]
fn an_older_signed_package_under_a_newer_tag_is_refused() {
    let scratch = tempfile::tempdir().unwrap();
    let top = format!("tomacco-2.0.0-Linux-{}", std::env::consts::ARCH);
    let package = tarball(&[(&format!("{top}/share/tomacco/VERSION"), b"1.0.0\n")]);
    let verified = Verified {
        version: Version::parse("2.0.0").unwrap(),
        package,
    };
    let error = unpack_package(&verified, scratch.path()).unwrap_err();
    assert_eq!(error.code, "update_invalid_package");
}

#[test]
fn running_work_refuses_and_says_what_it_is() {
    assert_eq!(busy_reason(&[], false, 0), None);
    let job = json!({"label":"Build app"});
    let reason = busy_reason(std::slice::from_ref(&job), false, 0).unwrap();
    assert!(reason.contains("Build app"));
    let reason = busy_reason(std::slice::from_ref(&job), true, 0).unwrap();
    assert_eq!(reason, PHONE_MESSAGE);
    assert!(
        busy_reason(&[], false, 1)
            .unwrap()
            .contains("display session")
    );
}

#[test]
fn off_needs_the_user_and_auto_needs_its_mode_and_a_new_unheld_version() {
    let off = mode_from(&json!({"updates":{"mode":"off"}}), None);
    let auto = mode_from(&json!({"updates":{"mode":"auto"}}), None);
    let available = json!({"check":{"latest":"2.0.0","update_available":true}});
    let code = |a: Value, mode: &Mode, state: &Value| {
        admit(&a, mode, state, "1.0.0")
            .err()
            .map(|error| error.code)
    };
    assert_eq!(code(json!({}), &off, &available).unwrap(), "update_off");
    assert_eq!(code(json!({"manual":true}), &off, &available), None);
    assert_eq!(code(json!({}), &notify(), &available), None);
    assert_eq!(
        code(json!({"auto":true}), &notify(), &available).unwrap(),
        "update_auto_off"
    );
    assert_eq!(code(json!({"auto":true}), &auto, &available), None);
    assert_eq!(
        code(json!({"auto":true}), &auto, &json!({})).unwrap(),
        "update_not_available"
    );
    let held = json!({"check":{"latest":"2.0.0","update_available":true},"hold_version":"2.0.0"});
    assert_eq!(
        code(json!({"auto":true}), &auto, &held).unwrap(),
        "update_held"
    );
    // The user may still ask for it.
    assert_eq!(code(json!({}), &auto, &held), None);
}

#[derive(Default)]
struct FakeInstaller {
    install_fails: bool,
    unhealthy: bool,
    rollback_fails: bool,
    journal: Option<String>,
    rolled_back: RefCell<Vec<String>>,
}
impl Installer for FakeInstaller {
    fn install(&self) -> Result<()> {
        if self.install_fails {
            Err(Error::new("update_installer_failed", "disk full"))
        } else {
            Ok(())
        }
    }
    fn journal(&self) -> Result<Option<String>> {
        Ok(self.journal.clone())
    }
    fn rollback(&self, journal: &str) -> Result<()> {
        self.rolled_back.borrow_mut().push(journal.to_string());
        if self.rollback_fails {
            Err(Error::new("update_installer_failed", "changed file"))
        } else {
            Ok(())
        }
    }
    fn healthy(&self, _expected: &str) -> Result<()> {
        if self.unhealthy {
            Err(Error::new("update_health_check_failed", "does not start"))
        } else {
            Ok(())
        }
    }
}

#[test]
fn a_healthy_installation_stays() {
    let installer = FakeInstaller {
        journal: Some("backup".into()),
        ..FakeInstaller::default()
    };
    assert_eq!(install_checked(&installer, "2.0.0").unwrap(), "backup");
    assert!(installer.rolled_back.borrow().is_empty());
}

#[test]
fn a_failed_health_check_rolls_back() {
    let installer = FakeInstaller {
        unhealthy: true,
        journal: Some("backup".into()),
        ..FakeInstaller::default()
    };
    let error = install_checked(&installer, "2.0.0").unwrap_err();
    assert_eq!(error.code, "update_install_failed");
    assert!(error.message.contains("does not start"));
    assert!(error.message.contains("previous version was restored"));
    assert_eq!(*installer.rolled_back.borrow(), ["backup"]);
}

#[test]
fn an_installer_that_fails_partway_rolls_back_and_one_that_wrote_nothing_has_nothing_to_undo() {
    let installer = FakeInstaller {
        install_fails: true,
        journal: Some("partial".into()),
        ..FakeInstaller::default()
    };
    assert_eq!(
        install_checked(&installer, "2.0.0").unwrap_err().code,
        "update_install_failed"
    );
    assert_eq!(*installer.rolled_back.borrow(), ["partial"]);
    let installer = FakeInstaller {
        install_fails: true,
        ..FakeInstaller::default()
    };
    assert_eq!(
        install_checked(&installer, "2.0.0").unwrap_err().code,
        "update_install_failed"
    );
    assert!(installer.rolled_back.borrow().is_empty());
}

#[test]
fn a_failed_rollback_says_where_the_backup_is() {
    let installer = FakeInstaller {
        unhealthy: true,
        rollback_fails: true,
        journal: Some("backup-id".into()),
        ..FakeInstaller::default()
    };
    let error = install_checked(&installer, "2.0.0").unwrap_err();
    assert_eq!(error.code, "update_rollback_failed");
    assert!(error.message.contains("backup-id"));
}

fn installed_home() -> (tempfile::TempDir, PathBuf) {
    let home = tempfile::tempdir().unwrap();
    let library = home.path().join(INSTALLED_LIBRARY);
    let launchers = home.path().join(INSTALLED_LAUNCHERS);
    fs::create_dir_all(&library).unwrap();
    fs::create_dir_all(&launchers).unwrap();
    fs::write(library.join(CLI_NAME), b"").unwrap();
    fs::write(library.join("VERSION"), b"1.0.0\n").unwrap();
    fs::write(
        launchers.join(CLI_NAME),
        format!("#!/bin/sh\n{INSTALLER_MARKER}\n"),
    )
    .unwrap();
    let executable = library.join(CLI_NAME);
    (home, executable)
}

#[test]
fn an_installation_by_the_installer_can_be_updated() {
    let (home, executable) = installed_home();
    let found = installation(&executable);
    assert_eq!(found.kind, "user_install");
    assert_eq!(found.home.as_deref(), Some(home.path()));
}

#[test]
fn an_unwritable_installation_is_left_alone() {
    if unsafe { libc::geteuid() } == 0 {
        return; // Permissions do not stop root.
    }
    let (home, executable) = installed_home();
    let library = home.path().join(INSTALLED_LIBRARY);
    fs::set_permissions(&library, fs::Permissions::from_mode(0o555)).unwrap();
    let found = installation(&executable);
    fs::set_permissions(&library, fs::Permissions::from_mode(0o755)).unwrap();
    assert_eq!(found.kind, "unwritable");
    assert!(found.home.is_none());
    assert_eq!(
        refuse_unless_managed(&found).unwrap_err().code,
        "update_unwritable"
    );
}

#[test]
fn a_foreign_launcher_means_someone_else_installed_it() {
    let (home, executable) = installed_home();
    fs::write(
        home.path().join(INSTALLED_LAUNCHERS).join(CLI_NAME),
        b"#!/bin/sh\n",
    )
    .unwrap();
    assert_eq!(installation(&executable).kind, "unmanaged");
}

#[test]
fn a_checkout_and_a_system_package_are_left_alone() {
    let checkout = tempfile::tempdir().unwrap();
    fs::create_dir_all(checkout.path().join("native")).unwrap();
    fs::create_dir_all(checkout.path().join("tools")).unwrap();
    fs::create_dir_all(checkout.path().join("target/release")).unwrap();
    fs::write(checkout.path().join("Cargo.toml"), b"").unwrap();
    fs::write(checkout.path().join("native/main.rs"), b"").unwrap();
    let found = installation(&checkout.path().join("target/release/tomaccoctl"));
    assert_eq!(found.kind, "source_checkout");
    assert!(found.message.contains("source checkout"));
    assert_eq!(
        installation(Path::new("/usr/bin/tomaccoctl")).kind,
        "system_package"
    );
    assert_eq!(
        installation(Path::new("/srv/tools/tomaccoctl")).kind,
        "unmanaged"
    );
}

#[test]
fn the_configured_key_is_empty_until_a_release_key_is_set_and_a_bad_one_counts_as_none() {
    assert!(decode_key("").is_none());
    assert!(decode_key("c2hvcnQ=").is_none());
    assert_eq!(decode_key(&STANDARD.encode(test_key())), Some(test_key()));
    assert_eq!(
        package_name(&Version::parse("1.2.3").unwrap()),
        format!(
            "{PACKAGE_PREFIX}-1.2.3-Linux-{}.tar.gz",
            std::env::consts::ARCH
        )
    );
}

#[test]
fn a_portable_installation_updates_from_the_portable_package() {
    let version = Version::parse("1.2.3").unwrap();
    assert_eq!(
        package_file(&version, true),
        format!(
            "{PACKAGE_PREFIX}-1.2.3-Linux-{}-portable.tar.gz",
            std::env::consts::ARCH
        )
    );
    let library = tempfile::tempdir().unwrap();
    assert!(!is_portable(library.path()));
    fs::create_dir(library.path().join("runtime")).unwrap();
    assert!(!is_portable(library.path()));
    fs::write(library.path().join("qt.conf"), "[Paths]\nPrefix=runtime\n").unwrap();
    assert!(is_portable(library.path()));
}

#[test]
fn headers_give_the_last_status_and_any_location() {
    let proxied = "HTTP/1.1 200 Connection established\r\n\r\nHTTP/2 302\r\nLocation: https://github.com/x\r\n\r\n";
    let reply = parse_headers(proxied).unwrap();
    assert_eq!(reply.status, 302);
    assert_eq!(reply.location.as_deref(), Some("https://github.com/x"));
    assert!(parse_headers("").is_none());
}

#[test]
fn configure_keeps_what_it_is_not_told() {
    let mut config = json!({"updates":{"token_file":"/kept"}});
    configure(&mut config, &json!({"mode":"auto"})).unwrap();
    assert_eq!(
        config["updates"],
        json!({"token_file":"/kept","mode":"auto"})
    );
    configure(&mut config, &json!({"token_file":""})).unwrap();
    assert_eq!(config["updates"], json!({"mode":"auto"}));
}

/// What the release tooling produces: pure Ed25519 from OpenSSL, base64 in the
/// `.sig` file. The key existed only to make this vector.
#[test]
fn a_signature_made_by_openssl_verifies() {
    let key = decode_key("rH5agwRq7RWpPm2l+XkxGfnFvQ+yRcAw8nPMhuF5yuU=").unwrap();
    let message = b"tomacco update test vector\n";
    let signature =
        b"5halaIQ2vA/cDQvsEGhCr+tf5XNGpYkivRJJuyFWnJlWT7OWl++YEjodY8i9WbfMl2roqF1WHUG22i04v6s4Ag==";
    assert_eq!(verify_signature(&[key], message, signature).unwrap(), 0);
    assert!(verify_signature(&[key], b"other", signature).is_err());
}
