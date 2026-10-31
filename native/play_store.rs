//! Google Play edit transactions. Credentials and tokens never enter job records.
use crate::{api, core::*, jobs, toolchain as tc};
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
};
const OAUTH: &str = "https://oauth2.googleapis.com/token";
struct Client {
    scratch: PathBuf,
    token: String,
}
impl Client {
    fn authenticate(file: &Path, scratch: PathBuf) -> Result<Self> {
        let m = fs::metadata(file)?;
        if m.permissions().mode() & 0o077 != 0 {
            return Err(Error::new(
                "secret_permissions",
                "Service-account JSON must be readable only by its owner",
            ));
        }
        let credentials = read_json(file, None)?;
        if credentials["type"] != "service_account"
            || s(&credentials, "private_key").is_empty()
            || s(&credentials, "client_email").is_empty()
        {
            return Err(Error::new(
                "invalid_credentials",
                "Select a Google service-account key JSON",
            ));
        }
        if credentials.get("token_uri").is_some() && s(&credentials, "token_uri") != OAUTH {
            return Err(Error::new(
                "invalid_credentials",
                "Only Google's official OAuth token endpoint is supported",
            ));
        }
        let created = now() as u64;
        let header = json!({"alg":"RS256","typ":"JWT","kid":credentials["private_key_id"]});
        let claim = json!({"iss":credentials["client_email"],"scope":"https://www.googleapis.com/auth/androidpublisher","aud":OAUTH,"iat":created,"exp":created+3600});
        let unsigned = format!(
            "{}.{}",
            URL_SAFE_NO_PAD.encode(header.to_string()),
            URL_SAFE_NO_PAD.encode(claim.to_string())
        );
        let key = scratch.join("key.pem");
        let body = scratch.join("unsigned.jwt");
        let signature = scratch.join("signature");
        atomic_write(&key, s(&credentials, "private_key").as_bytes())?;
        atomic_write(&body, unsigned.as_bytes())?;
        tc::run(
            &argv(&[
                &tc::find("openssl")?,
                "dgst",
                "-sha256",
                "-sign",
                &key.to_string_lossy(),
                "-out",
                &signature.to_string_lossy(),
                &body.to_string_lossy(),
            ]),
            15,
            None,
            true,
        )?;
        let assertion = format!(
            "{unsigned}.{}",
            URL_SAFE_NO_PAD.encode(fs::read(signature)?)
        );
        let form = scratch.join("oauth-form");
        atomic_write(&form,format!("grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion={assertion}").as_bytes())?;
        let mut client = Self {
            scratch,
            token: String::new(),
        };
        let reply = client.request(
            "POST",
            OAUTH,
            Some(&form),
            "application/x-www-form-urlencoded",
            false,
        )?;
        client.token = s(&reply, "access_token").to_string();
        if client.token.is_empty() {
            return Err(Error::new(
                "authentication_failed",
                "Google did not return an access token",
            ));
        }
        Ok(client)
    }
    fn request(
        &self,
        method: &str,
        url: &str,
        body: Option<&Path>,
        content_type: &str,
        authenticated: bool,
    ) -> Result {
        if !url.starts_with("https://androidpublisher.googleapis.com/") && url != OAUTH {
            return Err(Error::new("invalid_endpoint", "Unsupported store endpoint"));
        }
        let id = uuid::Uuid::new_v4().simple().to_string();
        let headers = self.scratch.join(format!("{id}.headers"));
        let response = self.scratch.join(format!("{id}.response"));
        let mut text = format!("Content-Type: {content_type}\n");
        if authenticated {
            text.push_str(&format!("Authorization: Bearer {}\n", self.token));
        }
        atomic_write(&headers, text.as_bytes())?;
        let mut args = argv(&[
            &tc::find("curl")?,
            "--silent",
            "--show-error",
            "--fail-with-body",
            "--connect-timeout",
            "15",
            "--max-time",
            "600",
            "--request",
            method,
            "--header",
            &format!("@{}", headers.display()),
            "--output",
            &response.to_string_lossy(),
            "--write-out",
            "%{http_code}",
            url,
        ]);
        if let Some(body) = body {
            args.extend(argv(&["--data-binary", &format!("@{}", body.display())]));
        }
        let result = tc::run(&args, 610, None, false)?;
        let status = s(&result, "stdout").trim().parse::<u16>().unwrap_or(0);
        if !(200..300).contains(&status) {
            let message = read_json(&response, Some(json!({}))).unwrap_or(json!({}));
            return Err(Error::new("store_request_failed",format!("Google Play request failed (HTTP {status})")).details(json!({"status":status,"error":message["error"],"description":message["error_description"],"transport":s(&result,"stderr")})));
        }
        if !response.exists() || fs::metadata(&response)?.len() == 0 {
            return Ok(json!({}));
        }
        read_json(&response, None)
    }
    fn json(&self, method: &str, url: &str, value: &Value) -> Result {
        let path = self
            .scratch
            .join(format!("{}.json", uuid::Uuid::new_v4().simple()));
        atomic_json(&path, value)?;
        self.request(method, url, Some(&path), "application/json", true)
    }
}
pub fn handle(op: &str, a: &Value) -> Result {
    let credentials = secret_path(s(a, "credentials"), "credentials")?;
    identifier(
        s(a, "package"),
        "application package",
        Some(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+"),
    )?;
    if op == "play.prepare" {
        let bundle = path(s(a, "path"), false, true)?;
        if bundle.extension().is_none_or(|p| p != "aab") {
            return Err(Error::new(
                "invalid_artifact",
                "Google Play uploads require an Android App Bundle (.aab)",
            ));
        }
        if s(a, "status") == "inProgress" && a["user_fraction"].as_f64().is_none() {
            return Err(Error::new(
                "invalid_argument",
                "A staged rollout requires user_fraction",
            ));
        }
    } else {
        identifier(
            s(a, "edit_id"),
            "Play edit ID",
            Some("[A-Za-z0-9_-]{1,200}"),
        )?;
    }
    let directory = home()?
        .join("play")
        .join(uuid::Uuid::new_v4().simple().to_string());
    private_dir(&directory)?;
    let mut spec = a.clone();
    let private = home()?.join("private");
    private_dir(&private)?;
    let scratch = tempfile::Builder::new()
        .prefix("play-")
        .tempdir_in(private)?;
    fs::set_permissions(scratch.path(), fs::Permissions::from_mode(0o700))?;
    spec["scratch"] = json!(scratch.path());
    spec["credentials"] = json!(credentials);
    if op == "play.prepare" {
        spec["path"] = json!(path(s(a, "path"), false, true)?);
    }
    spec["operation"] = json!(op);
    atomic_json(&directory.join("request.json"), &spec)?;
    let job = jobs::submit(
        op,
        json!({"steps":[{"argv":[crate::core::own_executable()?,"_play",directory.join("request.json")]}],"cwd":directory,"timeout":1800,"exclusive":format!("play:{}",s(a,"package")),"always_artifacts":["report.json"],"temporary_directories":[jobs::temporary_directory(scratch.path())?]}),
    )?;
    let _ = scratch.keep();
    Ok(job)
}
pub fn worker(file: &str) -> Result<()> {
    let file = path(file, false, true)?;
    let a = read_json(&file, None)?;
    let result = execute(&a);
    let report = match &result {
        Ok(value) => json!({"passed":true,"result":value,"created":now()}),
        Err(e) => json!({"passed":false,"error":e.value(),"created":now()}),
    };
    atomic_json(&file.parent().unwrap().join("report.json"), &report)?;
    result.map(|_| ())
}
fn execute(a: &Value) -> Result {
    if s(a, "operation") == "play.prepare" {
        api::dispatch("release.verify", &json!({"path":a["path"]}))?;
    }
    let client = Client::authenticate(
        &path(s(a, "credentials"), false, true)?,
        path(s(a, "scratch"), true, true)?,
    )?;
    let base = format!(
        "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{}/edits",
        s(a, "package")
    );
    match s(a, "operation") {
        "play.commit" => client.json(
            "POST",
            &format!(
                "{}/{}:commit?changesNotSentForReview={}",
                base,
                s(a, "edit_id"),
                b(a, "changes_not_sent_for_review")
            ),
            &json!({}),
        ),
        "play.abort" => client.request(
            "DELETE",
            &format!("{}/{}", base, s(a, "edit_id")),
            None,
            "application/json",
            true,
        ),
        "play.prepare" => {
            let edit = client.json("POST", &base, &json!({}))?;
            let id = s(&edit, "id");
            identifier(id, "edit ID", Some("[A-Za-z0-9_-]{1,200}"))?;
            let edit_url = format!("{base}/{id}");
            let result = (|| -> Result {
                let upload = format!(
                    "https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications/{}/edits/{id}/bundles?uploadType=media",
                    s(a, "package")
                );
                let bundle = client.request(
                    "POST",
                    &upload,
                    Some(&PathBuf::from(s(a, "path"))),
                    "application/octet-stream",
                    true,
                )?;
                let version = bundle["versionCode"].as_u64().ok_or_else(|| {
                    Error::new(
                        "store_response_invalid",
                        "Google did not return a bundle version code",
                    )
                })?;
                let mut release = json!({"name":a["release_name"],"versionCodes":[version.to_string()],"status":a["status"]});
                if let Some(notes) = a.get("release_notes") {
                    release["releaseNotes"] = notes.clone();
                }
                if s(a, "status") == "inProgress" {
                    release["userFraction"] = a["user_fraction"].clone();
                }
                let track = client.json(
                    "PUT",
                    &format!("{edit_url}/tracks/{}", s(a, "track")),
                    &json!({"track":a["track"],"releases":[release]}),
                )?;
                client.json("POST", &format!("{edit_url}:validate"), &json!({}))?;
                Ok(
                    json!({"state":"validated","committed":false,"package":a["package"],"edit_id":id,"expires":edit["expiryTimeSeconds"],"bundle":bundle,"track":track,"next":{"operation":"play.commit","arguments":{"package":a["package"],"edit_id":id,"credentials":a["credentials"]}}}),
                )
            })();
            if result.is_err() {
                let _ = client.request("DELETE", &edit_url, None, "application/json", true);
            }
            result
        }
        _ => Err(Error::new("operation_not_found", s(a, "operation"))),
    }
}
