# Tomacco · App – Mobile-Designstand (Bogen)

Stand 2026-11-06. Bogen-Datei `u8uvnj33hx` „Tomacco · App“, Brand `mm6iq5cs67` (Tomacco v6).
Seiten: `pvcps58bsa` 01 · Phone flows, `ui226jha6w` 02 · Components & states, `u17qz4xpxe` 03 · Landscape, tablet, desktop.

Alle Inhalte der Boards sind Designdaten mit logischen Viewports (393×852 Referenz, geprüft bei 360/393/412).
Kein Board ist eine gemessene Pixel-Auflösung und nichts davon wurde auf dem Pixel ausgeführt.

## Was der Nutzer abgelehnt hatte und was geändert wurde

Nutzerfeedback: „die screens müssen noch viel besser werden, das ist noch zu rough und keine gute hierarchie, typografie usw“.

Zwei technische Ursachen hinter der flachen Typografie, beide belegt und behoben:

1. **Archivo war im Dokument nur mit Gewicht 800–900 registriert.** Die Brand hatte dasselbe Font-Asset für die Display-Rolle (800/900) und die Body-Rolle (400–700); Bogen dedupliziert Fontfaces nach Quelle, die Body-Face fiel weg. Jedes `font-weight: 400` wurde mit der 800er-Face gerendert.
   Fix über die Brand-API, ohne Bogen-Quellcode: Body-Family `tep0nsj50x` bekam das variable Asset `bogen-asset:40d79a2a…` (wght 100–900, Latin-1 + Latin Extended-A), Datei neu verknüpft. `fonts_captured` zeigt jetzt Archivo 400–700 und 800–900 getrennt. Beleg: `refined/Tomacco-App-Fontprobe-…png` (400/500/600/700 sichtbar unterschiedlich, Umlaute in Archivo).
2. **Benannte Textstile waren wirkungslos.** `html_write` speichert `text-style` als Namen, der Renderer sucht den Key. Alle Layer, die per Name „Mobile / Section“ usw. trugen, rendeten als 16 px regular. Fix: Stile per Key (`mobile-section`) angewendet.
   Hinweis für die Bogen-Lane: `packages/core/src/html-import.ts:690` vs. `render.ts:63`.

## Rollensystem (Textstile in der Datei)

| Stil | Wert | Verwendung |
|---|---|---|
| `mobile-page-title` | Archivo 22 / 600 / 1.2 / −0.01em | Titel in der Top-Leiste |
| `mobile-section` | 13 / 500 / 1.3 / +0.02em, muted | Abschnitts- und Feldlabels, Satzschreibung, nie Mono-Großbuchstaben |
| `mobile-row-title` | 16 / 600 / 1.3 | Name einer Listenzeile (Projekt, Job, Gerät) |
| `mobile-body` | 16 / 400 / 1.4 | Fließtext, Einstellungs-Zeilentitel, Werte |
| `mobile-small` | 14 / 400 / 1.4 | Zeilen-Detail |
| `mobile-label` | 14 / 500 / 1.3 | Tabs, Chips, Textaktionen, Streifen |
| `mobile-meta` | 13 / 400 / 1.4 | Zeit, Status, Hinweise |
| `mobile-code` | JetBrains Mono 13 / 400 / 1.5 | Pfade, Operationsnamen, Logs, Zahlenfelder |
| `mobile-button` | 16 / 600 / 1.2 | Schaltflächen |

Tokens `app-mobile-*` (Schriftgrößen 12/13/14/16/18/22/30, Touch 48, Control 44, Bar 56, Nav 64, Gutter 16, Row 56, Status-Inset 28, Gesten-Inset 24) ergänzen die bestehenden `app-*` Desktop-Tokens derselben Brand. Farben, Familien und Radien sind unverändert.

## Komposition

- 4-px-Raster: Zeilen 12 px vertikal / 16 px Gutter, Abschnittstitel 24–28 px davor und 8 px danach, Karten 12 px innen.
- Listenzeile (`Mobile / Row`): Statuspunkt und Meta sitzen an der Titelzeile (`align-items: flex-start`), Detail bricht um statt Ellipsis; Meta maximal 40 % Breite.
- Eine primäre Aktion pro Aufgabe (Tomato), sekundäre als leise Textaktionen oder quiet Buttons, destruktiv als Textaktion oder Danger-Fläche in der Bestätigung.
- Operationsnamen nur noch als Meta/Code-Kontext (Detailzeile, Formular-Call), nie in Buttons oder Titeln. Jobtitel folgen `qml/OperationTitles.js` (project.run → „Build & Run“).
- Keine „(Beispiel)“-Werte in Controls; die Legende auf Board 00 kennzeichnet alle Daten als Designdaten.
- Deutsche Zahlenschreibung (1,2 GB) bleibt absichtlich; brand_check meldet sie als weichen Hinweis, weil die Brand englisch geschrieben ist.
- 30 px Display-Größe (`app-mobile-font-display`) ist eine Mobile-Rolle, nicht der Web-Typoschritt 28.32 px.

## Komponenten (Seite 02)

| Komponente | ID | Props |
|---|---|---|
| Mobile / Top bar | `1n6uusdg0c` | title, action, show-back, show-action, show-more |
| Mobile / Bottom nav | `6krtv8w1gr` | on-projekt, on-geraete, on-jobs, on-dateien, on-mehr |
| Mobile / Button primary | `72m32u1yq5` | label |
| Mobile / Button quiet | `r2p95dxapq` | label |
| Mobile / Row | `jydau75ebx` | title, detail, meta, show-meta, show-dot, dot |
| Mobile / Job strip | `v0ch34s475` | label, note, dot |
| Mobile / Status inset | `70eykt0rqq` | – |
| Mobile / Settings row | `8z8ua2ed5h` | title, detail, show-detail, value, show-value, show-chevron |
| Mobile / Toggle row (off/on) | `kva3z3w5bf` / `4pjgm6jfd2` | title, detail, show-detail, state |
| Mobile / Card row | `sgk6v24r35` | title, detail, show-detail, show-chevron |

## Referenzscreens und Belege

| Screen | Artboard | Vorher | Nachher |
|---|---|---|---|
| 04 Projekte | `0cvk8ur23v` | `before/Tomacco-App-04-Projekte-393.png` | `refined/Tomacco-App-04-Projekte-393.png` |
| 05 Build & Run | `ue1pxajpns` | `before/Tomacco-App-05-Projekt-Build-Run-393.png` | `refined/Tomacco-App-05-Projekt-Build-Run-393.png` |
| 22 Einstellungen | `n9p6dcd11d` | `before/Tomacco-App-22-Einstellungen-393.png` | `refined/Tomacco-App-22-Einstellungen-393.png` |

Alle übrigen Boards (00–23, Components, States, Fontprobe, Landscape, Tablet, Desktop) liegen als `refined/*.png`; die Rohfassung vom 2026-11-05 liegt daneben im Ordner `docs/design/` (Dateien ohne Unterordner).

Prüfungen: `responsive_check` 360/393/412 ohne Überlauf für 04, 05, 09, 12, 20, 21, 22 (02 wird bei 360 noch durch die Listenhöhe begrenzt, siehe offen); `brand_check` Seite 01: Score 96, 0 harte Verstöße, 11 weiche (Zahlenformat, 30 px Display, Radien der Radio-Marker); `design_lint` der drei Referenzscreens: nur Token-Hinweise, behoben.

Bilder: Demo Studio zeigt den Frame `tomacco-demo-studio-20261105/end-to-end/preview-1.png` (Ausgabe von `demo.preview`, 1080×1920); das Live-Gerät zeigt `tomacco-demo-studio-20261105/source-frame.png` (vorhandener Capture-Frame). Beides sind vorhandene Artefakte vom 2026-11-05, keine neuen Geräteaufnahmen und keine laufende Spiegelung.

Bogen-Checkpoints: `mw425g4a-8a8bd145` (Rohstand vor der Überarbeitung), `mw4tbo22-e4a661cd` (Font-Fix), `mw4tofvf-8998a432` (Referenzscreens), `mw4u4lek-15ecdd50` (Propagation auf alle Flows).

## Offen

- 02 Toolchain bei 360 px: die Liste braucht einen eigenen Scrollbereich, damit die beiden Aktionen unten bleiben.
- Root-Abnahme der Kommentare `uj73ig7agx`, `8nteueyy96`, `qffi28xwnv`, `94ynmnu3vz`, `pueu3bs4mh`, `d5rqhtf79q` sowie der Font-Threads; Antworten mit Belegen sind hinterlegt, Auflösen bleibt bei der Person.
- Native Umsetzung: siehe `.scratch/pixel-native-progress.json`.

## Neue UI-Strings für die i18n-Lane (qsTr)

Xcode-Projekt importieren · Liest ein .xcodeproj und legt die Tomacco-Konfiguration an · Zuletzt · Gespeicherte Arbeitsbereiche · Weitere · Anderen Ordner öffnen · Ziel · Ändern · Bauen und starten · Nur bauen · Letzte Jobs · Alle · Darstellung · Oberflächengröße · Gerätespiegel und Screenshots bleiben unverändert · Erscheinungsbild · Omarchy-Akzent übernehmen · Nur bei ausreichendem Kontrast · Speicher und Netz · Artefakte, Aufnahmen und SDK-Pakete · Downloads nur im WLAN · SDK-Pakete und Images fragen sonst nach · Hosts · Agent und System · Agent-Verbindung · Kein offener Netzwerkport · Benachrichtigung bei Jobende · Diagnosepaket erstellen · Szene geändert · Entwurf bleibt in dieser Sitzung erhalten · Keyframe bei %1 hinzufügen · Werte zwischen Keyframes werden interpoliert · Jobs laufen im ControlPlane weiter, auch wenn die App im Hintergrund ist · Host einrichten · Telefon per USB · Telefon per WLAN · Übernimmt der Host · Play-Upload fragt danach einzeln nach.

## App-Renderings der fertigen Telefon-Routen (2026-11-07)

Artboard `f9343218ev` auf Seite `pvcps58bsa` zeigt die gebaute App (Qt offscreen, `test-phone-routes`) neben den Boards: Geräte Liste/Detail, Dateien Suche/Datei, Jobs Detail, Projekt Build & Run und Liste, Einstellungen-Sheet, Agent-Clients sowie die Geräteseite bei 412 px und 200 %. Dateien: `docs/design/phone-final/`. Bogen-Checkpoint `mw5run13-bd8b7896`; `brand_check` des Artboards: 0 harte Verstöße.

Abweichungen der App von den Boards, bewusst: Die Meta-Angabe einer Zeile rutscht unter 300 logischen px unter den Titel (sonst würde der Titel bei 200 % zu schmal); Telefon-Seiten mit eigener Kopfzeile (Projekt, Geräte) zeigen kein Abschnittsband; ein Schalter „Downloads nur im WLAN“ fehlt, weil es dafür keine Einstellung im Kern gibt.

## Datei-, Ordner- und Speichern-Wahl (2026-11-08)

Eine Komponentenfamilie für alle 24 Einstiege (23 frühere Qt-`FileDialog`/`FolderDialog` und die Projektwahl), umgesetzt in `qml/FileChooserDialog.qml`.

| Was | Seite | ID |
|---|---|---|
| Komponente `Mobile / File chooser` (Varianten `form`: `sheet` / `dialog`; Props title, path, file-name, show-file, file-type, show-type, problem, show-problem, action; Slot `entries`) | 02 | `wn42y1snnf` (sheet), `suu0bhd363` (dialog) |
| Komponente `Mobile / File chooser entry` (Varianten `kind`: `folder` / `file`; Props name, target, show-target) | 02 | `8m44q5eqhx`, `5cj9u8jtpg` |
| Komponente `Mobile / File chooser replace` (Prop question) | 02 | `dyqk7tkjs2` |
| Artboard Komponente & Zustände (Speichern, Ordner, Fehler) | 02 | `cy5ndx7v8b` |
| Artboard Desktop-Dialog (100 %) | 02 | `kkdhpfje2k` |
| Board 24 · Datei wählen · Sheet · 393 | 01 | `xw5eiqpyd2` |
| Desktop · Speichern & Ersetzen? · 1440×900 | 03 | `kznxb39dd7` |
| App-Render · Datei-, Ordner- und Speichern-Wahl (offscreen, synthetische Dateien) | 03 | `m2ga8mchqa` |

Texte auf den Boards sind Designdaten in den deutschen Übersetzungen der App (`i18n/tomacco_de.ts`). Das App-Render-Board zeigt echte Aufnahmen der gebauten App (Qt offscreen, Software-Renderer, isolierte Umgebung, nur synthetische Dateien); die Tastatur im Telefonbild ist eine simulierte Aussparung. Dateien: `docs/design/adaptive-dialogs/`.

Prüfungen: `brand_check` 0 harte Verstöße auf allen fünf Boards (Board 24: 91, Komponenten-Artboard: 92, Desktop-Dialog: 96, Desktop-Screen: 94, App-Render: 87); einziger weicher Hinweis ist der absichtlich blasse deaktivierte „Öffnen“-Knopf im Fehlerzustand (`28ye2fsz4m/ru48bv1nf3`, 3,94:1, wie `Theme.faint` in der App). `responsive_check`: Board 24 und das Komponenten-Artboard ohne Überlauf bei 320/360/393/412, das Desktop-Board bei 1440/1380/900 (nach Zentrieren der Frage).

Bogen-Checkpoints: `mw73b3ja-8f55895a` (Stand vor dieser Arbeit), `mw73t6do-44d8c362` (Komponenten, Boards, Belege). Keine Seite und keine Brand neu angelegt; bestehende Boards unverändert.
