//! Asking the person over MCP elicitation before a step that needs their
//! yes: accepting the Android SDK licences, publishing to a store, and the
//! virtual iOS device's setup stages marked `ask_first`. Only a client that
//! declared elicitation is asked; for any other the call runs exactly as
//! it does from the CLI.
use crate::core::*;
use serde_json::{Value, json};

/// A question asked before an operation runs: the argument a yes sets, and
/// whether a no stops the call (`true`) or lets it run unchanged.
struct Before {
    question: String,
    yes: Option<(&'static str, Value)>,
    no_stops: bool,
}

/// The question to ask before this call, if it needs one. An argument the
/// agent already gave, such as `accept_licenses`, is its answer: then the
/// person is not asked again.
fn before(name: &str, arguments: &Value) -> Option<Before> {
    match name {
        "android.sdk_install" if arguments.get("accept_licenses").is_none() => Some(Before {
            question: format!(
                "Install the Android SDK packages {} and accept their licences? Without a yes, packages whose licence is not yet accepted are skipped.",
                strings(&arguments["packages"]).join(", ")
            ),
            yes: Some(("accept_licenses", json!(true))),
            no_stops: false,
        }),
        "play.commit" => Some(Before {
            question: format!(
                "Commit the prepared Google Play edit {} of {}? This publishes what the edit holds.",
                s(arguments, "edit_id"),
                s(arguments, "package")
            ),
            yes: None,
            no_stops: true,
        }),
        "ios.store_ship" if b(arguments, "upload") => Some(Before {
            question: format!(
                "Build {} and upload the IPA to App Store Connect? It is not submitted for review.",
                s(arguments, "path")
            ),
            yes: None,
            no_stops: true,
        }),
        "apple.store" if s(arguments, "action") == "upload" => Some(Before {
            question: format!(
                "Upload {} to App Store Connect from the Mac {}?",
                s(arguments, "ipa"),
                s(arguments, "remote")
            ),
            yes: None,
            no_stops: true,
        }),
        _ => None,
    }
}

/// Whether this call publishes or uploads, so that it runs only after the
/// person's yes: `workflow.run` asks for `confirm` on such a step.
pub fn needs_yes(name: &str, arguments: &Value) -> bool {
    before(name, arguments).is_some_and(|gate| gate.no_stops)
}

/// The form a question is asked with: one yes-or-no field.
fn form() -> Value {
    json!({"type":"object","properties":{"approve":{"type":"boolean","title":"Go ahead","description":"Yes to go ahead, no to stop","default":false}},"required":["approve"]})
}

/// The person's answer: Some(true) for yes, Some(false) for no or a closed
/// form, None when the client could not ask.
fn answer(ask: &crate::mcp::Ask, question: &str) -> Option<(bool, String)> {
    let reply = ask(question, &form())?;
    let action = s(&reply, "action").to_string();
    let yes = action == "accept" && reply["content"]["approve"] == true;
    Some((
        yes,
        if action == "accept" && !yes {
            "decline".into()
        } else {
            action
        },
    ))
}

fn declined(question: &str, action: &str, error: Option<&Error>) -> Error {
    let mut details = json!({ "question": question, "action": action });
    if let Some(error) = error {
        details["error"] = error.value();
    }
    Error::new(
        "consent_declined",
        format!("The person did not agree: {question}"),
    )
    .remedy(
        "Do not repeat the call unless the person changes their mind; ask them what to do instead",
    )
    .details(details)
}

/// Runs one operation, asking the person first where it needs their yes,
/// and once more when it refuses with `details.consent`: the argument that
/// carries their yes, as `ios.virtual.setup` does for an `ask_first` stage.
pub fn call(
    name: &str,
    mut arguments: Value,
    ask: &crate::mcp::Ask,
    run: impl Fn(&str, &Value) -> Result,
) -> Result {
    if let Some(gate) = before(name, &arguments) {
        if let Some((yes, action)) = answer(ask, &gate.question) {
            if yes {
                if let Some((key, value)) = gate.yes {
                    arguments[key] = value;
                }
            } else if gate.no_stops {
                return Err(declined(&gate.question, &action, None));
            }
        }
    }
    let error = match run(name, &arguments) {
        Err(error) => error,
        done => return done,
    };
    let consent = error
        .details
        .as_ref()
        .map(|details| details["consent"].clone())
        .unwrap_or_default();
    let key = s(&consent, "argument");
    if key.is_empty() || arguments.get(key) == Some(&consent["value"]) {
        return Err(error);
    }
    let question = s(&consent, "question");
    match answer(ask, question) {
        None => Err(error),
        Some((false, action)) => Err(declined(question, &action, Some(&error))),
        Some((true, _)) => {
            arguments[key] = consent["value"].clone();
            run(name, &arguments)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    /// A client that answers each question with the next reply and records it.
    struct Client {
        replies: RefCell<Vec<Option<Value>>>,
        asked: RefCell<Vec<String>>,
    }
    impl Client {
        fn new(replies: Vec<Option<Value>>) -> Self {
            Client {
                replies: RefCell::new(replies),
                asked: RefCell::new(Vec::new()),
            }
        }
        fn ask(&self, question: &str, schema: &Value) -> Option<Value> {
            assert_eq!(schema["required"], json!(["approve"]));
            self.asked.borrow_mut().push(question.to_string());
            self.replies.borrow_mut().remove(0)
        }
    }
    fn yes() -> Option<Value> {
        Some(json!({"action":"accept","content":{"approve":true}}))
    }
    #[test]
    fn licences_are_accepted_only_after_a_yes() {
        let ran = RefCell::new(Vec::new());
        let run = |_: &str, arguments: &Value| -> Result {
            ran.borrow_mut().push(arguments.clone());
            Ok(json!({"id":"job"}))
        };
        let install = json!({"packages":["platform-tools"]});
        let client = Client::new(vec![yes()]);
        call(
            "android.sdk_install",
            install.clone(),
            &|q, s| client.ask(q, s),
            run,
        )
        .unwrap();
        assert!(client.asked.borrow()[0].contains("platform-tools"));
        assert_eq!(ran.borrow()[0]["accept_licenses"], true);
        // A no runs the install as the CLI would, without accepting.
        let client = Client::new(vec![Some(json!({"action":"decline"}))]);
        call(
            "android.sdk_install",
            install.clone(),
            &|q, s| client.ask(q, s),
            run,
        )
        .unwrap();
        assert!(ran.borrow()[1].get("accept_licenses").is_none());
        // An unchecked box is a no too.
        let client = Client::new(vec![Some(
            json!({"action":"accept","content":{"approve":false}}),
        )]);
        call(
            "android.sdk_install",
            install.clone(),
            &|q, s| client.ask(q, s),
            run,
        )
        .unwrap();
        assert!(ran.borrow()[2].get("accept_licenses").is_none());
        // The agent's own answer is not asked again; a client that cannot ask changes nothing.
        let client = Client::new(vec![]);
        let given = json!({"packages":["platform-tools"],"accept_licenses":false});
        call(
            "android.sdk_install",
            given.clone(),
            &|q, s| client.ask(q, s),
            run,
        )
        .unwrap();
        call("android.sdk_install", install.clone(), &|_, _| None, run).unwrap();
        assert!(client.asked.borrow().is_empty());
        assert_eq!(ran.borrow()[3], given);
        assert_eq!(ran.borrow()[4], install);
    }
    #[test]
    fn publishing_stops_at_a_no() {
        let ran = RefCell::new(0);
        let run = |_: &str, _: &Value| -> Result {
            *ran.borrow_mut() += 1;
            Ok(json!({}))
        };
        let commit = json!({"credentials":"/k.json","package":"dev.app","edit_id":"e1"});
        let client = Client::new(vec![Some(json!({"action":"cancel"}))]);
        let error = call("play.commit", commit.clone(), &|q, s| client.ask(q, s), run).unwrap_err();
        assert_eq!(error.code, "consent_declined");
        assert_eq!(error.details.unwrap()["action"], "cancel");
        assert!(client.asked.borrow()[0].contains("e1 of dev.app"));
        assert_eq!(*ran.borrow(), 0);
        let client = Client::new(vec![yes()]);
        call("play.commit", commit.clone(), &|q, s| client.ask(q, s), run).unwrap();
        call("play.commit", commit, &|_, _| None, run).unwrap();
        assert_eq!(*ran.borrow(), 2);
        // Only an upload is asked about.
        let client = Client::new(vec![]);
        call(
            "ios.store_ship",
            json!({"path":"/p"}),
            &|q, s| client.ask(q, s),
            run,
        )
        .unwrap();
        call(
            "apple.store",
            json!({"action":"validate"}),
            &|q, s| client.ask(q, s),
            run,
        )
        .unwrap();
        assert!(client.asked.borrow().is_empty());
        let client = Client::new(vec![Some(json!({"action":"decline"}))]);
        let upload = json!({"path":"/p","upload":true});
        assert!(
            call(
                "ios.store_ship",
                upload.clone(),
                &|q, s| client.ask(q, s),
                run
            )
            .is_err()
        );
        assert!(needs_yes("ios.store_ship", &upload));
        assert!(needs_yes("play.commit", &json!({})));
        assert!(!needs_yes("ios.store_ship", &json!({"path":"/p"})));
        assert!(!needs_yes(
            "android.sdk_install",
            &json!({"packages":["x"]})
        ));
    }
    #[test]
    fn a_refusal_that_names_its_consent_is_asked_and_repeated() {
        let run = |_: &str, arguments: &Value| -> Result {
            if arguments["confirm"] == true {
                return Ok(json!({"id":"job"}));
            }
            Err(Error::new("ios_setup_confirm", "Restore: ask the person first").details(
                json!({"consent":{"argument":"confirm","value":true,"question":"Restore the image?"}}),
            ))
        };
        let setup = json!({"step":"run"});
        let client = Client::new(vec![yes()]);
        let done = call(
            "ios.virtual.setup",
            setup.clone(),
            &|q, s| client.ask(q, s),
            run,
        )
        .unwrap();
        assert_eq!(done["id"], "job");
        assert_eq!(client.asked.borrow().as_slice(), ["Restore the image?"]);
        let client = Client::new(vec![Some(json!({"action":"decline"}))]);
        let error = call(
            "ios.virtual.setup",
            setup.clone(),
            &|q, s| client.ask(q, s),
            run,
        )
        .unwrap_err();
        assert_eq!(error.code, "consent_declined");
        assert_eq!(error.details.unwrap()["error"]["code"], "ios_setup_confirm");
        // Without elicitation the refusal is today's.
        let error = call("ios.virtual.setup", setup, &|_, _| None, run).unwrap_err();
        assert_eq!(error.code, "ios_setup_confirm");
        // A refusal although the argument was given is not asked about again.
        let stubborn = |_: &str, _: &Value| -> Result {
            Err(Error::new("ios_setup_confirm", "no").details(
                json!({"consent":{"argument":"confirm","value":true,"question":"Again?"}}),
            ))
        };
        let client = Client::new(vec![yes()]);
        let error = call(
            "ios.virtual.setup",
            json!({}),
            &|q, s| client.ask(q, s),
            stubborn,
        )
        .unwrap_err();
        assert_eq!(error.code, "ios_setup_confirm");
        assert_eq!(client.asked.borrow().len(), 1);
    }
}
