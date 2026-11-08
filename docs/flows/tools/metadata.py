#!/usr/bin/env python3
"""Source-driven companion boards and final index; no device or network calls."""
import sys, json, pathlib, html, collections, hashlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import atlas
ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
esc = html.escape

def p(s):
    return '<p style="margin:0;font-size:20px;line-height:1.45;color:var(--dim)">'+esc(s)+'</p>'

def title(id, name, lead):
    return '<header data-name="Kopf · '+id+'" style="display:flex;flex-direction:column;gap:16px;color:var(--fg)"><div style="font-family:var(--mono);font-size:16px;color:var(--at)">'+id+' · TOMACCO / FLOWS &amp; LOGIK</div><h1 style="margin:0;font-size:var(--text-docs-title);font-weight:900;line-height:.98;letter-spacing:-.04em;text-wrap:balance">'+esc(name)+'</h1>'+p(lead)+'</header>'

def link(target, label):
    return '<div data-name="AtlasLink → '+esc(target)+'" style="display:flex;align-items:center;gap:20px;min-height:52px;padding:12px 16px;border:2px solid var(--rule);box-sizing:border-box;color:var(--fg);font-size:20px;line-height:1.3"><span style="font-family:var(--mono);font-size:16px;color:var(--at);flex-shrink:0">'+target+'</span><span>'+esc(label)+'</span></div>'

def section(name, body):
    return '<section data-name="'+esc(name)+'" style="display:flex;flex-direction:column;gap:20px"><h2 style="margin:0;font-size:32px;line-height:1.1;font-weight:900;letter-spacing:-.04em;color:var(--fg)">'+esc(name)+'</h2>'+body+'</section>'

def shortlink(target):
    return '<div data-name="AtlasLink → '+target+'" style="display:flex;align-items:center;justify-content:center;min-height:44px;min-width:68px;padding:0 12px;box-sizing:border-box;border:2px solid var(--rule);font-family:var(--mono);font-size:16px;color:var(--fg)">'+target+'</div>'

ROUTES={'runtime':['D04','D08','D09','D10'], 'preview':['D28'], 'project':['D06','D07'], 'devices':['D08','D09','D31'], 'ios-research':['D11','D32'], 'jobs':['D18'], 'tools':['D29','D30'], 'debug':['D22'], 'testing':['D21'], 'profiling':['D22'], 'files':['D19'], 'release':['D24'], 'demo':['D23']}

def coverage(by):
    ops=json.loads((REPO/'assets/operations.json').read_text())
    domains=collections.defaultdict(list)
    for op in ops: domains[op['name'].split('.')[0]].append(op['name'])
    rows=[]
    for domain, names in sorted(domains.items()):
        bids=[b['id'] for b in by.values() if set(names)&set(b.get('ops',[]))]
        destinations=''.join(shortlink(bid) for bid in bids)
        rows.append('<div style="display:grid;grid-template-columns:220px 72px 1fr;gap:16px;padding:12px 0;border-top:1px solid var(--rule);align-items:center"><div style="font-family:var(--mono);font-size:20px;color:var(--fg)">'+esc(domain)+'</div><div style="font-family:var(--mono);font-size:18px;color:var(--dim)">'+str(len(names))+'</div><div style="display:flex;flex-wrap:wrap;gap:8px">'+destinations+'</div></div>')
    # Domain rows intentionally retain all relevant flow targets, not just one arbitrary default.
    body=section('33 Domänen · 296 Verträge',p('Jede Operation ist im Datensatz logic-operation-flow-map ihrem Ablauf, einem logischen Knoten und den Quellankern zugeordnet. Die Liste hier ist der Einstieg je Domäne. Abdeckung bedeutet dokumentierter Vertrag; nicht jede Argumentkombination oder Hardware wurde getestet.')+''.join(rows))
    rr=[]
    for route, bids in ROUTES.items():
        rr.append('<div style="display:grid;grid-template-columns:220px 1fr;gap:16px;padding:12px 0;border-top:1px solid var(--rule);align-items:center"><div style="font-family:var(--mono);font-size:20px;color:var(--fg)">'+route+'</div><div style="display:flex;flex-wrap:wrap;gap:8px">'+''.join(shortlink(bid) for bid in bids)+'</div></div>')
    body+=section('13 CLI-Ziele · tatsächliche UI-Routen',p('desktop.open und --workspace verwenden diese 13 Namen. Main.showWorkspace behandelt runtime und preview gesondert; ios-research zeigt Geräte mit dem iOS-Panel. Die Desktop-Rail hat elf Einträge. Telefon: vier primäre Ziele plus Mehr; die interne Extra-ID web-preview führt zur Web-Vorschau, ist aber kein weiteres CLI-Ziel.')+''.join(rr))
    body+=section('Zusätzliche Einstiege außerhalb der Registry',p('tomaccoctl setup agents mit status/remove und den zehn Client-Adaptern ist CLI-Bootstrap außerhalb der 296 Operationen (D29). agent.policy ist die einzige agent.*-Operation. Studio.serveFolder ist ein GUI-Helfer für Ordner/Build/HTTP-Server (D28). setup status und native Fenster-/Dateiwahl-/QSettings-Helfer sind ebenfalls keine erfundenen typed operations.')+link('D29','Coding-Agent verbinden')+link('D28','Web-Vorschau und serveFolder')+link('D30','Schema und Bestätigung im Tools-Formular'))
    body+=section('Weiter',link('00','Übersicht und alle Abläufe')+link('S1','Stand und nächste Nachweise'))
    return body

def payloads():
    bs=atlas.load_boards(); by={b['id']:b for b in bs}
    current=atlas.provenance(); short=current['product_source'][:7]
    installation='installiert' if current.get('desktop_installed') else 'noch in Prüfung'
    candidate_note=' · '+current['candidate_version']+' Kandidat (privat)' if current.get('candidate_visibility')=='private' and current.get('candidate_version') else ''
    installed_provenance='Produkt '+short+' · '+installation+candidate_note
    public_release=current.get('public_release_version','1.4.1')
    test_summary=current.get('desktop_test_summary','14 CTest-Prüfungen bestanden;') if current.get('desktop_installed') else 'Die abschließende Testqualifikation läuft noch;'
    ocr_summary=p('screen.ocr bietet optional invert:adaptive und detail:matches für eine kompakte Zielsuche. auto bleibt der Standard mit beiden Polaritäten; adaptive kann weitere Wörter oder Duplikate übersehen. testing.action, testing.find sowie OCR-Warte- und Abwesenheitsprüfungen bleiben bei auto. Gemessen werden echte Engine-Aufrufe und getrennte Capture-/Lese-/Gesamtzeiten. Das ist keine Gerätefreigabe und kein Geschwindigkeitsvergleich mit anderen Produkten.') if current.get('ocr_delta') else ''
    perf_summary=''
    if current.get('perf_delta'):
        perf_summary=p('Persistente MCP-Verbindungen halten für geeignete System-Tesseract-5-Installationen einen isolierten OCR-Worker warm. Kalte CLI-Aufrufe, eigene Tesseract-Binaries, große Bilder und gleichzeitig belegte Worker nutzen weiter den CLI-Prozess. Auto/Adaptive, Erkennungsdaten und die 296 öffentlichen Verträge bleiben gleich.')+p(current.get('perf_measurement_text','Die Messwerte des endgültig installierten Performance-Builds werden getrennt qualifiziert.'))+p('Das warme Modell belegt vorübergehend mehr residenten Speicher. Ein einzelner Worker wird nach 55 Sekunden Leerlauf beendet und vollständig aufgeräumt; 128 Lesevorgänge, Konfigurationswechsel und Prozessende erneuern ihn ebenfalls. Laufzeit, Speicher und Antwortgröße sind begrenzt. Nach einem unbekannten Fehler während einer Erkennung gibt es keinen stillen zweiten Versuch. Abbruch und Zeitüberschreitung bleiben Fehler des aktuellen Aufrufs.')+p('Unabhängige frische App-, Tastatur- und Fokus-Lesevorgänge laufen in derselben WDA-Sitzung parallel. Eingaben bleiben geordnet, Fehler bleiben einzeln sichtbar; reale iPhone-Latenz ist damit nicht gemessen. Die lokale Qualifikation baut ausdrücklich im Release-Modus. Die CMake-Standardeinstellung wurde nicht geändert.')
    delta_name='Performance-Desktopstand' if current.get('perf_delta') else 'OCR-Desktopstand'
    arm_delta_note=' Für den neueren '+delta_name+' gibt es noch kein neu qualifiziertes ARM-Paket.' if (current.get('ocr_delta') or current.get('perf_delta')) and current.get('arm_source_baseline') != current.get('runtime_code_commit') else ''
    source_keys=format(current.get('source_keys',1898),',').replace(',','.')
    out={}
    out['00header']=title('00','App-Flows und Logik','Vom ersten Start bis zur Wiederherstellung: Desktop, Telefon, CLI und Agenten nutzen dieselben Verträge. Jeder Ablauf zeigt Entscheidungen, Fehler, Zustände und die dazugehörigen Quellstellen.')+'<div style="display:flex;flex-wrap:wrap;gap:16px;font-family:var(--mono);font-size:16px;line-height:1.5;color:var(--dim)"><span>'+esc(installed_provenance)+'</span><span>296 Operationen / 33 Domänen</span><span>Öffentliches Release '+esc(public_release)+'</span><span>Stand 08.11.2026</span></div>'
    groups=[('Orientierung & Oberfläche',[1,2,3,4,20]),('Einrichten, Projekt, Build',[5,6,7,19,25]),('Geräte & Ausführung',[8,9,10,11,26,31]),('Beobachten, handeln, absichern',[12,13,14,15,16,17,27]),('Jobs, Qualität & Veröffentlichung',[18,21,22,23,24,32,33]),('Browser & Coding-Agent',[28,29,30])]
    chunks=[]
    for name, nums in groups:
        rows=''.join(link('D%02d'%n,by.get('D%02d'%n,{}).get('title',{'D31':'Android-Werkzeuge und AVD-Lebenszyklus','D32':'Virtuelles iOS: Forschung und verifizierte Zustände','D33':'Artefakte, Toolchains und Diagnose'}.get('D%02d'%n,''))) for n in nums)
        chunks.append(section(name,'<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">'+rows+'</div>'))
    chunks.append(section('Abdeckung & aktueller Stand', '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">'+link('C1','296 Operationen, 33 Domänen und alle Routen')+link('S1','Implementiert, abhängig, ungeprüft und geplant')+'</div>'))
    out['00index']='<nav data-name="Atlas-Index" style="display:flex;flex-direction:column;gap:40px">'+''.join(chunks)+'</nav>'
    out['S1header']=title('S1','Stand und nächste Nachweise','Quellcode und Fixture-Prüfungen erklären das Verhalten. Geräte- und Transportqualität brauchen eigene Nachweise. Dieser Atlas hält diese Ebenen getrennt.')
    out['S1body']='<div data-name="Statusinhalt" style="display:flex;flex-direction:column;gap:40px">'+''.join([
        section('Implementiert und lokal qualifiziert',p('Desktop-Produkt '+short+' ist '+installation+candidate_note+'. Öffentlich veröffentlicht ist '+public_release+'. Die Dialoge passen sich an Fenster, UI-Skala und Telefon-Layout an. '+test_summary+' '+source_keys+' Quelltexte, 50 vollständige Übersetzungen sowie Englisch sind vorhanden. Sprachkataloge wurden technisch geprüft, nicht von Muttersprachlern abgenommen.')+p('Demo Studio behält optional den Originalton derselben gespeicherten Szene. Stumm bleibt der Standard, PNG-Vorschauen bleiben stumm. Reale lokale Medien-Fixtures prüfen Ausschnitt, verzögerte und kurze Tonspuren sowie feine und grobe Video-Zeitbasen; beide Streams werden unabhängig dekodiert. Audio-Mixing und mehrere Clips sind noch Folgeschritte.')+ocr_summary+perf_summary+p('296 typisierte Operationen in 33 Domänen; zehn Coding-Agent-Adapter. Die CLI kennt 13 Workspace-Ziele. Das Telefon nutzt dieselbe App und dieselbe Steuerung. Die Diagramme dokumentieren Quelllogik; sie sind keine Gerätefreigabe.')),
        section('Von Host, Werkzeugen oder Gerät abhängig',p('Android braucht passende SDK-/JDK-Werkzeuge, ein autorisiertes Gerät oder einen nutzbaren Emulator. iPhone per USB braucht Vertrauen, Developer-Image und WebDriverAgent; AirPlay-Video und Bluetooth-Eingabe sind getrennte Transporte. Apple Simulator und Xcode brauchen macOS.')+p('Remote-Operationen werden ausdrücklich aufgerufen. Build und Run wählen nie still einen anderen Host. Das angeschlossene MacBook läuft mit Omarchy Linux ARM: Es ist weder ein Xcode-Mac noch ein x86_64-Host für das experimentelle virtuelle iPhone.')),
        section('Noch auf realer Hardware zu prüfen',p('Das veröffentlichte experimentelle ARM-Paket '+current.get('arm_package_version','1.5.0')+' stammt aus '+current.get('arm_source_baseline','')[:7]+'. '+current.get('arm_status_text','')+' '+current.get('candidate_arm_status_text','')+' Das Pixel wurde nicht verändert oder installiert; die Freigabe nach laufenden Flash- und Mobile-Tests steht aus. Lokale Klassen im Paritätsbericht sind Anforderungen, keine Pixel-Nachweise.')+p('AirPlay-Audio-Synchronität, Mausswipes, physische iPhone-Eingaben und verifiziertes Füllen sind in diesem Stand nicht physisch qualifiziert. Ein wartender Empfänger und bestandene Fixtures beweisen diese Eigenschaften nicht.')),
        section('Geplant oder experimentell',p('Das virtuelle iPhone bleibt ein experimenteller Forschungs-Runtime mit eigenen Images und engen Host-Anforderungen. D32 zeigt Vorbereitung, Ausführung, Ergebnisbelege und verifizierte Speicherzustände; ein bestätigter Befehl ist kein Beweis für einen sichtbaren App-Zustand.')+p('Demo Studio rendert heute prozedurale Geräteansichten mit QPainter-Perspektive und exportiert geprüfte Medien. Frei steuerbare 3D-Szenen, weitere Renderphasen sowie TV-/Wear-Support gehören zur Roadmap. Sie werden hier nicht als fertig dargestellt.')),
        section('Weiter',link('00','Übersicht und alle Abläufe')+link('C1','Abdeckung und Quellverträge'))])+'</div>'
    out['C1header']=title('C1','Abdeckung und Einstiegspunkte','Die Registry ist der Vertrag. Der Atlas verbindet alle registrierten Operationen mit ihren Abläufen und zeigt, wie die App dieselben Funktionen erreichbar macht.')
    out['C1body']=coverage(by)
    return out

if __name__=='__main__':
    out=payloads(); (ROOT/'build').mkdir(exist_ok=True)
    (ROOT/'build'/'metadata-payloads.json').write_text(json.dumps(out,ensure_ascii=False))
    print(json.dumps({k:len(v) for k,v in out.items()}))
