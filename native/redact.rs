//! Removes secrets from what Tomacco shows: error messages, job records and
//! job logs as operations, MCP and the CLI return them.
//!
//! Operations take secrets as owner-only files and pass only their paths, so
//! a secret should never reach any of these. This is the second line: a tool
//! that prints a token, a password typed where a path belongs, or a header in
//! a transport error is masked before it leaves the process. It masks known
//! shapes; it cannot recognise every secret.
use regex::Regex;
use serde_json::Value;
use std::{borrow::Cow, sync::OnceLock};

pub const MASK: &str = "[redacted]";

/// Object keys whose value is a secret itself. Keys that name a file holding
/// one (`password_file`, `token_file`, `credentials`, `key_path`) are paths
/// and stay readable.
const SECRET_KEYS: &[&str] = &[
    "password",
    "passwd",
    "passphrase",
    "secret",
    "client_secret",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "id_token",
    "private_key",
    "token",
    "authorization",
];

fn patterns() -> &'static [(Regex, &'static str)] {
    static PATTERNS: OnceLock<Vec<(Regex, &'static str)>> = OnceLock::new();
    PATTERNS.get_or_init(|| {
        [
            // PEM private keys, whole.
            (
                r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)",
                "[redacted private key]",
            ),
            // HTTP authorization headers and bearer tokens.
            (
                r#"(?i)\b((?:proxy-)?authorization\s*[:=]\s*"?)[^\r\n"]+"#,
                "${1}[redacted]",
            ),
            (r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}", "${1}[redacted]"),
            // Credentials in a URL.
            (r"(://[^/\s:@]+:)[^@\s/]+@", "${1}[redacted]@"),
            // Signing passwords on a command line (apksigner, keytool, jarsigner).
            (r"(?i)(--(?:ks|key)-pass\s+pass:)\S+", "${1}[redacted]"),
            (r"(?i)(-(?:store|key)pass\s+)\S+", "${1}[redacted]"),
            // A secret field in JSON, a query string, an environment or a form.
            (
                r#"(?i)((?:^|[^A-Za-z0-9_])"?(?:password|passwd|passphrase|secret|client_secret|api[_-]?key|access_token|refresh_token|id_token|private_key|token)"?\s*[:=]\s*)("(?:[^"\\]|\\.)*"|[^\s,;&"'}]+)"#,
                "${1}\"[redacted]\"",
            ),
            // Token formats with a recognisable prefix.
            (
                r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
                "[redacted]",
            ),
            (r"\bya29\.[A-Za-z0-9._-]{10,}", "[redacted]"),
            (r"\bAIza[0-9A-Za-z_-]{35}", "[redacted]"),
            (r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "[redacted]"),
            (r"\bxox[abprs]-[A-Za-z0-9-]{10,}", "[redacted]"),
            (r"\bglpat-[A-Za-z0-9_-]{20,}", "[redacted]"),
        ]
        .into_iter()
        .map(|(pattern, replacement)| (Regex::new(pattern).expect("valid pattern"), replacement))
        .collect()
    })
}

/// `text` with every recognised secret masked.
pub fn text(text: &str) -> Cow<'_, str> {
    let mut result = Cow::Borrowed(text);
    for (pattern, replacement) in patterns() {
        if let Cow::Owned(changed) = pattern.replace_all(&result, *replacement) {
            result = Cow::Owned(changed);
        }
    }
    result
}

/// Masks secrets in a JSON value in place: the value of a secret key, and
/// recognised secrets inside every string.
pub fn value(value: &mut Value) {
    match value {
        Value::String(string) => {
            if let Cow::Owned(changed) = text(string) {
                *string = changed;
            }
        }
        Value::Array(items) => items.iter_mut().for_each(self::value),
        Value::Object(map) => {
            for (key, item) in map.iter_mut() {
                let secret = SECRET_KEYS
                    .iter()
                    .any(|name| key.eq_ignore_ascii_case(name));
                if secret && !item.is_null() && item.as_str() != Some("") {
                    *item = Value::String(MASK.into());
                } else {
                    self::value(item);
                }
            }
        }
        _ => (),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn known_secret_shapes_are_masked() {
        let cases = [
            ("Authorization: Bearer abc.def.ghi-123", "abc.def.ghi"),
            (
                "curl: header = \"Authorization: token ghp_aaaaaaaaaaaaaaaaaaaaaaaa\"",
                "ghp_",
            ),
            ("using ghp_0123456789abcdefABCDEF0123 now", "ghp_0123"),
            (
                "github_pat_11ABCDEFG0123456789_abcdefghijk",
                "github_pat_11",
            ),
            (
                "{\"access_token\":\"ya29.a0AfH6SMBx-secret\",\"expires_in\":3599}",
                "ya29",
            ),
            ("token=s3cr3t-value&other=1", "s3cr3t"),
            ("PASSWORD=hunter22 ./gradlew", "hunter22"),
            (
                "\"private_key\": \"-----BEGIN PRIVATE KEY-----\\nMIIE\\n-----END PRIVATE KEY-----\\n\"",
                "MIIE",
            ),
            (
                "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
                "MIIEow",
            ),
            (
                "apksigner sign --ks-pass pass:hunter22 --ks app.jks",
                "hunter22",
            ),
            ("keytool -storepass hunter22 -keypass hunter33", "hunter2"),
            ("https://user:hunter22@example.com/repo.git", "hunter22"),
            ("key AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
            ("AIzaSyA-0123456789abcdefghijklmnopqrstu", "AIzaSy"),
            ("xoxb-123456789012-abcdefghij", "xoxb-1"),
        ];
        for (input, secret) in cases {
            let masked = text(input);
            assert!(!masked.contains(secret), "{input:?} became {masked:?}");
            assert!(masked.contains("redacted"), "{input:?} became {masked:?}");
        }
    }

    #[test]
    fn ordinary_text_and_paths_to_secrets_stay_readable() {
        for input in [
            "Path not found: /home/me/keys/password.txt",
            "\"password_file\": \"/home/me/.secrets/release\"",
            "\"token_file\":\"/home/me/.config/gh-token\"",
            "-storepass:file /tmp/x/password",
            "--ks-pass file:/tmp/x/password",
            "BUILD SUCCESSFUL in 3s",
            "tokens: 12, keystore: release.jks",
            "https://example.com/path?page=2",
        ] {
            assert_eq!(text(input), input);
        }
    }

    #[test]
    fn secret_keys_are_masked_anywhere_in_a_value() {
        let mut record = json!({
            "plan": {"steps":[{"argv":["curl","-H","Authorization: Bearer abcdefgh12345"]}],
                     "env": {"API_KEY": "k-123", "JAVA_HOME": "/usr/lib/jvm"}},
            "result": {"error": "GitHub refused ghp_0123456789abcdefABCDEF0123"},
            "password_file": "/home/me/password",
            "credentials": "/home/me/service-account.json",
            "token": "",
        });
        value(&mut record);
        let text = record.to_string();
        assert!(!text.contains("abcdefgh12345"), "{text}");
        assert!(!text.contains("k-123"), "{text}");
        assert!(!text.contains("ghp_0123"), "{text}");
        assert_eq!(record["plan"]["env"]["JAVA_HOME"], "/usr/lib/jvm");
        assert_eq!(record["password_file"], "/home/me/password");
        assert_eq!(record["credentials"], "/home/me/service-account.json");
        assert_eq!(record["token"], "");
    }
}
