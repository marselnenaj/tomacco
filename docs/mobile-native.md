# Tomacco auf dem Pixel (Omarchy Mobile) – nativer Stand

Stand 2026-11-06, Branch `feat/pixel-native-app`, Worktree `/home/maslos/Work/tomacco-pixel-native`, Basis `bdd708c`.
Alles hier ist auf dem x86_64-Host gebaut und offscreen geprüft. Nichts davon lief auf dem Pixel; das Gerät ist gesperrt, bis der Besitzer die Sperre aufhebt.

## Was existiert und was belegt ist

| Bereich | Stand | Beleg |
|---|---|---|
| Designsystem | Bogen `u8uvnj33hx`, 24 Flows + States + Landscape/Tablet/Desktop, Textrollen, 10 Komponenten | `docs/design/README.md`, `docs/design/refined/*.png` |
| Phone-Schwelle | `NativeWorkspace.phone` und `Workspace.phone` unter 600 logischen px (nach UI-Skala) | `qml/NativeWorkspace.qml`, `qml/Workspace.qml` |
| Fenster-Minimum | `Main.qml` lässt das Fenster so schmal wie den Bildschirm, statt 560 px zu erzwingen | `qml/Main.qml` |
| Bottom-Navigation | `qml/MobileNav.qml`: Projekt, Geräte, Jobs, Dateien, Mehr (Sheet mit den übrigen Bereichen), 48 px Ziele, Labels immer sichtbar | `tests/mobile_nav_test.cpp`: 10/10 bestanden bei 360/393/412 px und 100/175/200 % |
| Listenzeile | `qml/MobileRow.qml` nach Bogen „Mobile / Row“: Titel 16/600, Detail 14 (oder Mono 13), Meta 13 max. 40 %, Punkt und Meta an der Titelzeile, min. 56 px, wächst mit Umbruch | `tests/mobile_row_test.cpp`: 8/8 bestanden |
| Aktionsknöpfe | `qml/StudioButton.qml` mit `touch` (2026-11-07, `integration/finish-tomacco`): die Beschriftung bricht um statt abgeschnitten zu werden, der Knopf wächst mit (min. 48 px skaliert); in einem Flow nie breiter als die Zeile, in Layouts mit `Layout.fillWidth` + `Layout.maximumWidth: naturalWidth` schmaler statt über den Rand; Icon am Zeilenanfang, bei RTL rechts; Desktop- und Icon-Knöpfe unverändert | `tests/studio_button_test.cpp`: 7/7 bestanden; `tests/phone_routes_test.cpp` `translated_action_labels_stay_whole` (de/ta/ar 360 px, ja 393 px, je 200 %) |
| Phone-Tokens | `Theme.js` `mobileType`/`mobileSizes`, `Theme.qml` `mobileFont*`, `mobileTouch/Control/Bar/Nav/Row/Gutter`; skalieren mit der UI-Größe, berühren keine Gerätegeometrie | qmllint ohne Befund für MobileRow, MobileNav, Theme |
| Jobs auf dem Telefon | Liste oder Detail statt Split, „Alle Jobs“ zurück | `qml/Workspace.qml` (uncommitted, noch ohne eigenen Test) |
| Paritätsmatrix | 274 Operationen: 175 lokal, 62 nur mit Host, 37 bedingt, 0 nicht verfügbar; `verified_on_pixel` überall false | `docs/mobile-parity.json`, `tools/build_mobile_parity.py --check` |

Tests bauen mit `cmake -S . -B build -G Ninja -DBUILD_TESTING=ON` und `ninja -C build test-mobile-nav test-mobile-row test-studio-button`; sie laufen mit isoliertem `HOME`, `XDG_CONFIG_HOME`, `TOMACCO_HOME` und `QT_QPA_PLATFORM=offscreen`, ohne Control, CLI, Geräte oder Netzwerk.

## Nicht geprüft

- Die gesamte Desktop-Suite (`test-desktop-ui`, 107 Tests) wurde in diesem Worktree nicht gebaut: sie braucht den Rust-Kern (`tomacco-core`) und WebEngine; das sprengt das 120-Sekunden-Fenster des Sandbox-Befehls. Die Phone-Pfade sind so geschrieben, dass sie unterhalb der 600-px-Schwelle greifen und Desktopfenster unverändert lassen, aber das ist nicht durch die Suite belegt.
- ARM64-Build (`tools/build_portable.py build --arch aarch64`) nicht gestartet (emuliert, Stunden).
- Kein Pixel, kein MacBook, kein ADB/SSH/USB.

## Nächste Schritte (Code)

1. `MobileRow` in die Telefon-Pfade einhängen: Jobliste (`Workspace.qml`, Abschnitt jobs), Geräteliste (`AndroidDevices.qml`), Dateisuche (`FileWorkspace.qml`); Desktop-Delegates unverändert lassen.
2. Projektseite als Liste (Suche, Zuletzt, gespeicherte Arbeitsbereiche, Anderen Ordner öffnen, Xcode-Projekt importieren = `project.import_xcode`) unterhalb der Schwelle; Build & Run als Ziel-Karte mit einer primären Aktion.
3. Einstellungen als Settings-/Toggle-Zeilen (Sprache, Oberflächengröße, Erscheinungsbild, Omarchy-Akzent, Speicher, WLAN-Downloads, Hosts, Agent-Verbindung, Benachrichtigung, Diagnosepaket).
4. Desktop-Suite in einem Build-Verzeichnis mit längerem Zeitfenster bauen und die bestehenden 107 Tests plus die Phone-Tests laufen lassen; dann Jobs-Phone-Umschaltung mit Test belegen.
5. Neue Strings laufen über `qsTr`; die Liste steht in `docs/design/README.md` für die i18n-Lane.

## Git

Der Sandbox-Befehl kann in diesem Worktree nicht committen (gemeinsames `.git` ist schreibgeschützt). Alle Änderungen liegen uncommitted im Worktree; `git status` listet sie. Kein Push, kein Release.

## Stand 2026-11-07: Telefon-Routen fertig (Branch `integration/finish-tomacco`)

Unter 600 logischen px (Fensterbreite nach UI-Größe) zeigt jede Route eine Telefonansicht, die dieselben Daten und Operationen wie der Desktop nutzt. Nichts davon lief auf dem Pixel.

| Route | Telefon | Beleg |
|---|---|---|
| Geräte | Liste aus `MobileRow` (Emulatoren, Telefone, iPhones), Geräteseite mit Zustand, einer Primäraktion, Geräteaktionen nur im laufenden Zustand, Verwalten über die bestehenden Dialoge mit ihren Bestätigungen; „nicht mehr gelistet“ und „nicht autorisiert“ | `devices_list_detail_and_back` |
| Dateien | ein Bereich zur Zeit (Liste/Suche oder Datei), Namen und Pfade umbrechend, Speichern/Pfad kopieren/Neu laden, Code scrollt seitlich, Laden/kein Treffer/Fehler | `files_browse_search_and_file` |
| Projekt, Build & Run | Projektliste (Suche, Zuletzt, gespeicherte Arbeitsbereiche, Ordner öffnen, Pfad, neues Projekt, Xcode-Import) und Übersicht mit Ziel, Mac-Host-Hinweis für iOS, genau einer Primäraktion und eingeklappten Optionen | `project_list_overview_and_options` |
| Projektordner wählen | „Ordner öffnen“ öffnet die App-eigene Projektauswahl als Sheet über die ganze Breite: ganzer Pfad umbrechend und eingebbar, Ordnernamen umbrechend, Symlinks mit Ziel, 48-px-Ziele, Abbrechen lässt das Projekt unverändert | `project_chooser_is_a_phone_sheet_with_touch_targets` |
| Einstellungen, Agent | Sheets über die volle Breite oberhalb der Tastatur; Gruppe „Agents and system“ (Agent verbinden, Richtlinie, Mac-Hosts, SDK-Pfade, Speicher, Fehlerbericht) öffnet die echten Formulare | `settings_sheet_and_agent_clients` |
| Jobs, Tools | Detail mit voller Überschrift, 48-px-Zurück und Aktionen in eigener Zeile; Tools als Liste oder Formular | `jobs_detail_targets_and_back` |
| Übergreifend | Escape und System-Zurück schließen das Detail, Tastatur-/SafeArea-Insets, fokussiertes Feld bleibt sichtbar, Zustand über Schwelle, Querformat, Tablet und externes Display | `keyboard_inset_keeps_the_focused_field_visible`, `threshold_and_orientation_keep_each_route_state` |

Tests: `test-phone-routes` (`tests/phone_routes_test.cpp`) lädt `Main.qml` mit Backend und Control-Programm auf einem telefonbreiten Offscreen-Bildschirm, isoliert (eigenes `TOMACCO_HOME`, XDG, QSettings), AVD-Dateien als Fixture für das echte `android.avds` und ein Stand-in-`adb`, das nur eine vorbereitete Liste ausgibt. Lauf: `bash .scratch/run_qt_halves.sh test-phone-routes` (beide Hälften grün), Desktop `bash .scratch/run_qt_halves.sh test-desktop-ui` (113/113). Renderings: `docs/design/phone-final/`. Fortschritt und neue Quelltexte für die Übersetzungen: `docs/phone-finish-progress.json`.

Nicht geprüft: echte Bildschirmtastatur und SafeArea-Werte eines Geräts (Insets im Test eingespeist), die System-Zurück-Taste auf Android (synthetische `Key_Back`), WLAN-only-Downloads (kein Backend-Schalter vorhanden, daher kein Schalter in der App), ARM64-Build.
