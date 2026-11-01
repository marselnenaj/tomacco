import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import QtCore
import "."
import "AppFiles.js" as AppFiles

Rectangle {
    id: view
    objectName: "iosResearch"
    required property var workspace
    implicitHeight: summary.implicitHeight + 32
    radius: Theme.radius; color: Theme.surface; border.color: Theme.subtle
    property string enginePath: ""
    property string bundlePath: ""
    property string pmpBundlePath: ""
    property string mode: "ios"
    property string pmpController: "asc-v6"
    property string cpuMidr: "0x610f0000"
    property string cpuAffinity: "0x0"
    property bool showCpuConfig: false
    property int systemSeconds: 360
    property int pmpSeconds: 15
    property string stopAddress: ""
    property bool captureRam: false
    property string jobId: ""
    property string jobState: ""
    property real jobCreated: 0
    property string reportDirectory: ""
    property var report: ({})
    property var requests: ({})
    property bool starting: false
    property bool cancelling: false
    property bool stopWaitingForPreview: false
    // The guest could not be asked to save before it ends: Stop then reads
    // "Stop without saving" and cancels the job, so a hung guest can always be ended.
    property bool forceStop: false
    property bool polling: false
    property string notice: ""
    property bool failed: false
    property bool showFrame: false
    property bool showRegisters: false
    property string memoryAddress: "0x1000000"
    property int memoryLength: 256
    property bool readingMemory: false
    property var memoryResult: ({})
    property string memoryError: ""
    readonly property bool paravirtualMode: mode==="paravirtual"
    property bool interactiveGuest: false
    readonly property bool pmpMode: mode==="pmp"
    readonly property string selectedBundle: pmpMode ? pmpBundlePath : bundlePath
    readonly property bool opened: dialog.visible
    readonly property bool active: starting || cancelling || jobState==="queued" || jobState==="running"
    readonly property bool canStart: !paravirtualMode && !active && enginePath.trim().length>0 && selectedBundle.trim().length>0
    readonly property string stateLabel: starting ? qsTr("Checking image…") : cancelling ? qsTr("Stopping…") :
        jobState==="succeeded" && report.breakpoint_hit ? qsTr("Breakpoint reached") :
        ({queued:qsTr("Waiting to start"),running:report.state==="preparing"?qsTr("Preparing working copy"):pmpMode?qsTr("Firmware running"):qsTr("iOS diagnostics running"),succeeded:qsTr("Diagnostics complete"),failed:qsTr("Diagnostics ended with a finding"),cancelled:qsTr("Run ended"),timed_out:qsTr("Time limit reached")})[jobState] || qsTr("Ready for a test run")
    readonly property string framePath: report.frame_capture && report.frame_capture.path || ""
    readonly property var lastObservation: report.observations && report.observations.length ? report.observations[report.observations.length-1] : ({})
    readonly property var controllerState: lastObservation.controller_state || null
    readonly property var firstFault: report.bus_faults && report.bus_faults.events && report.bus_faults.events.length ? report.bus_faults.events[0] : null
    readonly property var firstSysregFault: report.sysreg_faults && report.sysreg_faults.events && report.sysreg_faults.events.length ? report.sysreg_faults.events[0] : null
    Settings {
        category: "IosResearch"
        property alias engine: view.enginePath
        property alias bundle: view.bundlePath
        property alias pmpBundle: view.pmpBundlePath
        property alias researchMode: view.mode
        property alias controller: view.pmpController
        property alias pmpCpuMidr: view.cpuMidr
        property alias pmpCpuAffinity: view.cpuAffinity
        property alias systemDuration: view.systemSeconds
        property alias pmpDuration: view.pmpSeconds
        property alias breakpoint: view.stopAddress
        property alias saveRam: view.captureRam
        property alias lastJob: view.jobId
    }
    // One-step virtual iPhone. The profile, its disks and the long boot belong
    // to the control plane; this view only shows the shared state.
    property var virtualPhone: ({configured:false})
    property string virtualNotice: ""
    property string virtualStatusError: ""
    readonly property bool virtualBusy: !!virtualPhone.busy
    // The profile's own apps (ios.virtual.configure add_application, remove_application,
    // application): the path typed or picked for the next add, whether such a change
    // is in flight, and the refusal the control plane gave the last one.
    property string virtualAppPath: ""
    property bool virtualAppBusy: false
    property string virtualAppNotice: ""
    readonly property var virtualApplications: virtualPhone.applications || []
    // Saved phone states: the list from ios.virtual.states, the one chosen, the
    // name typed for the next save, whether a change is in flight, its outcome,
    // and whether Delete has been pressed once (it asks a second time).
    property var virtualStates: []
    property int virtualStateIndex: 0
    property string virtualStateName: ""
    property bool virtualStatePending: false
    property bool virtualStatesLoading: false
    property string virtualStateNotice: ""
    property bool virtualStateFailed: false
    property bool virtualStateDeleteArmed: false
    readonly property bool virtualStateIdle: !virtualStatePending && !virtualBusy
    readonly property var selectedState: virtualStates.length?virtualStates[Math.min(virtualStateIndex,virtualStates.length-1)]:null
    function stateText(state) {
        let parts=[state.name,qsTr("run %1").arg(state.run)]
        if(state.base)parts.push(qsTr("next start"))
        if(!state.exists)parts.push(qsTr("disks gone"))
        return parts.join(" · ")
    }
    function stateCall(operation, args) {
        if(virtualStatePending)return null
        virtualStatePending=true;virtualStateNotice="";virtualStateFailed=false
        return call(operation,args)
    }
    function saveState() {
        let name=virtualStateName.trim()
        if(!name || !virtualStateIdle)return null
        return stateCall("ios.virtual.state_save",{name:name})
    }
    // The app the start opens: the owned diagnostic, or one of the listed bundles.
    readonly property string researchApplication: "io.github.marselnenaj.tomacco.research-app"
    readonly property var virtualOpenChoices: [{id:researchApplication,name:qsTr("Built-in test app (diagnostics)")}].concat(virtualApplications.filter(app=>!!app.bundle_id).map(app=>({id:app.bundle_id,name:view.applicationName(app)})))
    readonly property int virtualOpenIndex: Math.max(0,virtualOpenChoices.findIndex(choice=>choice.id===(virtualPhone.application || "")))
    // The device the phone is described as: the profile's own, with a Home
    // button, or one of the built-in iPhone models.
    readonly property var virtualModels: [{id:"",name:qsTr("Classic (Home button)"),source:""}].concat(virtualPhone.models || [])
    readonly property int virtualModelIndex: Math.max(0,virtualModels.findIndex(m=>m.id===(virtualPhone.model || "")))
    readonly property bool virtualModelPending: virtualRunning && (virtualPhone.model || "")!==(virtualPhone.running_model || "")
    readonly property bool virtualStarting: virtualBusy && virtualPhone.busy.action==="ios.virtual.start"
    readonly property bool virtualStopping: virtualBusy && virtualPhone.busy.action==="ios.virtual.stop"
    readonly property bool virtualRunning: virtualPhone.guest_running===true
    readonly property string virtualLabel: {
        let phone=virtualPhone
        if(!phone.configured)return ""
        if(virtualStatusError)return qsTr("Status currently unknown")
        if(phone.busy && phone.busy.action==="ios.virtual.stop")return qsTr("stopping…")
        let starting=({preparing:qsTr("preparing…"),booting:qsTr("iOS is starting… this takes many minutes"),unlocking:qsTr("trying to unlock…")})[phone.phase]
        if(phone.busy && starting)return starting
        if(virtualRunning && phone.phase==="awaiting-unlock")return qsTr("locked · unlock on the device")
        if(virtualRunning && phone.phase==="launch-uncertain")return qsTr("running · app launch unclear")
        if(virtualRunning && phone.phase==="launch-accepted")return phone.application_scene_verified===true?qsTr("running · app visible"):qsTr("running · app launch accepted")
        if(virtualRunning)return phone.phase==="ready" || !starting ? qsTr("running") : starting
        // The guest ended by itself, at its session limit or in a crash.
        if(phone.guest_ended && phone.guest_gpu_memory_stop)return phone.guest_saved?qsTr("off · stopped and saved because the graphics card ran out of memory"):qsTr("off · stopped because the graphics card ran out of memory, the next start uses the last saved state")
        if(phone.guest_ended)return phone.guest_saved?qsTr("off · session was saved and ended at its time limit"):qsTr("off · session ended without saving, the next start uses the last saved state")
        return phone.phase==="failed" ? qsTr("Start failed") : phone.phase==="unverified" ? qsTr("off · last run not verified") : qsTr("off")
    }
    // Polled while the Apple page is shown, while the guest has its own window, and while a
    // detach request waits for the guest of a configured virtual iPhone (without such a
    // phone the status says nothing about when a guest appears).
    Timer {objectName:"virtualStatusPoll";interval:3000;repeat:true;running:view.visible || nativeGuestView.detached || (view.wantDetached && view.virtualPhone.configured===true);triggeredOnStart:true;onTriggered:view.call("ios.virtual.status",{})}
    property bool guestSolo: false
    readonly property bool guestWindowVisible: nativeGuestView.windowVisible
    signal guestCloseRequested()
    signal guestDockRequested()
    // Agent access to the guest display: detach it as a device window or dock
    // it. A request made while the guest is still starting is kept and applied
    // as soon as its display exists, but not for ever: a guest that appears
    // hours later must not open a window nobody is waiting for any more.
    property bool wantDetached: false
    property int detachTimeoutMs: 600000
    Timer {id:detachExpiry;objectName:"guestDetachExpiry";interval:view.detachTimeoutMs;running:view.wantDetached && !nativeGuestView.detached;onTriggered:view.wantDetached=false}
    function configureGuest(args) {
        if(args.detached===undefined)return
        wantDetached=args.detached===true
        if(!wantDetached)nativeGuestView.detached=false
        else if(nativeGuestView.running)nativeGuestView.detached=true
        else {detachExpiry.restart();call("ios.virtual.status",{})}
    }
    function guestState() {return {running:nativeGuestView.running,detached:nativeGuestView.detached,detach_pending:wantDetached && !nativeGuestView.detached,window_visible:nativeGuestView.windowVisible,frame_width:nativeGuestView.frameWidth,frame_height:nativeGuestView.frameHeight,frame_sha256:nativeGuestView.frameHash,captured_at:nativeGuestView.capturedAt,frames_per_second:nativeGuestView.framesPerSecond,frame_transport:nativeGuestView.fileFrames?"file":"inline",queued_inputs:nativeGuestView.inputQueue.length}}
    function call(operation, args) {
        let id=Agent.request(operation,args)
        requests[id]={operation:operation,jobId:args.job_id || "",arguments:args}
        return id
    }
    function applicationName(app) {return app.display_name || app.name || (app.bundle || "").split("/").filter(part=>part).pop() || "App"}
    // What the status knows about a listed app: whether the control plane inspected
    // it when it was added (a row written by hand names only the path), whether this
    // run's volume has it installed, and whether the start opens it.
    function applicationState(app) {
        if(!app.bundle_id)return qsTr("unverified · only the path is listed, the next start checks the bundle")
        let installed=virtualPhone.installed_applications || ({})
        let parts=[qsTr("verified"),installed[app.bundle_id]?qsTr("installed in the current run"):qsTr("not installed in the current run")]
        if(app.bundle_id===virtualPhone.application)parts.push(qsTr("opened at start"))
        return parts.join(" · ")
    }
    // The start's time-zone step, as the state records it; nothing before the first start that had it.
    function timeZoneText() {
        let zone=virtualPhone.time_zone
        if(!zone || !zone.outcome)return ""
        if(zone.outcome==="applied")return (zone.changed===false?qsTr("Time zone %1 was already set"):qsTr("Time zone %1 applied")).arg(zone.zone)+(zone.link?" · "+zone.link:"")
        if(zone.outcome==="skipped")return qsTr("Time zone not set · %1").arg(({unsupported:qsTr("the device's helper tools are too old for this (before build 368)"),host_zone_unknown:qsTr("this computer's time zone has no standard name such as Europe/Vienna")})[zone.reason] || zone.reason || qsTr("skipped"))
        return qsTr("Time zone %1 not applied · %2").arg(zone.zone || "").arg(zone.outcome)
    }
    function configureApplications(args) {
        if(virtualAppBusy)return null
        virtualAppBusy=true;virtualAppNotice=""
        let id=call("ios.virtual.configure",args)
        requests[id].application=true
        return id
    }
    // An .app dropped on the device's picture: added to the profile, and onto the phone at once when it runs.
    function dropApplication(path) {
        if(virtualAppBusy){nativeGuestView.appNotice=qsTr("Another app change is still running.");return}
        let args={add_application:path};if(virtualRunning)args.deploy=true
        requests[configureApplications(args)].dropped=AppFiles.name(path)
        nativeGuestView.appNotice=qsTr("Checking %1…").arg(AppFiles.name(path))
    }
    function addApplication(path) {
        path=(path || "").trim()
        if(path)configureApplications({add_application:path})
    }
    function fileUrl(path) {return "file://"+path.split("/").map(encodeURIComponent).join("/")}
    function setBundle(path) {if(pmpMode)pmpBundlePath=path;else bundlePath=path}
    function hex(value) {return typeof value==="number" ? "0x"+value.toString(16) : value || "—"}
    function resetCapture() {memoryResult=({});memoryError="";showRegisters=false}
    function readMemory() {
        if(readingMemory || active || !report.memory_capture)return
        readingMemory=true;memoryError="";memoryResult=({})
        call("ios.emulator.memory",{job_id:jobId,address:memoryAddress.trim(),length:memoryLength})
    }
    function memoryText() {
        let data=memoryResult.data
        if(!data)return ""
        let base=parseInt(memoryResult.address,16), lines=[]
        for(let offset=0;offset<data.length/2;offset+=16) {
            let bytes=data.slice(offset*2,(offset+16)*2).match(/.{2}/g).join(" ")
            lines.push((base+offset).toString(16).padStart(8,"0")+"  "+bytes.padEnd(47," ")+"  "+memoryResult.ascii.slice(offset,offset+16))
        }
        return lines.join("\n")
    }
    function isResearchJob(job) {
        let key=job && job.plan && job.plan.operation_key
        return key && (key.operation==="ios.emulator.research" || key.operation==="ios.emulator.pmp" || key.operation==="ios.emulator.paravirtual")
    }
    function adoptJob(job) {
        if(!isResearchJob(job))return false
        let key=job.plan.operation_key, args=key.arguments
        if(jobId!==job.id) {
            stopWaitingForPreview=false;forceStop=false
            report=({});reportDirectory="";showFrame=false;resetCapture()
            Qt.callLater(()=>researchScroll.contentItem.contentY=0)
        }
        jobId=job.id;jobState=job.state;jobCreated=job.created || 0
        mode=key.operation==="ios.emulator.paravirtual" ? "paravirtual" : key.operation==="ios.emulator.pmp" ? "pmp" : "ios"
        interactiveGuest=!!args.interactive
        enginePath=args.engine
        if(!paravirtualMode)setBundle(args.firmware)
        if(pmpMode) {pmpController=args.controller || "none";pmpSeconds=args.seconds || 15;stopAddress=args.stop_at || "";captureRam=!!args.capture_ram;cpuMidr=args.cpu_midr || "0x610f0000";cpuAffinity=args.cpu_affinity || "0x0"}
        else systemSeconds=args.seconds || 360
        return true
    }
    function refresh() {
        if(!jobId || polling)return
        polling=true
        call("ios.emulator.report",{job_id:jobId})
    }
    function applyReport(value) {
        jobState=value.job_state || ""
        report=value.report || ({})
        reportDirectory=value.report_directory || ""
        if(jobState!=="queued" && jobState!=="running") {cancelling=false;stopWaitingForPreview=false;forceStop=false}
        if(report.error && jobState!=="cancelled") {
            failed=true;notice=report.error.message
        }
    }
    function start() {
        if(!canStart)return
        starting=true;failed=false;notice=""
        let args={engine:enginePath.trim(),firmware:selectedBundle.trim(),seconds:pmpMode?pmpSeconds:systemSeconds}
        if(pmpMode) {args.controller=pmpController;args.stop_at=stopAddress.trim();args.capture_ram=captureRam;args.cpu_midr=cpuMidr.trim();args.cpu_affinity=cpuAffinity.trim()}
        call(pmpMode?"ios.emulator.pmp":"ios.emulator.research",args)
    }
    function stop() {
        if(!jobId || cancelling)return
        cancelling=true
        // Asking the guest to save first is the safe order. Once it has refused,
        // the button ends the job outright, on the user's second click.
        if(paravirtualMode && interactiveGuest && !forceStop) {
            stopWaitingForPreview=true
            stopAfterPreview()
        }
        else call("job.cancel",{job_id:jobId})
    }
    function stopAfterPreview() {
        if(!stopWaitingForPreview || nativeGuestView.pending)return
        stopWaitingForPreview=false
        call("ios.emulator.control",{job_id:jobId,action:"stop"})
    }
    // The control plane refuses to stop a guest it cannot ask to save. One that
    // is still starting has nothing to save, so it is cancelled at once. For a
    // guest that is busy, silent or unreachable the dialog stays usable: the
    // reason is shown and the button turns into "Stop without saving".
    function stopRefused(error) {
        if(error.code==="ios_control_starting" || error.code==="ios_guest_starting") {
            failed=false;notice=qsTr("The device is still starting, there is nothing to save. Ending the run…")
            call("job.cancel",{job_id:jobId})
            return
        }
        cancelling=false;forceStop=true;failed=true
        notice=qsTr("The device could not save before stopping (%1). “Stop without saving” ends the run at once; the next start uses the last saved state.").arg((error.message || qsTr("no answer"))+(error.remedy?" · "+error.remedy:""))
    }
    function open() { dialog.open();refresh() }
    Component.onCompleted: refresh()
    Connections {
        target: workspace
        function onJobsChanged() {
            if(view.starting)return
            let selected=workspace.jobs.find(x=>x.id===view.jobId)
            if(selected && !view.jobCreated)view.adoptJob(selected)
            let job=null
            for(let candidate of workspace.jobs) {
                if(view.isResearchJob(candidate) && (!job || candidate.created>job.created))job=candidate
            }
            if(!job || job.id===view.jobId || (view.jobId && job.created<=view.jobCreated))return
            view.adoptJob(job);view.cancelling=false;view.notice="";view.failed=false
            view.refresh()
        }
    }
    Connections {
        target: Agent
        function onApiFinished(id,operation,response) {
            let context=view.requests[id]
            if(!context)return
            delete view.requests[id]
            // An app change the control plane refused is shown next to the app list, with the
            // reason and remedy; an accepted one comes back as the new status.
            if(operation==="ios.virtual.configure" && context.application) {
                view.virtualAppBusy=false
                if(context.dropped)nativeGuestView.appNotice=!response.ok?response.error.message
                    :context.arguments.deploy?qsTr("%1 added · installing on the running device").arg(context.dropped)
                    :qsTr("%1 added · the next start installs it").arg(context.dropped)
                if(!response.ok) {view.virtualAppNotice=response.error.message+(response.error.remedy?" · "+response.error.remedy:"");return}
                view.virtualAppNotice=""
                if(context.arguments.add_application)view.virtualAppPath=""
            }
            if(operation==="ios.virtual.status" || (operation==="ios.virtual.configure" && response.ok)) {
                if(!response.ok) {
                    view.virtualStatusError=(response.error || {}).message || qsTr("Device status unreachable")
                    return
                }
                view.virtualStatusError=""
                view.virtualPhone=response.result
                // The saved states follow the status, one request at a time.
                if(response.result.configured===true && dialog.visible && !view.virtualStatesLoading){view.virtualStatesLoading=true;view.call("ios.virtual.states",{})}
                // The device list may be closed: follow the virtual iPhone's guest job directly.
                let guest=response.result.guest_job
                if(response.result.guest_running && guest && guest!==view.jobId && !view.starting)view.call("job.get",{job_id:guest})
                return
            }
            if(operation==="job.get") {
                if(response.ok && context.jobId===view.virtualPhone.guest_job && response.result.id!==view.jobId && view.adoptJob(response.result)) {
                    view.cancelling=false;view.notice="";view.failed=false;view.refresh()
                }
                return
            }
            if(operation==="ios.virtual.states") {
                view.virtualStatesLoading=false
                if(response.ok)view.virtualStates=response.result.states || []
                return
            }
            if(["ios.virtual.state_save","ios.virtual.state_use","ios.virtual.state_delete"].indexOf(operation)>=0) {
                view.virtualStatePending=false
                view.virtualStateFailed=!response.ok
                if(!response.ok)view.virtualStateNotice=response.error.message+(response.error.remedy?" · "+response.error.remedy:"")
                else if(operation==="ios.virtual.state_save"){view.virtualStateNotice=qsTr("Saved as %1").arg(response.result.saved.name);view.virtualStateName=""}
                else if(operation==="ios.virtual.state_use")view.virtualStateNotice=qsTr("The next start begins from %1").arg(response.result.used)
                else view.virtualStateNotice=response.result.removed?qsTr("%1 deleted with its disk copy").arg(response.result.deleted):qsTr("%1 deleted · its disk copy stays (%2)").arg(response.result.deleted).arg(response.result.kept_because)
                view.call("ios.virtual.states",{});view.call("ios.virtual.status",{})
                return
            }
            if(operation==="ios.virtual.start" || operation==="ios.virtual.stop") {
                view.virtualNotice=response.ok?"":response.error.message
                view.call("ios.virtual.status",{});return
            }
            if(operation==="ios.emulator.memory") {
                view.readingMemory=false
                if(context.jobId!==view.jobId)return
                if(response.ok)view.memoryResult=response.result
                else view.memoryError=response.error.message
                return
            }
            if(operation==="ios.emulator.report")view.polling=false
            if(operation==="ios.emulator.report" && context.jobId!==view.jobId) {view.refresh();return}
            if(operation==="ios.emulator.control" && context.jobId!==view.jobId)return
            let startOperation=operation==="ios.emulator.research" || operation==="ios.emulator.pmp"
            if(startOperation)view.starting=false
            if(!response.ok) {
                if(operation==="ios.emulator.control") {view.stopRefused(response.error);return}
                view.failed=true;view.notice=response.error.message+(response.error.remedy?" · "+response.error.remedy:"")
                if(operation==="job.cancel")view.cancelling=false
                if(operation==="ios.emulator.report")view.jobState="unavailable"
                let active=response.error.details && response.error.details.job
                if(response.error.code==="ios_emulator_busy" && active) {
                    if(view.adoptJob(active))view.refresh()
                }
                return
            }
            let value=response.result
            if(startOperation) {
                view.jobId=value.id;view.jobState=value.state;view.forceStop=false;view.report=({});view.reportDirectory=value.report_directory;view.showFrame=false
                view.resetCapture()
                view.jobCreated=value.created
                view.notice=value.reused?qsTr("The existing run is reused."):""
                view.refresh()
            } else if(operation==="ios.emulator.report")view.applyReport(value)
            else if(operation==="job.cancel")view.refresh()
            else if(operation==="ios.emulator.control") {view.notice=qsTr("Closing the session…");view.refresh()}
        }
    }
    Timer {interval:1800;repeat:true;running:view.active && !!view.jobId;onTriggered:view.refresh()}
    ColumnLayout {
        id: summary; anchors {left:parent.left;right:parent.right;top:parent.top;margins:16} spacing:8
        RowLayout {
            Layout.fillWidth:true
            StudioIcon {name:"phone";color:Theme.muted}
            Label {text:qsTr("iOS on Linux");color:Theme.text;font.pixelSize:Theme.fontLead;font.weight:Font.DemiBold;Layout.fillWidth:true}
            StudioButton {objectName:"iosResearchOpen";text:view.active?qsTr("View run"):qsTr("Open test system");onClicked:view.open()}
        }
        Label {Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;text:qsTr("Real iOS directly on Linux. The virtual iOS device shows the lock and Home screens and starts its own test app; general app development is not enabled yet.")}
        Label {visible:!!view.jobId;Layout.fillWidth:true;text:view.stateLabel;color:view.active?Theme.accentInk:Theme.muted;font.pixelSize:Theme.fontSmall}
        Rectangle {visible:view.virtualPhone.configured===true;Layout.fillWidth:true;implicitHeight:1;color:Theme.border}
        RowLayout {
            objectName:"virtualPhoneRow"
            visible:view.virtualPhone.configured===true;Layout.fillWidth:true;spacing:8
            ColumnLayout {
                Layout.fillWidth:true;spacing:2
                Label {text:qsTr("Virtual iOS device · experimental");color:Theme.text;font.weight:Font.DemiBold}
                Label {objectName:"virtualPhoneState";Layout.fillWidth:true;wrapMode:Text.Wrap;font.pixelSize:Theme.fontSmall
                    color:view.virtualRunning || view.virtualBusy?Theme.accentInk:view.virtualPhone.phase==="failed"?Theme.danger:Theme.muted
                    text:qsTr("Run %1 · %2").arg(view.virtualPhone.run).arg(view.virtualLabel)}
            }
            BusyIndicator {running:view.virtualBusy;visible:running;Layout.preferredWidth:24;Layout.preferredHeight:24}
            StudioButton {objectName:"virtualPhoneStart";text:qsTr("Start");iconName:"play";accent:true;visible:!view.virtualRunning;enabled:!view.virtualBusy && !view.virtualStatusError;tip:qsTr("Prepare, boot, unlock and open the test app");onClicked:view.call("ios.virtual.start",{})}
            StudioButton {objectName:"virtualPhoneShow";text:qsTr("Show");visible:view.virtualRunning;onClicked:view.open()}
            StudioButton {objectName:"virtualPhoneWindow";text:qsTr("Own window");iconName:"phone";visible:view.virtualRunning;enabled:view.paravirtualMode && view.interactiveGuest;onClicked:nativeGuestView.detached=true}
            // A start can be stopped as well: it is cancelled and its guest, which
            // cannot save anything before iOS is up, is ended.
            StudioButton {objectName:"virtualPhoneStop";text:view.virtualStarting?qsTr("Cancel"):qsTr("Stop");iconName:"stop";visible:view.virtualRunning || view.virtualStarting;enabled:!view.virtualStopping;tip:view.virtualStarting?qsTr("Cancel the start and stop the device without saving"):qsTr("Save storage and shut down cleanly");onClicked:view.call("ios.virtual.stop",{})}
        }
        RowLayout {
            objectName:"virtualPhoneModelRow"
            visible:view.virtualPhone.configured===true;Layout.fillWidth:true;spacing:8
            Label {text:qsTr("Model");color:Theme.muted;font.pixelSize:Theme.fontSmall}
            StudioComboBox {
                objectName:"virtualPhoneModel"
                Layout.preferredWidth:260
                model:view.virtualModels.map(m=>m.source==="derived"?qsTr("%1 · derived").arg(m.name):m.source==="apple"?qsTr("%1 · Apple variant").arg(m.name):m.name)
                currentIndex:view.virtualModelIndex
                enabled:!view.virtualBusy
                onActivated:index=>view.call("ios.virtual.configure",{model:view.virtualModels[index].id})
            }
            Label {
                Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;font.pixelSize:Theme.fontSmall
                text:view.virtualModelPending?qsTr("Applies from the next start."):qsTr("iOS draws itself for this device; the data stays the same.")
            }
        }
        Label {objectName:"virtualPhoneError";visible:!!view.virtualStatusError || view.virtualPhone.configured===true && (!!view.virtualNotice || !!view.virtualPhone.error && !view.virtualBusy);Layout.fillWidth:true;wrapMode:Text.Wrap;font.pixelSize:Theme.fontSmall;color:Theme.danger;text:view.virtualStatusError || view.virtualNotice || view.virtualPhone.error || ""}
    }
    StudioDialog {
        id:dialog;objectName:"iosResearchDialog";parent:Overlay.overlay;anchors.centerIn:parent
        width:Math.min(760,parent.width-48);height:Math.min(view.paravirtualMode?900:760,parent.height-48)
        title:qsTr("iOS test system");modal:true;standardButtons:Dialog.Close
        contentItem:StudioScrollView {
            id:researchScroll
            objectName:"iosResearchScroll"
            clip:true;contentWidth:availableWidth
            ColumnLayout {
                width:parent.width;spacing:14
                Label {Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;text:view.paravirtualMode?qsTr("Virtual iOS device started by your agent · its real screen appears here and you can use it."):qsTr("Run iOS, or the iPhone's power-management firmware on its own, directly on Linux. Each run writes its own report; runs started from the command line or by an agent appear here too.")}
                IosGuestView {
                    id:nativeGuestView
                    Layout.fillWidth:true
                    visible:view.paravirtualMode
                    enabled:!view.cancelling
                    jobId:view.jobId
                    running:view.active && view.interactiveGuest
                    // The docked picture takes the dialog's height but for its
                    // own status row; the settings below it scroll.
                    dockHeight:researchScroll.height-120
                    // Only the phone's own guest is known to be a particular model.
                    deviceProfile:view.jobId && view.jobId===view.virtualPhone.guest_job && view.virtualPhone.running_model
                        ?(Studio.models.find(m=>m.id===view.virtualPhone.running_model && m.platform==="ios") || ({})):({})
                    observing:dialog.visible && view.paravirtualMode && !view.cancelling
                    onPendingChanged:if(!pending)view.stopAfterPreview()
                    solo:view.guestSolo
                    appKinds:view.virtualPhone.configured===true?["app"]:[]
                    appRefusal:view.virtualPhone.configured===true?qsTr("The virtual iOS device takes one .app bundle built for arm64 iOS."):qsTr("Set up the virtual iOS device's profile before adding apps.")
                    onAppDropped:path=>view.dropApplication(path)
                    onRunningChanged:if(running && view.wantDetached)detached=true
                    onDetachedChanged:if(!detached)view.wantDetached=false
                    onCloseRequested:view.guestCloseRequested()
                    onDockRequested:view.guestDockRequested()
                }
                RowLayout {Layout.fillWidth:true;enabled:!view.active
                    Label {text:qsTr("System");color:Theme.text;font.weight:Font.DemiBold}
                    StudioComboBox {objectName:"iosResearchMode";Layout.fillWidth:true;model:[qsTr("iOS"), qsTr("Power-management firmware (PMP)"), qsTr("Virtual iOS device · started by an agent")];currentIndex:view.paravirtualMode?2:view.pmpMode?1:0;onActivated:view.mode=["ios","pmp","paravirtual"][currentIndex];Accessible.name:qsTr("What to run")}
                }
                ColumnLayout {
                    visible:!view.paravirtualMode;Layout.fillWidth:true;spacing:14
                RowLayout {visible:view.pmpMode;Layout.fillWidth:true;enabled:!view.active
                    Label {text:qsTr("Controller");color:Theme.text;font.weight:Font.DemiBold}
                    StudioComboBox {objectName:"iosResearchController";Layout.fillWidth:true;model:[qsTr("Simulated controller with timers · experimental"), qsTr("No controller · baseline for comparison")];currentIndex:view.pmpController==="asc-v6"?0:1;onActivated:view.pmpController=currentIndex===0?"asc-v6":"none";Accessible.name:qsTr("Simulated firmware controller")}
                }
                Label {text:qsTr("Emulator");color:Theme.text;font.weight:Font.DemiBold}
                RowLayout {Layout.fillWidth:true;enabled:!view.active
                    StudioTextField {objectName:"iosResearchEngine";text:view.enginePath;onTextEdited:view.enginePath=text;Layout.fillWidth:true;placeholderText:"qemu-system-aarch64"}
                    StudioButton {text:qsTr("Choose");onClicked:engineFile.open()}
                }
                Label {text:view.pmpMode?qsTr("Prepared firmware package"):qsTr("Prepared iOS package");color:Theme.text;font.weight:Font.DemiBold}
                RowLayout {Layout.fillWidth:true;enabled:!view.active
                    StudioTextField {objectName:"iosResearchBundle";text:view.selectedBundle;onTextEdited:view.setBundle(text);Layout.fillWidth:true;placeholderText:view.pmpMode?qsTr("Folder with pmp-research.json"):qsTr("Folder with tomacco-ios.json")}
                    StudioButton {text:qsTr("Choose");onClicked:bundleFolder.open()}
                }
                RowLayout {Layout.fillWidth:true
                    Label {text:qsTr("Maximum run time");color:Theme.muted}
                    StudioSpinBox {
                        id:duration;from:view.pmpMode?1:30;to:view.pmpMode?60:900
                        value:view.pmpMode?view.pmpSeconds:view.systemSeconds;stepSize:view.pmpMode?1:30
                        editable:true;enabled:!view.active;Accessible.name:qsTr("Maximum run time in seconds")
                        onValueModified: {if(view.pmpMode)view.pmpSeconds=value;else view.systemSeconds=value}
                    }
                    Label {text:qsTr("seconds");color:Theme.muted}
                    Item {Layout.fillWidth:true}
                }
                ColumnLayout {visible:view.pmpMode;Layout.fillWidth:true;enabled:!view.active;spacing:6
                    StudioButton {objectName:"iosResearchCpuConfig";text:view.showCpuConfig?qsTr("Hide processor settings"):qsTr("Processor settings");onClicked:view.showCpuConfig=!view.showCpuConfig}
                    ColumnLayout {visible:view.showCpuConfig;Layout.fillWidth:true;spacing:6
                        Label {Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.faint;font.pixelSize:Theme.fontSmall;text:qsTr("The processor identity the firmware sees when it starts. Other values make it take other start-up paths; they add no hardware features.")}
                        RowLayout {Layout.fillWidth:true
                            Label {text:"MIDR";color:Theme.muted;Layout.preferredWidth:70}
                            StudioTextField {objectName:"iosResearchCpuMidr";Layout.fillWidth:true;text:view.cpuMidr;onTextEdited:view.cpuMidr=text;placeholderText:"0x610f0000";Accessible.name:"CPU MIDR"}
                        }
                        RowLayout {Layout.fillWidth:true
                            Label {text:qsTr("Core ID");color:Theme.muted;Layout.preferredWidth:70}
                            StudioTextField {objectName:"iosResearchCpuAffinity";Layout.fillWidth:true;text:view.cpuAffinity;onTextEdited:view.cpuAffinity=text;placeholderText:"0x0";Accessible.name:qsTr("Processor core ID (MPIDR affinity)")}
                        }
                    }
                    Label {text:qsTr("Stop at a code address");color:Theme.text;font.weight:Font.DemiBold}
                    StudioTextField {objectName:"iosResearchBreakpoint";Layout.fillWidth:true;text:view.stopAddress;onTextEdited:view.stopAddress=text;placeholderText:qsTr("Address of a firmware instruction, e.g. 0x10280d4 · optional");Accessible.name:qsTr("Firmware breakpoint")}
                    StudioCheckBox {objectName:"iosResearchCaptureRam";text:qsTr("Save a copy of the firmware's memory with the report (2 MiB)");checked:view.captureRam;onToggled:view.captureRam=checked}
                    Label {Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.faint;font.pixelSize:Theme.fontSmall;text:qsTr("The run stops at that address and reports the processor state. The firmware itself is not modified.")}
                }
                }
                Flow {Layout.fillWidth:true;spacing:8
                    StudioButton {visible:!view.paravirtualMode;objectName:"iosResearchStart";text:qsTr("Start test run");iconName:"play";accent:true;enabled:view.canStart;onClicked:view.start()}
                    StudioButton {objectName:"iosResearchStop";text:view.cancelling?qsTr("Stopping…"):view.forceStop?qsTr("Stop without saving"):qsTr("Stop");tip:view.forceStop?qsTr("Stop without saving · ends the run at once, whatever the device has not saved yet is lost"):"";enabled:!!view.jobId && view.active && !view.starting && !view.cancelling;onClicked:view.stop()}
                    StudioButton {visible:!view.paravirtualMode;text:qsTr("Prepare package");enabled:!view.active;onClicked:{dialog.close();workspace.showAction(view.pmpMode?"ios.emulator.prepare_pmp":"ios.emulator.prepare_research")}}
                }
                Rectangle {Layout.fillWidth:true;implicitHeight:1;color:Theme.border}
                RowLayout {Layout.fillWidth:true
                    BusyIndicator {running:view.active;visible:running;Layout.preferredWidth:24;Layout.preferredHeight:24}
                    Label {objectName:"iosResearchState";text:view.stateLabel;color:Theme.text;font.pixelSize:Theme.fontLead;Layout.fillWidth:true;wrapMode:Text.Wrap}
                }
                Label {objectName:"iosResearchCapability";Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.warning;text:view.pmpMode?qsTr("For controller development: Apple's original firmware runs alone on a simulated ARM processor. The simulated controller still rests on unconfirmed hardware guesses; the firmware's services and its connection to iOS are not verified."):view.paravirtualMode?qsTr("Experimental: iOS shows its first system screens. Setup, graphics and native apps do not fully work yet."):qsTr("Work in progress: an agent starts the virtual iOS device, then it appears here. Starting takes many minutes. Apart from the built-in test app, apps with a user interface have not been shown to work yet.")}
                Label {visible:!!view.notice;Layout.fillWidth:true;wrapMode:Text.Wrap;color:view.failed?Theme.danger:Theme.muted;text:view.notice}
                // The configured virtual iPhone's own record: the start's time-zone step and the
                // developer apps of its profile, which are changed here through ios.virtual.configure.
                Rectangle {
                    objectName:"virtualPhoneApps"
                    visible:view.virtualPhone.configured===true
                    Layout.fillWidth:true;implicitHeight:applicationsColumn.implicitHeight+24
                    color:Theme.inset;radius:Theme.radius;border.color:Theme.border
                    ColumnLayout {
                        id:applicationsColumn;spacing:8
                        anchors {left:parent.left;right:parent.right;top:parent.top;margins:12}
                        Label {text:view.virtualPhone.run!==undefined?qsTr("Virtual iOS device · run %1").arg(view.virtualPhone.run):qsTr("Virtual iOS device");color:Theme.text;font.weight:Font.DemiBold}
                        Label {objectName:"virtualPhoneTimeZone";visible:text.length>0;Layout.fillWidth:true;wrapMode:Text.Wrap;font.pixelSize:Theme.fontSmall
                            color:view.virtualPhone.time_zone && view.virtualPhone.time_zone.outcome==="applied"?Theme.muted:Theme.warning;text:view.timeZoneText()}
                        Label {text:qsTr("Your apps");color:Theme.text;font.weight:Font.DemiBold}
                        Label {Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.faint;font.pixelSize:Theme.fontSmall;text:qsTr("Your own arm64 .app bundles. Each start installs them together with the device's helper tools. Changes apply from the next start.")}
                        Label {objectName:"virtualPhoneAppsEmpty";visible:view.virtualApplications.length===0;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;font.pixelSize:Theme.fontSmall;text:qsTr("No apps of your own yet · the start opens the built-in test app.")}
                        Repeater {model:view.virtualApplications
                            RowLayout {
                                id:appRow
                                required property var modelData
                                required property int index
                                objectName:"virtualPhoneApp-"+index
                                Layout.fillWidth:true;spacing:8
                                ColumnLayout {Layout.fillWidth:true;spacing:2
                                    Label {objectName:"virtualPhoneAppTitle-"+appRow.index;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.text;text:view.applicationName(appRow.modelData)+(appRow.modelData.bundle_id?" · "+appRow.modelData.bundle_id:"")}
                                    Label {objectName:"virtualPhoneAppState-"+appRow.index;Layout.fillWidth:true;wrapMode:Text.Wrap;font.pixelSize:Theme.fontSmall;color:appRow.modelData.bundle_id?Theme.muted:Theme.warning;text:view.applicationState(appRow.modelData)}
                                    Label {Layout.fillWidth:true;elide:Text.ElideMiddle;color:Theme.faint;font.pixelSize:Theme.fontMicro;text:appRow.modelData.bundle || ""}
                                }
                                StudioButton {objectName:"virtualPhoneAppRemove-"+appRow.index;text:qsTr("Remove");quiet:true;enabled:!view.virtualAppBusy && !view.virtualBusy;tip:qsTr("Remove from the profile · an app already installed stays on the device");onClicked:view.configureApplications({remove_application:appRow.modelData.bundle})}
                            }
                        }
                        RowLayout {Layout.fillWidth:true;spacing:8
                            Label {text:qsTr("Open at start");color:Theme.muted;font.pixelSize:Theme.fontSmall}
                            StudioComboBox {
                                objectName:"virtualPhoneOpenApp"
                                Layout.fillWidth:true
                                model:view.virtualOpenChoices.map(choice=>choice.name)
                                currentIndex:view.virtualOpenIndex
                                enabled:!view.virtualAppBusy && !view.virtualBusy
                                onActivated:index=>view.configureApplications({application:view.virtualOpenChoices[index].id})
                                Accessible.name:qsTr("App to open at start")
                            }
                        }
                        RowLayout {Layout.fillWidth:true;spacing:8
                            StudioTextField {objectName:"virtualPhoneAppPath";Layout.fillWidth:true;text:view.virtualAppPath;onTextEdited:view.virtualAppPath=text;onAccepted:view.addApplication(text);enabled:!view.virtualAppBusy;placeholderText:qsTr("Path to an .app directory (arm64)")}
                            StudioButton {text:qsTr("Choose");enabled:!view.virtualAppBusy;onClicked:applicationFolder.open()}
                            StudioButton {objectName:"virtualPhoneAppAdd";text:view.virtualAppBusy?qsTr("Checking…"):qsTr("Add app…");accent:true;enabled:!view.virtualAppBusy && !view.virtualBusy && view.virtualAppPath.trim().length>0;tip:qsTr("Check the bundle and add it to the profile · the next start installs it");onClicked:view.addApplication(view.virtualAppPath)}
                        }
                        Label {objectName:"virtualPhoneAppError";visible:!!view.virtualAppNotice;Layout.fillWidth:true;wrapMode:Text.Wrap;font.pixelSize:Theme.fontSmall;color:Theme.danger;text:view.virtualAppNotice}
                        // Saved phone states (ios.virtual.states and its siblings): a name kept on
                        // the last verified run, and a later start from it.
                        Label {text:qsTr("Saved states");color:Theme.text;font.weight:Font.DemiBold}
                        Label {Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.faint;font.pixelSize:Theme.fontSmall;text:qsTr("Keep the last stopped run under a name and start from it again later. Saving copies nothing.")}
                        Label {objectName:"virtualPhoneStatesEmpty";visible:view.virtualStates.length===0;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;font.pixelSize:Theme.fontSmall;text:qsTr("No saved states yet.")}
                        RowLayout {Layout.fillWidth:true;spacing:8;visible:view.virtualStates.length>0
                            StudioComboBox {
                                objectName:"virtualPhoneStateChoice"
                                Layout.fillWidth:true
                                model:view.virtualStates.map(state=>view.stateText(state))
                                currentIndex:Math.min(view.virtualStateIndex,view.virtualStates.length-1)
                                onActivated:index=>{view.virtualStateIndex=index;view.virtualStateDeleteArmed=false}
                                Accessible.name:qsTr("Saved state")
                            }
                            StudioButton {objectName:"virtualPhoneStateUse";text:qsTr("Start from it");enabled:view.virtualStateIdle && !view.virtualRunning && !!view.selectedState && view.selectedState.exists===true && view.selectedState.base!==true;tip:qsTr("The next start begins from this state · the phone must be off");onClicked:view.stateCall("ios.virtual.state_use",{name:view.selectedState.name})}
                            StudioButton {objectName:"virtualPhoneStateDelete";text:view.virtualStateDeleteArmed?qsTr("Delete for good"):qsTr("Delete");danger:view.virtualStateDeleteArmed;quiet:!view.virtualStateDeleteArmed;enabled:view.virtualStateIdle && !!view.selectedState;tip:qsTr("Remove the name, and its disk copy when nothing else keeps it");onClicked:{if(!view.virtualStateDeleteArmed){view.virtualStateDeleteArmed=true;return}view.virtualStateDeleteArmed=false;view.stateCall("ios.virtual.state_delete",{name:view.selectedState.name})}}
                        }
                        RowLayout {Layout.fillWidth:true;spacing:8
                            StudioTextField {objectName:"virtualPhoneStateName";Layout.fillWidth:true;text:view.virtualStateName;onTextEdited:view.virtualStateName=text;onAccepted:view.saveState();enabled:view.virtualStateIdle;placeholderText:qsTr("Name for the last stopped run")}
                            StudioButton {objectName:"virtualPhoneStateSave";text:qsTr("Save state");enabled:view.virtualStateIdle && view.virtualStateName.trim().length>0;tip:qsTr("Keep the last stopped run under this name");onClicked:view.saveState()}
                        }
                        Label {objectName:"virtualPhoneStateNotice";visible:!!view.virtualStateNotice;Layout.fillWidth:true;wrapMode:Text.Wrap;font.pixelSize:Theme.fontSmall;color:view.virtualStateFailed?Theme.danger:Theme.muted;text:view.virtualStateNotice}
                    }
                }
                Label {objectName:"iosResearchCpuIdentity";visible:view.pmpMode && !!view.report.cpu_policy;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;font.pixelSize:Theme.fontSmall;text:view.report.cpu_policy?qsTr("Processor: MIDR %1 · core ID %2 · %3").arg(view.report.cpu_policy.midr).arg(view.report.cpu_policy.affinity || qsTr("not recorded")).arg(view.report.cpu_identity_verified?qsTr("configuration verified"):qsTr("not verified")):""}
                Label {objectName:"iosResearchBreakpointResult";visible:!!view.report.breakpoint_hit;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.accentInk;text:view.report.breakpoint_stop?qsTr("Stopped before the instruction at %1 · this does not show that the firmware started completely").arg(view.report.breakpoint_stop.address):""}
                Label {objectName:"iosResearchRamResult";visible:!!view.report.memory_capture;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;text:view.report.memory_capture?qsTr("Memory copy: 2 MiB from address %1 · saved in the report folder").arg(view.report.memory_capture.physical_base):""}
                Label {visible:!!view.report.memory_capture_error;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.warning;text:view.report.memory_capture_error?qsTr("Memory copy failed: %1").arg(view.report.memory_capture_error.message):""}
                ColumnLayout {
                    visible:view.pmpMode && !!view.lastObservation.cpu_registers
                    Layout.fillWidth:true;spacing:8
                    StudioButton {objectName:"iosResearchRegisters";text:view.showRegisters?qsTr("Hide processor registers"):qsTr("Show processor registers");onClicked:view.showRegisters=!view.showRegisters}
                    Label {visible:view.showRegisters;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.faint;font.pixelSize:Theme.fontSmall;text:qsTr("From the last snapshot · raw 64-bit values")}
                    GridLayout {visible:view.showRegisters;Layout.fillWidth:true;columns:2;columnSpacing:18;rowSpacing:4
                        Repeater {model:view.lastObservation.cpu_registers || []
                            Label {required property var modelData;Layout.fillWidth:true;font.family:Theme.mono;font.pixelSize:Theme.fontSmall;color:Theme.text;text:modelData.name.padEnd(6," ")+modelData.value;Accessible.name:text}
                        }
                    }
                }
                Rectangle {
                    visible:view.pmpMode && !!view.report.memory_capture
                    Layout.fillWidth:true;implicitHeight:memoryInspector.implicitHeight+24
                    color:Theme.inset;radius:Theme.radius;border.color:Theme.border
                    ColumnLayout {
                        id:memoryInspector;spacing:8
                        anchors {left:parent.left;right:parent.right;top:parent.top;margins:12}
                        Label {text:qsTr("Saved memory");color:Theme.text;font.weight:Font.DemiBold}
                        Label {Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;font.pixelSize:Theme.fontSmall;text:qsTr("Addresses 0x1000000 to 0x11fffff of the firmware's memory. Reading checks the saved copy; the firmware is not run again for it.")}
                        RowLayout {Layout.fillWidth:true;enabled:!view.active && !view.readingMemory
                            StudioTextField {objectName:"iosResearchMemoryAddress";Layout.fillWidth:true;text:view.memoryAddress;onTextEdited:view.memoryAddress=text;Accessible.name:qsTr("Memory address")}
                            StudioSpinBox {from:1;to:4096;value:view.memoryLength;stepSize:16;editable:true;onValueModified:view.memoryLength=value;Accessible.name:qsTr("Number of bytes")}
                            StudioButton {objectName:"iosResearchMemoryRead";text:view.readingMemory?qsTr("Loading…"):qsTr("Read");onClicked:view.readMemory()}
                        }
                        Label {visible:!!view.memoryError;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.warning;text:view.memoryError}
                        Label {objectName:"iosResearchMemoryRange";visible:!!view.memoryResult.data;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;font.pixelSize:Theme.fontSmall;text:view.memoryResult.data?qsTr("%n byte(s) from %1 · checksum verified","",view.memoryResult.bytes).arg(view.memoryResult.address):""}
                        StudioScrollView {
                            visible:!!view.memoryResult.data;Layout.fillWidth:true;Layout.preferredHeight:240;clip:true
                            TextArea {objectName:"iosResearchMemoryData";readOnly:true;selectByMouse:true;text:view.memoryText();wrapMode:TextEdit.NoWrap;font.family:Theme.mono;font.pixelSize:Theme.fontSmall;color:Theme.text;background:null;Accessible.name:qsTr("Memory contents in hexadecimal")}
                        }
                    }
                }
                Rectangle {
                    visible:!!view.controllerState;Layout.fillWidth:true;implicitHeight:controllerSummary.implicitHeight+24
                    color:Theme.inset;radius:Theme.radius;border.color:Theme.border
                    ColumnLayout {
                        id:controllerSummary;spacing:8
                        anchors {left:parent.left;right:parent.right;top:parent.top;margins:12}
                        Label {text:qsTr("Controller state");color:Theme.text;font.weight:Font.DemiBold}
                        Label {objectName:"iosResearchControllerState";Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;text:view.controllerState?qsTr("%1 reads · %2 writes · %3 rejected").arg(view.controllerState.reads).arg(view.controllerState.writes).arg(view.controllerState.rejected_accesses):""}
                        Repeater {model:view.controllerState?view.controllerState.queues:[]
                            Label {required property var modelData;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;text:qsTr("%1: %n message(s)","",modelData.count).arg(modelData.direction)+" · "+(modelData.enabled?qsTr("interrupts enabled"):qsTr("interrupts blocked"))+(modelData.staged?" · "+qsTr("transfer waiting"):"")}
                        }
                        Repeater {model:view.controllerState?view.controllerState.timers:[]
                            Label {required property var modelData;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;text:qsTr("%1: %2 · state %3").arg(modelData.source).arg(modelData.input_asserted?qsTr("signal active"):qsTr("no signal")).arg(view.hex(modelData.state))}
                        }
                        Label {Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.faint;font.pixelSize:Theme.fontSmall;text:qsTr("Reading this state changes nothing: no message is taken from a queue and no interrupt is answered.")}
                    }
                }
                Label {objectName:"iosResearchFirstFault";visible:!!view.firstFault;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.warning;text:view.firstFault?qsTr("First invalid memory access: code at %1 · address %2 · %3").arg(view.hex(view.firstFault.pc)).arg(view.hex(view.firstFault.physical)).arg(({read:qsTr("read","access"),write:qsTr("write","access"),execute:qsTr("execute","access")})[view.firstFault.access]):""}
                Label {objectName:"iosResearchSysregFault";visible:!!view.firstSysregFault;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.warning;text:view.firstSysregFault?qsTr("Processor register not supported yet: %1 · %2 by code at %3").arg(view.firstSysregFault.register).arg(({read:qsTr("read","access"),write:qsTr("write","access")})[view.firstSysregFault.access]).arg(view.firstSysregFault.pc):""}
                Label {visible:!!view.lastObservation.controller_snapshot_error;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.warning;text:view.lastObservation.controller_snapshot_error?qsTr("Reading the controller state failed: %1").arg(view.lastObservation.controller_snapshot_error.message):""}
                Label {visible:!!view.report.platform;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;text:view.report.platform?qsTr("Snapshots of the running system: %1 · %2").arg(view.report.platform.snapshots.length).arg(view.report.guest_observations_verified?qsTr("data from iOS verified"):qsTr("incomplete run")):""}
                Label {visible:!!view.report.platform;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;text:view.report.platform?qsTr("System services not found in the snapshots: %1").arg(view.report.platform.missing_services.join(", ")):""}
                Flow {Layout.fillWidth:true;spacing:8
                    StudioButton {text:qsTr("Job & logs");enabled:!!view.jobId;onClicked:{dialog.close();workspace.selectedJob=view.jobId;workspace.showTab("jobs");workspace.request("job.get",{job_id:view.jobId})}}
                    StudioButton {text:qsTr("Report folder");enabled:!!view.reportDirectory;onClicked:Qt.openUrlExternally(view.fileUrl(view.reportDirectory))}
                    StudioButton {text:view.showFrame?qsTr("Hide boot image"):qsTr("Boot image");enabled:!!view.framePath;onClicked:view.showFrame=!view.showFrame}
                    StudioButton {text:qsTr("Refresh");enabled:!!view.jobId && !view.polling;onClicked:view.refresh()}
                }
                Image {visible:view.showFrame && !!view.framePath;Layout.fillWidth:true;Layout.preferredHeight:220;fillMode:Image.PreserveAspectFit;source:view.showFrame && view.framePath?view.fileUrl(view.framePath):"";cache:false;asynchronous:true}
                Label {visible:view.showFrame && !!view.framePath;Layout.fillWidth:true;wrapMode:Text.Wrap;text:qsTr("Actual image from the start-up screen · not yet the device's main display");color:Theme.faint;font.pixelSize:Theme.fontSmall}
            }
        }
    }
    FileDialog {id:engineFile;title:qsTr("Choose iOS emulator");onAccepted:view.enginePath=decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,""))}
    FolderDialog {id:bundleFolder;title:view.pmpMode?qsTr("Choose prepared firmware package"):qsTr("Choose prepared iOS package");onAccepted:view.setBundle(decodeURIComponent(selectedFolder.toString().replace(/^file:\/\//,"")))}
    FolderDialog {id:applicationFolder;title:qsTr("Choose app bundle (.app directory)");onAccepted:view.virtualAppPath=decodeURIComponent(selectedFolder.toString().replace(/^file:\/\//,""))}
}
