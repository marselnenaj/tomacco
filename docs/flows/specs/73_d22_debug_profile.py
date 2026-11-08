# D22 debugger sessions and performance measurement (lane A, source-only, product 4584d6b).
# N and E come from atlas.py.
RP = 40


def cy(r):
    return round(r * (184 + RP)) + RP + 60


BOARDS = [{
    "id": "D22", "type": "Zustandsmaschine", "title": "Debuggen und Performance messen",
    "lead": "Eine Debug-Sitzung gehört einem eigenen Worker und überlebt Client und Desktop; eine Android-Sitzung besitzt ihr Gerät exklusiv. Messungen werden zu JSON-Berichten, die gegen eine Baseline und ein Budget geprüft werden; fehlt ein Messwert, gilt das Budget als verfehlt.",
    "rowpad": RP, "oy": 36,
    "lanes": [
        {"label": "Debug-Sitzung · jdwp · android-lldb · lldb", "r": 0, "rows": 4},
        {"label": "Messen, Baseline, Budget", "r": 4, "rows": 3.3},
        {"label": "Gradle-Jobs · Macrobenchmark und Baseline Profile", "r": 7.8, "rows": 2.4},
    ],
    "nodes": [
        N("d0", "start", 0, 0, "Debugger starten", "debug.start {backend}"),
        N("d1", "decision", 1, 0, "Gerät schon in Sitzung?"),
        N("d2", "step", 2, 0, "Worker starten", "_debug_worker · setsid · bis 50 s"),
        N("d3", "decision", 3, 0, "Bereit?"),
        N("d4", "error", 4, 0, "Start fehlgeschlagen", "debug_timeout · debug_worker_failed · app_not_debuggable · launcher_missing"),
        N("d5", "ok", 0, 1, "Sitzung wiederverwendet", "reused: true"),
        N("d6", "decision", 1, 1, "Gleiche Anfrage, bereit?"),
        N("d7", "state", 3, 1, "running", "Ziel läuft · Haltepunkte setzbar"),
        N("d8", "note", 4, 1, "debug.breakpoints ersetzt je Klasse (JDWP) oder absoluter Datei (LLDB); ungeladene Klassen bleiben pending"),
        N("d9", "error", 1, 2, "Gerät belegt", "device_debug_busy"),
        N("d10", "step", 2, 2, "Steuern", "debug.control"),
        N("d11", "state", 3, 2, "stopped", "debug.state · debug.events"),
        N("d12", "step", 4, 2, "Untersuchen", "threads · stack · variables · evaluate"),
        N("d13", "note", 0, 2, "Virtuelles iPhone: eigener lldb-Job ios.virtual.debug (D11)"),
        N("d14", "ok", 2, 3, "disconnected", "Ziel läuft weiter · Gerät frei"),
        N("d15", "error", 3, 3, "worker_exited", "debug.list erkennt toten Worker"),
        N("d16", "note", 4, 3, "JDWP-evaluate liest nur Variablen und Felder, ruft keine Methoden; LLDB wertet Ausdrücke aus"),
        N("m0", "start", 0, 4, "App bereit auf dem Gerät", "device · bundle_id"),
        N("m1", "step", 1, 4, "Messen", "profile.startup · frames · memory · usage"),
        N("m2", "step", 2, 4, "Bericht speichern", "report: JSON mit metrics"),
        N("m3", "decision", 3, 4, "Baseline vorhanden?"),
        N("m4", "state", 4, 4, "Baseline auswählen", "kein automatischer Erstbericht"),
        N("m5", "step", 0, 5, "Trace aufnehmen, auswerten", "profile.capture → profile.analyze"),
        N("m6", "decision", 3, 5.3, "Budget eingehalten?", "profile.compare · budget"),
        N("m7", "ok", 4, 5.3, "passed: true", "changes mit delta_percent"),
        N("m8", "error", 3, 6.6, "passed: false", "Grenze überschritten · measurement_unavailable"),
        N("m9", "note", 0, 6.6, "Gleiche App nötig (profile_mismatch). testing.run nimmt dasselbe Budget für measure-Schritte", span=2),
        N("m10", "note", 4, 6.6, "iPhone-Startzeit ist bildbasiert und grob; virtuelles iPhone emuliert, viel langsamer"),
        N("j0", "start", 0, 8.1, "Module laut project.inspect", "benchmark_modules · baseline_profile_modules"),
        N("j1", "decision", 1, 8.1, "Modul vorhanden?"),
        N("j2", "state", 2, 8.1, "Job läuft", "profile.macrobenchmark · baseline_profile"),
        N("j3", "decision", 3, 8.1, "succeeded?"),
        N("j4", "ok", 4, 8.1, "Bericht", "benchmark_results · baseline_rules"),
        N("j5", "error", 1, 9.4, "Kein passendes Modul", "benchmark_module_missing · baseline_plugin_missing"),
        N("j6", "error", 3, 9.4, "failed", "Artefakte und JUnit bleiben"),
        N("j7", "step", 4, 9.4, "Profil übernehmen, vergleichen", "baseline_install (destruktiv) · baseline_compare"),
    ],
    "edges": [
        E("d0", "d1"), E("d1", "d6", "ja"), E("d1", "d2", "nein"), E("d2", "d3"),
        E("d3", "d4", "nein", style="err"), E("d3", "d7", "ja", style="ok"),
        E("d6", "d5", "ja", style="ok"), E("d6", "d9", "nein", style="err"),
        E("d7", "d11", "Haltepunkt · pause"), E("d11", "d12", both=True), E("d11", "d10"),
        E("d10", "d7", "continue · step", s1="t", s2="l"),
        E("d10", "d14", "disconnect"), E("d11", "d15", "Worker weg", style="err"),
        E("m0", "m1"), E("m1", "m2"), E("m2", "m3"), E("m3", "m4", "nein"), E("m3", "m6", "ja"),
        E("m5", "m2", s1="r", s2="b"),
        E("m6", "m7", "ja", style="ok"), E("m6", "m8", "nein", style="err"),
        E("j0", "j1"), E("j1", "j5", "nein", style="err"), E("j1", "j2", "ja"), E("j2", "j3"),
        E("j3", "j4", "ja", style="ok"), E("j3", "j6", "nein", style="err"), E("j4", "j7"),
    ],
}]
BOARDS[0].update({
    "notes": [
        "Backends: jdwp (Android/JVM; launch startet die App neu und wartet auf den Debugger), android-lldb (natives Android mit ungestrippter .so, Symbolpfade, source_map) und lldb (lokales Swift/natives Programm). Ein USB-iPhone hat in debug.* keinen Weg; das virtuelle iPhone nutzt ios.virtual.debug.",
        "Zulassung je Gerät ist serialisiert: dieselbe Anfrage (Digest der Argumente) an eine bereite Sitzung wird wiederverwendet, jede andere bekommt device_debug_busy, bis die Sitzung getrennt ist. Antworten des Workers sind auf 2 MiB begrenzt (response_too_large).",
        "disconnect lässt das Ziel weiterlaufen und beendet es nicht; die Android-Aufräumung (am clear-debug-app, Port-Weiterleitung) läuft beim Beenden oder wird von debug.list/debug.state nachgeholt, wenn der Worker tot ist.",
        "Messarten: profile.startup (cold/warm/hot, am start -W; iPhone: Bild-Zeitmessung), profile.frames (gfxinfo, optional während testing.run), profile.memory (meminfo), profile.usage (CPU und Akku), profile.capture + profile.analyze (Perfetto trace_processor). Fehlt eine Messung, bleibt sie unavailable statt geschätzt.",
        "profile.compare braucht explizit baseline und current; der erste Bericht wird nicht automatisch zur Baseline. Es vergleicht Berichte derselben App und Art; ohne budgets bleibt passed true. profile.budget prüft gespeicherte Berichte gegen maximum/minimum.",
        "Gradle-Jobs: profile.macrobenchmark führt connected<Variant>AndroidTest des Benchmark-Moduls aus, profile.baseline_profile generate<Variant>BaselineProfile. baseline_compare braucht einen installierten Release-Build mit profileinstaller (sonst profile_install_failed) und lässt die App auf speed-profile.",
    ],
    "sources": [
        "native/debugger.rs:1, 66-108 metadata/worker_exited/rpc, 110-160 handle, 196-290 start() Zulassung/reused/device_debug_busy/debug_timeout/debug_worker_failed, 356 launcher_missing",
        "native/android_debug.rs:149-173 native_debug_timeout/app_not_debuggable; native/jdwp.rs:184; native/dap.rs:150",
        "qml/DebugWorkspace.qml:41-109 start/state/threads/stack/variables/evaluate/breakpoints/events/control, 246 Programmwahl",
        "native/profiling.rs:10-19 handle; native/perf.rs:996-1001 profile_mismatch, Budget/measurement_unavailable; native/macrobench.rs:532-551, 663, 711, 774, 857, 1056 Modul-/Profilfehler",
        "qml/ProfileWorkspace.qml:13-58 capture/analyze/compare, 63 Trace-Wahl",
        "assets/operations.json debug.*, profile.*",
    ],
    "links": [["D07", "Build & Run"], ["D08", "Android"], ["D11", "Virtuelles iPhone"], ["D16", "Reservierungen"], ["D18", "Jobs"], ["D21", "UI-Tests"]],
    "routes": ["debug", "profiling", "jobs"],
    "ops": ["debug.start", "debug.list", "debug.state", "debug.events", "debug.breakpoints", "debug.threads", "debug.stack", "debug.variables", "debug.evaluate", "debug.control", "profile.capture", "profile.analyze", "profile.compare", "profile.startup", "profile.frames", "profile.memory", "profile.usage", "profile.budget", "profile.macrobenchmark", "profile.benchmark_results", "profile.baseline_profile", "profile.baseline_rules", "profile.baseline_install", "profile.baseline_compare"],
    "status": "implementiert; Geräte-Debugging und Messungen in diesem Stand nicht auf Hardware qualifiziert",
})
BOARDS[0]["lanes"][-1]["rows"] = 2.51  # last lane ends at the canvas bottom
