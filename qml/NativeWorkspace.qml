import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import QtCore
import Tomacco.Native 1.0
import "."
import "NativeProfiles.js" as NativeProfiles
import "ColorNames.js" as ColorNames
import "AppFiles.js" as AppFiles

Rectangle {
    id: view
    objectName: "nativeWorkspace"
    required property var workspace
    signal webRequested()
    color: Theme.background
    // A project folder dropped anywhere on the window opens as the project.
    // Above everything, so its frame covers the window; it takes only folders,
    // so app files pass on to the device's own drop target.
    DropArea {
        id:folderDrop;objectName:"projectFolderDrop";anchors.fill:parent;z:100
        property string folder:""
        onEntered:drag=>{
            const paths=AppFiles.paths(drag.urls)
            folder=paths.length===1?Studio.projectFolder(paths[0]):""
            if(!folder){drag.accepted=false;return}
            drag.accept(Qt.CopyAction)
        }
        onExited:folder=""
        onDropped:drop=>{
            const paths=AppFiles.paths(drop.urls)
            const chosen=paths.length===1?Studio.projectFolder(paths[0]):""
            folder=""
            if(!chosen){drop.accepted=false;return}
            drop.accept(Qt.CopyAction)
            view.workspace.openProject(chosen);view.navigate("project")
        }
        DropOverlay {objectName:"projectFolderDropOverlay";anchors.fill:parent;visible:folderDrop.containsDrag;text:qsTr("Drop to open %1 as the project").arg(AppFiles.name(folderDrop.folder))}
    }
    property var requests: ({})
    property var targets: []
    property var remotes: ({})
    property var sessions: []
    property var live: ({})
    property var saved: ({})
    property string platform: "android"
    property string target: ""
    property string remote: ""
    property string appleTarget: "device"
    property string runnerBundle: "com.facebook.WebDriverAgentRunner.xctrunner"
    property var iosStatus: ({checks:[]})
    property string launchJob: ""
    property var launchRecord: ({})
    property bool launchPending: false
    readonly property bool sessionRunning: !!launchJob && launchRecord.interactive===true && ["ready","session"].indexOf(launchRecord.phase)>=0
    readonly property string launchDevice: {let steps=launchRecord.plan ? launchRecord.plan.steps || [] : [];let args=steps.length?steps[0].argv || []:[];let index=args.indexOf("-d");return index>=0 && index+1<args.length?args[index+1]:""}
    readonly property bool appRunning: sessionRunning && launchDevice===target
    property var launchTarget: ({})
    property var pendingLaunch: null
    Connections {
        target: view.workspace
        function onRuntimeRequested(platform,device) {view.preferredTarget="";let row=workspace.devices.find(d=>d.id===device && d.platform===platform) || {id:device,name:device,platform:platform,kind:"device"};if(!view.targets.some(d=>d.id===device && d.platform===platform))view.targets=view.targets.concat([row]);view.choosePlatform(platform);if(platform==="ios")view.chooseAppleTarget("device");view.target=device;view.workspace.close();view.connectTarget()}
        function onIosSetupRequested() {view.choosePlatform("ios");view.chooseAppleTarget("device");view.workspace.close();iphoneSetup.open();view.diagnoseIos()}
    }
    Settings {category:"NativeIphone";property alias runnerBundle:view.runnerBundle}
    property string notice: ""
    // The request behind a failure on the notice line, to try again; any other notice drops it.
    property var noticeRetry: null
    onNoticeChanged: noticeRetry=null
    function retryNotice() {
        const again=noticeRetry
        if(!again)return
        notice=""
        if(again.op==="runtime.start")connectTarget()
        else if(again.op==="project.run")launch()
        else request(again.op,again.args)
    }
    property bool failed: false
    property bool connecting: false
    property bool autoAttach: true
    property bool showDeviceFrame: true
    property bool forwardAudio: true
    property string audioPendingSession: ""
    readonly property var liveAudio: live.audio || ({enabled:false,status:"off"})
    readonly property bool audioPending: !!live.session && audioPendingSession===live.session
    property int frameFinish: 0
    readonly property var liveDevice: targets.find(d=>d.id===live.device && d.platform===live.platform) || {id:live.device || "",kind:live.kind || "",profile_id:live.profile_id || ""}
    readonly property var frameMatch: NativeProfiles.match(Studio.models,live.platform || platform,liveDevice,display.sourceSize.width,display.sourceSize.height)
    readonly property var frameProfile: frameMatch.profile
    readonly property bool hasFrame: !!frameProfile.id && frameMatch.compatible
    // The live device can leave the workspace as its own device-shaped window.
    property bool detached: false
    // Device-only: the detached device is the whole app and the main window
    // stays hidden. Closing the device window or losing the device ends the
    // app; the device and its jobs belong to the control plane and keep running.
    property bool solo: initialDeviceOnly
    // A device window that lives elsewhere in the app, such as the virtual iPhone.
    property bool externalDeviceWindow: false
    readonly property bool deviceWindowVisible: deviceWindow.visible || externalDeviceWindow
    // Never leave an invisible app behind: after two minutes without any
    // device window the main window returns.
    property int soloFallbackMs: 120000
    Timer {interval:view.soloFallbackMs;running:view.solo && !view.deviceWindowVisible && !view.quitting;onTriggered:view.showMainWindow()}
    // Losing every device window in solo mode ends the app after a short
    // grace period, so a reconnecting stream does not close it.
    Timer {id:soloGrace;interval:4000;onTriggered:if(view.solo && !view.deviceWindowVisible)view.quitApp()}
    onDeviceWindowVisibleChanged: if(!deviceWindowVisible && solo)soloGrace.restart()
    property bool quitting: false
    // Bounds the wait for the session to stop before the app ends.
    Timer {id:quitTimer;interval:2500;onTriggered:view.quitApp()}
    UpdateModel {id:updates;onRestartRequested:view.restartForUpdate();onBannerKindChanged:view.noteUpdate()}
    // A new version found while no Tomacco window is active is also reported on
    // the desktop, once per version.
    function noteUpdate() {
        // The activity panel's notifications list each new version and each restart it asks for, once.
        const centreKey=updates.bannerKind==="available"?"available:"+updates.latest:updates.bannerKind==="restart"?"restart:"+updates.status.current:""
        if(centreKey && String(Studio.setting("updates/centreNoted",""))!==centreKey) {
            Studio.saveSetting("updates/centreNoted",centreKey)
            if(updates.bannerKind==="available")notifications.add("update","idle",qsTr("Tomacco %1 is available").arg(updates.latest),qsTr("Open the update settings to install it."),{update:true})
            else notifications.add("update","warn",qsTr("Version %1 is installed. Restart to use it.").arg(updates.status.current),"",{update:true})
        }
        if(updates.bannerKind!=="available" || appActive)return
        if(String(Studio.setting("updates/notifiedVersion",""))===updates.latest)return
        Studio.saveSetting("updates/notifiedVersion",updates.latest)
        Studio.notify(qsTr("Tomacco %1 is available").arg(updates.latest),qsTr("Open Tomacco to see what's new and install it."),"update:"+updates.latest)
    }
    // Unsaved edits stay and are shown; the new version starts only once they are dealt with.
    // It opens with this project and view, at this window's size; device
    // sessions belong to the control plane and keep running for it.
    function restartForUpdate() {
        if(workspace.hasUnsaved()){quitApp();return}
        const w=view.Window.window
        if(w && w.saveGeometry)w.saveGeometry()
        const tab=workspace.opened?workspace.section:"runtime"
        const tabs=["runtime","project","devices","jobs","tools","debug","testing","profiling","files","release"]
        if(Studio.relaunch(projectPath,tabs.indexOf(tab)>=0?tab:"runtime"))quitApp()
        else{notice=qsTr("Tomacco could not be started again. Close it and open it from the application menu.");failed=true}
    }
    // True when version a is newer than b ("1.2.0" > "1.1.0"; "1.2.0" > "1.2.0-rc.1").
    function newerVersion(a,b) {
        const parse=v=>{const parts=String(v).split("-");return {numbers:parts[0].split(".").map(x=>parseInt(x,10) || 0),pre:parts.length>1}}
        const x=parse(a),y=parse(b)
        for(let i=0;i<3;++i)if((x.numbers[i] || 0)!==(y.numbers[i] || 0))return (x.numbers[i] || 0)>(y.numbers[i] || 0)
        return !x.pre && y.pre
    }
    // Shown once: the onboarding on the very first start, then what changed on
    // the first start of each new version. fresh overrides Studio.firstStart (the tests use it).
    function startupSheets(fresh) {
        if(captureMode || smokeTest || initialDeviceOnly)return
        const current=Qt.application.version
        if(!current)return
        const seen=String(Studio.setting("app/lastVersion",""))
        if(seen===current)return
        Studio.saveSetting("app/lastVersion",current)
        if(fresh===undefined?Studio.firstStart:fresh){if(String(Studio.setting("onboarding/shown",false))!=="true")onboarding.open();return}
        // No version recorded but settings exist: an update from a release before this one.
        if(seen && !newerVersion(current,seen))return
        whatsNew.open()
    }
    // Unsaved source edits live in the hidden main window; show it instead of
    // ending the app with them.
    function quitApp() {
        quitTimer.stop()
        if(workspace.hasUnsaved()){showMainWindow();detached=false;return}
        quitting=true;Qt.quit()
    }
    // Also restores a main window the user had closed: docking must never
    // leave the app running without any window.
    function showMainWindow() {solo=false;let w=view.Window.window;if(w){w.visible=true;w.raise();w.requestActivate()}}
    // Closing the solo device window ends its session, then the app. With
    // unsaved edits nothing is stopped: the device docks into the main window.
    function closeSolo() {
        if(workspace.hasUnsaved()){showMainWindow();detached=false;return}
        clearPending()
        if(live.session){request("runtime.stop",{session:live.session},{quit:true,quiet:true});quitTimer.start()}else quitApp()
    }
    // A device that is not live yet: an emulator that is still booting (by
    // AVD name) or a known device that is being connected (by id). Its window
    // shows the frame and a startup screen until the real stream takes over.
    property string pendingAvd: ""
    property string pendingDevice: ""
    property string pendingTarget: ""
    property int pendingFailures: 0
    // Sessions known before the wait began say nothing about the awaited device.
    property int sessionsSerial: 0
    property int pendingSerial: 0
    property var pendingProfile: ({})
    readonly property bool pendingWanted: pendingAvd!=="" || pendingDevice!==""
    readonly property bool pending: pendingWanted && !(pendingTarget!=="" && display.state==="live" && live.device===pendingTarget)
    readonly property string pendingLabel: pendingAvd || (targets.find(d=>d.id===pendingDevice) || {name:pendingDevice}).name
    // The startup frame of a device whose model is not known: a plain body
    // of its own platform, never another vendor's.
    readonly property var genericProfile: (targets.find(d=>d.id===pendingDevice) || {}).platform==="ios"
        ? ({id:"",platform:"ios",width:393,height:852,radius:55,bezel:10,cutout:"island",colors:["#3a3c41"]})
        : ({id:"",platform:"android",width:412,height:915,radius:46,bezel:8,cutout:"punch",colors:["#3d424c"]})
    readonly property bool deviceShown: display.state==="live" || pending
    function clearPending() {pendingAvd="";pendingDevice="";pendingTarget="";pendingFailures=0}
    // While a specific device is awaited nothing else may attach: the picture
    // under the startup screen, and the session a close would stop, must be its own.
    function awaitDevice(avd,device,profileId) {
        clearPending();pendingAvd=avd;pendingDevice=device
        let known=targets.find(d=>d.id===device) || ({})
        pendingProfile=Studio.models.find(p=>p.id===(profileId || known.profile_id || "")) || ({})
        if(!pendingWanted)return
        pendingLaunch=null;autoAttach=false;live=({});display.session=({})
        pendingSerial=sessionsSerial;pollAwaited()
    }
    function pollAwaited() {request("runtime.list",{},{quiet:true});request("device.list",{platform:"all"},{quiet:true})}
    // Connects the awaited device. A session that already exists is attached
    // as it is, with whatever settings it was started with; a new one is
    // started only once the device is ready.
    function adoptAwaited() {
        if(!pending || connecting || sessionsSerial===pendingSerial)return
        let wanted=targets.find(x=>x.kind!=="simulator" && (x.platform!=="android" || (x.state==="device" && x.booted!==false)) && (pendingAvd?x.avd===pendingAvd:x.id===pendingDevice))
        let id=wanted?wanted.id:pendingDevice
        if(!id)return
        if(live.session && live.device===id && ["failed","disconnected"].indexOf(live.status)<0){pendingTarget=id;return}
        let active=sessions.find(x=>x.status==="connected" && x.device===id)
        if(active){pendingTarget=id;autoAttach=true;attach(active);return}
        if(!wanted)return
        // Switch to the platform the way choosePlatform() does, but without attaching one of its
        // sessions: attach() also starts audio forwarding, and a session of some other device must
        // neither show nor sound. connectTarget() below attaches or starts the awaited device.
        if(platform!==wanted.platform){pendingLaunch=null;autoAttach=false;live=({});display.session=({});platform=wanted.platform}
        if(wanted.platform==="ios")chooseAppleTarget("device")
        // Only the awaited device may show.
        if(live.device!==id){live=({});display.session=({})}
        pendingTarget=id;target=id;connectTarget()
    }
    // In solo mode the main window returns, so the reason is not lost with the window.
    function givePendingUp(message) {
        if(solo)showMainWindow()
        clearPending();notice=message;failed=true
    }
    // Deferred: clearing the names inside this handler would feed back into
    // the binding that just changed.
    onPendingChanged: if(!pending && pendingWanted)Qt.callLater(()=>{if(!view.pending)view.clearPending()})
    Timer {interval:2000;repeat:true;running:view.pendingWanted;onTriggered:view.pollAwaited()}
    Timer {interval:300000;running:view.pendingWanted;onTriggered:view.givePendingUp(qsTr("The device did not become ready in time."))}
    property real detachedZoom: 0
    onDetachedChanged: if(detached && detachedZoom<=0)deviceWindow.setZoom(Math.min(1,deviceWindow.fitZoom))
    Settings {category:"NativeDisplay";property alias showDeviceFrame:view.showDeviceFrame;property alias detachedZoom:view.detachedZoom;property alias showTouches:view.showTouches}
    // A mark where the pointer touches the device and the pointer's position in
    // the device's pixels and as fractions (what tomaccoctl's touch input takes).
    property bool showTouches: false
    Settings {category:"NativeAudio";property alias forwardAudio:view.forwardAudio}
    // Settings moved to or from another computer (Backend::exportSettings).
    property string settingsTransfer: ""
    property bool settingsTransferFailed: false
    function exportSettingsTo(path) {
        // The views' own values, which their Settings store a moment later.
        let extra={"Notifications/jobs":notifyJobs,"NativeDisplay/showDeviceFrame":showDeviceFrame,"NativeDisplay/showActivity":showActivity,
            "NativeDisplay/showTouches":showTouches,"NativeAudio/forwardAudio":forwardAudio}
        if(runnerBundle)extra["NativeIphone/runnerBundle"]=runnerBundle
        // The update choice is tomaccoctl's; it goes along unless a build or the environment fixes it.
        if(updates.status.mode && !updates.forced){extra["updates/mode"]=updates.mode;extra["updates/channel"]=updates.channel}
        const error=Studio.exportSettings(path,extra)
        settingsTransferFailed=!!error
        settingsTransfer=error || qsTr("Settings saved to %1").arg(path)
    }
    function importSettingsFrom(path) {
        const result=Studio.importSettings(path)
        settingsTransferFailed=!result.ok
        if(!result.ok){settingsTransfer=result.error;return}
        // The views keep their own copies of these; they take the imported values at once.
        const flag=(key,current)=>String(Studio.setting(key,current))==="true"
        notifyJobs=flag("Notifications/jobs",notifyJobs)
        showDeviceFrame=flag("NativeDisplay/showDeviceFrame",showDeviceFrame)
        showActivity=flag("NativeDisplay/showActivity",showActivity)
        showTouches=flag("NativeDisplay/showTouches",showTouches)
        forwardAudio=flag("NativeAudio/forwardAudio",forwardAudio)
        runnerBundle=String(Studio.setting("NativeIphone/runnerBundle",runnerBundle))
        const mode=result.extra["updates/mode"]
        if(mode && !updates.forced && mode!==updates.mode)updates.setMode(mode)
        const channel=result.extra["updates/channel"]
        if(channel && !updates.forced && channel!==updates.channel)updates.setChannel(channel)
        settingsTransfer=qsTr("%n setting(s) imported","",result.applied)+(result.ignored?" · "+qsTr("%n ignored","",result.ignored):"")
    }
    // A build, test or app start that ends while no Tomacco window is active is
    // reported on the desktop, unless turned off in the settings. Jobs shorter
    // than notifyAfterSeconds are left out; cancelled ones too, someone ended them.
    property bool notifyJobs: true
    Settings {category:"Notifications";property alias jobs:view.notifyJobs}
    // The agent's session log the activity panel reads; the one this desktop was started with by default.
    property string sessionLogPath: Studio.sessionLogFromEnvironment()
    Settings {category:"Activity";property alias sessionLog:view.sessionLogPath}
    property bool appActive: Qt.application.state===Qt.ApplicationActive
    readonly property int notifyAfterSeconds: 10
    property var seenJobStates: ({})
    // The activity panel's list of recent notifications.
    NotificationCentre {id:notifications}
    readonly property alias notificationCentre: notifications
    // A notification's link: its job under Builds & jobs, its device as the live device, or the update settings.
    function openNotification(entry) {
        if(entry.job) {
            const job=workspace.jobs.find(x=>x.id===entry.job) || {id:entry.job}
            workspace.selectJob(job);workspace.showTab("jobs")
        } else if(entry.device) {
            navigate("runtime")
            if(entry.platform==="android" || entry.platform==="ios")choosePlatform(entry.platform)
            target=entry.device
        } else if(entry.update) openSettings(qsTr("Updates"))
    }
    function noteJobs(rows) {
        let states={}
        for(const job of rows) {
            states[job.id]=job.state
            const before=seenJobStates[job.id]
            if(!before || ["queued","running"].indexOf(before)<0 || ["succeeded","failed","timed_out","cancelled"].indexOf(job.state)<0) continue
            // Every end this window sees goes to the list, also while it is active.
            const ended=job.state==="succeeded"?qsTr("Completed after %1"):job.state==="failed"?qsTr("Failed after %1"):job.state==="timed_out"?qsTr("Timed out after %1"):qsTr("Cancelled after %1")
            notifications.add("job",job.state==="succeeded"?"ok":job.state==="cancelled"?"idle":"failed",job.label,ended.arg(workspace.jobDuration(job)),{job:job.id})
            if(job.state==="cancelled") continue
            if(!notifyJobs || appActive || (job.updated || 0)-(job.created || 0)<notifyAfterSeconds) continue
            const outcome=job.state==="succeeded"?qsTr("Completed after %1"):job.state==="failed"?qsTr("Failed after %1"):qsTr("Timed out after %1")
            Studio.notify(job.label,outcome.arg(workspace.jobDuration(job)),job.id)
        }
        seenJobStates=states
    }
    Connections {target:view.workspace;function onJobsChanged() {view.noteJobs(view.workspace.jobs)}}
    // Clicking the notification brings the window back with the job's log.
    Connections {
        target:Studio
        function onNotificationActivated(tag) {
            if(tag.indexOf("update:")===0){view.showMainWindow();return}
            const job=view.workspace.jobs.find(x=>x.id===tag)
            view.showMainWindow()
            if(job)view.workspace.selectJob(job)
            view.workspace.showTab("jobs")
        }
    }
    readonly property string projectPath: workspace.project.path || ""
    function targetRows() {return targets.filter(x=>x.platform===platform && (platform!=="ios" || x.kind===appleTarget))}
    readonly property var visibleTargets: targetRows()
    readonly property bool targetIsLive: display.state==="live" && live.device===target && live.platform===platform
    readonly property bool hasTarget: visibleTargets.some(x=>x.id===target)
    function reconcileTarget() {
        let rows=targetRows()
        if(preferredTarget && rows.some(x=>x.id===preferredTarget) && !live.session){target=preferredTarget;preferredTarget="";return}
        if(!rows.some(x=>x.id===target))target=rows.length?rows[0].id:""
    }
    // The device the project opened last ran on, until it is chosen: as soon as it is
    // connected, unless another device is live or the user chose one.
    property string preferredTarget: ""
    function rememberTarget() {preferredTarget="";if(projectPath)workspace.rememberForProject({platform:platform,device:target,appleTarget:appleTarget})}
    function applyProjectPreferences(path) {
        const saved=workspace.preferencesFor(path)
        preferredTarget=saved.device || ""
        // A live device stays; the choice applies to the next connection.
        if(live.session)return
        if(saved.platform==="android" || saved.platform==="ios")choosePlatform(saved.platform)
        if(saved.appleTarget==="device" || saved.appleTarget==="simulator")chooseAppleTarget(saved.appleTarget)
        reconcileTarget()
    }
    Connections {target:view.workspace;function onProjectPreferencesApplied(path) {view.applyProjectPreferences(path)}}
    function state() {
        const rect=display.contentRect, first=display.mapToItem(view,rect.x,rect.y), last=display.mapToItem(view,rect.x+rect.width,rect.y+rect.height)
        return {session:live.session || "",platform:live.platform || platform,device:live.device || target,
            status:display.state,error:display.error,frames:display.frameCount,fps:display.framesPerSecond,
            width:display.sourceSize.width,height:display.sourceSize.height,orientation:display.sourceSize.width>display.sourceSize.height?"landscape":"portrait",
            transport:live.transport || "",codec:live.codec || "",audio:liveAudio,appleTarget:appleTarget,setupVisible:iphoneSetup.visible,setupChecks:iosStatus.checks,
            project:projectPath,launch_job:launchJob,launch_phase:launchRecord.phase || "",app_running:appRunning,
            profile_id:frameProfile.id || "",profile_source:frameMatch.source,reference_density:frameProfile.dpr || null,
            frame_visible:hasFrame && showDeviceFrame,detached:detached && deviceWindow.visible,solo:solo,pending:pending,pending_avd:pendingAvd,pending_device:pendingDevice,rounded_screen:nativeFrame.maskScreen,renderer:nativeFrame.GraphicsInfo.api===GraphicsInfo.Software?"software":"gpu",frame_compatible:frameMatch.compatible,unfolded:frameMatch.unfolded,
            screen_rect:{x:first.x,y:first.y,width:last.x-first.x,height:last.y-first.y}}
    }
    DeviceWindow {
        id:deviceWindow
        visible:view.detached && view.deviceShown
        solo:view.solo
        allowClose:view.quitting
        controlsEnabled:display.state==="live" && !view.pending
        deviceWidth:nativeFrame.width;deviceHeight:nativeFrame.height
        zoom:view.detachedZoom>0?view.detachedZoom:1
        onZoomChanged:view.detachedZoom=zoom
        caption:view.pending?view.pendingLabel:(view.liveDevice.name || view.live.device || "")
        showBack:view.live.platform!=="ios";showRotate:true
        showFlip:nativeFrame.framed && !!nativeFrame.rear && nativeFrame.measured
        onDockRequested:{view.showMainWindow();view.detached=false}
        onCloseRequested:view.closeSolo()
        onHomeRequested:view.input({action:"key",key:"home"})
        onBackRequested:view.input({action:"key",key:"back"})
        onRotateRequested:view.input({action:"rotate",orientation:display.sourceSize.width>display.sourceSize.height?"portrait":"landscape_left"})
        onScreenshotRequested:view.capture()
        AppDropArea {
            parent:deviceWindow.host;anchors.fill:parent;z:2;enabled:view.detached
            kinds:view.installKinds;targetName:view.deviceName;refusal:view.installRefusal
            onAppsDropped:paths=>view.installApps(paths)
            onRefused:reason=>{view.notice=reason;view.failed=true}
        }
    }
    function choosePlatform(value) {
        if(platform===value)return
        pendingLaunch=null;autoAttach=false;live=({});display.session=({});platform=value
        let active=sessions.find(x=>x.status==="connected" && x.platform===value && (value!=="ios" || (!!x.remote===(appleTarget==="simulator"))))
        if(active)attach(active)
    }
    function chooseAppleTarget(value) {
        if(appleTarget===value)return
        pendingLaunch=null;autoAttach=false;live=({});display.session=({});appleTarget=value
    }
    function configure(args) {
        if(args.platform)choosePlatform(args.platform)
        if(args.apple_target)chooseAppleTarget(args.apple_target)
        if(args.frame!==undefined)showDeviceFrame=args.frame
        if(args.pending_avd!==undefined) {
            if(args.pending_avd)choosePlatform("android")
            awaitDevice(args.pending_avd,"",args.profile_id || "")
        }
        if(args.device) {
            target=args.device
            let active=sessions.find(x=>x.status==="connected" && x.platform===platform && x.device===target)
            if(active){autoAttach=true;attach(active)}
            // The app owns the session's settings (audio, runner, remote), so
            // callers ask it to connect instead of starting a session themselves.
            else if(args.connect)awaitDevice("",args.device,args.profile_id || "")
            else{autoAttach=true;live=({});display.session=({})}
        }
        if(args.detached===false && solo)showMainWindow()
        if(args.detached!==undefined)detached=args.detached
        // Solo needs a device window that exists or is on its way.
        if(args.solo!==undefined){if(args.solo && (deviceWindowVisible || (detached && deviceShown)))solo=true;else if(!args.solo && solo)showMainWindow()}
        if(args.setup===true){iphoneSetup.open();diagnoseIos()}else if(args.setup===false)iphoneSetup.close()
    }
    function captureItem(includeFrame) {return includeFrame?nativeFrame:display}
    function request(op,args,context) {
        let signature=JSON.stringify(args || {})
        if(context && context.quiet)for(let key in requests)if(requests[key].op===op && requests[key].signature===signature)return Number(key)
        let id=Agent.request(op,args || {});requests[id]=Object.assign({op:op,signature:signature},context || {});return id
    }
    function refresh() {
        request("runtime.list",{}, {quiet:true});request("device.list",{platform:"all"},{quiet:true});request("remote.list",{},{quiet:true});request("workspace.list",{},{quiet:true})
        if(platform==="ios" && appleTarget==="simulator" && remote)loadApple()
    }
    function loadApple() {if(remote)request("apple.devices",{remote:remote},{remote:remote})}
    function connectTarget() {
        if(!targetRows().some(x=>x.id===target))return
        connecting=true;notice=qsTr("Connecting…");failed=false;autoAttach=true
        let args={platform:platform,device:target};if(platform==="ios" && appleTarget==="simulator")args.remote=remote
        if(platform==="android")args.audio=forwardAudio
        if(platform==="ios" && appleTarget==="device")args.runner_bundle_id=runnerBundle
        request("runtime.start",args)
    }
    function attach(session) {live=session;frameFinish=0;platform=session.platform;if(session.platform==="ios")appleTarget=session.remote?"simulator":"device";target=session.device;if(session.remote)remote=session.remote;display.session=session;notice="";connecting=false;if(session.platform==="android" && session.status==="connected" && forwardAudio && !liveAudio.configured && !liveAudio.error && !audioPending)requestAudio(true);if(pendingLaunch && pendingLaunch.platform===session.platform && pendingLaunch.device===session.device)startPendingLaunch()}
    function requestAudio(enabled) {
        if(!live.session || live.platform!=="android" || audioPending)return
        forwardAudio=enabled;audioPendingSession=live.session
        request("runtime.audio",{session:live.session,action:enabled?"start":"stop"},{session:live.session})
    }
    function stop() {pendingLaunch=null;autoAttach=false;if(live.session)request("runtime.stop",{session:live.session});live=({});display.session=({})}
    function input(args) {display.sendInput(args)}
    // Apps dropped on the device view install on the device it shows, or on
    // the chosen one, through device.install: APKs on Android, one signed
    // IPA on an iPhone over USB.
    readonly property var installTarget: live.session && display.state==="live" ? ({platform:live.platform,device:live.device,kind:live.remote?"simulator":"device"})
        : hasTarget ? ({platform:platform,device:target,kind:platform==="ios"?appleTarget:"device"}) : null
    readonly property var installKinds: !installTarget ? [] : installTarget.platform==="android" ? ["apk"] : installTarget.kind==="device" ? ["ipa"] : []
    readonly property string installRefusal: !installTarget ? qsTr("Choose a device first, then drop the app on it.")
        : installTarget.platform==="android" ? qsTr("Android devices install .apk files.")
        : installTarget.kind==="device" ? qsTr("An iPhone installs one signed .ipa file.")
        : qsTr("Apps for the Mac's simulator are installed with Apple's tools.")
    function installApps(paths) {
        if(!AppFiles.installable(paths,installKinds)){notice=installRefusal;failed=true;return}
        request("device.install",{platform:installTarget.platform,device:installTarget.device,paths:paths})
        notice=qsTr("Installing %1 on %2…").arg(paths.map(AppFiles.name).join(", ")).arg(deviceName || installTarget.device);failed=false
    }
    // A recording of the live Android screen, saved under Videos/Tomacco and shown in the file manager.
    ScreenRecorder {id:recorder;objectName:"nativeRecorder";onNoticeChanged:if(notice){view.notice=notice;view.failed=failed}}
    function toggleRecording() {
        if(recorder.recording){recorder.stop();return}
        if(!live.session || live.platform!=="android"){notice=qsTr("Connect an Android device to record its screen.");failed=true;return}
        recorder.start("android.record",{device:live.device,path:Studio.recordingPath(deviceName || live.device),seconds:180})
    }
    // The picture is saved under Pictures/Tomacco and put on the clipboard.
    function capture() {
        if(!live.session){notice=qsTr("Connect a device to take a screenshot.");failed=true;return}
        request("runtime.capture",{session:live.session,path:Studio.screenshotPath(live.device)})
    }
    function build(action) {
        if(!projectPath)return
        if(platform==="ios" && appleTarget==="simulator") {
            workspace.showAction(action==="test"?"apple.test":"apple.build")
            workspace.formValues=Object.assign({},workspace.formValues,{remote:remote,path:projectPath,destination:"platform=iOS Simulator,id="+target})
            workspace.open();return
        }
        workspace.request("project.execute",{path:projectPath,platform:platform,configuration:"debug",action:action,format:platform==="ios"?"ipa":"apk",sign:false})
        workspace.showTab("jobs")
    }
    function launch() {
        if(launchPending || launchJob || !projectPath || !targetRows().some(x=>x.id===target))return
        rememberTarget()
        launchTarget={platform:platform,device:target,runner_bundle_id:runnerBundle,audio:forwardAudio}
        pendingLaunch={path:projectPath,platform:platform,device:target,configuration:"debug",bundle_id:workspace.project.configuration.applicationId || ""}
        // Start the automation runner first. project.run then installs the app
        // and launches its actual signed bundle ID, keeping it in front.
        if(platform==="ios" && appleTarget==="device" && workspace.project.kinds.indexOf("xtool")>=0 && (!live.session || live.device!==target || live.status!=="connected"))connectTarget()
        else startPendingLaunch()
    }
    function startPendingLaunch() {
        let args=pendingLaunch;pendingLaunch=null
        if(!args)return
        launchPending=true;request("project.run",args)
        notice=qsTr("Building and starting your app…");failed=false
    }
    function diagnoseIos() {let args={};if(projectPath)args.path=projectPath;if(target && appleTarget==="device")args.device=target;request("ios.doctor",args)}
    function setupRemedy(name,fallback) {return ({swift:qsTr("Install the Swift toolchain that matches your Apple SDK."),xtool:qsTr("Install xtool for Linux."),"go-ios":qsTr("Set up go-ios for the iPhone developer services."),iproxy:qsTr("Install libusbmuxd for the USB connection."),idevicepair:qsTr("Install libimobiledevice for device pairing."),apple_sdk:qsTr("Download the Xcode archive from Apple, then choose ‘Import SDK’. Processing runs on Linux."),apple_auth:qsTr("Choose ‘Sign in to Apple’ and finish signing in in the local terminal."),usbmuxd:qsTr("Connect the unlocked iPhone. The usbmuxd service must be available."),iphone:qsTr("Connect an iPhone or iPad via USB and confirm ‘Trust This Computer’."),project:qsTr("This Linux build path needs a SwiftPM/xtool project. Flutter and Xcode projects are not converted automatically yet.")})[name] || fallback}
    function syncWorkspaceTarget() {if(!workspace.opened) {workspace.selectedDevice=target;workspace.selectedPlatform=platform}}
    onPlatformChanged: {syncWorkspaceTarget();reconcileTarget();if(platform==="ios" && appleTarget==="simulator")loadApple()}
    onAppleTargetChanged: {reconcileTarget();if(appleTarget==="simulator")loadApple()}
    onTargetsChanged: reconcileTarget()
    onTargetChanged: syncWorkspaceTarget()
    Component.onCompleted: {refresh();showCliError();Qt.callLater(startupSheets)}
    function showCliError() {if(Agent.cliError){notice=Agent.cliError;failed=true}}
    Connections {target:Agent;function onCliChanged() {view.showCliError()}}
    Timer {interval:1800;repeat:true;running:view.visible;triggeredOnStart:true;onTriggered:{view.request("runtime.list",{},{quiet:true});if(!workspace.opened)view.request("job.list",{},{quiet:true})}}
    Connections {
        target:Agent
        function onApiFinished(id,op,response) {
            let c=view.requests[id];if(!c)return;delete view.requests[id]
            // A reply that arrives after the quit was abandoned must not end the app.
            if(c.quit){if(quitTimer.running)view.quitApp();return}
            if(op==="runtime.audio") {
                if(view.audioPendingSession===c.session)view.audioPendingSession=""
                if(view.live.session!==c.session)return
                if(response.ok)view.live=Object.assign({},view.live,{audio:response.result})
                else {view.notice=response.error.message;view.failed=true}
                return
            }
            if(!response.ok){
                if(op==="project.run")view.launchPending=false
                if(op==="runtime.start")view.connecting=false
                if(!c.quiet){view.notice=response.error.message+(response.error.remedy?"\n"+qsTr("What to do: %1").arg(response.error.remedy):"");view.failed=true;view.noticeRetry={op:op,args:JSON.parse(c.signature)}}
                // An awaited device is retried a few times, then given up with this reason.
                if(op==="runtime.start" && view.pendingWanted && response.error.code!=="runtime_start_timeout") {
                    view.pendingFailures+=1
                    if(view.pendingFailures>=3)view.givePendingUp(response.error.message)
                }
                if(op==="runtime.start" && response.error.code==="runtime_start_timeout" && response.error.details && response.error.details.session){view.live=response.error.details;view.autoAttach=true;view.notice=qsTr("The runtime is still starting…")}
                else if(op==="runtime.start")view.pendingLaunch=null
                if(response.error.code==="workspace_busy" && response.error.details.job){view.launchRecord=response.error.details.job;view.launchJob=view.launchRecord.id;workspace.selectJob(view.launchRecord);view.notice=qsTr("A job is already running for this project. You can open or stop it under Builds & jobs.")}
                return
            }
            let r=response.result
            if(op==="runtime.start")view.attach(r)
            if(op==="runtime.list") {
                view.sessions=r;view.sessionsSerial+=1
                let active=r.find(x=>x.session===view.live.session)
                if(active && ["failed","disconnected"].indexOf(active.status)>=0 && view.live.status!==active.status){notifications.add("device",active.status==="failed"?"failed":"warn",(view.targets.find(d=>d.id===active.device) || {}).name || active.device,active.error || qsTr("Session ended"),{platform:active.platform,device:active.device});view.live=active;view.notice=active.error || qsTr("Session ended");view.failed=active.status==="failed";view.pendingLaunch=null}
                else if(active && !view.audioPending)view.live=Object.assign({},view.live,{audio:active.audio})
                if(view.autoAttach && (!view.live.session || !view.live.video_socket || display.state!=="live")) {
                    let next=r.find(x=>x.status==="connected" && x.platform===view.platform && (!view.target || x.device===view.target) && (view.platform!=="ios" || (!!x.remote===(view.appleTarget==="simulator"))) && (!view.live.session || view.live.session===x.session || ["failed","disconnected"].indexOf(view.live.status)>=0))
                    if(next && display.session.session!==next.session)view.attach(next)
                }
                view.adoptAwaited()
            }
            if(op==="device.list") {
                view.targets=view.targets.filter(x=>x.kind==="simulator").concat(r.devices.map(x=>Object.assign({},x,{kind:x.kind || "device"})))
                view.adoptAwaited()
            }
            if(op==="remote.list") {view.remotes=r;if(!view.remote && Object.keys(r).length)view.remote=Object.keys(r)[0]}
            if(op==="remote.configure") {macSetup.close();view.remote=r.name;view.request("remote.list",{});view.loadApple()}
            if(op==="apple.devices" && c.remote===view.remote) {
                let rows=[];let devices=r.simulators.devices || {}
                for(let os in devices)for(let d of devices[os])if(d.isAvailable!==false)rows.push({id:d.udid,name:d.name,platform:"ios",kind:"simulator",state:d.state,os:os.split(".").pop()})
                view.targets=view.targets.filter(x=>x.kind!=="simulator").concat(rows)
                if(view.platform==="ios" && view.appleTarget==="simulator" && rows.length && !rows.some(x=>x.id===view.target))view.target=(rows.find(x=>x.state==="Booted") || rows[0]).id
                view.notice=rows.length?"":qsTr("No iOS simulator runtime is installed on this Mac yet. Install one in Xcode → Settings → Components.")
            }
            if(op==="workspace.list")view.saved=r
            if(op==="ios.doctor")view.iosStatus=r
            if(op==="ios.prepare" || op==="ios.wda_prepare") {if(r.id){workspace.selectJob(r);view.notice=qsTr("Setup started · progress under Builds & jobs")}else view.notice=qsTr("Apple sign-in opened in the local terminal");view.failed=false}
            if(op==="project.run") {workspace.rememberJob(r,op,JSON.parse(c.signature));view.launchPending=false;view.launchJob=r.id;view.launchRecord=r;workspace.selectJob(r);view.notice=r.reused?qsTr("Reused the running session"):qsTr("Building and starting your app…");if(workspace.project.kinds.indexOf("flutter")>=0 && (!view.live.session || view.live.device!==view.launchTarget.device))view.request("runtime.start",view.launchTarget)}
            if(op==="job.list") {
                workspace.jobs=r
                let active=r.find(x=>x.plan && ["queued","running"].indexOf(x.state)>=0 && (x.plan.exclusive===view.projectPath || x.plan.cwd===view.projectPath))
                if(active){view.launchJob=active.id;view.launchRecord=active;if(view.appRunning)view.notice=qsTr("App running · apply changes with Hot Reload");else if(view.sessionRunning)view.notice=qsTr("App running on %1 · end the session before switching targets").arg(view.launchDevice)}
                else if(view.launchJob){let ended=r.find(x=>x.id===view.launchJob);view.launchJob="";view.launchRecord=({});if(ended){view.notice=ended.state==="succeeded"?qsTr("App launched"):qsTr("Session ended · %1").arg(workspace.jobStateLabel(ended));view.failed=ended.state==="failed"}}
            }
            if(op==="job.send"){view.notice=qsTr("Applying changes to the running app…");view.failed=false}
            if(op==="job.cancel"){view.notice=qsTr("Ending the session…");view.failed=false}
            if(op==="job.get" && r.id===view.launchJob && ["succeeded","failed","cancelled","timed_out"].indexOf(r.state)>=0) {
                view.launchJob=""
                if(r.state==="succeeded") {view.notice=qsTr("App launched");if(!view.live.session || view.live.device!==view.launchTarget.device)view.request("runtime.start",view.launchTarget)}
                else {view.notice=qsTr("App launch: %1. Details under Builds & jobs.").arg(workspace.jobStateLabel(r));view.failed=true}
            }
            if(op==="workspace.context") {view.preferredTarget="";if(r.workspace.device)workspace.rememberForProject({platform:r.workspace.platform || "android",device:r.workspace.device},r.project.path);workspace.openProject(r.project.path);view.platform=r.workspace.platform || "android";view.target=r.workspace.device || ""}
            if(op==="device.install"){workspace.rememberJob(r,op,JSON.parse(c.signature));if(r.id)workspace.selectJob(r);view.notice=qsTr("Installing · progress under Builds & jobs");view.failed=false}
            if(op==="runtime.capture"){view.notice=(Studio.copyImage(r.path)?qsTr("Screenshot saved and copied · %1"):qsTr("Capture saved · %1")).arg(r.path);view.failed=false}
        }
    }
    readonly property alias workspaceHost: workspaceHost
    readonly property bool compact: width<1120
    readonly property bool shortWindow: height<620
    readonly property bool railWide: width>=1360
    // Below this the device bar's actions are icons and the bar's name goes.
    readonly property bool tight: width<760
    // In a narrow window the activity panel takes the whole stage while it is open.
    readonly property bool activityOnly: activityVisible && width<860
    readonly property string currentSection: workspace.opened ? workspace.section : "runtime"
    // The number in each entry is its Ctrl shortcut.
    readonly property var navigation: [
        {id:"runtime",label:qsTr("Live device"),icon:"phone",key:"1",group:qsTr("WORKSPACE")},
        {id:"files",label:qsTr("Files"),icon:"files",key:"2"},
        {id:"jobs",label:qsTr("Builds & jobs"),icon:"jobs",key:"3"},
        {id:"devices",label:qsTr("Devices"),icon:"devices",key:"4"},
        {id:"project",label:qsTr("Overview"),icon:"project",key:"5"},
        {id:"debug",label:"Debugger",icon:"debug",key:"6",group:qsTr("DEVELOPMENT")},
        {id:"testing",label:qsTr("UI tests"),icon:"testing",key:"7"},
        {id:"profiling",label:"Performance",icon:"profiling",key:"8"},
        {id:"release",label:"Release",icon:"release",key:"9"},
        {id:"tools",label:qsTr("Agents"),icon:"agent",key:"0",group:qsTr("AUTOMATION")}
    ]
    // Project folders opened before, but the open one, to reopen from the start screen and the palette.
    readonly property var recentProjects: Studio.recentProjects.filter(p=>p!==projectPath).map(p=>({path:p,name:AppFiles.name(p)}))
    // What the palette offers besides the views and the agent tools.
    readonly property var commands: [
        {id:"open-project",title:qsTr("Open project"),detail:qsTr("Command"),icon:"files",key:qsTr("Ctrl")+"+O",run:()=>projectFolder.open()},
        {id:"activity",title:view.activityVisible?qsTr("Hide activity"):qsTr("Show activity"),detail:qsTr("Command"),icon:"activity",key:qsTr("Ctrl")+"+J",run:()=>view.toggleActivity()},
        {id:"web-preview",title:qsTr("Web preview"),detail:qsTr("Command"),icon:"web",key:"",run:()=>view.webRequested()},
        {id:"copy-context",title:qsTr("Copy context"),detail:qsTr("Command"),icon:"copy",key:"",run:()=>view.copyContext()},
        {id:"appearance",title:Theme.isDark?qsTr("Switch to light appearance"):qsTr("Switch to dark appearance"),detail:qsTr("Command"),icon:Theme.isDark?"sun":"moon",key:"",run:()=>Studio.setAppearance(Theme.isDark?"light":"dark")},
        {id:"settings",title:qsTr("Settings"),detail:qsTr("Command"),icon:"settings",key:qsTr("Ctrl")+"+,",run:()=>settingsDialog.open()},
        {id:"save-layout",title:qsTr("Save the current layout"),detail:qsTr("Window layouts"),icon:"window",key:"",run:()=>view.openSettings(qsTr("Window layouts"))},
        {id:"stage-log",title:view.showStageLog?qsTr("Hide the log beside the device"):qsTr("Show the log beside the device"),detail:qsTr("Command"),search:qsTr("Split view"),icon:"jobs",key:"",run:()=>{view.showStageLog=!view.showStageLog}},
        {id:"touches",title:view.showTouches?qsTr("Hide touches and pointer coordinates"):qsTr("Show touches and pointer coordinates"),detail:qsTr("Command"),icon:"testing",key:"",run:()=>{view.showTouches=!view.showTouches}},
        {id:"screenshot",title:qsTr("Save and copy a device screenshot"),detail:qsTr("Command"),icon:"camera",key:qsTr("Ctrl")+"+Shift+S",run:()=>view.capture()},
        {id:"shortcuts",title:qsTr("Keyboard shortcuts"),detail:qsTr("Command"),icon:"keyboard",key:"F1",run:()=>shortcutsDialog.open()},
        {id:"about",title:qsTr("About Tomacco"),detail:qsTr("Command"),icon:"info",key:"",run:()=>about.open()},
        {id:"onboarding",title:qsTr("Get started"),detail:qsTr("Command"),search:qsTr("Connect your coding agent"),icon:"agent",key:"",run:()=>onboarding.open()}
    ].concat(layoutNames.map(name=>({id:"layout:"+name,title:qsTr("Use window layout %1").arg(name),detail:qsTr("Window layouts"),search:name,icon:"window",key:"",run:()=>view.applyLayout(name)})))
     .concat(recentProjects.map(p=>({id:"recent:"+p.path,title:qsTr("Open recent project %1").arg(p.name),detail:p.path,search:p.name,icon:"files",key:"",run:()=>workspace.openProject(p.path)})))
    function navigate(section) { if(section==="runtime")workspace.close();else workspace.showTab(section) }
    function copyContext() {Studio.copy(JSON.stringify({project:projectPath,runtime:live,cli:Agent.cliPath,mcp:{command:Agent.cliPath,args:["mcp"]}},null,2));notice=qsTr("Project and runtime context copied");failed=false}
    // The panel is open by default where there is room; in a narrow window it
    // opens only on request and gives way again when asked.
    property bool showActivity: true
    property bool activityForced: false
    readonly property bool activityVisible: showActivity && (width>=1120 || activityForced)
    function toggleActivity() {if(activityVisible){showActivity=false;activityForced=false}else{showActivity=true;activityForced=true}}
    Settings {category:"NativeDisplay";property alias showActivity:view.showActivity}
    // The log beside the device: shown or not, which log and how wide, kept across starts.
    property bool showStageLog: false
    property string stageLogMode: "device"
    property int stageLogWidth: 420
    Settings {category:"StageLog";property alias shown:view.showStageLog;property alias mode:view.stageLogMode;property alias width:view.stageLogWidth}
    // Named window layouts, such as "testing" or "debugging": the open view,
    // the activity panel, the log beside the device with its mode and width,
    // the device windows and the window's size. Kept as JSON in the settings.
    property var windowLayouts: {try {const value=JSON.parse(String(Studio.setting("layouts/saved","{}")));return value && typeof value==="object" && !Array.isArray(value)?value:{}} catch(e) {return {}}}
    readonly property var layoutNames: Object.keys(windowLayouts).sort((a,b)=>a.localeCompare(b))
    readonly property int maxLayouts: 12
    function currentLayout() {
        const w=view.Window.window
        const maximized=!!w && (w.visibility===Window.Maximized || w.visibility===Window.FullScreen)
        return {view:currentSection,activity:showActivity,stageLog:showStageLog,stageLogMode:stageLogMode,stageLogWidth:stageLogWidth,
                deviceWindow:detached,iosWindow:workspace.guestWindowVisible,maximized:maximized,width:w?w.width:0,height:w?w.height:0}
    }
    function saveLayout(name) {
        name=String(name).trim().slice(0,40)
        if(!name)return false
        if(!windowLayouts[name] && layoutNames.length>=maxLayouts){notice=qsTr("At most %1 window layouts are kept. Delete one first.").arg(maxLayouts);failed=true;return false}
        windowLayouts=Object.assign({},windowLayouts,{[name]:currentLayout()})
        Studio.saveSetting("layouts/saved",JSON.stringify(windowLayouts))
        notice=qsTr("Window layout “%1” saved").arg(name);failed=false
        return true
    }
    function deleteLayout(name) {
        if(!windowLayouts[name])return
        let next=Object.assign({},windowLayouts);delete next[name]
        windowLayouts=next
        Studio.saveSetting("layouts/saved",JSON.stringify(windowLayouts))
    }
    function applyLayout(name) {
        const layout=windowLayouts[name]
        if(!layout)return false
        const sections=["runtime"].concat(navigation.map(x=>x.id))
        navigate(sections.indexOf(layout.view)>=0?layout.view:"runtime")
        showActivity=layout.activity!==false;activityForced=showActivity && width<1120
        showStageLog=layout.stageLog===true
        if(layout.stageLogMode==="device" || layout.stageLogMode==="job")stageLogMode=layout.stageLogMode
        if(Number(layout.stageLogWidth)>=Theme.panelWidth*.75)stageLogWidth=Math.round(Number(layout.stageLogWidth))
        // The device window shows as soon as a device is live.
        detached=layout.deviceWindow===true
        if((layout.iosWindow===true)!==workspace.guestWindowVisible)workspace.configureIosGuest({detached:layout.iosWindow===true})
        const w=view.Window.window
        if(w && !solo) {
            if(layout.maximized)w.showMaximized()
            else {
                if(w.visibility===Window.Maximized || w.visibility===Window.FullScreen)w.showNormal()
                if(Number(layout.width)>=w.minimumWidth)w.width=Math.min(Number(layout.width),Math.max(w.minimumWidth,Screen.desktopAvailableWidth))
                if(Number(layout.height)>=w.minimumHeight)w.height=Math.min(Number(layout.height),Math.max(w.minimumHeight,Screen.desktopAvailableHeight))
            }
        }
        notice=qsTr("Window layout “%1”").arg(name);failed=false
        return true
    }
    // One word for what the device is doing, used wherever its state shows.
    readonly property string deviceState: pending ? ((pendingAvd && !pendingTarget) ? "booting" : "connecting")
        : display.state==="live" ? "live"
        : (connecting || display.state==="connecting") ? "connecting"
        : (live.status==="failed" || !!display.error) ? "failed"
        : live.session ? "ended" : "idle"
    readonly property string deviceStateLabel: ({booting:qsTr("Booting"),connecting:qsTr("Connecting"),live:qsTr("Live"),failed:qsTr("Failed"),ended:qsTr("Disconnected"),idle:qsTr("No device")})[deviceState]
    readonly property string deviceTone: ({booting:"busy",connecting:"busy",live:"live",failed:"failed",ended:"warn",idle:"idle"})[deviceState]
    readonly property string deviceName: pending ? pendingLabel : display.state==="live" ? (frameProfile.name || liveDevice.name || live.name || live.device || "") : ((targets.find(d=>d.id===target) || {}).name || "")
    readonly property string deviceMetrics: display.state!=="live" ? "" : display.sourceSize.width+"×"+display.sourceSize.height+(display.framesPerSecond>0?" · "+Math.round(display.framesPerSecond)+" fps":"")
    readonly property var runningJobs: workspace.jobs.filter(x=>["queued","running"].indexOf(x.state)>=0)
    Shortcut {sequence:"Ctrl+K";enabled:view.visible;onActivated:commandPalette.open()}
    Shortcut {sequence:"Ctrl+O";enabled:view.visible;onActivated:projectFolder.open()}
    Shortcut {sequence:"Ctrl+J";enabled:view.visible;onActivated:view.toggleActivity()}
    Shortcut {sequence:"Ctrl+,";enabled:view.visible;onActivated:settingsDialog.open()}
    Shortcut {sequence:"F1";enabled:view.visible;onActivated:shortcutsDialog.open()}
    Shortcut {sequence:"Ctrl+1";enabled:view.visible;onActivated:view.navigate("runtime")}
    Shortcut {sequence:"Ctrl+2";enabled:view.visible;onActivated:view.navigate("files")}
    Shortcut {sequence:"Ctrl+3";enabled:view.visible;onActivated:view.navigate("jobs")}
    Shortcut {sequence:"Ctrl+4";enabled:view.visible;onActivated:view.navigate("devices")}
    Shortcut {sequence:"Ctrl+5";enabled:view.visible;onActivated:view.navigate("project")}
    Shortcut {sequence:"Ctrl+6";enabled:view.visible;onActivated:view.navigate("debug")}
    Shortcut {sequence:"Ctrl+7";enabled:view.visible;onActivated:view.navigate("testing")}
    Shortcut {sequence:"Ctrl+8";enabled:view.visible;onActivated:view.navigate("profiling")}
    Shortcut {sequence:"Ctrl+9";enabled:view.visible;onActivated:view.navigate("release")}
    Shortcut {sequence:"Ctrl+0";enabled:view.visible;onActivated:view.navigate("tools")}
    ColumnLayout {
        anchors.fill:parent;spacing:0
        // The title bar: who, what, and the ways out.
        Rectangle {
            Layout.fillWidth:true;Layout.preferredHeight:Theme.barHeight;color:Theme.surface
            RowLayout {
                anchors.fill:parent;anchors.leftMargin:Theme.gapMd+Theme.gapXxs;anchors.rightMargin:Theme.gapSm;spacing:Theme.gapXs
                Row {spacing:Theme.gapXs+1
                    BrandMark {size:Theme.gapXl;anchors.verticalCenter:parent.verticalCenter}
                    StudioHeading {visible:!view.tight;text:Qt.application.name;size:Theme.fontTitle;wide:true;wrapMode:Text.NoWrap;anchors.verticalCenter:parent.verticalCenter}
                }
                Rectangle {Layout.preferredWidth:1;Layout.preferredHeight:Theme.iconSize;color:Theme.border;Layout.leftMargin:Theme.gapMd;Layout.rightMargin:Theme.gapXs}
                StudioButton {
                    objectName:"projectButton";quiet:true;Layout.fillWidth:true;Layout.minimumWidth:Theme.gapXxxl;Layout.maximumWidth:Math.min(implicitWidth,view.compact?200:320)
                    text:workspace.project.name || qsTr("No project yet")
                    tip:workspace.project.path || qsTr("Open a project folder · Ctrl+O")
                    onClicked:{if(workspace.project.path)view.navigate("project");else projectFolder.open()}
                }
                StudioComboBox {visible:Object.keys(view.saved).length>0 && !view.compact;Layout.preferredWidth:170;model:Object.keys(view.saved);displayText:qsTr("Switch workspace");onActivated:view.request("workspace.context",{name:currentText})}
                Item {Layout.fillWidth:true}
                StudioButton {objectName:"commandButton";text:qsTr("Search");displayText:view.width<760?"":text;iconName:"search";keyHint:view.width<760?"":qsTr("Ctrl")+" K";tip:qsTr("Search navigation and all tools · Ctrl+K");onClicked:commandPalette.open()}
                StudioButton {text:qsTr("Web preview");displayText:view.compact?"":text;iconName:"web";quiet:true;tip:qsTr("Web preview");onClicked:view.webRequested()}
                StudioButton {text:qsTr("Open project");displayText:view.compact?"":text;iconName:"files";quiet:true;tip:qsTr("Open a project folder · Ctrl+O");onClicked:projectFolder.open()}
                StudioButton {objectName:"activityButton";iconName:"activity";displayText:"";text:qsTr("Activity");quiet:true;selected:view.activityVisible;visible:!workspace.opened;tip:qsTr("Jobs and agent calls · Ctrl+J");onClicked:view.toggleActivity()}
                StudioButton {objectName:"settingsButton";iconName:"settings";displayText:"";text:qsTr("Settings");quiet:true;tip:qsTr("Appearance and language · Ctrl+,");onClicked:settingsDialog.open()}
                StudioButton {objectName:"aboutButton";iconName:"info";quiet:true;tip:qsTr("About Tomacco · version, license and source code");Accessible.name:qsTr("About Tomacco");onClicked:about.open()}
            }
            Rectangle {anchors.bottom:parent.bottom;width:parent.width;height:1;color:Theme.subtle}
        }
        UpdateBanner {Layout.fillWidth:true;Layout.preferredHeight:implicitHeight;model:updates}
        RowLayout {
            Layout.fillWidth:true;Layout.fillHeight:true;spacing:0
            // The rail: icons in a narrow window, icons, names and keys in a wide one.
            Rectangle {
                Layout.preferredWidth:view.railWide?Theme.railWideWidth:Theme.railWidth;Layout.fillHeight:true;color:Theme.surface
                ColumnLayout {
                    anchors.fill:parent;anchors.margins:Theme.gapSm;spacing:Theme.gapXs
                    StudioScrollView {
                        Layout.fillWidth:true;Layout.fillHeight:true;clip:true;contentWidth:availableWidth
                        Column {width:parent.width;spacing:Theme.gapXxs
                            Repeater {model:view.navigation
                                Column {
                                    required property var modelData
                                    required property int index
                                    width:parent.width
                                    Item {width:parent.width;height:!modelData.group?0:view.railWide?(index===0?Theme.gapLg+Theme.gapXs:Theme.gapXl+Theme.gapSm):(index===0?0:Theme.gapMd+1)
                                        StudioSection {visible:view.railWide;anchors.left:parent.left;anchors.leftMargin:Theme.gapSm;anchors.bottom:parent.bottom;anchors.bottomMargin:Theme.gapXs;text:modelData.group || ""}
                                        Rectangle {visible:!view.railWide && parent.height>0;width:parent.width;height:1;y:Theme.gapXs+Theme.gapXxs;color:Theme.subtle}
                                    }
                                    StudioButton {
                                        objectName:"nav-"+modelData.id
                                        width:parent.width;height:Theme.rowHeight
                                        text:modelData.label;displayText:view.railWide?modelData.label:""
                                        iconName:modelData.icon;keyHint:view.railWide?modelData.key:""
                                        alignLeft:true;quiet:true;marker:true;selected:view.currentSection===modelData.id
                                        tip:modelData.label+" · "+qsTr("Ctrl")+"+"+modelData.key
                                        onClicked:view.navigate(modelData.id)
                                    }
                                }
                            }
                        }
                    }
                    StudioButton {text:qsTr("Copy context");displayText:view.railWide?text:"";iconName:"copy";quiet:true;alignLeft:true;Layout.fillWidth:true;tip:qsTr("Copy what your agent needs: this project and the live device");onClicked:view.copyContext()}
                    Item {
                        id:agentStatus;objectName:"agentStatus";Layout.fillWidth:true;Layout.preferredHeight:Theme.controlSmall
                        readonly property bool ready:!Agent.cliError && !Agent.socketError
                        StudioStatus {
                            x:view.railWide?Theme.gapMd:(parent.width-7)/2;anchors.verticalCenter:parent.verticalCenter
                            width:view.railWide?parent.width-Theme.gapMd:7
                            tone:agentStatus.ready?"ok":"failed"
                            text:!view.railWide?"":Agent.cliError?qsTr("Control program unavailable"):Agent.socketError?qsTr("Agent API not ready"):qsTr("Ready for agents")
                        }
                        // The details on hover: the reason, or how many tools agents can call through the CLI and MCP.
                        HoverHandler {id:agentHover}
                        ToolTip.visible:agentHover.hovered;ToolTip.delay:550
                        ToolTip.text:Agent.cliError || Agent.socketError || qsTr("%n tool(s) available to agents through the CLI and MCP","",workspace.operations.length)
                    }
                }
                Rectangle {anchors.right:parent.right;width:1;height:parent.height;color:Theme.subtle}
            }
            Item {id:workspaceHost;Layout.fillWidth:true;Layout.fillHeight:true;visible:workspace.opened}
            ColumnLayout {
                visible:!workspace.opened;Layout.fillWidth:true;Layout.fillHeight:true;spacing:0
                // Which device, and the one thing to do with it.
                Rectangle {
                    Layout.fillWidth:true;Layout.preferredHeight:deviceBar.implicitHeight+Theme.gapSm*2;color:Theme.surface
                    ColumnLayout {
                        id:deviceBar;anchors.fill:parent;anchors.margins:Theme.gapSm;spacing:Theme.gapSm
                        RowLayout {Layout.fillWidth:true;spacing:Theme.gapXs
                            StudioSegmented {current:view.platform;model:[{id:"android",label:"Android"},{id:"ios",label:"Apple"}];onChosen:id=>{view.choosePlatform(id);view.rememberTarget()}}
                            RowLayout {
                                visible:view.platform==="ios" && !view.compact;spacing:Theme.gapXs
                                StudioComboBox {Layout.preferredWidth:180;model:[qsTr("iPhone via USB"),qsTr("Simulator on a Mac")];currentIndex:view.appleTarget==="device"?0:1;onActivated:{view.chooseAppleTarget(currentIndex===0?"device":"simulator");view.rememberTarget()}}
                                StudioComboBox {visible:view.appleTarget==="simulator";Layout.preferredWidth:150;model:Object.keys(view.remotes);currentIndex:Object.keys(view.remotes).indexOf(view.remote);displayText:currentText || qsTr("Choose a Mac");onActivated:{view.remote=currentText;view.target="";view.loadApple()}}
                            }
                            StudioComboBox {id:targetPicker;objectName:"targetPicker";Layout.fillWidth:true;Layout.minimumWidth:120;Layout.maximumWidth:360;textRole:"name";valueRole:"id";model:view.visibleTargets;currentIndex:view.visibleTargets.findIndex(x=>x.id===view.target);displayText:currentIndex>=0?currentText:qsTr("Choose a device");onActivated:{view.target=currentValue;view.rememberTarget()}}
                            StudioButton {iconName:"refresh";tip:qsTr("Search for devices again");quiet:true;onClicked:view.refresh()}
                            StudioButton {iconName:"plus";tip:qsTr("Set up another Android model");quiet:true;visible:view.platform==="android";onClicked:workspace.showTab("devices")}
                            StudioButton {visible:view.platform==="ios" && !view.compact;text:view.appleTarget==="device"?qsTr("Set up iPhone"):qsTr("Connect Mac");quiet:true;onClicked:{if(view.appleTarget==="device"){iphoneSetup.open();view.diagnoseIos()}else macSetup.open()}}
                            Item {Layout.fillWidth:true}
                            StudioButton {text:view.connecting?qsTr("Connecting…"):view.targetIsLive?qsTr("Connected"):qsTr("Connect");displayText:view.tight?"":text;tip:view.tight?text:"";iconName:view.targetIsLive?"check":"phone";selected:view.targetIsLive;enabled:view.hasTarget && !view.connecting && !view.targetIsLive;onClicked:view.connectTarget()}
                            StudioButton {objectName:"runAppButton";text:view.appRunning?"Hot Reload":view.sessionRunning?qsTr("Project busy"):view.launchJob || view.launchPending?qsTr("Build running…"):qsTr("Run app");displayText:view.tight?"":text;tip:view.tight?text:"";iconName:view.appRunning?"refresh":"play";accent:true;visible:!!view.projectPath && (view.platform!=="ios" || view.appleTarget==="device");enabled:view.appRunning || (view.hasTarget && !view.launchJob && !view.launchPending && !view.pendingLaunch && !view.connecting);onClicked:if(view.appRunning)view.request("job.send",{job_id:view.launchJob,command:"reload"});else view.launch()}
                            StudioButton {text:qsTr("Stop");displayText:view.tight?"":text;tip:view.tight?text:"";iconName:"stop";visible:!!view.launchJob;onClicked:view.request("job.cancel",{job_id:view.launchJob})}
                        }
                        RowLayout {
                            visible:view.platform==="ios" && view.compact;Layout.fillWidth:true;spacing:Theme.gapXs
                            StudioComboBox {Layout.preferredWidth:170;model:[qsTr("iPhone via USB"),qsTr("Simulator on a Mac")];currentIndex:view.appleTarget==="device"?0:1;onActivated:{view.chooseAppleTarget(currentIndex===0?"device":"simulator");view.rememberTarget()}}
                            StudioComboBox {visible:view.appleTarget==="simulator";Layout.fillWidth:true;model:Object.keys(view.remotes);currentIndex:Object.keys(view.remotes).indexOf(view.remote);displayText:currentText || qsTr("Choose a Mac");onActivated:{view.remote=currentText;view.target="";view.loadApple()}}
                            Item {visible:view.appleTarget!=="simulator";Layout.fillWidth:true}
                            StudioButton {text:view.appleTarget==="device"?qsTr("Set up iPhone"):qsTr("Connect Mac");quiet:true;onClicked:{if(view.appleTarget==="device"){iphoneSetup.open();view.diagnoseIos()}else macSetup.open()}}
                        }
                    }
                    Rectangle {anchors.bottom:parent.bottom;width:parent.width;height:1;color:Theme.subtle}
                }
                RowLayout {
                    Layout.fillWidth:true;Layout.fillHeight:true;spacing:0
                    // The stage: the device and nothing else.
                    // The device and, when asked, a log beside it; the handle between them resizes.
                    SplitView {
                        id:stageSplit;objectName:"stageSplit"
                        Layout.fillWidth:true;Layout.fillHeight:true;visible:!view.activityOnly
                        orientation:Qt.Horizontal
                        handle:Rectangle {
                            implicitWidth:Theme.gapXs
                            color:SplitHandle.pressed?Theme.accent:SplitHandle.hovered?Theme.border:Theme.subtle
                        }
                        // The device with its own controls beside it.
                        RowLayout {
                            SplitView.fillWidth:true;SplitView.minimumWidth:Theme.gapXxxl*4;spacing:0
                            Item {
                                id:frameDock
                                Layout.fillWidth:true;Layout.fillHeight:true;clip:true
                                NativeDeviceFrame {
                                    id:nativeFrame;objectName:"nativeDeviceFrame";anchors.centerIn:parent
                                    parent:view.detached?deviceWindow.host:frameDock
                                    onBezelPressed:if(view.detached)deviceWindow.move()
                                    profile:view.pending?(view.pendingProfile.id?view.pendingProfile:view.genericProfile):view.frameProfile
                                    sourceSize:view.pending?Qt.size(0,0):display.sourceSize
                                    framed:view.pending || (view.showDeviceFrame && view.hasFrame);finishIndex:view.frameFinish;unfolded:!view.pending && view.frameMatch.unfolded
                                    backgroundColor:view.detached?"transparent":Theme.background
                                    roundedScreen:view.detached
                                    scale:view.detached?deviceWindow.zoom:Math.max(.01,Math.min((frameDock.width-Theme.gapXl)/width,(frameDock.height-Theme.gapLg)/height))
                                    visible:view.deviceShown
                                    onHardwareKey:key=>view.input({action:"key",key:key})
                                    rearView:view.detached && deviceWindow.flipped
                                    Rectangle {
                                        objectName:"nativeBootScreen"
                                        parent:nativeFrame.screen;anchors.fill:parent;z:1;visible:view.pending;color:Theme.deviceScreen
                                        MouseArea {anchors.fill:parent;onPressed:if(view.detached)deviceWindow.move()}
                                        Column {
                                            anchors.centerIn:parent;width:parent.width-Theme.gapXxxl;spacing:Theme.gapMd
                                            BrandMark {anchors.horizontalCenter:parent.horizontalCenter;size:Theme.gapXxxl*3}
                                            StudioHeading {width:parent.width;horizontalAlignment:Text.AlignHCenter;text:view.pendingLabel;color:Theme.deviceScreenText;size:Theme.fontDisplay;wrapMode:Text.NoWrap;elide:Text.ElideMiddle}
                                            Text {width:parent.width;horizontalAlignment:Text.AlignHCenter;text:view.pendingTarget || !view.pendingAvd?qsTr("Connecting the picture…"):qsTr("Android is starting…");color:Theme.deviceScreenMuted;font.pixelSize:Theme.fontBody}
                                            // Starting takes a while with no measure of how far along it is, so
                                            // this says only that it is still going.
                                            Item {
                                                width:parent.width;height:Theme.gapXxs;clip:true
                                                Rectangle {anchors.fill:parent;color:Theme.deviceScreenMuted;opacity:.25}
                                                Rectangle {
                                                    width:parent.width/3;height:parent.height;color:Theme.accent
                                                    XAnimator on x {from:-parent.width/3;to:parent.width;duration:1400;loops:Animation.Infinite;running:view.pending && !Theme.reducedMotion}
                                                }
                                            }
                                        }
                                    }
                                    NativeView {id:display;parent:nativeFrame.screen;objectName:"nativeDisplay";anchors.fill:parent;session:view.live;onInputError:message=>{view.notice=message;view.failed=true}}
                                    // Touches and coordinates, for writing UI tests. Only passive
                                    // handlers: every press still reaches the display below.
                                    Item {
                                        id:touchOverlay;objectName:"nativeTouchOverlay"
                                        parent:nativeFrame.screen;anchors.fill:parent;z:2
                                        visible:view.showTouches && display.state==="live" && !view.pending
                                        readonly property rect area:display.contentRect
                                        // The device pixel and fraction under a point of this item.
                                        function at(px,py) {
                                            const r=area,w=display.sourceSize.width,h=display.sourceSize.height
                                            const fx=Math.max(0,Math.min(1,(px-r.x)/r.width)),fy=Math.max(0,Math.min(1,(py-r.y)/r.height))
                                            return {x:Math.min(w-1,Math.floor(fx*w)),y:Math.min(h-1,Math.floor(fy*h)),fx:fx,fy:fy}
                                        }
                                        readonly property bool pointing:hover.hovered || touchPoint.active
                                        readonly property point pointer:touchPoint.active?touchPoint.point.position:hover.point.position
                                        readonly property var spot:at(pointer.x,pointer.y)
                                        readonly property string label:spot.x+", "+spot.y+"  ·  "+spot.fx.toFixed(3)+", "+spot.fy.toFixed(3)
                                        HoverHandler {id:hover}
                                        PointHandler {
                                            id:touchPoint;acceptedButtons:Qt.LeftButton
                                            onActiveChanged:if(active)touchMark.show();else touchMark.release()
                                        }
                                        Rectangle {
                                            id:touchMark;objectName:"nativeTouchMark"
                                            // Scaled with the device, so it stays finger-sized on screen.
                                            readonly property real size:34/Math.max(.01,nativeFrame.scale)
                                            width:size;height:size;radius:size/2;visible:opacity>0;opacity:0
                                            x:touchOverlay.pointer.x-width/2;y:touchOverlay.pointer.y-height/2
                                            color:"#40ffffff";border.color:"#b0ffffff";border.width:1.5/Math.max(.01,nativeFrame.scale)
                                            function show(){fade.stop();opacity=1}
                                            function release(){fade.restart()}
                                            NumberAnimation {id:fade;target:touchMark;property:"opacity";to:0;duration:Theme.reducedMotion?0:420}
                                        }
                                        Rectangle {
                                            objectName:"nativeTouchCoordinates"
                                            visible:touchOverlay.pointing
                                            readonly property real unit:1/Math.max(.01,nativeFrame.scale)
                                            scale:unit;transformOrigin:Item.TopLeft
                                            width:coordinateText.implicitWidth+Theme.gapSm*2;height:coordinateText.implicitHeight+Theme.gapXs*2
                                            // Beside the pointer, kept inside the screen.
                                            x:Math.max(0,Math.min(touchOverlay.width-width*unit,touchOverlay.pointer.x+Theme.gapMd*unit))
                                            y:Math.max(0,Math.min(touchOverlay.height-height*unit,touchOverlay.pointer.y+Theme.gapMd*unit))
                                            radius:Theme.radiusSmall;color:Theme.ink;opacity:.88
                                            Text {id:coordinateText;objectName:"nativeTouchCoordinatesText";anchors.centerIn:parent;text:touchOverlay.label;color:Theme.paper;font.family:Theme.mono;font.pixelSize:Theme.fontSmall}
                                        }
                                    }
                                }
                                // On a wide stage the room beside the phone says what it is and what is happening.
                                ColumnLayout {
                                    objectName:"stageCaption"
                                    readonly property real spare:(frameDock.width-nativeFrame.width*nativeFrame.scale)/2
                                    visible:!view.detached && view.deviceShown && spare>=260
                                    x:Theme.gapXl;y:Theme.gapLg;width:spare-Theme.gapXxxl;spacing:Theme.gapXs
                                    StudioHeading {Layout.fillWidth:true;text:view.deviceName;size:Math.min(Theme.fontGiant,Math.max(Theme.fontDisplay,parent.width/9))}
                                    Text {Layout.fillWidth:true;visible:!!view.deviceMetrics;text:view.deviceMetrics;color:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontSmall}
                                }
                                ColumnLayout {
                                    objectName:"stageJob"
                                    readonly property real spare:(frameDock.width-nativeFrame.width*nativeFrame.scale)/2
                                    visible:!view.detached && view.deviceShown && spare>=260 && view.runningJobs.length>0
                                    x:Theme.gapXl;anchors.bottom:parent.bottom;anchors.bottomMargin:Theme.gapLg;width:spare-Theme.gapXxxl;spacing:Theme.gapXs
                                    StudioStatus {tone:"busy";mono:true;text:view.runningJobs.length?view.workspace.jobStateLabel(view.runningJobs[0]):""}
                                    StudioHeading {Layout.fillWidth:true;text:view.runningJobs.length?view.runningJobs[0].label:"";size:Theme.fontDisplay;maximumLineCount:3;elide:Text.ElideRight}
                                }
                                AppDropArea {
                                    objectName:"nativeAppDrop";anchors.fill:parent;z:2;enabled:!view.detached
                                    kinds:view.installKinds;targetName:view.deviceName;refusal:view.installRefusal
                                    onAppsDropped:paths=>view.installApps(paths)
                                    onRefused:reason=>{view.notice=reason;view.failed=true}
                                }
                                StudioEmptyState {
                                    objectName:"nativeDetachedNotice";actionName:"nativeDockButton"
                                    anchors.centerIn:parent;width:Math.min(parent.width-Theme.gapXxxl,480);visible:view.detached && view.deviceShown
                                    title:qsTr("Your device is in its own window.")
                                    body:qsTr("Drag it by its body. Ctrl+wheel resizes it. Close it to bring it back.")
                                    actionText:qsTr("Bring it back");actionIcon:"dock"
                                    onAction:view.detached=false
                                }
                                DeviceHero {
                                    objectName:"nativeEmptyState"
                                    anchors.fill:parent;visible:!view.deviceShown
                                    title:view.connecting?qsTr("Connecting"):view.platform==="ios" && !view.hasTarget?(view.appleTarget==="device"?qsTr("Plug in your iPhone."):qsTr("Connect a Mac.")):qsTr("Give your agent a phone.")
                                    body:view.platform==="ios" && !view.hasTarget?(view.appleTarget==="device"?qsTr("Plug it in and unlock it. Build Swift apps on Linux and control them from here."):qsTr("The Apple simulator needs a Mac with Xcode. Yours or a rented one, over SSH.")):qsTr("It builds, runs, taps through and debugs iOS and Android apps. On Linux. No Mac needed.")
                                    actionText:view.hasTarget?qsTr("Connect %1").arg(view.deviceName):view.platform==="ios"?(view.appleTarget==="device"?qsTr("Set up iPhone"):qsTr("Set up Mac")):qsTr("Add a device")
                                    errorText:display.error
                                    secondaryText:view.projectPath?"":qsTr("Open a project")
                                    onSecondary:projectFolder.open()
                                    recent:view.recentProjects
                                    onRecentChosen:path=>view.workspace.openProject(path)
                                    steps:[
                                        {key:"project",label:qsTr("Project"),detail:workspace.project.name || qsTr("Open a project"),done:!!view.projectPath},
                                        {key:"device",label:qsTr("Device"),detail:view.deviceName || qsTr("Choose a device"),done:view.hasTarget},
                                        {key:"run",label:qsTr("Run"),detail:qsTr("Run app"),done:view.appRunning}
                                    ]
                                    onAction:{if(view.hasTarget)view.connectTarget();else if(view.platform==="ios"){if(view.appleTarget==="device"){iphoneSetup.open();view.diagnoseIos()}else macSetup.open()}else workspace.showTab("devices")}
                                    onStepChosen:key=>{if(key==="project")projectFolder.open();else if(key==="device")workspace.showTab("devices");else if(view.projectPath && view.hasTarget)view.launch();else commandPalette.open()}
                                }
                            }
                            // The device's own controls, out of the picture's way.
                            Rectangle {
                                objectName:"deviceTools";visible:(display.state==="live" || !!view.live.session) && !view.activityOnly
                                Layout.preferredWidth:Theme.controlHeight+Theme.gapSm*2;Layout.fillHeight:true;color:Theme.surface
                                Rectangle {width:1;height:parent.height;color:Theme.subtle}
                                Column {
                                    anchors.horizontalCenter:parent.horizontalCenter;y:Theme.gapSm;spacing:Theme.gapXxs
                                    StudioButton {text:qsTr("Back");displayText:"";iconName:"back";quiet:true;tip:text;visible:display.state==="live" && view.live.platform!=="ios";onClicked:view.input({action:"key",key:"back"})}
                                    StudioButton {text:"Home";displayText:"";iconName:"home";quiet:true;tip:text;visible:display.state==="live";onClicked:view.input({action:"key",key:"home"})}
                                    StudioButton {text:qsTr("Rotate");displayText:"";iconName:"rotate";quiet:true;tip:text;visible:display.state==="live";onClicked:view.input({action:"rotate",orientation:display.sourceSize.width>display.sourceSize.height?"portrait":"landscape_left"})}
                                    StudioButton {objectName:"nativeScreenshot";text:"Screenshot";displayText:"";iconName:"camera";quiet:true;tip:qsTr("Save and copy a screenshot")+" · "+qsTr("Ctrl")+"+Shift+S";visible:display.state==="live";onClicked:view.capture()}
                                    StudioButton {objectName:"nativeRecord";text:recorder.recording?qsTr("Stop recording"):qsTr("Record the screen");displayText:"";iconName:recorder.recording?"stop":"record";quiet:!recorder.recording;danger:recorder.recording;selected:recorder.recording;tip:recorder.recording?qsTr("Stop and save the recording · %1 s").arg(recorder.elapsed):qsTr("Record the screen to a video, at most 3 minutes");visible:display.state==="live" && view.live.platform==="android";enabled:!recorder.pending && !recorder.stopping;onClicked:view.toggleRecording()}
                                    StudioButton {text:qsTr("Enter text");displayText:"";iconName:"keyboard";quiet:true;tip:text;visible:display.state==="live";onClicked:textEntry.open()}
                                    StudioButton {objectName:"nativeDetachButton";text:view.detached?qsTr("Dock"):qsTr("Own window");displayText:"";iconName:view.detached?"dock":"window";quiet:true;selected:view.detached;tip:view.detached?text:qsTr("Detach the device into its own window");visible:display.state==="live";onClicked:view.detached=!view.detached}
                                    StudioButton {objectName:"stageLogToggle";text:qsTr("Log beside the device");displayText:"";iconName:"jobs";quiet:true;selected:view.showStageLog;tip:view.showStageLog?qsTr("Hide the log beside the device"):qsTr("Show the device's or a job's log beside the device");visible:display.state==="live";onClicked:view.showStageLog=!view.showStageLog}
                                    Rectangle {width:Theme.controlHeight;height:1;color:Theme.subtle;visible:display.state==="live"}
                                    StudioButton {objectName:"nativeAudioToggle";text:view.audioPending?qsTr("Sound…"):view.liveAudio.error?qsTr("Sound error"):qsTr("Sound");displayText:"";iconName:view.liveAudio.enabled?"volume":"muted";selected:view.liveAudio.enabled;quiet:true;visible:view.live.platform==="android" && !!view.live.session;enabled:!view.audioPending && view.live.status==="connected";tip:view.liveAudio.error || (view.liveAudio.enabled?qsTr("Stop forwarding sound; restore output on the device"):qsTr("Play the app's sound on this computer"));onClicked:view.requestAudio(!view.liveAudio.enabled)}
                                    StudioButton {objectName:"nativeAudioLog";iconName:"jobs";quiet:true;danger:true;visible:!!view.liveAudio.error && !!view.liveAudio.job_id;tip:qsTr("View the sound forwarding error");onClicked:workspace.request("job.get",{job_id:view.liveAudio.job_id})}
                                    StudioButton {objectName:"nativeTouchToggle";text:qsTr("Touches and coordinates");displayText:"";iconName:"testing";selected:view.showTouches;quiet:true;visible:display.state==="live";tip:view.showTouches?qsTr("Hide touches and pointer coordinates"):qsTr("Show touches and pointer coordinates, for writing UI tests");onClicked:view.showTouches=!view.showTouches}
                                    StudioButton {objectName:"nativeFrameToggle";text:qsTr("Frame");displayText:"";iconName:"frame";selected:view.showDeviceFrame;quiet:true;visible:view.hasFrame;tip:view.frameMatch.source==="display-match"?qsTr("Reference frame recognized from the display size; the frame geometry is approximated"):qsTr("Show the device frame; Android provides the real status bar");onClicked:view.showDeviceFrame=!view.showDeviceFrame}
                                    StudioButton {text:qsTr("Color");displayText:"";iconName:"color";quiet:true;visible:view.hasFrame && view.showDeviceFrame && view.frameProfile.colors.length>1;tip:view.frameProfile.colorNames?ColorNames.name(view.frameProfile.colorNames[view.frameFinish]):qsTr("Case color");onClicked:view.frameFinish=(view.frameFinish+1)%view.frameProfile.colors.length}
                                    Rectangle {width:Theme.controlHeight;height:1;color:Theme.subtle;visible:!!view.live.session}
                                    StudioButton {text:qsTr("Disconnect");displayText:"";iconName:"unplug";quiet:true;tip:text;visible:!!view.live.session;onClicked:view.stop()}
                                }
                            }
                        }
                        StageLogPane {
                            id:stageLog
                            visible:view.showStageLog
                            SplitView.preferredWidth:view.stageLogWidth
                            SplitView.minimumWidth:Theme.panelWidth*.75
                            SplitView.maximumWidth:Math.max(Theme.panelWidth*.75,stageSplit.width-Theme.gapXxxl*4)
                            workspace:view.workspace
                            device:display.state==="live" ? (view.live.device || "") : ""
                            platform:view.live.platform || view.platform
                            launchJob:view.launchJob
                            mode:view.stageLogMode
                            onModeChanged:view.stageLogMode=mode
                            onCloseRequested:view.showStageLog=false
                            onWidthChanged:if(stageSplit.resizing && visible)view.stageLogWidth=Math.round(width)
                        }
                    }
                    ActivityPanel {
                        objectName:"activityPanel";visible:view.activityVisible
                        Layout.preferredWidth:Theme.panelWidth;Layout.fillWidth:view.activityOnly;Layout.fillHeight:true
                        workspace:view.workspace
                        notifications:notifications
                        onNotificationChosen:entry=>view.openNotification(entry)
                        sessionLog:view.sessionLogPath
                        stateLabel:view.deviceStateLabel;stateTone:view.deviceTone
                        deviceName:view.deviceName;deviceMetrics:view.deviceMetrics
                    }
                }
            }
        }
        Rectangle {
            Layout.fillWidth:true;Layout.preferredHeight:view.notice?noticeLabel.implicitHeight+Theme.gapMd*2:0;visible:!!view.notice;color:view.failed?Theme.dangerSurface:Theme.elevated
            Rectangle {width:Theme.gapXxs;height:parent.height;color:view.failed?Theme.danger:Theme.accent}
            RowLayout {anchors.fill:parent;anchors.leftMargin:Theme.gapLg;anchors.rightMargin:Theme.gapSm;spacing:Theme.gapSm
                Text {id:noticeLabel;Layout.fillWidth:true;text:view.notice;color:view.failed?Theme.danger:Theme.text;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap;maximumLineCount:4;elide:Text.ElideRight}
                StudioButton {objectName:"noticeRetry";text:qsTr("Try again");iconName:"refresh";visible:view.failed && !!view.noticeRetry;onClicked:view.retryNotice()}
                StudioButton {text:qsTr("Open job");quiet:true;visible:!!view.launchJob;onClicked:workspace.showTab("jobs")}
                StudioButton {iconName:"close";quiet:true;tip:qsTr("Dismiss notice");onClicked:view.notice=""}
            }
        }
        // The status line: one block that says what the device is doing, then the facts, in mono.
        Rectangle {
            Layout.fillWidth:true;Layout.preferredHeight:Theme.statusHeight;color:Theme.surface
            Rectangle {width:parent.width;height:1;color:Theme.subtle}
            RowLayout {anchors.fill:parent;anchors.topMargin:1;anchors.rightMargin:Theme.gapMd;spacing:Theme.gapMd
                Rectangle {
                    objectName:"deviceStateBlock"
                    Layout.fillHeight:true;Layout.preferredWidth:stateWord.implicitWidth+Theme.gapLg
                    color:view.deviceTone==="live"?Theme.accent:view.deviceTone==="failed"?Theme.danger:view.deviceTone==="busy"?Theme.warning:Theme.elevated
                    Text {id:stateWord;anchors.centerIn:parent;text:view.deviceStateLabel;color:["live","failed","busy"].indexOf(view.deviceTone)>=0?Theme.accentText:Theme.muted;font.family:Theme.mono;font.pixelSize:Theme.fontMicro;font.weight:Font.Bold}
                }
                Text {visible:!!view.deviceName;text:view.deviceName+(view.deviceMetrics?" · "+view.deviceMetrics:"");color:Theme.muted;font.family:Theme.mono;font.pixelSize:Theme.fontMicro;Layout.maximumWidth:view.width*.4;elide:Text.ElideRight}
                Text {text:workspace.opened && workspace.status ? workspace.status : workspace.project.path || qsTr("No project open");Layout.fillWidth:true;elide:Text.ElideMiddle;color:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontMicro}
                UpdateNotice {model:updates;onOpened:settingsDialog.open()}
                StudioStatus {objectName:"statusJobs";visible:view.runningJobs.length>0;tone:"busy";mono:true;size:Theme.fontMicro;text:qsTr("%n job(s) running","",view.runningJobs.length)}
                Text {visible:view.width>=760;text:qsTr("Ctrl+K search · F1 keys");color:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontMicro}
            }
        }
    }
    CommandPalette {id:commandPalette;workspace:view.workspace;navigation:view.navigation;commands:view.commands;onNavigate:section=>view.navigate(section)}
    FolderDialog {id:projectFolder;title:qsTr("Open project");onAccepted:workspace.openProject(decodeURIComponent(selectedFolder.toString().replace(/^file:\/\//,"")))}
    // The settings' search: a section shows when every word typed appears in
    // its title, its options or its explanations.
    property string settingsQuery: ""
    // What the search holds when the dialog opens, for commands that open one section.
    property string settingsOpenQuery: ""
    function openSettings(query) {settingsOpenQuery=query || "";settingsDialog.open()}
    function settingShown(words) {
        const query=settingsQuery.trim().toLowerCase()
        if(!query)return true
        const text=words.join(" ").toLowerCase()
        return query.split(/\s+/).every(word=>text.indexOf(word)>=0)
    }
    StudioDialog {
        id:settingsDialog;objectName:"settingsDialog";title:qsTr("Settings");anchors.centerIn:parent;width:Math.min(view.width-Theme.gapXxxl,460);height:Math.min(view.height-Theme.gapLg,implicitHeight);modal:true;standardButtons:Dialog.Close
        onOpened:{view.settingsTransfer="";settingsSearch.text=view.settingsOpenQuery;view.settingsOpenQuery="";Studio.reloadOmarchyTheme();settingsSearch.forceActiveFocus()}
        readonly property bool nothingShown:!appearanceSection.visible && !languageSection.visible && !notificationsSection.visible && !agentSection.visible && !updatesSection.visible && !layoutsSection.visible && !transferSection.visible
        contentItem:ColumnLayout {spacing:Theme.gapMd
            StudioTextField {
                id:settingsSearch;objectName:"settingsSearch";Layout.fillWidth:true;Layout.rightMargin:Theme.gapMd
                placeholderText:qsTr("Search settings");Accessible.name:qsTr("Search settings")
                onTextChanged:view.settingsQuery=text
                // Esc empties the search first, then closes the dialog.
                Keys.onEscapePressed:event=>{if(text){text="";event.accepted=true}else{event.accepted=false;settingsDialog.close()}}
                Keys.onDownPressed:nextItemInFocusChain().forceActiveFocus()
            }
            StudioScrollView {Layout.fillWidth:true;Layout.fillHeight:true;Layout.preferredHeight:settingsColumn.implicitHeight;clip:true;contentWidth:availableWidth;ColumnLayout {id:settingsColumn;width:parent.width-Theme.gapMd;spacing:Theme.gapLg
            Text {objectName:"settingsNoMatch";Layout.fillWidth:true;visible:settingsDialog.nothingShown;text:qsTr("No setting matches “%1”.").arg(view.settingsQuery.trim());color:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            ColumnLayout {id:appearanceSection;objectName:"settingsAppearance";Layout.fillWidth:true;spacing:Theme.gapSm
                visible:view.settingShown([qsTr("Appearance"),qsTr("System"),qsTr("Dark"),qsTr("Light"),qsTr("Use the Omarchy theme's accent"),"Omarchy",qsTr("Theme")])
                Text {text:qsTr("Appearance");color:Theme.muted;font.pixelSize:Theme.fontSmall}
                // System follows the desktop's dark or light preference at once.
                StudioSegmented {
                    objectName:"appearancePicker";Layout.fillWidth:true;current:Studio.appearance
                    model:[{id:"system",label:qsTr("System")},{id:"dark",label:qsTr("Dark")},{id:"light",label:qsTr("Light")}]
                    onChosen:id=>Studio.setAppearance(id)
                }
                // The active Omarchy theme's accent instead of the brand's red, where it reads clearly.
                StudioCheckBox {objectName:"omarchyAccentToggle";Layout.fillWidth:true;visible:Theme.omarchy.available===true;text:qsTr("Use the Omarchy theme's accent");checked:Studio.followOmarchyAccent;onToggled:Studio.setFollowOmarchyAccent(checked)}
                RowLayout {
                    Layout.fillWidth:true;visible:Theme.omarchy.available===true && Studio.followOmarchyAccent;spacing:Theme.gapSm
                    Rectangle {Layout.preferredWidth:Theme.iconSize;Layout.preferredHeight:Theme.iconSize;radius:Theme.radiusSmall;color:Theme.omarchy.accent || "transparent";border.color:Theme.border}
                    Text {
                        objectName:"omarchyAccentState";Layout.fillWidth:true;color:Theme.faint;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap
                        text:Theme.omarchyAccentRefused?qsTr("The accent of %1 does not read clearly on this background, so Tomacco keeps its own.").arg(Theme.omarchy.name || ""):qsTr("Accent from the Omarchy theme %1.").arg(Theme.omarchy.name || "")
                    }
                }
            }
            // Interface language: the desktop's language, or one chosen here (stored, applied at once).
            ColumnLayout {id:languageSection;objectName:"settingsLanguage";Layout.fillWidth:true;spacing:Theme.gapSm
                visible:view.settingShown([qsTr("Language"),qsTr("System"),"English Deutsch Français Español Italiano",qsTr("Interface language; System follows the desktop language")])
                Text {text:qsTr("Language");color:Theme.muted;font.pixelSize:Theme.fontSmall}
                StudioComboBox {
                    id:languagePicker;objectName:"languagePicker";Layout.fillWidth:true
                    readonly property var codes:["system","en","de","fr","es","it"]
                    readonly property string choice:Studio.language
                    // Each language names itself, the same in every language.
                    model:[qsTr("System"),"English","Deutsch","Français","Español","Italiano"]
                    function sync() {currentIndex=Math.max(0,codes.indexOf(choice))}
                    onChoiceChanged:sync()
                    onModelChanged:Qt.callLater(sync)
                    Component.onCompleted:sync()
                    onActivated:index=>Studio.setLanguage(codes[index])
                    ToolTip.visible:hovered;ToolTip.delay:550;ToolTip.text:qsTr("Interface language; System follows the desktop language")
                    Accessible.name:qsTr("Language")
                }
            }
            ColumnLayout {id:notificationsSection;objectName:"settingsNotifications";Layout.fillWidth:true;spacing:Theme.gapSm
                visible:view.settingShown([qsTr("Notifications"),qsTr("When a job ends in the background")])
                Text {text:qsTr("Notifications");color:Theme.muted;font.pixelSize:Theme.fontSmall}
                StudioCheckBox {objectName:"jobNotificationsToggle";Layout.fillWidth:true;text:qsTr("When a job ends in the background");checked:view.notifyJobs;onToggled:view.notifyJobs=checked}
            }
            // An agent's session log, read into the activity panel.
            ColumnLayout {id:agentSection;objectName:"settingsAgent";Layout.fillWidth:true;spacing:Theme.gapSm
                visible:view.settingShown([qsTr("Agent activity"),qsTr("Session log, e.g. %1").arg(""),"TOMACCO_SESSION_LOG MCP",qsTr("Set TOMACCO_SESSION_LOG to this file in your agent's MCP settings, and the activity panel lists every operation it calls, with its duration and result.")])
                Text {text:qsTr("Agent activity");color:Theme.muted;font.pixelSize:Theme.fontSmall}
                StudioTextField {
                    objectName:"sessionLogField";Layout.fillWidth:true;font.family:Theme.mono;font.pixelSize:Theme.fontSmall
                    text:view.sessionLogPath;placeholderText:qsTr("Session log, e.g. %1").arg("/home/me/agent-session.jsonl")
                    onEditingFinished:view.sessionLogPath=text.trim()
                }
                Text {Layout.fillWidth:true;text:qsTr("Set TOMACCO_SESSION_LOG to this file in your agent's MCP settings, and the activity panel lists every operation it calls, with its duration and result.");color:Theme.faint;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            }
            UpdateSection {id:updatesSection;objectName:"settingsUpdates";Layout.fillWidth:true;model:updates;visible:view.settingShown(keywords)}
            // Named window layouts: which views and panes are open, how wide, and the device windows.
            ColumnLayout {id:layoutsSection;objectName:"settingsLayouts";Layout.fillWidth:true;spacing:Theme.gapSm
                visible:view.settingShown([qsTr("Window layouts"),qsTr("Save the current layout"),qsTr("A layout keeps the open view, the activity panel, the log beside the device and its width, the device windows and the window's size."),qsTr("Use"),qsTr("Delete")].concat(view.layoutNames))
                Text {text:qsTr("Window layouts");color:Theme.muted;font.pixelSize:Theme.fontSmall}
                Text {Layout.fillWidth:true;text:qsTr("A layout keeps the open view, the activity panel, the log beside the device and its width, the device windows and the window's size.");color:Theme.faint;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
                Repeater {
                    model:view.layoutNames
                    RowLayout {
                        required property string modelData
                        Layout.fillWidth:true;spacing:Theme.gapXs
                        Text {Layout.fillWidth:true;text:modelData;color:Theme.text;font.pixelSize:Theme.fontBody;elide:Text.ElideRight}
                        StudioButton {objectName:"useLayout-"+modelData;text:qsTr("Use");quiet:true;Accessible.name:qsTr("Use window layout %1").arg(modelData);onClicked:{view.applyLayout(modelData);settingsDialog.close()}}
                        StudioButton {objectName:"deleteLayout-"+modelData;iconName:"close";quiet:true;tip:qsTr("Delete");Accessible.name:qsTr("Delete window layout %1").arg(modelData);onClicked:view.deleteLayout(modelData)}
                    }
                }
                RowLayout {Layout.fillWidth:true;spacing:Theme.gapXs
                    StudioTextField {id:layoutName;objectName:"settingsLayoutName";Layout.fillWidth:true;placeholderText:qsTr("Name, e.g. testing or debugging");onAccepted:if(text.trim()){view.saveLayout(text);text=""}}
                    StudioButton {objectName:"settingsSaveLayout";text:qsTr("Save the current layout");quiet:true;enabled:!!layoutName.text.trim();onClicked:{view.saveLayout(layoutName.text);layoutName.text=""}}
                }
            }
            // Moving to another computer: the preferences as a JSON file.
            ColumnLayout {id:transferSection;objectName:"settingsTransfer";Layout.fillWidth:true;spacing:Theme.gapSm
                visible:view.settingShown([qsTr("Another computer"),qsTr("Appearance, language, notifications, updates and the device view's choices, as a JSON file. Paths, window sizes and history stay on this computer."),qsTr("Export settings…"),qsTr("Import settings…")])
                Text {text:qsTr("Another computer");color:Theme.muted;font.pixelSize:Theme.fontSmall}
                Text {Layout.fillWidth:true;text:qsTr("Appearance, language, notifications, updates and the device view's choices, as a JSON file. Paths, window sizes and history stay on this computer.");color:Theme.faint;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
                Flow {Layout.fillWidth:true;spacing:Theme.gapSm
                    StudioButton {objectName:"exportSettingsButton";text:qsTr("Export settings…");quiet:true;onClicked:settingsExport.open()}
                    StudioButton {objectName:"importSettingsButton";text:qsTr("Import settings…");quiet:true;onClicked:settingsImport.open()}
                }
                Text {objectName:"settingsTransferResult";Layout.fillWidth:true;visible:!!text;text:view.settingsTransfer;color:view.settingsTransferFailed?Theme.danger:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            }
        }}}
    }
    FileDialog {id:settingsExport;title:qsTr("Export settings");fileMode:FileDialog.SaveFile;defaultSuffix:"json";nameFilters:[qsTr("Tomacco settings (*.json)")];onAccepted:view.exportSettingsTo(decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,"")))}
    FileDialog {id:settingsImport;title:qsTr("Import settings");nameFilters:[qsTr("Tomacco settings (*.json)")];onAccepted:view.importSettingsFrom(decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,"")))}
    OnboardingSheet {id:onboarding;anchors.centerIn:parent;width:Math.min(view.width-Theme.gapXxxl,560);height:Math.min(view.height-Theme.gapLg,implicitHeight)}
    StudioDialog {
        id:whatsNew;objectName:"whatsNewSheet";title:qsTr("What's new in %1").arg(Qt.application.version);anchors.centerIn:parent;width:Math.min(view.width-Theme.gapXxxl,620);height:Math.min(view.height-Theme.gapLg,implicitHeight);modal:true;standardButtons:Dialog.Close
        property string highlights:""
        onAboutToShow:highlights=Studio.releaseHighlights(Qt.application.version)
        contentItem:StudioScrollView {clip:true;contentWidth:availableWidth;ColumnLayout {width:parent.width-Theme.gapMd;spacing:Theme.gapMd
            Text {
                objectName:"whatsNewText";Layout.fillWidth:true;visible:!!whatsNew.highlights;text:whatsNew.highlights;textFormat:Text.MarkdownText
                color:Theme.text;font.pixelSize:Theme.fontBody;wrapMode:Text.WordWrap;lineHeight:1.3
            }
            Text {Layout.fillWidth:true;visible:!whatsNew.highlights;text:qsTr("Tomacco is now version %1. The release notes list what changed.").arg(Qt.application.version);color:Theme.muted;font.pixelSize:Theme.fontBody;wrapMode:Text.WordWrap}
            Flow {Layout.fillWidth:true;spacing:Theme.gapXs
                StudioButton {text:qsTr("Release notes");quiet:true;visible:!!Studio.documentPath("RELEASE_NOTES.md");onClicked:Studio.openDocument("RELEASE_NOTES.md")}
                StudioButton {text:qsTr("On GitHub ↗");quiet:true;tip:Studio.sourceUrl.toString();onClicked:Studio.openExternal(Studio.sourceUrl)}
            }
        }}
    }
    StudioDialog {
        id:shortcutsDialog;objectName:"shortcutsDialog";title:qsTr("Keyboard shortcuts");anchors.centerIn:parent;width:Math.min(view.width-Theme.gapXxxl,520);height:Math.min(view.height-Theme.gapXxxl,implicitHeight);modal:true;standardButtons:Dialog.Close
        contentItem:StudioScrollView {
            clip:true;contentWidth:availableWidth
            GridLayout {
                width:parent.width;columns:2;columnSpacing:Theme.gapXl;rowSpacing:Theme.gapSm
                Repeater {
                    model:[["Ctrl+K",qsTr("Search views, commands and tools")],["F1",qsTr("Keyboard shortcuts")],["Ctrl+O",qsTr("Open project")],["Ctrl+J",qsTr("Show or hide activity")],["Ctrl+,",qsTr("Settings")],["Ctrl+S",qsTr("Save the current file")],["Ctrl+Shift+S",qsTr("Save and copy a device screenshot")],["Ctrl+Shift+F",qsTr("Search the project's source code")],["F11",qsTr("Full screen · web preview")]].concat(view.navigation.map(x=>["Ctrl+"+x.key,x.label])).map(x=>[x[0].replace("Ctrl",qsTr("Ctrl")),x[1]])
                    delegate:Item {
                        required property var modelData
                        required property int index
                        Layout.columnSpan:2;Layout.fillWidth:true;implicitHeight:Theme.controlSmall
                        Text {anchors.verticalCenter:parent.verticalCenter;width:130;text:parent.modelData[0];color:Theme.text;font.family:Theme.mono;font.pixelSize:Theme.fontSmall}
                        Text {x:130;anchors.verticalCenter:parent.verticalCenter;width:parent.width-130;text:parent.modelData[1];color:Theme.muted;font.pixelSize:Theme.fontBody;elide:Text.ElideRight}
                    }
                }
            }
        }
    }
    StudioDialog {
        id:textEntry;anchors.centerIn:parent;width:Math.min(view.width-Theme.gapXxxl,520);modal:true;title:view.live.platform==="ios"?qsTr("Replace text field"):qsTr("Insert text");standardButtons:Dialog.Ok | Dialog.Cancel
        contentItem:ColumnLayout {spacing:Theme.gapMd
            Label {Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;text:view.live.platform==="ios"?qsTr("First click the text field you want in the app. Its entire accessible value is replaced, umlauts and emoji included."):qsTr("First click into the app's text field. This text is pasted through the device clipboard.")}
            StudioScrollView {Layout.fillWidth:true;Layout.preferredHeight:150;TextArea {id:typedText;wrapMode:Text.Wrap;selectByMouse:true;placeholderText:qsTr("Text…");color:Theme.text;placeholderTextColor:Theme.faint;selectionColor:Theme.accent;selectedTextColor:Theme.accentText;background:Rectangle {color:Theme.inset;radius:Theme.radiusSmall;border.color:Theme.border}}}
        }
        onAccepted:display.insertText(typedText.text)
    }
    StudioDialog {
        id:iphoneSetup;objectName:"iphoneSetup";title:qsTr("iPhone development without a Mac");anchors.centerIn:parent;width:Math.min(view.width-Theme.gapXl,650);height:Math.min(view.height-Theme.gapXl,810);modal:true;standardButtons:Dialog.Close
        contentItem:StudioScrollView {
            clip:true;contentWidth:availableWidth
            ColumnLayout {width:parent.width-Theme.gapMd;spacing:Theme.gapMd
                Text {Layout.fillWidth:true;text:qsTr("Linux builds your SwiftPM/xtool app. The connected iPhone runs it. Set up the Apple SDK and signing here once.");wrapMode:Text.WordWrap;color:Theme.muted;font.pixelSize:Theme.fontBody;lineHeight:1.3}
                Repeater {model:view.iosStatus.checks
                    ColumnLayout {required property var modelData;Layout.fillWidth:true;spacing:Theme.gapXxs
                        StudioStatus {tone:modelData.ready?"ok":"warn";size:Theme.fontBody;text:({swift:"Swift",xtool:"xtool", "go-ios":qsTr("iPhone services"),iproxy:qsTr("USB picture transfer"),idevicepair:qsTr("Device pairing"),apple_sdk:qsTr("Apple SDK"),apple_auth:qsTr("Apple sign-in"),usbmuxd:qsTr("USB connection"),iphone:"iPhone / iPad",project:qsTr("Project format")}[modelData.name] || modelData.name)}
                        Text {Layout.fillWidth:true;Layout.leftMargin:Theme.gapLg-1;visible:!modelData.ready;text:view.setupRemedy(modelData.name,modelData.remedy);color:Theme.faint;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
                    }
                }
                Flow {Layout.fillWidth:true;Layout.topMargin:Theme.gapXs;spacing:Theme.gapXs
                    StudioButton {text:qsTr("Check again");onClicked:view.diagnoseIos()}
                    StudioButton {text:qsTr("Sign in to Apple");onClicked:view.request("ios.prepare",{action:"login"})}
                    StudioButton {text:qsTr("Download SDK from Apple");onClicked:Qt.openUrlExternally("https://developer.apple.com/download/all/?q=Xcode")}
                    StudioButton {text:qsTr("Import SDK");onClicked:appleSdkFile.open()}
                    StudioButton {text:qsTr("Pair device");enabled:view.hasTarget;onClicked:view.request("ios.prepare",{action:"pair",device:view.target})}
                    StudioButton {text:qsTr("Start iOS tunnel");enabled:view.hasTarget;onClicked:view.request("ios.prepare",{action:"tunnel",device:view.target})}
                    StudioButton {text:qsTr("Load developer image");enabled:view.hasTarget;onClicked:view.request("ios.prepare",{action:"developer-image",device:view.target})}
                    StudioButton {text:qsTr("Download control package");onClicked:view.request("ios.wda_prepare",{})}
                    StudioButton {text:qsTr("Sign & install control");enabled:view.hasTarget;onClicked:view.request("ios.wda_prepare",{device:view.target})}
                }
                Text {Layout.fillWidth:true;text:qsTr("Turn on Developer Mode on the iPhone and confirm the pairing. The Apple sign-in opens a local terminal; passwords are not entered in Tomacco or MCP.");color:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
                StudioTextField {Layout.fillWidth:true;text:view.runnerBundle;placeholderText:qsTr("Installed WebDriverAgent bundle ID");onEditingFinished:view.runnerBundle=text.trim()}
                StudioButton {text:qsTr("Show installed apps");enabled:view.hasTarget;onClicked:{workspace.showAction("ios.apps");workspace.formValues={device:view.target};workspace.open();iphoneSetup.close()}}
                Text {Layout.fillWidth:true;text:qsTr("Signing can change the bundle ID. Then enter the ID of the installed WebDriverAgent runner. A modern local iOS emulator is not included at present.");color:Theme.faint;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
                Text {Layout.fillWidth:true;visible:!!view.notice;text:view.notice;color:view.failed?Theme.danger:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            }
        }
    }
    FileDialog {id:appleSdkFile;title:qsTr("Import the Apple SDK from Xcode");nameFilters:[qsTr("Xcode archive (*.xip)"),qsTr("All files (*)")];onAccepted:{workspace.request("ios.sdk_install",{path:decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,""))});workspace.showTab("jobs");iphoneSetup.close()}}
    StudioDialog {
        id:about;objectName:"aboutDialog";title:qsTr("About Tomacco");anchors.centerIn:parent;width:Math.min(view.width-Theme.gapXxxl,620);height:Math.min(view.height-Theme.gapLg,implicitHeight);modal:true;standardButtons:Dialog.Close
        contentItem:StudioScrollView {clip:true;contentWidth:availableWidth;ColumnLayout {width:parent.width-Theme.gapMd;spacing:Theme.gapMd
            RowLayout {Layout.fillWidth:true;spacing:Theme.gapLg
                Image {source:"qrc:/assets/icon.svg";Layout.preferredWidth:Theme.gapXxxl;Layout.preferredHeight:Theme.gapXxxl;sourceSize:Qt.size(96,96)}
                ColumnLayout {Layout.fillWidth:true;spacing:Theme.gapXs
                    Text {text:Qt.application.name;color:Theme.text;font.pixelSize:Theme.fontTitle;font.weight:Font.DemiBold;font.letterSpacing:Theme.trackingTitle}
                    Text {objectName:"aboutVersion";text:qsTr("Version %1").arg(Qt.application.version || "–");color:Theme.muted;font.family:Theme.mono;font.pixelSize:Theme.fontSmall}
                }
            }
            Text {Layout.fillWidth:true;text:qsTr("Build, run and test native Android and iOS apps on Linux, by hand or with your agents.");color:Theme.muted;font.pixelSize:Theme.fontBody;wrapMode:Text.WordWrap}
            GridLayout {Layout.fillWidth:true;columns:2;columnSpacing:Theme.gapLg;rowSpacing:Theme.gapXs
                Text {text:qsTr("Control program");color:Theme.faint;font.pixelSize:Theme.fontSmall}
                Text {objectName:"aboutCliVersion";Layout.fillWidth:true;text:Agent.cliVersion?"tomaccoctl "+Agent.cliVersion:qsTr("Not available");color:Agent.cliVersion?Theme.text:Theme.danger;font.pixelSize:Theme.fontSmall;elide:Text.ElideRight}
                Text {text:"Qt";color:Theme.faint;font.pixelSize:Theme.fontSmall}
                Text {Layout.fillWidth:true;text:Studio.qtVersion;color:Theme.text;font.pixelSize:Theme.fontSmall}
            }
            Text {Layout.fillWidth:true;visible:!!Agent.cliVersion && !!Qt.application.version && Agent.cliVersion!==Qt.application.version;text:qsTr("The desktop and its control program come from different versions. Install both from the same build.");color:Theme.warning;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            Rectangle {Layout.fillWidth:true;Layout.preferredHeight:1;color:Theme.subtle}
            Text {Layout.fillWidth:true;text:qsTr("Tomacco is free software under the GNU General Public License. Source: GPL-2.0-or-later. Distributed binaries: GPL-3.0-or-later, because they include Apache-2.0 components. It comes with ABSOLUTELY NO WARRANTY.");color:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap;lineHeight:1.3}
            Text {Layout.fillWidth:true;text:qsTr("Third-party components keep their own licenses: THIRD_PARTY_NOTICES.md lists them, and docs/legal.md has further legal notes.");color:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap;lineHeight:1.3}
            Flow {Layout.fillWidth:true;spacing:Theme.gapXs
                StudioButton {text:qsTr("Source code ↗");tip:Studio.sourceUrl.toString();onClicked:Studio.openExternal(Studio.sourceUrl)}
                StudioButton {text:qsTr("Release notes");quiet:true;onClicked:Studio.openDocument("RELEASE_NOTES.md")}
                StudioButton {text:qsTr("License");quiet:true;onClicked:Studio.openDocument("LICENSE")}
                StudioButton {text:qsTr("Third-party notices");quiet:true;onClicked:Studio.openDocument("THIRD_PARTY_NOTICES.md")}
                StudioButton {text:qsTr("Legal notes");quiet:true;onClicked:Studio.openDocument("legal.md")}
                StudioButton {text:qsTr("Sponsor");quiet:true;onClicked:Qt.openUrlExternally("https://github.com/sponsors/marselnenaj")}
            }
        }}
    }
    StudioDialog {
        id:macSetup;title:qsTr("Connect the Apple simulator to Linux");anchors.centerIn:parent;width:Math.min(view.width-Theme.gapXxxl,570);height:Math.min(view.height-Theme.gapLg,implicitHeight);modal:true;standardButtons:Dialog.Cancel
        contentItem:StudioScrollView {clip:true;contentWidth:availableWidth;ColumnLayout {width:parent.width-Theme.gapMd;spacing:Theme.gapMd
            Text {Layout.fillWidth:true;text:qsTr("The Mac needs Xcode with an iOS simulator runtime, SSH access and idb_companion. Your own Mac or a rented one works.");wrapMode:Text.WordWrap;color:Theme.muted;font.pixelSize:Theme.fontSmall}
            StudioTextField {id:macName;Layout.fillWidth:true;placeholderText:qsTr("Connection name, e.g. build-mac")}
            StudioTextField {id:macHost;Layout.fillWidth:true;placeholderText:qsTr("SSH target, e.g. user@mac.local")}
            StudioTextField {id:macDirectory;Layout.fillWidth:true;placeholderText:qsTr("Project directory on the Mac, e.g. /Users/me/Projects/app")}
            Text {Layout.fillWidth:true;text:qsTr("Mac terminal: brew install facebook/fb/idb\nSSH uses your existing keys and the known host identity.");wrapMode:Text.WordWrap;color:Theme.muted;font.family:Theme.mono;font.pixelSize:Theme.fontSmall}
            StudioButton {text:qsTr("Copy companion command");onClicked:Studio.copy("brew install facebook/fb/idb")}
            StudioButton {text:qsTr("Save & load simulators");accent:true;enabled:!!macName.text && !!macHost.text && macDirectory.text.startsWith("/");onClicked:view.request("remote.configure",{name:macName.text,host:macHost.text,directory:macDirectory.text})}
            Text {visible:view.failed;Layout.fillWidth:true;text:view.notice;color:Theme.danger;wrapMode:Text.Wrap;font.pixelSize:Theme.fontSmall}
        }}
    }
}
