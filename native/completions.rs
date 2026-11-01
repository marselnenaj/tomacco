//! Shell completion scripts for `tomaccoctl completions SHELL`. Operation
//! names are asked of `tomaccoctl schema --names` at completion time, so a
//! script stays right after an update.
use crate::core::*;

/// The public commands, with the line each shell shows beside them.
pub const COMMANDS: &[(&str, &str)] = &[
    ("call", "Run one operation"),
    ("schema", "Print operation schemas"),
    ("help", "Describe an operation"),
    ("setup", "Check and set up this machine"),
    ("doctor", "Check toolchains and devices"),
    ("report", "Write a redacted bug-report bundle"),
    ("mcp", "Serve operations over MCP stdio"),
    ("replay", "Replay a session log"),
    ("update", "Check for and install a new release"),
    ("uninstall", "Remove this installation"),
    ("verify", "Check a release file's checksum and signature"),
    ("completions", "Print a shell completion script"),
];
pub const SHELLS: &[&str] = &["bash", "zsh", "fish"];

pub fn script(shell: &str) -> Result<String> {
    let names: Vec<&str> = COMMANDS.iter().map(|(name, _)| *name).collect();
    let words = names.join(" ");
    Ok(match shell {
        "bash" => format!(
            r#"# tomaccoctl completion for bash. Load it with:
#   source <(tomaccoctl completions bash)
_tomaccoctl() {{
    local cur=${{COMP_WORDS[COMP_CWORD]}}
    local words
    if [ "$COMP_CWORD" -eq 1 ]; then
        words="{words} --help --version"
    else
        case ${{COMP_WORDS[1]}} in
        call)
            if [ "$COMP_CWORD" -eq 2 ] && [[ $cur != -* ]]; then
                words=$("${{COMP_WORDS[0]}}" schema --names 2>/dev/null)
            else
                words="--wait --help --pretty --compact"
            fi ;;
        help)
            if [ "$COMP_CWORD" -eq 2 ]; then
                words=$("${{COMP_WORDS[0]}}" schema --names 2>/dev/null)
            fi ;;
        schema)
            if [[ $cur != -* ]]; then
                words=$("${{COMP_WORDS[0]}}" schema --names 2>/dev/null)
            else
                words="--names --pretty --compact"
            fi ;;
        doctor) words="--pretty --compact" ;;
        replay) words="--dry-run --pretty --compact" ;;
        setup) words="--yes" ;;
        update) words="--check" ;;
        verify)
            if [[ $cur == -* ]]; then
                words="--sig --sha256 --json"
            else
                COMPREPLY=($(compgen -f -- "$cur"))
                return
            fi ;;
        completions) words="{shells}" ;;
        esac
    fi
    COMPREPLY=($(compgen -W "$words" -- "$cur"))
}}
complete -o default -F _tomaccoctl tomaccoctl
"#,
            shells = SHELLS.join(" ")
        ),
        "zsh" => format!(
            r#"#compdef tomaccoctl
# tomaccoctl completion for zsh. Load it with:
#   source <(tomaccoctl completions zsh)
# or save it as _tomaccoctl in a directory on $fpath.
_tomaccoctl() {{
    local -a commands operations
    commands=({described})
    if (( CURRENT == 2 )); then
        _describe command commands
        return
    fi
    case $words[2] in
    call|schema|help)
        if (( CURRENT == 3 )) && [[ $PREFIX != -* ]]; then
            operations=(${{(f)"$($words[1] schema --names 2>/dev/null)"}})
            compadd -a operations
        elif [[ $words[2] == call ]]; then
            compadd -- --wait --help --pretty --compact
        elif [[ $words[2] == help ]]; then
            return
        else
            compadd -- --names --pretty --compact
        fi ;;
    doctor) compadd -- --pretty --compact ;;
    replay) _files; compadd -- --dry-run --pretty --compact ;;
    setup) compadd -- --yes ;;
    update) compadd -- --check ;;
    verify) _files; compadd -- --sig --sha256 --json ;;
    completions) compadd {shells} ;;
    esac
}}
if [[ $funcstack[1] == _tomaccoctl ]]; then
    _tomaccoctl "$@"
else
    compdef _tomaccoctl tomaccoctl
fi
"#,
            described = COMMANDS
                .iter()
                .map(|(name, about)| format!("'{name}:{about}'"))
                .collect::<Vec<_>>()
                .join(" "),
            shells = SHELLS.join(" ")
        ),
        "fish" => {
            let mut out = String::from(
                "# tomaccoctl completion for fish. Load it with:\n\
                 #   tomaccoctl completions fish | source\n\
                 # or save it as ~/.config/fish/completions/tomaccoctl.fish.\n\
                 complete -c tomaccoctl -f\n",
            );
            for (name, about) in COMMANDS {
                out.push_str(&format!(
                    "complete -c tomaccoctl -n __fish_use_subcommand -a {name} -d '{about}'\n"
                ));
            }
            out.push_str(&format!(
                "complete -c tomaccoctl -n '__fish_seen_subcommand_from call schema help; and test (count (commandline -opc)) -eq 2' -a '(tomaccoctl schema --names 2>/dev/null)'
complete -c tomaccoctl -n '__fish_seen_subcommand_from call' -l wait -d 'Follow a job until it ends'
complete -c tomaccoctl -n '__fish_seen_subcommand_from call' -l help -d 'Describe the operation'
complete -c tomaccoctl -n '__fish_seen_subcommand_from schema' -l names -d 'Only the operation names'
complete -c tomaccoctl -n '__fish_seen_subcommand_from call schema doctor' -l pretty -d 'Indented JSON'
complete -c tomaccoctl -n '__fish_seen_subcommand_from call schema doctor' -l compact -d 'One line of JSON'
complete -c tomaccoctl -n '__fish_seen_subcommand_from replay' -F
complete -c tomaccoctl -n '__fish_seen_subcommand_from replay' -l dry-run -d 'Only list the calls'
complete -c tomaccoctl -n '__fish_seen_subcommand_from setup' -l yes -d 'Answer yes to every question'
complete -c tomaccoctl -n '__fish_seen_subcommand_from update' -l check -d 'Only check for a new release'
complete -c tomaccoctl -n '__fish_seen_subcommand_from verify' -F
complete -c tomaccoctl -n '__fish_seen_subcommand_from verify' -l sig -r -F -d 'The signature file'
complete -c tomaccoctl -n '__fish_seen_subcommand_from verify' -l sha256 -r -F -d 'The checksum file'
complete -c tomaccoctl -n '__fish_seen_subcommand_from verify' -l json -d 'One JSON envelope'
complete -c tomaccoctl -n '__fish_seen_subcommand_from completions' -a '{}'
",
                SHELLS.join(" ")
            ));
            out
        }
        _ => {
            return Err(
                Error::new("invalid_request", format!("Unknown shell: {shell}")).remedy(
                    match closest(shell, SHELLS.iter().copied()) {
                        Some(near) => format!("Did you mean {near}?"),
                        None => format!("Choose one of {}", SHELLS.join(", ")),
                    },
                ),
            );
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    #[test]
    fn every_shell_completes_the_commands_and_asks_for_operation_names() {
        for shell in SHELLS {
            let text = script(shell).unwrap();
            for (command, _) in COMMANDS {
                assert!(text.contains(command), "{shell} lacks {command}");
            }
            assert!(text.contains("schema --names"), "{shell}");
            assert!(text.contains("wait"), "{shell}");
        }
        let error = script("bsh").unwrap_err();
        assert_eq!(error.remedy, "Did you mean bash?");
        assert!(script("powershell").is_err());
    }

    /// Runs the bash script against a stand-in tomaccoctl, as bash would on
    /// Tab. Skips where bash is missing.
    #[test]
    fn bash_completes_commands_operations_and_flags() {
        if Command::new("bash").arg("--version").output().is_err() {
            eprintln!("skipped: bash is not installed");
            return;
        }
        let directory = tempfile::tempdir().unwrap();
        let fake = directory.path().join("tomaccoctl");
        std::fs::write(
            &fake,
            "#!/bin/sh\n[ \"$1 $2\" = 'schema --names' ] && printf 'job.wait\\njob.logs\\nproject.run\\n'\n",
        )
        .unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&fake, std::fs::Permissions::from_mode(0o755)).unwrap();
        let complete = |line: &[&str]| {
            let words: Vec<String> = line.iter().map(|w| format!("'{w}'")).collect();
            let program = format!(
                "{}\nCOMP_WORDS=({}); COMP_WORDS[0]='{}'; COMP_CWORD={}; _tomaccoctl; printf '%s\\n' \"${{COMPREPLY[@]}}\"",
                script("bash").unwrap(),
                words.join(" "),
                fake.display(),
                line.len() - 1
            );
            let out = Command::new("bash")
                .args(["--norc", "-c", &program])
                .output()
                .unwrap();
            assert!(out.status.success(), "{out:?}");
            String::from_utf8(out.stdout).unwrap()
        };
        assert_eq!(complete(&["tomaccoctl", "sch"]), "schema\n");
        assert_eq!(
            complete(&["tomaccoctl", "call", "job."]),
            "job.wait\njob.logs\n"
        );
        assert_eq!(
            complete(&["tomaccoctl", "call", "job.wait", "--w"]),
            "--wait\n"
        );
        assert_eq!(complete(&["tomaccoctl", "schema", "--n"]), "--names\n");
        assert_eq!(
            complete(&["tomaccoctl", "help", "project."]),
            "project.run\n"
        );
        assert_eq!(
            complete(&["tomaccoctl", "call", "job.wait", "--h"]),
            "--help\n"
        );
        assert_eq!(complete(&["tomaccoctl", "completions", "f"]), "fish\n");
    }
}
