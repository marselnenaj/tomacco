import QtQuick
import QtQuick.Shapes
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
    id: root
    moduleName: "io.github.marselnenaj.tomacco"
    // Omarchy routes summon/toggle to the focused monitor’s bar instance.
    manageIpc: false
    readonly property color foreground: bar ? bar.foreground : Color.foreground
    readonly property color dim: root.alpha(foreground, 0.62)
    readonly property color faint: root.alpha(foreground, 0.4)
    readonly property color accent: "#f2361f"
    readonly property color good: "#2fa84f"
    readonly property color bad: bar ? bar.urgent : Color.urgent
    readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
    // This file runs inside the Omarchy shell, where the desktop's translations are not
    // available: English by default, German when the system's locale is German.
    readonly property bool german: Qt.locale().name.startsWith("de")
    function tr(english,deutsch) { return german ? deutsch : english }
    readonly property string home: Quickshell.env("HOME")
    readonly property string cli: home + "/.local/bin/tomaccoctl"
    // Replaced by the installer with the content hash of this file.
    readonly property string build: "@TOMACCO_PANEL_BUILD@"
    readonly property bool notificationOwner: button.QsWindow.window && button.QsWindow.window.screen === Quickshell.screens[0]

    property var snapshot: ({jobs:[],sessions:[],runtimes:[],workspaces:{},running:0})
    property var devices: []
    property var avds: []
    property var diagnostics: []
    // The one-step virtual iPhone (experimental research runtime).
    property var phone: ({configured:false})
    readonly property bool phoneRunning: phone.guest_running===true
    readonly property bool phoneBusy: !!phone.busy
    readonly property bool phoneStarting: phoneBusy && phone.busy.action==="ios.virtual.start"
    readonly property bool phoneStopping: phoneBusy && phone.busy.action==="ios.virtual.stop"
    readonly property string phoneLabel: {
        if(phone.busy && phone.busy.action==="ios.virtual.stop") return root.tr("stopping …","wird beendet …")
        let starting=({preparing:root.tr("preparing …","wird vorbereitet …"),booting:root.tr("iOS is starting … takes many minutes","iOS startet … dauert viele Minuten"),unlocking:root.tr("unlocking …","wird entsperrt …")})[phone.phase]
        if(phone.busy && starting) return starting
        if(phoneRunning) return phone.phase==="ready" || !starting ? root.tr("running","läuft") : starting
        if(phone.guest_ended) return phone.guest_saved ? root.tr("off · saved and ended at the time limit","aus · am Zeitlimit gesichert beendet") : root.tr("off · the session ended without saving","aus · Sitzung endete ohne Sichern")
        return phone.phase==="failed" ? root.tr("start failed","Start fehlgeschlagen") : phone.phase==="unverified" ? root.tr("off · last run not saved","aus · letzter Lauf nicht gesichert") : root.tr("off","aus")
    }
    property var previous: ({})
    property bool initialized: false
    property bool hasError: false
    property string message: ""
    property bool messageIsError: false
    property string busyLabel: ""
    property var queue: []
    property real now: Date.now()/1000

    readonly property var jobs: snapshot.jobs || []
    readonly property var liveJobs: jobs.filter(j=>j.state==="running" || j.state==="queued")
    // Emulators and audio bridges run for a whole session. They are listed,
    // but only real builds and tasks make the bar icon show activity.
    function isService(job) { return /^Emulator /.test(job.label) || /^Android(-Ton| audio)/.test(job.label) || job.label==="Run virtual iOS platform" }
    readonly property var workJobs: liveJobs.filter(j=>!isService(j))
    readonly property var recentJobs: jobs.filter(j=>j.state!=="running" && j.state!=="queued").slice(0,5)
    // Jobs come newest first.
    readonly property var latestJob: jobs.length ? jobs[0] : null
    readonly property var liveRuntimes: (snapshot.runtimes || []).filter(x=>x.status==="connected")
    readonly property var liveRuntime: liveRuntimes.length ? liveRuntimes[0] : null
    readonly property bool desktopOpen: (snapshot.sessions || []).some(x=>x.session==="main")
    readonly property bool anyLive: liveRuntimes.length>0 || phoneRunning
    readonly property var readyDevice: devices.find(isReady) || null
    readonly property bool canPopOut: !!liveRuntime || !!readyDevice || phoneRunning
    // Pinned emulators first; device.list already lists pinned devices first.
    readonly property var idleAvds: avds.filter(a=>!devices.some(d=>d.avd===a.name)).sort((a,b)=>(b.pinned===true)-(a.pinned===true))
    readonly property var workspaceNames: Object.keys(snapshot.workspaces || {})
    // update.status (no network): a new version, or one installed while the
    // open workspace still runs the old one.
    property var updateState: ({})
    readonly property string desktopVersion: ((snapshot.sessions || []).find(x=>x.session==="main") || {}).version || ""
    readonly property bool restartNeeded: desktopOpen && desktopVersion!=="" && !!updateState.current && updateState.current!==desktopVersion
    readonly property bool updateAvailable: updateState.update_available===true && !restartNeeded
    readonly property string updateNote: restartNeeded ? root.tr("Tomacco %1 is installed. Restart the workspace to use it · U","Tomacco %1 ist installiert. Workspace neu starten, um ihn zu nutzen · U").arg(updateState.current)
        : updateAvailable ? root.tr("Tomacco %1 is available · U opens the workspace to install it","Tomacco %1 ist verfügbar · U öffnet den Workspace zum Installieren").arg(updateState.latest || "") : ""
    // A small picture of the live device, refreshed while the popup is open.
    property string thumbnail: ""
    property string thumbnailDevice: ""
    readonly property string thumbnailPath: snapshot.data_directory ? snapshot.data_directory+"/panel-thumbnail.png" : ""
    readonly property string summary: hasError ? root.tr("Not reachable","Nicht erreichbar")
        : busyLabel ? busyLabel
        : workJobs.length ? (workJobs.length===1 ? workJobs[0].label : workJobs.length+root.tr(" jobs running"," Aufträge laufen"))
        : liveRuntime ? deviceName(liveRuntime.device)+" · live"
        : phoneBusy ? root.tr("iOS device ","iOS-Gerät ")+phoneLabel
        : devices.length ? devices.length+(devices.length===1?root.tr(" device ready"," Gerät bereit"):root.tr(" devices ready"," Geräte bereit"))
        : phoneRunning ? root.tr("Virtual iOS device running","Virtuelles iOS-Gerät läuft")
        : root.tr("Ready","Bereit")

    implicitWidth: button.implicitWidth
    implicitHeight: button.implicitHeight

    function alpha(c,a) { return Qt.rgba(c.r,c.g,c.b,a) }
    function isReady(device) { return device.state==="device" || device.state==="connected" || device.state==="online" }
    function refresh() { if(!poll.running) { poll.started=false; poll.running=true } }
    function unreachable(detail) { hasError=true; message=detail || root.tr("Tomacco is not reachable. Check the installation.","Tomacco ist nicht erreichbar. Installation prüfen."); messageIsError=true }
    function scan() { if(!deviceScan.running) deviceScan.running=true; if(!avdScan.running) avdScan.running=true; if(!phoneScan.running) phoneScan.running=true }
    function deviceName(id) { let d=devices.find(x=>x.id===id); return d ? d.name : id }
    function runtimeFor(id) { return liveRuntimes.find(x=>x.device===id) || null }
    function stateLabel(state) {
        return ({queued:root.tr("Waiting","Wartet"),running:root.tr("Running","Läuft"),succeeded:root.tr("Succeeded","Erfolgreich"),failed:root.tr("Failed","Fehlgeschlagen"),cancelled:root.tr("Cancelled","Abgebrochen"),timed_out:root.tr("Timed out","Zeitlimit")})[state] || state
    }
    // What a job is doing now: its phase, or why it waits.
    function phaseLabel(job) {
        if(job.state==="queued") return job.queued_reason ? root.tr("waiting: ","wartet: ")+job.queued_reason : ""
        let phase=job.phase || ""
        if(!phase || job.state!=="running") return ""
        return ({building:root.tr("building","baut"),waiting:root.tr("waiting","wartet"),deploy:root.tr("installing","installiert"),session:root.tr("session running","Sitzung läuft"),streaming:root.tr("streaming","überträgt"),ready:root.tr("ready","bereit")})[phase] || phase.replace(/[_-]/g," ")
    }
    function captureThumbnail() {
        if(!opened || !liveRuntime || !thumbnailPath || thumbScan.running) return
        thumbScan.device=liveRuntime.device
        thumbScan.command=[cli,"call","runtime.capture",JSON.stringify({session:liveRuntime.session,path:thumbnailPath})]
        thumbScan.running=true
    }
    function stateColor(state) {
        return state==="succeeded" ? good : state==="failed" || state==="timed_out" ? bad : state==="running" || state==="queued" ? accent : faint
    }
    function span(seconds) {
        seconds=Math.max(0,Math.round(seconds))
        if(seconds<60) return seconds+" s"
        if(seconds<3600) return Math.floor(seconds/60)+" min"
        if(seconds<86400) return Math.floor(seconds/3600)+" h "+Math.floor(seconds%3600/60)+" min"
        return Math.floor(seconds/86400)+root.tr(" d"," T")
    }
    // Commands run strictly one after another; a failed step drops the rest
    // of its own chain so a later step never acts on a missing result.
    function run(label, steps) {
        queue=queue.concat(steps.map((step,index)=>({label:label,operation:step[0],args:step[1] || {},chain:steps.length-1-index,note:step[2] || ""})))
        next()
    }
    function next() {
        if(action.running || !queue.length) { if(!action.running) busyLabel=""; return }
        let step=queue[0]
        action.step=step; action.started=false; busyLabel=step.label; message=""
        action.command=[cli,"call",step.operation,JSON.stringify(step.args)]
        action.running=true
    }
    function openStudio(tab,path) { let args={tab:tab}; if(path) args.path=path; run(root.tr("Opening the workspace","Workspace wird geöffnet"),[["desktop.open",args]]); close() }
    // A connection made here has no window and therefore no sound; the app
    // adds sound according to its own setting when it shows the device.
    function connectDevice(device) {
        let args={platform:device.platform,device:device.id}
        if(device.platform==="android") args.audio=false
        run(device.name+root.tr(" connecting"," wird verbunden"),[["runtime.start",args]])
    }
    function disconnectDevice(device) { let live=runtimeFor(device.id); if(live) run(root.tr("Disconnecting","Verbindung wird getrennt"),[["runtime.stop",{session:live.session}]]) }
    function popOut(device) {
        let target=device || (liveRuntime ? {platform:liveRuntime.platform,id:liveRuntime.device,name:deviceName(liveRuntime.device)} : readyDevice)
        if(!target) { if(phoneRunning) showPhone(); return }
        // The app shows a session that exists as it is and otherwise connects
        // with its own settings (sound, iPhone runner, Mac). Starting one from
        // here with defaults would conflict with those.
        run(target.name+root.tr(" as a window"," als Fenster"),[
            ["desktop.open",{tab:"runtime",device_only:true}],
            ["preview.configure",{session:"main",native:{platform:target.platform,device:target.id,connect:true,profile_id:target.profile_id || "",frame:true,detached:true}}]])
        close()
    }
    function screenshot() {
        if(!liveRuntime) return
        let stamp=Qt.formatDateTime(new Date(),"yyyy-MM-dd-HHmmss")
        let path=home+"/Pictures/Tomacco/"+liveRuntime.device+"-"+stamp+".png"
        run(root.tr("Saving screenshot","Screenshot wird gespeichert"),[["runtime.capture",{session:liveRuntime.session,path:path},root.tr("Screenshot saved: ","Screenshot gespeichert: ")+path]])
    }
    // Opens a job's log in the desktop's viewer for text files; the control
    // plane names the file (job.get log_path).
    function openLog(job) {
        if(!job || logLookup.running) return
        logLookup.command=[cli,"call","job.get",JSON.stringify({job_id:job.id})]
        logLookup.running=true
        close()
    }
    // The emulator boots without its own window. The device window opens at
    // once with a startup screen and switches to the real picture when
    // Android is up; the desktop app does the waiting.
    function startAvd(avd) {
        run(avd.name+root.tr(" starting"," startet"),[
            ["android.avd_start",{name:avd.name,headless:true}],
            ["desktop.open",{tab:"runtime",device_only:true}],
            ["preview.configure",{session:"main",native:{platform:"android",pending_avd:avd.name,profile_id:avd.profile_id || "",frame:true,detached:true}}]])
        close()
    }
    readonly property var phoneWindowSteps: [
        ["desktop.open",{tab:"runtime",device_only:true}],
        ["preview.configure",{session:"main",ios_guest:{detached:true}}]]
    // The window opens as soon as the guest exists and shows iOS booting.
    function startPhone() { run(root.tr("Virtual iOS device starting","Virtuelles iOS-Gerät startet"),[["ios.virtual.start",{}]].concat(phoneWindowSteps)); close() }
    function showPhone() { run(root.tr("Virtual iOS device as a window","Virtuelles iOS-Gerät als Fenster"),phoneWindowSteps); close() }
    function stopPhone() { run(root.tr("Stopping the virtual iOS device","Virtuelles iOS-Gerät wird beendet"),[["ios.virtual.stop",{}]]) }
    function stopEmulator(device) { run(device.name+root.tr(" stopping"," wird beendet"),[["android.emulator",{device:device.id,action:"stop"}]]) }
    function runWorkspace(name) {
        let w=snapshot.workspaces[name]
        if(!w || !w.device) return
        let args={path:w.path,platform:w.platform,device:w.device,configuration:"debug"}
        if(w.bundle_id) args.bundle_id=w.bundle_id
        run(name+root.tr(" building and starting"," wird gebaut und gestartet"),[["project.run",args]])
    }
    onOpenedChanged: if(opened) { refresh(); scan(); if(!updateScan.running) updateScan.running=true }

    Process {
        id: poll
        command: [root.cli,"call","desktop.status","{}"]
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                let response=null
                try { response=JSON.parse(text) } catch(e) { root.unreachable(); return }
                if(!response.ok) { root.unreachable(response.error.message); return }
                try {
                    let value=response.result, states={}
                    for(let job of value.jobs) {
                        states[job.id]=job.state
                        let was=root.previous[job.id]
                        if(root.notificationOwner && root.initialized && (was==="running" || was==="queued") && ["succeeded","failed","timed_out"].indexOf(job.state)>=0 && !notify.running && root.setting("notifyBuilds",true)) {
                            notify.command=["notify-send","--app-name=Tomacco","--icon=io.github.marselnenaj.tomacco",job.state==="succeeded"?"--urgency=normal":"--urgency=critical",job.label,root.stateLabel(job.state)]
                            notify.running=true
                        }
                    }
                    // The notice about being unreachable goes when Tomacco answers again.
                    if(root.hasError) { root.message=""; root.messageIsError=false }
                    root.snapshot=value; root.previous=states; root.initialized=true; root.hasError=false
                } catch(e) { root.unreachable() }
            }
        }
        // A program that cannot start reports neither output nor an exit.
        property bool started: false
        onStarted: started=true
        onRunningChanged: if(!running && !started) root.unreachable()
        onExited: function(code) { if(code!==0) root.unreachable() }
    }
    Process {
        id: deviceScan
        command: [root.cli,"call","device.list","{\"platform\":\"all\"}"]
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                try {
                    let result=JSON.parse(text)
                    if(!result.ok) throw new Error(result.error.message)
                    root.devices=result.result.devices
                    root.diagnostics=result.result.diagnostics
                } catch(e) { root.message=String(e); root.messageIsError=true }
            }
        }
    }
    Process {
        id: avdScan
        command: [root.cli,"call","android.avds","{}"]
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                try { let result=JSON.parse(text); if(result.ok) root.avds=result.result.avds } catch(e) {}
            }
        }
    }
    Process {
        id: phoneScan
        command: [root.cli,"call","ios.virtual.status","{}"]
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                try { let result=JSON.parse(text); if(result.ok) root.phone=result.result } catch(e) {}
            }
        }
    }
    Process {
        id: updateScan
        command: [root.cli,"call","update.status","{}"]
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                try { let result=JSON.parse(text); if(result.ok) root.updateState=result.result } catch(e) {}
            }
        }
    }
    Process {
        id: thumbScan
        property string device: ""
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                try { let result=JSON.parse(text); if(result.ok) { root.thumbnail="file://"+root.thumbnailPath+"?"+Date.now(); root.thumbnailDevice=thumbScan.device } } catch(e) {}
            }
        }
    }
    Process {
        id: action
        property var step: null
        property bool started: false
        function finish(ok,text) {
            root.message=text
            root.messageIsError=!ok
            // Drop this step and, after a failure, the rest of its chain.
            root.queue=root.queue.slice(1+(ok || !step ? 0 : Math.min(step.chain,root.queue.length-1)))
            root.refresh(); root.scan()
        }
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                let value=null
                try { value=JSON.parse(text) } catch(e) {}
                if(value && value.ok===true) action.finish(true,action.step ? action.step.note : "")
                else action.finish(false,value && value.error ? value.error.message : root.tr("The action could not be completed.","Aktion konnte nicht abgeschlossen werden."))
            }
        }
        onStarted: started=true
        // A program that cannot start reports neither output nor an exit.
        onRunningChanged: if(!running && !started) { finish(false,root.tr("Tomacco is not reachable. Check the installation.","Tomacco ist nicht erreichbar. Installation prüfen.")); Qt.callLater(root.next) }
        onExited: Qt.callLater(root.next)
    }
    Process {
        id: logLookup
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                let path=""
                try { let result=JSON.parse(text); if(result.ok) path=result.result.log_path || "" } catch(e) {}
                if(!path) { root.message=root.tr("The job's log could not be found.","Das Log des Auftrags wurde nicht gefunden."); root.messageIsError=true; return }
                opener.command=["xdg-open",path]
                opener.running=true
            }
        }
    }
    Process { id: opener }
    Process { id: notify }
    // One instance per session answers IPC, so keys can be bound in Hyprland:
    //   omarchy-shell io.github.marselnenaj.tomacco window
    IpcHandler {
        enabled: root.notificationOwner
        target: root.moduleName
        function status(): string { return root.summary }
        function version(): string { return root.build }
        function refresh(): string { root.refresh(); root.scan(); return "ok" }
        function show(tab: string): string { root.openStudio(tab || "runtime"); return "ok" }
        function window(): string { if(!root.canPopOut) return root.tr("No device connected","Kein Gerät verbunden"); root.popOut(null); return "ok" }
        function screenshot(): string { if(!root.liveRuntime) return root.tr("No live device","Kein Live-Gerät"); root.screenshot(); return "ok" }
        function log(): string { if(!root.latestJob) return root.tr("No jobs yet","Noch keine Aufträge"); root.openLog(root.latestJob); return "ok" }
        function iphone(command: string): string {
            if(!root.phone.configured) return root.tr("No virtual iOS device set up","Kein virtuelles iOS-Gerät eingerichtet")
            if(command==="start") { if(root.phoneRunning || root.phoneBusy) return root.tr("Already running or starting","Läuft bereits oder wird gerade gestartet"); root.startPhone() }
            else if(command==="stop") { if(!root.phoneRunning) return root.tr("Not running","Läuft nicht"); root.stopPhone() }
            else if(command==="window") { if(!root.phoneRunning) return root.tr("Not running","Läuft nicht"); root.showPhone() }
            else return root.phoneLabel
            return "ok"
        }
        function emulator(name: string): string { let avd=root.avds.find(a=>a.name===name); if(!avd) return root.tr("Unknown emulator","Unbekannter Emulator"); root.startAvd(avd); return "ok" }
        function busy(): string { return root.busyLabel+"|"+root.queue.length+"|"+root.message }
        function update(): string { return root.restartNeeded ? "restart "+root.updateState.current : root.updateAvailable ? "available "+(root.updateState.latest || "") : "current "+(root.updateState.current || "") }
    }
    // Fast while this popup is open, and on the one bar that owns notifications while real work
    // runs. Emulators, audio bridges and the virtual iPhone live for hours and are not work, and
    // the bar of every other monitor sees the same jobs, so they poll slowly.
    Timer { interval: root.opened || (root.notificationOwner && root.workJobs.length>0) ? 2000 : 6000; repeat: true; running: true; triggeredOnStart: true; onTriggered: root.refresh() }
    Timer { interval: 1000; repeat: true; running: root.opened; triggeredOnStart: true; onTriggered: root.now=Date.now()/1000 }
    // Devices are rescanned often while the popup is open and rarely otherwise.
    Timer { interval: root.opened ? 15000 : 90000; repeat: true; running: true; triggeredOnStart: true; onTriggered: root.scan() }
    // The update state changes rarely and costs no network.
    Timer { interval: 600000; repeat: true; running: true; triggeredOnStart: true; onTriggered: if(!updateScan.running) updateScan.running=true }
    Timer { interval: 4000; repeat: true; running: root.opened && !!root.liveRuntime; triggeredOnStart: true; onTriggered: root.captureThumbnail() }

    component PhoneGlyph: Item {
        id: glyph
        property color tint: root.foreground
        property bool live: false
        property bool working: false
        property bool failed: false
        Rectangle {
            id: body
            anchors.centerIn: parent; width: Math.round(parent.height*.56); height: Math.round(parent.height*.94)
            radius: width*.24; color: "transparent"; border.width: Math.max(1.4,parent.height*.085)
            border.color: glyph.failed ? root.bad : glyph.tint
            // A lit screen means a device is live in the workspace.
            Rectangle { anchors.fill: parent; anchors.margins: parent.border.width+1.2; radius: Math.max(1,parent.radius-parent.border.width-1); color: glyph.tint; opacity: glyph.live ? .9 : 0; Behavior on opacity { NumberAnimation { duration: 180 } } }
            Rectangle { anchors.horizontalCenter: parent.horizontalCenter; y: parent.height-parent.border.width-height-1.6; width: parent.width*.34; height: 1.4; radius: 1; color: glyph.live ? root.alpha(Qt.rgba(0,0,0,1),.55) : parent.border.color }
        }
        // Running jobs orbit the phone instead of replacing it.
        Shape {
            anchors.centerIn: parent; width: parent.height*1.16; height: width
            visible: glyph.working; antialiasing: true; layer.enabled: true; layer.samples: 4
            ShapePath {
                strokeColor: root.accent; strokeWidth: Math.max(1.4,glyph.height*.085); fillColor: "transparent"; capStyle: ShapePath.RoundCap
                PathAngleArc { centerX: glyph.height*.58; centerY: glyph.height*.58; radiusX: glyph.height*.54; radiusY: glyph.height*.54; startAngle: 0; sweepAngle: 100 }
            }
            RotationAnimation on rotation { from: 0; to: 360; duration: 1300; loops: Animation.Infinite; running: glyph.working }
        }
    }

    BarIconButton {
        id: button
        anchors.fill: parent
        bar: root.bar
        active: root.workJobs.length>0 || root.anyLive || root.opened
        tooltipText: "Tomacco · " + root.summary + root.tr("\nLeft: overview · Middle: device as a window · Right: workspace","\nLinks: Übersicht · Mitte: Gerät als Fenster · Rechts: Workspace")
        iconComponent: Component {
            Item {
                PhoneGlyph {
                    anchors.centerIn: parent; width: parent.width; height: Math.min(parent.height,Style.space(17))
                    tint: root.barForeground
                    live: root.anyLive; working: root.workJobs.length>0 || root.busyLabel!==""; failed: root.hasError
                }
            }
        }
        onPressed: function(mouseButton) {
            if(mouseButton===Qt.RightButton) root.openStudio("runtime")
            else if(mouseButton===Qt.MiddleButton) root.popOut(null)
            else root.toggle()
        }
    }

    component LabelText: Text {
        textFormat: Text.PlainText
        color: root.foreground; font.family: root.fontFamily; font.pixelSize: Style.font.body
        elide: Text.ElideRight
    }
    component Dot: Rectangle {
        property bool pulsing: false
        width: Style.space(7); height: width; radius: width/2
        SequentialAnimation on opacity { running: parent.pulsing; loops: Animation.Infinite; alwaysRunToEnd: true
            NumberAnimation { to: .3; duration: 650 } NumberAnimation { to: 1; duration: 650 } }
    }
    // One small icon button for row actions. Glyphs are Font Awesome
    // codepoints shipped by the bar's Nerd Font.
    component IconAction: Rectangle {
        id: iconAction
        property string glyph: ""
        property string hint: ""
        property color tint: root.foreground
        signal triggered()
        width: Style.space(28); height: width; radius: Style.space(6)
        opacity: enabled ? 1 : .35
        color: area.pressed ? Style.pressedFill : area.containsMouse ? Style.hoverFill : Style.normalFill
        Text { anchors.centerIn: parent; text: iconAction.glyph; color: iconAction.tint; font.family: root.fontFamily; font.pixelSize: Style.font.body }
        MouseArea { id: area; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: iconAction.triggered() }
        PanelToolTip { visible: area.containsMouse && iconAction.hint!==""; text: iconAction.hint; fontFamily: root.fontFamily }
    }
    component Tile: Rectangle {
        id: tile
        property string glyph: ""
        property string label: ""
        property string key: ""
        property bool highlighted: false
        signal triggered()
        width: (tiles.width-tiles.spacing*3)/4; height: Style.space(62); radius: Style.space(8)
        opacity: enabled ? 1 : .35
        color: tileArea.pressed ? Style.pressedFill : tileArea.containsMouse ? Style.hoverFill : highlighted ? Style.selectedFill : Style.normalFill
        Column {
            anchors.centerIn: parent; spacing: Style.space(5)
            Text { anchors.horizontalCenter: parent.horizontalCenter; text: tile.glyph; color: tile.highlighted ? root.accent : root.foreground; font.family: root.fontFamily; font.pixelSize: Style.font.body*1.35 }
            Text { anchors.horizontalCenter: parent.horizontalCenter; text: tile.label; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption }
        }
        Text { visible: tile.key!==""; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: Style.space(5); text: tile.key; color: root.faint; font.family: root.fontFamily; font.pixelSize: Style.font.caption*.9 }
        MouseArea { id: tileArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: tile.triggered() }
    }
    component ListRow: Rectangle {
        id: row
        property color dotColor: root.faint
        property bool pulsing: false
        property string title: ""
        property string detail: ""
        property bool clickable: false
        default property alias actions: actionRow.data
        signal triggered()
        width: parent ? parent.width : 0; height: Math.max(Style.space(44),labels.implicitHeight+Style.space(12)); radius: Style.space(8)
        color: rowArea.containsMouse && clickable ? Style.hoverFill : "transparent"
        MouseArea { id: rowArea; anchors.fill: parent; hoverEnabled: true; enabled: row.clickable; cursorShape: Qt.PointingHandCursor; onClicked: row.triggered() }
        Dot { id: dot; x: Style.space(8); anchors.verticalCenter: parent.verticalCenter; color: row.dotColor; pulsing: row.pulsing }
        Column {
            id: labels
            anchors.left: dot.right; anchors.leftMargin: Style.space(10); anchors.right: actionRow.left; anchors.rightMargin: Style.space(8); anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(1)
            LabelText { width: parent.width; text: row.title }
            LabelText { width: parent.width; visible: row.detail!==""; text: row.detail; color: root.dim; font.pixelSize: Style.font.caption }
        }
        Row { id: actionRow; anchors.right: parent.right; anchors.rightMargin: Style.space(6); anchors.verticalCenter: parent.verticalCenter; spacing: Style.space(5) }
    }
    component Section: Column {
        property string title: ""
        property string trailing: ""
        width: parent ? parent.width : 0; spacing: Style.space(4)
        Item {
            width: parent.width; height: header.implicitHeight+Style.space(4)
            PanelSectionHeader { id: header; text: parent.parent.title; foreground: root.foreground; fontFamily: root.fontFamily }
            LabelText { anchors.right: parent.right; anchors.verticalCenter: header.verticalCenter; text: parent.parent.trailing; color: root.faint; font.pixelSize: Style.font.caption }
        }
    }
    component Hint: LabelText { width: parent ? parent.width : 0; color: root.dim; wrapMode: Text.Wrap; elide: Text.ElideNone; leftPadding: Style.space(8) }

    KeyboardPanel {
        id: popup
        anchorItem: button; owner: root; bar: root.bar; open: root.opened; focusTarget: keys
        contentWidth: popup.fittedContentWidth(Style.space(430))
        contentHeight: popup.fittedContentHeight(contents.implicitHeight,Style.space(880))
        PanelKeyCatcher {
            id: keys
            anchors.fill: parent
            onCloseRequested: root.close()
            onTabRequested: function(direction) { root.switchPanel(direction) }
            onTextKey: function(key) {
                key=key.toLowerCase()
                if(key==="o") root.openStudio("runtime")
                else if(key==="f") root.popOut(null)
                else if(key==="s") root.screenshot()
                else if(key==="g") root.openStudio("devices")
                else if(key==="a") root.openStudio("jobs")
                else if(key==="v") root.openLog(root.latestJob)
                else if(key==="r") { root.refresh(); root.scan() }
                else if(key==="u") { if(root.restartNeeded || root.updateAvailable) root.openStudio("runtime") }
            }
            Flickable {
                id: flick
                anchors.fill: parent; clip: true; contentWidth: width; contentHeight: contents.implicitHeight
                Column {
                    id: contents
                    width: parent.width; spacing: Style.space(14)

                    PanelHero {
                        foreground: root.foreground; fontFamily: root.fontFamily
                        title: "Tomacco"
                        meta: root.summary
                        iconComponent: Component { PhoneGlyph { width: Style.space(30); height: Style.space(34); live: root.anyLive; working: root.workJobs.length>0 || root.busyLabel!==""; failed: root.hasError } }
                        trailingControl: Component {
                            Row {
                                spacing: Style.space(6)
                                IconAction { glyph: ""; hint: root.tr("Refresh · R","Aktualisieren · R"); onTriggered: { root.refresh(); root.scan() } }
                                IconAction { glyph: ""; hint: root.desktopOpen ? root.tr("Show workspace · O","Workspace zeigen · O") : root.tr("Open workspace · O","Workspace öffnen · O"); tint: root.accent; onTriggered: root.openStudio("runtime") }
                            }
                        }
                    }

                    Rectangle {
                        visible: root.message!==""
                        width: parent.width; height: notice.implicitHeight+Style.space(14); radius: Style.space(8)
                        color: root.alpha(root.messageIsError ? root.bad : root.good,.12)
                        LabelText { id: notice; anchors.fill: parent; anchors.margins: Style.space(7); text: root.message; wrapMode: Text.Wrap; elide: Text.ElideNone; maximumLineCount: 4; font.pixelSize: Style.font.caption; color: root.messageIsError ? root.bad : root.foreground }
                    }

                    // A new version, or one that waits for a restart of the workspace.
                    Rectangle {
                        objectName: "updateNote"
                        visible: root.updateNote!==""
                        width: parent.width; height: updateText.implicitHeight+Style.space(14); radius: Style.space(8)
                        color: updateArea.containsMouse ? Style.hoverFill : root.alpha(root.accent,.12)
                        LabelText { id: updateText; anchors.fill: parent; anchors.margins: Style.space(7); text: root.updateNote; wrapMode: Text.Wrap; elide: Text.ElideNone; maximumLineCount: 3; font.pixelSize: Style.font.caption }
                        MouseArea { id: updateArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: root.openStudio("runtime") }
                    }

                    Row {
                        id: tiles
                        width: parent.width; spacing: Style.space(7)
                        Tile { glyph: ""; label: root.tr("As window","Als Fenster"); key: "F"; enabled: root.canPopOut; highlighted: root.anyLive; onTriggered: root.popOut(null) }
                        Tile { glyph: ""; label: "Screenshot"; key: "S"; enabled: !!root.liveRuntime; onTriggered: root.screenshot() }
                        Tile { glyph: ""; label: root.tr("Devices","Geräte"); key: "G"; onTriggered: root.openStudio("devices") }
                        Tile { glyph: ""; label: root.tr("Jobs","Aufträge"); key: "A"; onTriggered: root.openStudio("jobs") }
                    }
                    Row {
                        width: parent.width; spacing: Style.space(7)
                        Repeater {
                            model: [["project","",root.tr("Project","Projekt")],["files","",root.tr("Files","Dateien")],["debug","","Debugger"],["profiling","","Profiler"],["preview","","Web"]]
                            Rectangle {
                                required property var modelData
                                width: (parent.width-parent.spacing*4)/5; height: Style.space(30); radius: Style.space(6)
                                color: chipArea.pressed ? Style.pressedFill : chipArea.containsMouse ? Style.hoverFill : "transparent"
                                Row { anchors.centerIn: parent; spacing: Style.space(5)
                                    Text { anchors.verticalCenter: parent.verticalCenter; text: modelData[1]; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption }
                                    Text { anchors.verticalCenter: parent.verticalCenter; text: modelData[2]; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption }
                                }
                                MouseArea { id: chipArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: root.openStudio(modelData[0]) }
                            }
                        }
                    }

                    // The live device with a small picture of its screen; a click opens it as a window.
                    Rectangle {
                        objectName: "liveDevice"
                        visible: !!root.liveRuntime
                        width: parent.width; height: Style.space(112); radius: Style.space(8)
                        color: liveArea.containsMouse ? Style.hoverFill : Style.normalFill
                        Item {
                            id: thumbFrame
                            x: Style.space(8); y: Style.space(8); width: Math.round(height*.5); height: parent.height-Style.space(16)
                            Image {
                                id: thumb
                                anchors.fill: parent; fillMode: Image.PreserveAspectFit; cache: false; asynchronous: true; smooth: true; mipmap: true
                                sourceSize.height: Math.round(height*2)
                                source: root.liveRuntime && root.thumbnailDevice===root.liveRuntime.device ? root.thumbnail : ""
                            }
                            PhoneGlyph { anchors.fill: parent; visible: thumb.status!==Image.Ready; live: true }
                        }
                        Column {
                            anchors.left: thumbFrame.right; anchors.leftMargin: Style.space(12); anchors.right: parent.right; anchors.rightMargin: Style.space(8); anchors.verticalCenter: parent.verticalCenter
                            spacing: Style.space(2)
                            LabelText { width: parent.width; text: root.liveRuntime ? root.deviceName(root.liveRuntime.device) : "" }
                            LabelText { width: parent.width; text: root.liveRuntime ? (root.liveRuntime.platform==="ios" ? "Apple" : "Android")+" · live" : ""; color: root.dim; font.pixelSize: Style.font.caption }
                            LabelText { width: parent.width; text: root.tr("F · as a window","F · als Fenster"); color: root.faint; font.pixelSize: Style.font.caption }
                        }
                        MouseArea { id: liveArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: root.popOut(null) }
                    }

                    PanelSeparator { foreground: root.foreground }
                    Section {
                        title: root.tr("DEVICES","GERÄTE")
                        trailing: deviceScan.running ? root.tr("searching …","sucht …") : root.devices.length ? root.devices.length+root.tr(" connected"," verbunden") : ""
                        ListRow {
                            visible: root.phone.configured===true
                            dotColor: root.phoneRunning ? root.good : root.phoneBusy ? root.accent : root.phone.phase==="failed" ? root.bad : root.faint
                            pulsing: root.phoneBusy
                            title: root.tr("Virtual iOS device","Virtuelles iOS-Gerät")
                            detail: root.tr("iOS · experimental · ","iOS · experimentell · ")+root.phoneLabel
                            IconAction { glyph: ""; hint: root.tr("Open as its own window","Als eigenes Fenster öffnen"); visible: root.phoneRunning; onTriggered: root.showPhone() }
                            IconAction { glyph: ""; hint: root.tr("Start and show as a window · booting takes many minutes","Starten und als Fenster zeigen · der Boot dauert viele Minuten"); visible: !root.phoneRunning; enabled: !root.phoneBusy; tint: root.accent; onTriggered: root.startPhone() }
                            IconAction { glyph: ""; hint: root.phoneStarting ? root.tr("Cancel the start and end without saving","Start abbrechen und ohne Sichern beenden") : root.tr("Save the storage and shut down","Speicher sichern und herunterfahren"); visible: root.phoneRunning || root.phoneStarting; enabled: !root.phoneStopping; tint: root.bad; onTriggered: root.stopPhone() }
                        }
                        Hint { visible: root.devices.length===0 && root.idleAvds.length===0 && root.phone.configured!==true; text: deviceScan.running ? root.tr("Searching for devices …","Geräte werden gesucht …") : root.tr("No device connected. Plug in a smartphone or set up an emulator in the workspace.","Kein Gerät verbunden. Schließe ein Smartphone an oder richte im Workspace einen Emulator ein.") }
                        Repeater {
                            model: root.devices
                            ListRow {
                                required property var modelData
                                readonly property var live: root.runtimeFor(modelData.id)
                                readonly property bool ready: root.isReady(modelData)
                                dotColor: live ? root.good : ready ? root.accent : root.faint
                                // The nickname from device.label, when the device has one; a pin marks a favourite.
                                title: (modelData.pinned ? "\u2605 " : "")+modelData.name
                                detail: (modelData.platform==="ios" ? "Apple" : "Android")+" · "+(modelData.kind==="emulator" ? "Emulator" : root.tr("Device","Gerät"))+" · "+(live ? root.tr("live in the workspace","live im Workspace") : ready ? (modelData.booted===false ? root.tr("starting …","startet …") : root.tr("ready","bereit")) : modelData.state)
                                IconAction { glyph: ""; hint: root.tr("Open as its own window","Als eigenes Fenster öffnen"); enabled: ready; onTriggered: root.popOut(modelData) }
                                IconAction { glyph: live ? "" : ""; hint: live ? root.tr("Disconnect live","Live-Verbindung trennen") : root.tr("Connect live","Live verbinden"); enabled: ready; tint: live ? root.good : root.foreground; onTriggered: live ? root.disconnectDevice(modelData) : root.connectDevice(modelData) }
                                IconAction { glyph: ""; hint: root.tr("Stop emulator","Emulator beenden"); visible: modelData.kind==="emulator"; tint: root.bad; onTriggered: root.stopEmulator(modelData) }
                            }
                        }
                        Repeater {
                            model: root.idleAvds
                            ListRow {
                                required property var modelData
                                title: (modelData.pinned ? "\u2605 " : "")+(modelData.nickname || modelData.name)
                                detail: (modelData.profile_name || root.tr("Android emulator","Android-Emulator"))+root.tr(" · off"," · aus")
                                IconAction { glyph: ""; hint: root.tr("Start and show as a device window","Starten und als Gerätefenster zeigen"); tint: root.accent; onTriggered: root.startAvd(modelData) }
                            }
                        }
                        Hint { visible: root.diagnostics.length>0 && root.devices.length===0 && root.idleAvds.length===0; font.pixelSize: Style.font.caption; color: root.faint; text: root.diagnostics.map(d=>(d.platform==="ios"?"Apple":"Android")+": "+d.message).join("\n") }
                    }

                    PanelSeparator { foreground: root.foreground }
                    Section {
                        title: root.tr("JOBS","AUFTRÄGE")
                        trailing: root.workJobs.length ? root.workJobs.length+root.tr(" active"," aktiv") : root.latestJob ? root.tr("V · newest log","V · neuestes Log") : ""
                        Hint { visible: root.jobs.length===0; text: root.tr("No jobs yet. Builds from the CLI, MCP and the workspace appear here.","Noch keine Aufträge. Builds aus CLI, MCP und Workspace erscheinen hier.") }
                        Repeater {
                            model: root.liveJobs
                            ListRow {
                                required property var modelData
                                readonly property bool service: root.isService(modelData)
                                dotColor: service ? root.good : root.accent; pulsing: !service
                                title: modelData.label
                                readonly property string phase: service ? "" : root.phaseLabel(modelData)
                                detail: (service ? root.tr("Service running","Dienst läuft") : root.stateLabel(modelData.state))+(phase ? " · "+phase : "")+(modelData.created ? root.tr(" · for "," · seit ")+root.span(root.now-modelData.created) : "")
                                clickable: true; onTriggered: root.openStudio("jobs")
                                IconAction { glyph: ""; hint: root.tr("Open log","Log öffnen"); onTriggered: root.openLog(modelData) }
                                IconAction { glyph: ""; hint: service ? root.tr("Stop service","Dienst beenden") : root.tr("Cancel job","Auftrag abbrechen"); tint: root.bad; onTriggered: root.run(root.tr("Cancelling job","Auftrag wird abgebrochen"),[["job.cancel",{job_id:modelData.id}]]) }
                            }
                        }
                        Repeater {
                            model: root.recentJobs
                            ListRow {
                                required property var modelData
                                dotColor: root.stateColor(modelData.state)
                                title: modelData.label
                                detail: root.stateLabel(modelData.state)+root.tr(" · "," · vor ")+root.span(root.now-modelData.updated)+root.tr(" ago","")+(modelData.created && modelData.updated>modelData.created ? root.tr(" · took "," · Dauer ")+root.span(modelData.updated-modelData.created) : "")
                                clickable: true; onTriggered: root.openStudio("jobs")
                                IconAction { glyph: ""; hint: root.tr("Open log","Log öffnen"); onTriggered: root.openLog(modelData) }
                            }
                        }
                    }

                    PanelSeparator { foreground: root.foreground; visible: root.workspaceNames.length>0 }
                    Section {
                        visible: root.workspaceNames.length>0
                        title: root.tr("PROJECTS","PROJEKTE")
                        Repeater {
                            model: root.workspaceNames
                            ListRow {
                                required property string modelData
                                readonly property var entry: root.snapshot.workspaces[modelData]
                                dotColor: root.devices.some(d=>d.id===entry.device) ? root.good : root.faint
                                title: modelData
                                detail: entry.path.replace(root.home,"~")+(entry.device ? " · "+root.deviceName(entry.device) : "")
                                clickable: true; onTriggered: root.openStudio("files",entry.path)
                                IconAction { glyph: ""; hint: root.tr("Build and start on ","Bauen und auf ")+root.deviceName(entry.device)+root.tr(""," starten"); visible: !!entry.device; enabled: root.devices.some(d=>d.id===entry.device); tint: root.accent; onTriggered: root.runWorkspace(modelData) }
                                IconAction { glyph: ""; hint: root.tr("Open in the workspace","Im Workspace öffnen"); onTriggered: root.openStudio("files",entry.path) }
                            }
                        }
                    }
                    Item { width: 1; height: Style.space(2) }
                }
            }
        }
    }
}
