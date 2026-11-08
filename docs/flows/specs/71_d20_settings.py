# D20 settings: core settings.json vs GUI QSettings, UI size, language, settings transfer,
# device profiles (lane A, source-only, product 4584d6b). N and E come from atlas.py.
RP = 40


def cy(r):
    return round(r * (184 + RP)) + RP + 60


def gy(r1, r2):
    return (cy(r1) + cy(r2)) // 2


BOARDS = [{
    "id": "D20", "type": "Flowchart", "title": "Einstellungen, Größe und Sprache",
    "lead": "Zwei getrennte Speicher: settings.json gehört der Steuerung (Werkzeugpfade, Updates, Job-Grenzen, Agenten-Policy) und ist über settings.* erreichbar. Aussehen, Größe, Sprache und Ansichtswahl liegen in den QSettings der nativen Oberfläche (auch Telefonlayout); Export und Import sind GUI-Helfer, keine Operation.",
    "rowpad": RP,
    "lanes": [
        {"label": "settings.json · settings.configure", "r": 0, "rows": 2},
        {"label": "Oberflächengröße 75–200 %", "r": 2, "rows": 2},
        {"label": "Oberflächensprache", "r": 4, "rows": 2},
        {"label": "Oberflächen-Einstellungen übertragen (GUI)", "r": 6, "rows": 3.3},
        {"label": "Geräteprofile · catalog.*", "r": 9.3, "rows": 2.7},
    ],
    "nodes": [
        N("A0", "start", 0, 0, "SDK- und Werkzeugpfade setzen", "Tools-Formular · tomaccoctl call · MCP"),
        N("A1", "step", 1, 0, "Sperre nehmen, Datei lesen", "settings.lock · settings.json"),
        N("A2", "decision", 2, 0, "Datei gültig?"),
        N("A3", "decision", 3, 0, "Alle Werte gültig?"),
        N("A4", "ok", 4, 0, "Atomar gespeichert", "nichts installiert, nichts gestartet"),
        N("A5", "note", 0, 1, "Felder: android_sdk, java_home, android_ndk, swift_android_sdk, executables, updates, jobs.max_*, agent_policy", span=2),
        N("A6", "error", 2, 1, "Einstellungsdatei unbrauchbar", "invalid_settings"),
        N("A7", "error", 3, 1, "Abgelehnt, nichts geschrieben", "invalid_tool · not_executable · invalid_argument"),
        N("A8", "note", 4, 1, "agent_policy: GUI oder unbeschränktes Terminal; MCP / begrenzte CLI → policy_denied (D15)"),
        N("B0", "start", 0, 2, "Größer, kleiner, zurück", "Ctrl+Alt++ · Ctrl+Alt+− · Ctrl+Alt+0"),
        N("B1", "step", 1, 2, "Runden und begrenzen", "75–200 % in 25er-Schritten"),
        N("B2", "decision", 2, 2, "Anders als jetzt?"),
        N("B3", "state", 3, 2, "Gespeichert", "QSettings appearance/uiScale"),
        N("B4", "ok", 4, 2, "Ganze Oberfläche skaliert", "Theme.px · App-Schrift 13 px × Faktor"),
        N("B5", "stop", 2, 3, "Nichts geändert", "Knopf an der Grenze gesperrt"),
        N("B6", "note", 0, 3, "Auch in den Einstellungen (−, Wert, +, Zurücksetzen) und in der Befehlspalette; ein ungültiger gespeicherter Wert gilt als 100 %", span=2),
        N("B7", "note", 3, 3, "Gerätezoom, Screenshots und Eingabekoordinaten bleiben (D14); Telefonlayout bei Breite ÷ Faktor < 600 (D02)", span=2),
        N("C0", "start", 0, 4, "Sprache wählen", "System oder eine der 51"),
        N("C1", "decision", 1, 4, "Vollständig angeboten?"),
        N("C2", "state", 2, 4, "language gespeichert", "QSettings"),
        N("C3", "decision", 3, 4, "System?"),
        N("C4", "ok", 4, 4, "Sofort angewandt", "Katalog + Qt-Texte · QLocale · RTL"),
        N("C5", "step", 1, 5, "Alias oder System", "pt-PT → pt · es-419 → es-MX"),
        N("C6", "step", 3, 5, "Omarchy, dann Desktop, sonst English", "~/.config/omarchy/language"),
        N("C7", "note", 0, 5, "RTL: ar, fa, he, ur. TOMACCO_LANGUAGE übersteuert die Wahl"),
        N("D0", "start", 0, 6, "Einstellungen importieren", "Chooser open · *.json"),
        N("D1", "decision", 1, 6, "Lesbar, Format, ≤ 256 KiB?"),
        N("D2", "decision", 2, 6, "Version 1?"),
        N("D3", "step", 3, 6, "Jeden Schlüssel prüfen und setzen", "portableSetting()"),
        N("D4", "ok", 4, 6, "%n übernommen · %n ignoriert", "Ansichten übernehmen sofort"),
        N("D5", "error", 1, 7, "Nicht lesbar, zu groß oder kein Tomacco-Format"),
        N("D6", "error", 2, 7, "Von neuerem Tomacco: erst aktualisieren"),
        N("D7", "note", 3, 7, "updates/mode und channel gehen an tomaccoctl, außer Build oder Umgebung legen sie fest", span=2),
        N("D8", "start", 0, 8.3, "Einstellungen exportieren", "Chooser save · Ersetzen fragt der Chooser"),
        N("D9", "step", 1, 8.3, "Portable Schlüssel sammeln", "tomacco-desktop-settings v1"),
        N("D10", "decision", 2, 8.3, "Geschrieben?"),
        N("D11", "ok", 3, 8.3, "Gespeichert unter …"),
        N("D12", "error", 4, 8.3, "„… kann nicht geschrieben werden“"),
        N("E0", "start", 0, 9.6, "Geräteprofile importieren", "catalog.import {path, replace}"),
        N("E1", "decision", 1, 9.6, "Gültig, ID frei?"),
        N("E2", "ok", 2, 9.6, "Atomar importiert", "eigener Katalog"),
        N("E3", "start", 3, 9.6, "Profil exportieren", "catalog.export → neue Datei"),
        N("E4", "start", 4, 9.6, "Eigenes Profil entfernen", "catalog.remove · destruktiv"),
        N("E5", "error", 1, 10.9, "Abgelehnt", "invalid_profile · duplicate_profile · builtin_profile"),
        N("E6", "error", 3, 10.9, "Ziel existiert", "file_exists"),
        N("E7", "error", 4, 10.9, "Built-in oder unbekannt", "builtin_profile · profile_not_found"),
        N("E8", "note", 0, 10.9, "Built-in-Modelle sind unveränderlich; replace=true aktualisiert ein eigenes"),
    ],
    "edges": [
        E("A0", "A1"), E("A1", "A2"), E("A2", "A6", "nein", style="err"), E("A2", "A3", "ja"),
        E("A3", "A7", "nein", style="err"), E("A3", "A4", "ja", style="ok"),
        E("B0", "B1"), E("B1", "B2"), E("B2", "B3", "ja"), E("B2", "B5", "nein"), E("B3", "B4"),
        E("C0", "C1"), E("C1", "C2", "ja"), E("C1", "C5", "nein"), E("C5", "C2", s1="r", s2="b"),
        E("C2", "C3"), E("C3", "C6", "ja"), E("C3", "C4", "nein", style="ok"), E("C6", "C4", s1="r", s2="b", style="ok"),
        E("D0", "D1"), E("D1", "D5", "nein", style="err"), E("D1", "D2", "ja"),
        E("D2", "D6", "nein", style="err"), E("D2", "D3", "ja"), E("D3", "D4", style="ok"),
        E("D8", "D9"), E("D9", "D10"), E("D10", "D11", "ja", style="ok"),
        E("D10", "D12", "nein", s1="t", s2="t", style="err", via=[(792, gy(7, 8.3)), (1448, gy(7, 8.3))]),
        E("E0", "E1"), E("E1", "E2", "ja", style="ok"), E("E1", "E5", "nein", style="err"),
        E("E3", "E6", "existiert", style="err"), E("E4", "E7", "nicht eigen", style="err"),
    ],
}]
BOARDS[0].update({
    "notes": [
        "settings.json liegt unter $TOMACCO_HOME oder ~/.local/share/tomacco/; settings.get liest sie, settings.configure prüft Pfade (absolut, vorhanden; Werkzeuge bekannt und ausführbar) und schreibt atomar unter settings.lock. Es gibt keine settings-Import- oder Export-Operation.",
        "Die GUI-QSettings (auch Telefonlayout; Organisation marselnenaj, App Tomacco) halten appearance, appearance/omarchyAccent, appearance/uiScale, language, device, Notifications/jobs, NativeDisplay/*, NativeAudio/forwardAudio, NativeIphone/runnerBundle sowie Fenster, Verlauf und Ansichtszustände. Diese Darstellungswahl ist keine Laufzeit-Einstellung der Steuerung.",
        "Export nimmt nur portable, geprüfte Werte (Format tomacco-desktop-settings, version 1); Pfade, Fenstergrößen, Projektverlauf, Jobverlauf und Forschungseinstellungen bleiben. Tomacco speichert dort keine Zugangsdaten. Import übernimmt jeden gültigen Schlüssel einzeln und zählt ungültige als ignoriert.",
        "Sprachen: i18n/languages.json hat 51 vollständige Kataloge, 50 aus Omarchys Registry (mit English) und das allgemeine Spanisch es als Altkatalog außerhalb der Registry. Angeboten wird nur, was complete ist; English zuerst, dann nach englischem Namen.",
        "Schrift: Archivo und JetBrains Mono sind eingebaut; für Schriften, die Archivo nicht abdeckt, greifen Adwaita Sans, Noto Sans und DejaVu Sans des Systems. Zahlen und Daten folgen formatLocale; das JSON der Steuerung bleibt kanonisch.",
        "Geräteprofile (catalog.*) sind Laufzeit-Operationen mit eigener Prüfung; ihre Chooser-Wahl (D19) legt keine Datei an.",
    ],
    "sources": [
        "native/guard.rs:259-268 agent_policy: MCP oder begrenzte CLI verweigert; qml/NativeWorkspace.qml:1425-1438 Transfer auch phone",
        "native/toolchain.rs:136-145 settings() invalid_settings, 146-201 configure() lock/Pfade/executables/updates/jobs/agent_policy/atomic_json",
        "native/api.rs:96-103 settings.* und catalog.*; native/catalog.rs:80-150 invalid_profile/duplicate_profile/builtin_profile, 167-171 file_exists",
        "src/backend.cpp:130-206 Sprachregistry/offeredLanguage/matchLanguage, 209-231 Omarchy-Sprache/resolveLanguage, 240-277 applyLanguage/setLanguage (RTL, QLocale, Qt-Texte), 308-337 uiScale/setUiScale/applyInterfaceFont",
        "src/backend.cpp:345-364 portableSetting, 365-384 exportSettings, 385-411 importSettings (256 KiB, Format, version)",
        "qml/NativeWorkspace.qml:285-318 exportSettingsTo/importSettingsFrom, 769-771 Palette, 1340-1370 Größe und Sprache, 1426-1438 Übertragen; qml/Main.qml:207-209 Tastenkürzel; qml/Theme.qml:10-11 px()",
        "i18n/languages.json (51 Einträge, omarchy:false nur es; aliases)",
    ],
    "links": [["D02", "Desktop-Navigation & Telefonschwelle"], ["D05", "Einrichtung"], ["D14", "Koordinaten & Skalierung"], ["D15", "Policy & Freigabe"], ["D19", "Dateiwahl"], ["D25", "Tomacco-Update"]],
    "screens": [["pvcps58bsa", "n9p6dcd11d", "22 Einstellungen"], ["pvcps58bsa", "5t9p52u7cj", "02 Toolchain-Prüfung"]],
    "routes": ["settings-dialog", "tools"],
    "ops": ["settings.get", "settings.configure", "catalog.list", "catalog.import", "catalog.export", "catalog.remove"],
    "status": "implementiert (4584d6b); Übertragung und Sprache sind GUI-Helfer (auch Telefonlayout), keine Operationen",
})
BOARDS[0]["lanes"][-1]["rows"] = 2.49  # last lane ends at the canvas bottom
