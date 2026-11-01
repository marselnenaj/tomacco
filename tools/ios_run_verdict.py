"""Why a virtual iPhone run was or was not verified, in plain words.

A run is verified, and its disks become the base of the next start, only when
its report proves exit code 0 and complete storage hashes and its stop record
confirms the guest flushed its storage (Phone.finalize in ios_virtual_iphone.py).
`explain` turns that evidence and the launcher's classification of the run
(run_outcome in run_ios_paravirt.py) into sentences a person can act on:
what the run amounts to, each reason it was not verified, and what to do.
It reads only the records it is given.
"""

GIB = 1 << 30

# What each run outcome amounts to and what to do about it. The keys are every
# value run_outcome returns, the launcher's host-pressure refusal, and the two
# outcomes ios_virtual_iphone.py records itself (job-unknown, cancelled-before-boot).
OUTCOMES = {
    "bounded-session": (
        "The session ran until it was stopped or reached its time limit.",
        None),
    "emulator-exited": (
        "The emulator exited on its own with exit code 0.",
        None),
    "interrupted": (
        "The run was interrupted: its job was cancelled or the launcher got a signal before the session ended.",
        "Stop the phone with ios.virtual.stop instead of cancelling its job, so the guest can save first."),
    "observation-error": (
        "The launcher hit an error while it watched the guest or hashed its disks.",
        "ios.emulator.report names the error under observation_error or storage_hash_error. If it is a disk "
        "error, check the filesystem of the profile's installs, then start again."),
    "host-pressure-blocked": (
        "The host was too busy (I/O or memory pressure) to boot the guest; iOS never ran.",
        "Start again once the host is quieter. ios.virtual.status shows host pressure under host."),
    "kernel-panic": (
        "iOS panicked.",
        "ios.emulator.report has the panic under guest_evidence. Start again; if it repeats, keep the report "
        "(tomaccoctl report) and see docs/virtual-ios/troubleshooting.md."),
    "restore-failed": (
        "A restore step in the guest failed.",
        "ios.emulator.report lists the failed steps under guest_evidence.restore_failures."),
    "guest-control-startup-failed": (
        "The guest tools' control service did not start in iOS.",
        "Check the guest tools with ios.virtual.doctor; rebuild them (ios.emulator.build_guest_tools) if they "
        "do not match the profile, then start again."),
    "guest-process-crash": (
        "iOS processes ended abnormally in a way the emulator is not known to cause.",
        "ios.virtual.crash_reports and ios.virtual.syslog show which ones and why."),
    "hardware-fault": (
        "The guest touched hardware the engine does not model (bus or system register faults).",
        "ios.emulator.report lists them under guest_evidence; include it in a bug report (tomaccoctl report)."),
    "device-error": (
        "An emulated device reported errors.",
        "ios.emulator.report lists them under guest_evidence.device_errors."),
    "emulator-error": (
        "The emulator exited with an error.",
        "The guest job's log (job.get with its job_id) has the emulator's last lines."),
    "job-unknown": (
        "The control plane has no record of this run's guest job (another data directory, or a reset job store).",
        "Start again; the next start uses the last verified run."),
    "cancelled-before-boot": (
        "The start was stopped before iOS came up; nothing of that run was kept.",
        None),
}

GOOD_OUTCOMES = ("bounded-session", "emulator-exited")


def _gib(value):
    return f"{value / GIB:.1f} GiB"


def explain(report, stop):
    """{verified, outcome, summary, reasons, remedy} for a finished run.

    report is the launcher's result.json as ios.emulator.report returns it
    (an empty dict when there is none); stop is the stop record (stop.json),
    empty when the guest ended in any other way. Values of the wrong type
    count as absent, never as proof."""
    report = report if isinstance(report, dict) else {}
    stop = stop if isinstance(stop, dict) else {}
    outcome = report.get("outcome") if isinstance(report.get("outcome"), str) else None
    exit_code = report.get("exit_code") if type(report.get("exit_code")) is int else None
    hashes = report.get("storage_hashes_complete") is True
    flushed = stop.get("guest_storage_flushed") is True
    verified = exit_code == 0 and hashes and flushed
    summary, remedy = OUTCOMES.get(outcome, (
        "The run left no final report." if outcome is None else f"The launcher classified the run as {outcome}.",
        "ios.emulator.report shows what the guest job left."))
    evidence = report.get("guest_evidence") if isinstance(report.get("guest_evidence"), dict) else {}
    if outcome == "guest-process-crash":
        names = [row.get("service") for row in evidence.get("userspace_failures") or []
                 if isinstance(row, dict) and row.get("accounted") is None and isinstance(row.get("service"), str)]
        if names:
            summary += " Unaccounted: " + ", ".join(names[:5]) + (" and more." if len(names) > 5 else ".")
    elif outcome == "observation-error" and isinstance(report.get("observation_error"), str):
        summary += " The error: " + report["observation_error"][:300]
    elif outcome == "emulator-error" and exit_code is not None:
        summary = f"The emulator exited with exit code {exit_code}."
    reasons, remedies = [], [remedy] if remedy and outcome not in GOOD_OUTCOMES else []
    guard = report.get("vram_guard_stop")
    if isinstance(guard, dict):
        used, total = guard.get("used"), guard.get("total")
        amounts = (f" ({_gib(used)} of {_gib(total)} in use)"
                   if type(used) is int and type(total) is int and total > 0 else "")
        reasons.append("The graphics memory guard stopped the session because the card was almost full" + amounts + ".")
        remedies.append("Close other programs that use the graphics card (browsers, games, other guests) before "
                        "the next start; ios.virtual.status shows the card's free memory under host.gpu.")
    if verified:
        return {"verified": True, "outcome": outcome, "summary": summary, "reasons": reasons,
                "remedy": " ".join(remedies) or None}
    if outcome in ("job-unknown", "cancelled-before-boot"):
        pass
    elif exit_code is None:
        reasons.append("The emulator's exit code was not recorded, so nothing proves it ended cleanly.")
    elif exit_code != 0:
        reasons.append(f"The emulator exited with exit code {exit_code}, not 0.")
    if outcome not in ("job-unknown", "cancelled-before-boot") and not hashes:
        error = report.get("storage_hash_error")
        reasons.append("The disks' hashes after the run are incomplete"
                       + (f" ({error[:200]})" if isinstance(error, str) else "")
                       + ", so the saved state cannot be checked.")
    if outcome not in ("job-unknown", "cancelled-before-boot") and not flushed:
        if not stop:
            reasons.append("No stop asked the guest to flush its storage first: it ended on its own or was ended "
                           "by force.")
        else:
            reasons.append("The stop could not confirm that the guest flushed its storage"
                           + (f" ({stop['error']})" if isinstance(stop.get("error"), str) else "") + ".")
        remedies.append("Stop the phone with ios.virtual.stop while it answers; a stop that has to end an "
                        "unanswering guest cannot save it.")
    if outcome in ("job-unknown", "cancelled-before-boot"):
        reasons.append(summary)
    remedies.append("The next start uses the last verified run.")
    # Each remedy once, in order.
    remedies = list(dict.fromkeys(remedies))
    return {"verified": False, "outcome": outcome, "summary": summary, "reasons": reasons,
            "remedy": " ".join(remedies)}


def sentence(verdict):
    """The verdict as one line for a job's error message."""
    parts = [verdict["summary"]] + [reason for reason in verdict["reasons"] if reason != verdict["summary"]]
    return " ".join(parts)
