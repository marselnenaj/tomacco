# D21 UI test scenarios, QA environments, GPX playback, screen recording
# (lane A, source-only, product 4584d6b). N and E come from atlas.py.
RP = 40


def cy(r):
    return round(r * (184 + RP)) + RP + 60


def gy(r1, r2):
    return (cy(r1) + cy(r2)) // 2


BUS = gy(9, 10.3)
BOARDS = [{
    "id": "D21", "type": "Flowchart", "title": "UI-Tests, QA-Umgebungen, GPX und Aufnahme",
    "lead": "testing.run ist ein dauerhafter Job: je Gerät und Szenario laufen die Schritte der Reihe nach, mit Wiederholungen, Fehlerbild und Bericht. QA-Umgebungen gehen immer über einen Plan für ein benanntes Gerät. GPX spielt nur auf einem Android-Emulator.",
    "rowpad": RP,
    "lanes": [
        {"label": "UI-Szenario · testing.run", "r": 0, "rows": 4},
        {"label": "QA-Umgebung · Android", "r": 4, "rows": 3},
        {"label": "GPX-Route · nur Android-Emulator", "r": 7, "rows": 2},
        {"label": "Bildschirmaufnahme", "r": 9, "rows": 2.6},
    ],
    "nodes": [
        N("t0", "start", 0, 0, "Szenario, Geräte, leerer Berichtsordner", "Chooser JSON · Datei oder Ordner"),
        N("t1", "decision", 1, 0, "Eingaben gültig?"),
        N("t2", "step", 2, 0, "Versuch: Schritte der Reihe nach", "Job „Native UI“ · je Gerät × Szenario"),
        N("t3", "decision", 3, 0, "Bestanden?"),
        N("t4", "state", 4, 0, "passed", "flaky, wenn erst ein späterer Versuch besteht"),
        N("t5", "error", 1, 1, "Nicht gestartet", "invalid_argument · path_exists"),
        N("t6", "decision", 2, 1, "Versuch übrig?"),
        N("t7", "step", 3, 1, "Fehlerbild sichern", "failure_capture · app_crashed · Budget"),
        N("t8", "note", 0, 1, "testing.lint prüft vorher ohne Eingabe: not_found, ambiguous, disabled, off_screen"),
        N("t9", "note", 0, 2, "testing.record start … stop schreibt gesendete Eingaben als Schritte"),
        N("t10", "ok", 1, 2, "Job succeeded", "alle Geräte passed"),
        N("t11", "state", 2, 2, "failed", "Gerät × Szenario"),
        N("t12", "note", 3, 2, "Neue Bildschirme: baselines_missing → testing.baselines review/approve"),
        N("t13", "error", 0, 3, "Job failed", "scenario_failed · Artefakte bleiben"),
        N("t14", "decision", 1, 3, "Alle passed?"),
        N("t15", "step", 2, 3, "Bericht schreiben", "report.json · junit.xml · report.html · Verlauf"),
        N("q0", "start", 0, 4, "Preset speichern oder importieren", "tomacco.environment-preset v1"),
        N("q1", "step", 1, 4, "Plan für ein benanntes Gerät", "android.environment.plan · 15 min · einmal"),
        N("q2", "decision", 2, 4, "Plan frisch, Gerät gleich?"),
        N("q3", "decision", 3, 4, "Lücken ohne allow_partial?"),
        N("q4", "step", 4, 4, "Restore-Punkt, setzen, zurücklesen", "android.environment.apply"),
        N("q5", "note", 0, 5, "TalkBack ist nur der Dienst-Schalter, kein Screenreader-Test; testing.accessibility prüft statisch", span=2),
        N("q6", "error", 2, 5, "Neu planen", "environment_conflict"),
        N("q7", "error", 3, 5, "Nicht unterstützt", "environment_unsupported"),
        N("q8", "ok", 4, 5, "applied oder partially_applied", "je Einstellung: applied_unverified · not_applied"),
        N("q9", "error", 3, 6, "Schon zurückgesetzt", "environment_conflict"),
        N("q10", "step", 4, 6, "Zurücksetzen, gleiches Gerät", "android.environment.restore · GPS-Fix nicht"),
        N("g0", "start", 0, 7, "GPX wählen", "Chooser *.gpx · gpx.inspect"),
        N("g1", "decision", 1, 7, "Datei gültig?"),
        N("g2", "decision", 2, 7, "Emulator?"),
        N("g3", "state", 3, 7, "playing", "gpx.play-Job · geo fix"),
        N("g4", "state", 4, 7, "paused", "job.send pause · resume"),
        N("g5", "note", 0, 8, "Vorheriger Standort unbekannt, wird nicht wiederhergestellt"),
        N("g6", "error", 1, 8, "Abgelehnt", "gpx_invalid · gpx_too_large"),
        N("g7", "unsupported", 2, 8, "Echtes Android, iPhone", "gpx_unsupported"),
        N("g8", "stop", 3, 8, "stopped", "job.send stop · job.cancel"),
        N("g9", "ok", 4, 8, "finished", "ohne loop am Routenende"),
        N("r0", "start", 0, 9, "Aufnahme starten", "Job · job.send stop endet früh"),
        N("r1", "decision", 1, 9, "Welches Gerät?"),
        N("r2", "note", 2, 9, "Desktop-ScreenRecorder nur für Android; testing.record nimmt Eingaben auf, kein Video", span=2),
        N("r3", "state", 1, 10.3, "android.record", "MP4 · ≤ 180 s · pull auch nach stop"),
        N("r4", "state", 2, 10.3, "ios.record", "USB · go-ios + ffmpeg · ≤ 600 s"),
        N("r5", "state", 3, 10.3, "ios.virtual.record", "virtuelles iPhone · ffmpeg"),
        N("r6", "unsupported", 4, 10.3, "AirPlay: kein Video", "Desktop speichert nur Screenshot (D10)"),
    ],
    "edges": [
        E("t0", "t1"), E("t1", "t5", "nein", style="err"), E("t1", "t2", "ja"), E("t2", "t3"),
        E("t3", "t4", "ja", style="ok"), E("t3", "t7", "nein", style="err"), E("t7", "t6"),
        E("t6", "t2", "ja"), E("t6", "t11", "nein", style="err"),
        E("t11", "t15"), E("t4", "t15", s1="b", s2="r"),
        E("t15", "t14"), E("t14", "t10", "ja", style="ok"), E("t14", "t13", "nein", style="err"),
        E("q0", "q1"), E("q1", "q2"), E("q2", "q6", "nein", style="err"), E("q2", "q3", "ja"),
        E("q3", "q7", "ja", style="err"), E("q3", "q4", "nein"), E("q4", "q8", style="ok"),
        E("q8", "q10", "später"), E("q10", "q9", style="err"),
        E("g0", "g1"), E("g1", "g6", "nein", style="err"), E("g1", "g2", "ja"),
        E("g2", "g7", "nein", style="dim"), E("g2", "g3", "ja"),
        E("g3", "g4", both=True), E("g3", "g8", "stop"),
        E("g3", "g9", "Ende", s1="b", s2="t", fo=0.8, via=[(1202, gy(7, 8)), (1448, gy(7, 8))], style="ok"),
        E("r0", "r1"),
        E("r1", "r3", "Android", lp=(474, BUS + 8)),
        E("r1", "r4", "USB-iPhone", s1="b", s2="t", via=[(464, BUS), (792, BUS)]),
        E("r1", "r5", "virtuell", s1="b", s2="t", via=[(464, BUS), (1120, BUS)]),
        E("r1", "r6", "AirPlay", s1="b", s2="t", via=[(464, BUS), (1448, BUS)], style="dim"),
    ],
}]
BOARDS[0].update({
    "notes": [
        "Schritte eines Szenarios: wait/assert (testing.find), click/long_click/fill (testing.action), key/tap/swipe/text, launch/stop/clear, screenshot, pause, wait_text/wait_stable, assert_screen/wait_screen gegen Baselines, measure (Budget). Unbekannte Aktion: invalid_step.",
        "Plattformen: Android (adb) und platform ios für USB-iPhones mit WDA-Sitzung (Szenario wird vorab geprüft). Ein iPhone-Lauf liest keine Abstürze (crashes_error not_checked). Das virtuelle iPhone liefert keine UI-Hierarchie: Selektoren nur text/text_contains über OCR, testing.accessibility antwortet ui_hierarchy_unavailable.",
        "Ein fehlgeschlagener Lauf beendet den Job als failed (scenario_failed); report.json, junit.xml, Fehlerbilder und report.html bleiben. testing.report, testing.results, testing.history und testing.impact lesen diese Ergebnisse später aus.",
        "Einzelprüfungen ohne Szenario: testing.visual_diff (PNG gegen Baseline), testing.crashes (logcat), testing.coverage (JaCoCo). testing.setup installiert die Instrumentierung für Unicode-Texteingabe (fill), testing.instrumentation prüft sie.",
        "QA-Umgebung: Felder dark_mode, font_scale, animations, airplane_mode, app_locale, location, talkback. Apply schreibt zuerst den Restore-Punkt und liest jede Einstellung zurück; ein Wert, den Android nicht übernahm, heißt setting_not_applied. Ein Plan gilt 15 Minuten und einmal.",
        "GPX: gpx.inspect verweigert DOCTYPE/Entities, Dateien über 16 MiB und mehr als 200 000 Punkte. gpx.status liest Index, Runde und letzte Koordinate; ein fremder Job antwortet gpx_not_playback. Echte Android-Geräte und iPhones haben keinen verdrahteten Weg (gpx_unsupported).",
        "Aufnahmen sind Jobs: ein angenommener Job ist noch kein abspielbares Video. ios.record braucht das Developer-Image (ios.prepare) und meldet frames_written, frames_distinct, ended_early/stopped_early. Echte iPhone-Aufnahme und Pixel sind hier nicht qualifiziert.",
    ],
    "sources": [
        "native/automation.rs:354-412 run() Prüfungen/path_exists/jobs::submit, 413-470 resolve_baselines, 500-512 baselines_missing, 515-650 lint, 655-725 worker/retries/flaky/scenario_failed, 728-745 after_run, 986-1160 run_device Schritte/failure_capture/crashes/Budget",
        "native/recorder.rs:53-90 testing.record start/status/stop/discard; native/accessibility.rs testing.accessibility",
        "native/environment.rs:1-40 Vertrag/FIELDS, 105-110 environment_conflict/invalid, 800-925 apply (Plan-Prüfungen, environment_unsupported, Restore-Punkt, Status), 950-960 restore einmal",
        "native/gpx.rs:55 gpx_invalid, 173 gpx_too_large, 276-289 transport()/gpx_unsupported, 355-372 status/gpx_not_playback, 420-475 playing/paused/stopped/finished",
        "qml/TestWorkspace.qml:13-46 find/action/testing.run, qml/EnvironmentPanel.qml:66-134 Preset/Plan/Apply/Restore und Route, 242 Pause/Resume",
        "assets/operations.json android.record, ios.record, ios.virtual.record, testing.*",
    ],
    "links": [["D08", "Android"], ["D09", "iPhone USB"], ["D10", "AirPlay"], ["D11", "Virtuelles iPhone"], ["D12", "Beobachten & Handeln"], ["D16", "Reservierungen"], ["D18", "Jobs"], ["D22", "Profiling & Budget"]],
    "screens": [["pvcps58bsa", "z75fm4k6b6", "12 Jobs"], ["pvcps58bsa", "abwz1f9yky", "13 Job-Detail"]],
    "routes": ["testing", "jobs", "tools"],
    "ops": ["testing.find", "testing.action", "testing.run", "testing.record", "testing.lint", "testing.visual_diff", "testing.accessibility", "testing.crashes", "testing.results", "testing.coverage", "testing.report", "testing.history", "testing.impact", "testing.baselines", "testing.setup", "testing.instrumentation", "android.environment.capabilities", "android.environment.preset.list", "android.environment.preset.get", "android.environment.preset.save", "android.environment.preset.delete", "android.environment.preset.export", "android.environment.preset.import", "android.environment.inspect", "android.environment.plan", "android.environment.apply", "android.environment.restore", "android.environment.restores", "gpx.inspect", "gpx.play", "gpx.status", "android.record", "ios.record", "ios.virtual.record"],
    "status": "implementiert; GPX nur Emulator; iPhone-Aufnahme und Geräte-Lauf ohne Hardware-Qualifikation in diesem Stand",
})
BOARDS[0]["lanes"][-1]["rows"] = 2.12  # last lane ends at the canvas bottom
