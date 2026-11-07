import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "."
import "OperationTitles.js" as OperationTitles
import "LogFilter.js" as LogFilter

Rectangle {
    id: panel
    objectName: "workspacePanel"
    color: Theme.background
    property bool opened: false
    property int currentIndex: 0
    readonly property var sections: [
        {id:"project",tone:"inverse",title:qsTr("Project"),description:qsTr("Open your app, build it and run it on a device.")},
        {id:"devices",tone:"green",title:qsTr("Devices"),description:qsTr("Phones and emulators, ready to run your app.")},
        {id:"jobs",title:qsTr("Builds & jobs"),description:qsTr("Every build and agent job, with its logs and results.")},
        {id:"tools",title:qsTr("Agents & tools"),description:qsTr("Everything your agent can do, as a form you can fill in.")},
        {id:"debug",tone:"red",title:"Debugger",description:qsTr("Pause your app and see what it's doing.")},
        {id:"testing",tone:"inverse",title:qsTr("UI tests"),description:qsTr("Find anything in your running app and tap it.")},
        {id:"profiling",tone:"green",title:"Performance",description:qsTr("See where the time goes and catch regressions.")},
        {id:"files",title:qsTr("Files"),description:qsTr("Search, edit and save your project's files.")},
        {id:"release",tone:"red",title:qsTr("Signing & release"),description:qsTr("Sign it, version it and ship it.")},
        {id:"demo",title:qsTr("Demo Studio"),description:qsTr("Turn a screen recording into a video with a 3D device.")}
    ]
    readonly property string section: sections[currentIndex].id
    // Pages that only matter once they are shown are built then, not at the
    // start: the debugger, UI tests, performance, release and the iOS test
    // system. Once built they stay.
    property var visited: ({})
    function visit(name) { if(!visited[name]) visited=Object.assign({},visited,{[name]:true}) }
    // The index, not section: that binding may not have followed yet.
    onCurrentIndexChanged: if(opened) visit(sections[currentIndex].id)
    readonly property var iosResearch: iosResearchLoader.item
    // Before the iOS test system is built: what its state says about a phone nobody showed.
    readonly property var idleIosResearch: ({guest:{running:false,detached:false,detach_pending:false,window_visible:false},virtual_phone:{},visible:false,job_id:"",job_state:"",report_directory:"",guest_observations_verified:false,graphical_apps_verified:false})
    function open() { opened=true }
    function close() { opened=false }
    property var project: ({})
    // Every operation, converted from the control program's list once; each
    // read of Agent.operations would convert all of them again.
    readonly property var operations: Agent.operations
    property var savedWorkspaces: ({})
    property string revealPath: ""
    property int revealLine: 1
    function revealFile(file,line) { revealPath=file;revealLine=line || 1;if(project.path && project.path===projectPath.text) { fileWorkspace.openFile(file,revealLine);revealPath="" } }
    property var devices: []
    property var jobs: []
    property bool projectRequestPending: false
    readonly property var projectJob: jobs.find(x=>x.plan && ["queued","running"].indexOf(x.state)>=0 && (x.plan.exclusive===project.path || x.plan.cwd===project.path)) || ({})
    function jobStateLabel(job) {return ({waiting:qsTr("Waiting for project"),building:qsTr("Building"),streaming:qsTr("Streaming sound"),ready:qsTr("App running"),session:qsTr("App session running"),stopping:qsTr("Stopping")})[job.phase] || ({queued:qsTr("Waiting"),running:qsTr("Running"),succeeded:qsTr("Completed"),failed:qsTr("Failed"),cancelled:qsTr("Ended"),timed_out:qsTr("Timed out")})[job.state] || job.state}
    property var activeJob: ({})
    // The jobs list shows all jobs, the running ones or the ones that failed.
    property string jobFilter: "all"
    readonly property var shownJobs: jobs.filter(x=>jobFilter==="all" || (jobFilter==="running"?["queued","running"].indexOf(x.state)>=0:["failed","timed_out"].indexOf(x.state)>=0))
    property real now: Date.now()/1000
    Timer { interval: 1000; repeat: true; triggeredOnStart: true; running: panel.opened && panel.section==="jobs"; onTriggered: panel.now=Date.now()/1000 }
    function jobDuration(job) {
        const end=["queued","running"].indexOf(job.state)>=0 ? now : (job.updated || now)
        const s=Math.max(0,Math.round(end-(job.created || end)))
        if(s<60) return qsTr("%1 s").arg(s)
        if(s<3600) return qsTr("%1 min %2 s").arg(Math.floor(s/60)).arg(s%60)
        return qsTr("%1 h %2 min").arg(Math.floor(s/3600)).arg(Math.floor(s%3600/60))
    }
    // The project a job works in: the folder it holds exclusively, by name.
    function jobProject(job) {
        const folder=job.plan && job.plan.exclusive ? String(job.plan.exclusive) : ""
        return folder.replace(/\/+$/,"").split("/").pop()
    }
    // How this window started a job, so it can start the same work again. Jobs
    // of other clients carry no such record and cannot be run again from here.
    property var jobOrigins: { try { return JSON.parse(Studio.setting("jobs/origins","{}")) || {} } catch(e) { return {} } }
    function rememberJob(job,operation,args) {
        if(!job || !job.id || job.reused) return
        let origins=Object.assign({},jobOrigins)
        origins[job.id]={operation:operation,args:args || {},at:Date.now()/1000}
        const keep=Object.keys(origins).sort((a,b)=>origins[b].at-origins[a].at).slice(0,40)
        let kept={}
        for(const id of keep) kept[id]=origins[id]
        jobOrigins=kept
        Studio.saveSetting("jobs/origins",JSON.stringify(kept))
    }
    function canRerun(job) { return !!job && !!job.id && !!jobOrigins[job.id] && ["queued","running"].indexOf(job.state)<0 }
    function rerunJob(job) {
        if(!canRerun(job)) return -1
        const origin=jobOrigins[job.id]
        return request(origin.operation,origin.args)
    }
    // What each project used last: the device and its platform, the Apple target, the build's
    // platform, configuration and format. Stored on this computer for the 20 newest projects.
    property var projectPreferences: { try { return JSON.parse(Studio.setting("projects/preferences","{}")) || {} } catch(e) { return {} } }
    function preferencesFor(path) { return path && projectPreferences[path] ? projectPreferences[path] : {} }
    function rememberForProject(values,path) {
        const folder=path || project.path
        if(!folder) return
        let all=Object.assign({},projectPreferences)
        all[folder]=Object.assign({},all[folder] || {},values,{at:Date.now()/1000})
        const keep=Object.keys(all).sort((a,b)=>(all[b].at || 0)-(all[a].at || 0)).slice(0,20)
        let kept={}
        for(const key of keep) kept[key]=all[key]
        projectPreferences=kept
        Studio.saveSetting("projects/preferences",JSON.stringify(kept))
    }
    // The build choices of a project opened again, where they still apply.
    function applyBuildPreferences(path) {
        const saved=preferencesFor(path)
        const index=["android","ios","web","linux"].indexOf(saved.buildPlatform)
        if(index>=0 && (!project.platforms || !project.platforms.length || project.platforms.indexOf(saved.buildPlatform)>=0)) platform.currentIndex=index
        if(saved.configuration) configuration.currentIndex=Math.max(0,["debug","release"].indexOf(saved.configuration))
        if(saved.format) Qt.callLater(()=>{const at=format.model.indexOf(saved.format);if(at>=0)format.currentIndex=at})
        if(saved.device && devices.some(x=>x.id===saved.device)) { selectedDevice=saved.device; selectedPlatform=saved.platform || selectedPlatform }
    }
    signal projectPreferencesApplied(string path)
    property string selectedJob: ""
    property int logOffset: 0
    property string logText: ""
    // What the reader filters the log by: text and the lowest level shown.
    property string logQuery: ""
    property string logLevel: "all"
    readonly property bool logFiltered: logQuery.length>0 || logLevel!=="all"
    readonly property var logLines: logFiltered ? LogFilter.filter(logText,logQuery,logLevel) : []
    readonly property string shownLog: logFiltered ? logLines.join("\n") : logText
    property string resultText: ""
    property string status: qsTr("Open a project, connect a device and start a build.")
    // The progress banner shows while a request's own status text is on screen.
    property bool running: false
    onStatusChanged: running=false
    property bool failed: false
    // The last request that failed on screen: what went wrong, what to do about it, and the request to try again.
    property var failure: ({})
    onFailedChanged: if(!failed) failure=({})
    function retry() {
        if(!failure.operation) return -1
        const again=failure
        return request(again.operation,again.args)
    }
    property var pending: ({})
    property var formValues: ({})
    property var selectedOperation: null
    property string initialPath: ""
    property string selectedDevice: ""
    property string selectedPlatform: "android"
    // The live log of the selected Android device under Devices.
    property bool showDeviceLog: false
    signal profilesChanged()
    signal runtimeRequested(string platform, string device)
    signal iosSetupRequested()
    // Connect a coding agent: Main.qml opens the agent picker.
    signal connectAgentRequested()
    function chooseDevicePlatform(value) {
        selectedPlatform=value
        if(!devices.some(x=>x.id===selectedDevice && x.platform===value)) {
            let rows=devices.filter(x=>x.platform===value)
            selectedDevice=rows.length?rows[0].id:""
        }
    }
    function viewState() {
        return {visible:opened,tab:["project","devices","jobs","tools","debug","testing","profiling","files","release","demo"][panel.currentIndex],path:project.path || "",file:fileWorkspace.current,files:fileWorkspace.tabs,dirty:fileWorkspace.dirtyFiles(),entries:fileWorkspace.entries.length,notice:fileWorkspace.notice,
            ios_research:!iosResearch?idleIosResearch:{guest:iosResearch.guestState(),virtual_phone:iosResearch.virtualPhone,visible:iosResearch.opened,job_id:iosResearch.jobId,job_state:iosResearch.jobState,report_directory:iosResearch.reportDirectory,guest_observations_verified:iosResearch.report.guest_observations_verified===true,graphical_apps_verified:false}}
    }
    function hasUnsaved() {
        if(demoPage.item && demoPage.item.dirty) {
            // The same close/replacement guard protects a demo draft and
            // brings it back into view. Never reload or discard it here.
            showTab("demo")
            return true
        }
        return fileWorkspace.hasUnsaved()
    }
    function configureIosGuest(args) { visit("ios-research");iosResearchLoader.item.configureGuest(args) }
    // The virtual iPhone's own window takes part in the app's device-only mode.
    property bool guestSolo: false
    readonly property bool guestWindowVisible: !!iosResearch && iosResearch.guestWindowVisible
    signal guestCloseRequested()
    signal guestDockRequested()

    function showTab(name) {
        if(name==="ios-research") {currentIndex=1;chooseDevicePlatform("ios");open();visit("ios-research");Qt.callLater(()=>iosResearch.open());return}
        let index=["project","devices","jobs","tools","debug","testing","profiling","files","release","demo"].indexOf(name)
        if(index<0) throw new Error("Unknown workspace: "+name)
        panel.currentIndex=index
        open()
    }

    function request(operation,args,quiet) {
        let signature=JSON.stringify(args||{})
        if(quiet) for(let key in pending) if(pending[key].operation===operation && pending[key].signature===signature) return Number(key)
        let id=Agent.request(operation,args||{})
        if(operation==="project.run" || operation==="project.execute")projectRequestPending=true
        pending[id]={operation:operation,quiet:quiet||false,signature:signature}
        if(!quiet) { failed=false; failure=({}); status=qsTr("Running…"); running=true }
        return id
    }
    function chooseProjectFolder() { projectFolder.open() }
    function openProject(path) {
        projectPath.text=path
        request("project.inspect",{path:path})
    }
    function build(action) {
        request("project.execute",{path:projectPath.text,platform:platform.currentText,configuration:configuration.currentText,action:action,format:format.currentText,sign:platform.currentText==="ios" && signBuild.checked})
    }
    function selectJob(job) {
        selectedJob=job.id; activeJob=job; logOffset=0; logText=""; panel.currentIndex=2
        request("job.logs",{job_id:job.id,offset:0},true)
    }
    function chooseOperation(operation) {
        resultText="";status=qsTr("Ready · %1").arg(OperationTitles.title(operation));failed=false
        selectedOperation=operation
        toolPane="form"
        let values={}
        for(let key in operation.inputSchema.properties) {
            let spec=operation.inputSchema.properties[key]
            if(spec.default!==undefined) values[key]=spec.default
        }
        if(operation.inputSchema.properties.path && projectPath.text) values.path=projectPath.text
        if(operation.inputSchema.properties.device && selectedDevice) values.device=selectedDevice
        if(operation.inputSchema.properties.session) values.session=Agent.session
        if(operation.inputSchema.properties.job_id && selectedJob) values.job_id=selectedJob
        formValues=values
    }
    // Agents & tools with its result: on a phone the form and result pane, not the list.
    function showResult() { panel.currentIndex=3; toolPane="form" }
    function showAction(name) {
        panel.currentIndex=3
        for(let operation of panel.operations) if(operation.name===name) { chooseOperation(operation); break }
    }
    // The form's values as the operation's arguments; throws with what is wrong.
    function formArguments() {
        let args={}
        for(let key in selectedOperation.inputSchema.properties) {
            let value=formValues[key], spec=selectedOperation.inputSchema.properties[key]
            if(value===undefined || value==="") { if(selectedOperation.inputSchema.required.indexOf(key)>=0) throw new Error(qsTr("%1 is missing").arg(key)); continue }
            if(spec.type==="integer" || spec.type==="number") {
                value=Number(value)
                if(isNaN(value) || (spec.type==="integer" && !Number.isInteger(value))) throw new Error(qsTr("%1 must be a number").arg(key))
                if((spec.minimum!==undefined && value<spec.minimum) || (spec.maximum!==undefined && value>spec.maximum)) throw new Error(qsTr("%1 is out of range: %2").arg(key).arg(fieldLimits(spec)))
            }
            if((spec.type==="object" || spec.type==="array") && typeof value==="string") value=JSON.parse(value)
            args[key]=value
        }
        return args
    }
    // An operation that changes or removes something for good asks once before it runs.
    readonly property bool destructiveOperation: !!selectedOperation && !!selectedOperation.annotations && selectedOperation.annotations.destructiveHint===true
    property bool confirmingRun: false
    onSelectedOperationChanged: confirmingRun=false
    function submitForm(confirmed) {
        try {
            let args=formArguments()
            if(destructiveOperation && !confirmed) { confirmingRun=true; return }
            confirmingRun=false
            request(selectedOperation.name,args)
        } catch(error) { status=error.message; failed=true; confirmingRun=false }
    }
    // The limits of a number field, such as "1 – 3600" or "≥ 0".
    function fieldLimits(spec) {
        if(spec.minimum!==undefined && spec.maximum!==undefined) return spec.minimum+" – "+spec.maximum
        if(spec.minimum!==undefined) return "≥ "+spec.minimum
        if(spec.maximum!==undefined) return "≤ "+spec.maximum
        return ""
    }
    // The same call for a terminal or a script: tomaccoctl call NAME 'JSON'.
    function commandLine() {
        const json=JSON.stringify(formArguments())
        return "tomaccoctl call "+selectedOperation.name+" '"+json.replace(/'/g,"'\\''")+"'"
    }
    function copyCommand() {
        try { Studio.copy(commandLine()); status=qsTr("Command copied"); failed=false }
        catch(error) { status=error.message; failed=true }
    }
    onOpenedChanged: {
        if(!opened)return
        visit(sections[currentIndex].id)
        request("workspace.list",{},true)
        request("job.list",{},true)
        request("device.list",{},true)
        if(initialPath.length && !project.path) openProject(initialPath)
    }
    Connections {
        target: Agent
        function onApiFinished(id,operation,response) {
            let context=panel.pending[id]
            if(!context) return
            delete panel.pending[id]
            if(operation==="project.run" || operation==="project.execute")panel.projectRequestPending=false
            if(!response.ok) {
                if(!context.quiet) { panel.failed=true; panel.status=response.error.message+(response.error.remedy ? " · "+response.error.remedy : ""); panel.resultText=JSON.stringify(response,null,2)
                    panel.failure={message:response.error.message,remedy:response.error.remedy || "",code:response.error.code || "",operation:operation,args:JSON.parse(context.signature)} }
                if(response.error.code==="workspace_busy" && response.error.details.job){panel.selectJob(response.error.details.job);panel.failure=({});panel.status=qsTr("A job is already running for this project. Use the running session or end it first.")}
                return
            }
            let value=response.result
            if(operation==="workspace.list") { panel.savedWorkspaces=value
            } else if(operation==="workspace.save") { panel.request("workspace.list",{},true)
            } else if(operation==="workspace.context") { panel.openProject(value.project.path);panel.selectedDevice=value.workspace.device;panel.selectedPlatform=value.workspace.platform;bundleId.text=value.workspace.bundle_id
            } else if(operation==="job.list") {
                // An unchanged list keeps its rows, so a tap is not lost to a rebuild.
                if(JSON.stringify(value)!==JSON.stringify(panel.jobs))panel.jobs=value
                for(let job of value) if(job.id===panel.selectedJob) panel.activeJob=job
            } else if(operation==="job.logs") {
                if(value.id===panel.selectedJob && value.offset===panel.logOffset) { panel.logText=(panel.logText+value.text).slice(-200000); panel.logOffset=value.next_offset }
            } else if(operation==="project.inspect") {
                if(JSON.parse(context.signature).path!==projectPath.text)return
                projectPath.text=value.path
                panel.project=value; Studio.rememberProject(value.path); if(panel.revealPath) { fileWorkspace.openFile(panel.revealPath,panel.revealLine);panel.revealPath="" } panel.status=value.name+" · "+value.kinds.join(" / ")
                if(value.configuration.applicationId) bundleId.text=value.configuration.applicationId
                if(value.platforms.length) platform.currentIndex=Math.max(0,["android","ios","web","linux"].indexOf(value.platforms[0]))
                panel.applyBuildPreferences(value.path)
                panel.projectPreferencesApplied(value.path)
            } else if(operation==="device.list") {
                panel.devices=value.devices
                if(!context.quiet && !value.devices.length) panel.status=value.diagnostics.length ? value.diagnostics.map(x=>x.message).join(" · ") : qsTr("No devices connected. Enable USB debugging or trust on the device.")
            } else if(value && value.id && value.state) {
                panel.rememberJob(value,operation,JSON.parse(context.signature))
                panel.selectJob(value)
                panel.status=value.reused?qsTr("Reused running job · %1").arg(value.label):qsTr("Job started · %1").arg(value.label)
            } else if(!context.quiet) panel.status=qsTr("Completed")
            if(!context.quiet) panel.resultText=JSON.stringify(value,null,2)
            if(operation.indexOf("catalog.")===0 && operation!=="catalog.list") panel.profilesChanged()
        }
    }
    Timer { interval: 1800; running: panel.opened; repeat: true; onTriggered: {
        panel.request("job.list",{},true)
        if(panel.selectedJob) panel.request("job.logs",{job_id:panel.selectedJob,offset:panel.logOffset},true)
    } }
    // Split views run edge to edge under no page heading; the rail already says where you are.
    readonly property bool fullBleed: ["files","jobs","tools"].indexOf(section)>=0
    // A phone-wide window: the band is short, split views show the list or the detail, not both.
    // The window's width, as NativeWorkspace.phone: a panel narrowed for a moment by the
    // activity panel while the interface size changes does not flip the layout back and forth.
    readonly property bool phone: (Window.window ? Window.width : width)<Theme.px(600)
    // Phone panes: which detail a phone-wide panel shows instead of its list.
    // Plain state, so a window that grows past the threshold and shrinks again
    // comes back to the same job, device, file, tool form or project list.
    property string phoneDevice: ""
    property string toolPane: "list"
    property bool projectList: false
    readonly property bool phoneDetailOpen: phone && ((section==="jobs" && !!selectedJob)
        || (section==="devices" && !!phoneDevice)
        || (section==="tools" && toolPane==="form")
        || (section==="files" && fileWorkspace.phonePane==="file" && !!fileWorkspace.current)
        || (section==="project" && projectList && !!project.path))
    // Back on a phone: the top bar's back, Escape and the system back key close the detail first.
    function phoneBack() {
        if(!phoneDetailOpen) return false
        if(section==="jobs") selectedJob=""
        else if(section==="devices") phoneDevice=""
        else if(section==="tools") toolPane="list"
        else if(section==="files") fileWorkspace.phonePane="browse"
        else if(section==="project") projectList=false
        return true
    }
    Shortcut { sequences: ["Esc","Back"]; enabled: panel.visible && panel.opened && panel.phoneDetailOpen; onActivated: panel.phoneBack() }
    ColumnLayout {
        anchors.fill: parent
        spacing: 0
        StudioBand {
            id: band
            Layout.fillWidth: true
            // Phone pages with their own header (the project, the devices) leave the band out: at 200 % it would take half the screen.
            visible: !panel.fullBleed && !(panel.phone && ["devices","project"].indexOf(panel.section)>=0)
            roomy: panel.height>=Theme.px(680) && !panel.phone && !(panel.section==="debug" && !!debugPage.item && debugPage.item.active)
            title: panel.sections[panel.currentIndex].title
            lead: panel.sections[panel.currentIndex].description
            tone: panel.sections[panel.currentIndex].tone || "inverse"
            detail: panel.section==="debug" && debugPage.item ? debugPage.item.stateLabel : ""
            StudioButton { text: "Toolchain"; iconName: "settings"; inverted: true; invertedFill: band.ink; invertedText: band.color; visible: panel.section==="project"; onClicked: { panel.showAction("doctor");panel.request("doctor",{}) } }
        }
        Rectangle {
            Layout.fillWidth: true; implicitHeight: feedback.implicitHeight+Theme.gapSm*2
            visible: panel.failed || panel.running
            color: panel.failed ? Theme.dangerSurface : Theme.elevated
            Rectangle { width: Theme.gapXs; height: parent.height; color: panel.failed ? Theme.danger : Theme.accent }
            // A failure says what went wrong, what to do about it, and offers to try again.
            ColumnLayout {
                id: feedback; objectName: "workspaceFeedback"
                anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.gapSm; leftMargin: Theme.gapLg }
                spacing: Theme.gapXs
                RowLayout {
                    Layout.fillWidth: true; spacing: Theme.gapSm
                    Text { objectName: "workspaceFeedbackText"; Layout.fillWidth: true; text: panel.failure.operation ? panel.failure.message : panel.status; color: panel.failed ? Theme.danger : Theme.text; wrapMode: Text.Wrap; font.pixelSize: Theme.fontSmall; font.weight: panel.failure.operation ? Font.Bold : Font.Normal }
                    StudioButton { objectName: "workspaceRetry"; visible: panel.failed && !!panel.failure.operation; text: qsTr("Try again"); iconName: "refresh"; onClicked: panel.retry() }
                    StudioButton { visible: panel.failed; iconName: "close"; quiet: true; text: qsTr("Dismiss"); displayText: ""; tip: text; onClicked: panel.failed=false }
                }
                Text { objectName: "workspaceRemedy"; Layout.fillWidth: true; visible: !!panel.failure.remedy; text: qsTr("What to do: %1").arg(panel.failure.remedy || ""); color: Theme.text; wrapMode: Text.Wrap; font.pixelSize: Theme.fontSmall }
            }
        }
        StackLayout {
            currentIndex: panel.currentIndex; Layout.fillWidth: true; Layout.fillHeight: true
            // Phone pages drawn as rows run edge to edge; their rows and blocks keep the 16 px gutter themselves.
            Layout.margins: panel.fullBleed || (panel.phone && ["devices","project"].indexOf(panel.section)>=0) ? 0 : panel.width<Theme.px(850) ? Theme.gapLg : Theme.gapXl
            StudioScrollView {
                id:projectPage;objectName:"projectOverview";clip:true;contentWidth:availableWidth
                contentHeight:panel.phone?phoneProject.implicitHeight:projectGrid.implicitHeight
                GridLayout {
                    id:projectGrid;visible:!panel.phone
                    width:Math.min(projectPage.availableWidth-Theme.gapLg,Theme.px(1240));columns:1;rowSpacing:Theme.gapXs
                    StudioPanel {
                        Layout.fillWidth:true;Layout.columnSpan:parent.columns
                        title:panel.project.name || qsTr("Start with a project")
                        actions:[StudioButton {text:qsTr("New project");quiet:true;iconName:"plus";onClicked:panel.showAction("project.create")}]
                        GridLayout {Layout.fillWidth:true;Layout.minimumWidth:0;columns:width<Theme.px(620)?1:3;columnSpacing:Theme.gapXs;rowSpacing:Theme.gapXs
                            StudioTextField {id:projectPath;Layout.fillWidth:true;Layout.minimumWidth:0;font.family:Theme.mono;font.pixelSize:Theme.fontSmall;placeholderText:qsTr("Path to your Swift, Kotlin or Flutter project");onAccepted:panel.openProject(text)}
                            StudioButton {text:qsTr("Choose folder");accent:!panel.project.path;onClicked:projectFolder.open()}
                            StudioButton {text:qsTr("Open");visible:projectPath.text!==panel.project.path;enabled:!!projectPath.text;onClicked:panel.openProject(projectPath.text)}
                        }
                        RowLayout {visible:!!panel.project.path;Layout.fillWidth:true
                            Text {text:panel.project.kinds?panel.project.kinds.join("  ·  "):"";color:Theme.muted;font.family:Theme.mono;font.pixelSize:Theme.fontSmall;Layout.fillWidth:true;Layout.minimumWidth:0;elide:Text.ElideRight}
                            StudioButton {text:qsTr("Open files");quiet:true;iconName:"files";onClicked:panel.showTab("files")}
                        }
                    }
                    StudioPanel {
                        Layout.fillWidth:true
                        visible:!!panel.project.path
                        title:qsTr("Build")
                        GridLayout {Layout.fillWidth:true;columns:width<Theme.px(340)?1:(platform.currentText==="android" || platform.currentText==="ios"?3:2);columnSpacing:Theme.gapSm;rowSpacing:Theme.gapSm
                            ColumnLayout {Layout.fillWidth:true;Layout.preferredWidth:1;spacing:Theme.gapXs
                                StudioSection {text:qsTr("Platform")}
                                StudioComboBox {id:platform;objectName:"buildPlatform";model:["android","ios","web","linux"];Layout.fillWidth:true;implicitWidth:Theme.px(80);onActivated:panel.rememberForProject({buildPlatform:currentText});displayText:({android:"Android",ios:"iOS",web:"Web",linux:"Linux"})[currentText] || currentText}
                            }
                            ColumnLayout {Layout.fillWidth:true;Layout.preferredWidth:1;spacing:Theme.gapXs
                                StudioSection {text:qsTr("Configuration")}
                                StudioComboBox {id:configuration;objectName:"buildConfiguration";model:["debug","release"];Layout.fillWidth:true;implicitWidth:Theme.px(80);onActivated:panel.rememberForProject({configuration:currentText});displayText:currentText==="debug"?"Debug":"Release"}
                            }
                            ColumnLayout {visible:platform.currentText==="android" || platform.currentText==="ios";Layout.fillWidth:true;Layout.preferredWidth:1;spacing:Theme.gapXs
                                StudioSection {text:qsTr("Package format")}
                                StudioComboBox {id:format;objectName:"buildFormat";Layout.fillWidth:true;implicitWidth:Theme.px(80);onActivated:panel.rememberForProject({format:currentText});model:platform.currentText==="ios"?["ipa"]:platform.currentText==="android"?["apk","aab"]:["apk"];displayText:currentText.toUpperCase()}
                            }
                        }
                        StudioCheckBox {id:signBuild;visible:platform.currentText==="ios";text:qsTr("Sign build");checked:true}
                        Flow {Layout.fillWidth:true;spacing:Theme.gapXs
                            StudioButton {text:qsTr("Build");iconName:"play";accent:true;enabled:!!panel.project.path;onClicked:panel.build("build")}
                            StudioButton {text:"Tests";enabled:!!panel.project.path;onClicked:panel.build("test")}
                            StudioButton {text:qsTr("Check code");enabled:!!panel.project.path;onClicked:panel.build("lint")}
                            StudioButton {text:qsTr("Show build steps");quiet:true;enabled:!!panel.project.path;onClicked:{panel.request("project.plan",{path:projectPath.text,platform:platform.currentText,configuration:configuration.currentText,format:format.currentText});panel.showResult()}}
                            StudioButton {text:qsTr("SDK & tool paths");quiet:true;onClicked:panel.showAction("settings.configure")}
                        }
                    }
                    StudioPanel {
                        Layout.fillWidth:true
                        visible:!!panel.project.path
                        title:qsTr("Run on device")
                        actions:[StudioButton {text:qsTr("Manage devices");quiet:true;onClicked:panel.showTab("devices")}]
                        StudioComboBox {Layout.fillWidth:true;model:panel.devices;textRole:"name";valueRole:"id";currentIndex:panel.devices.findIndex(x=>x.id===panel.selectedDevice);displayText:currentIndex>=0?currentText:qsTr("Choose a smartphone or emulator");onActivated:{panel.selectedDevice=currentValue;panel.selectedPlatform=panel.devices[currentIndex].platform;panel.rememberForProject({device:panel.selectedDevice,platform:panel.selectedPlatform})}}
                        GridLayout {Layout.fillWidth:true;Layout.minimumWidth:0;columns:width<Theme.px(500)?1:2;columnSpacing:Theme.gapXs;rowSpacing:Theme.gapXs
                            StudioTextField {id:bundleId;Layout.fillWidth:true;Layout.minimumWidth:0;font.family:Theme.mono;font.pixelSize:Theme.fontSmall;placeholderText:qsTr("Application ID, e.g. com.example.app")}
                            StudioButton {text:panel.projectJob.id?qsTr("Job running"):qsTr("Build & run");iconName:"play";accent:true;enabled:!!panel.selectedDevice && !!panel.project.path && !panel.projectRequestPending && !panel.projectJob.id;onClicked:panel.request("project.run",{path:projectPath.text,platform:panel.selectedPlatform,device:panel.selectedDevice,bundle_id:bundleId.text,configuration:configuration.currentText})}
                        }
                        Text {visible:!panel.selectedDevice;Layout.fillWidth:true;text:qsTr("Plug in a phone or start an emulator under Devices.");color:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
                    }
                    StudioPanel {
                        Layout.fillWidth:true;Layout.columnSpan:parent.columns
                        title:qsTr("Save workspace")
                        lead:qsTr("Remember this project, platform and device together.")
                        GridLayout {Layout.fillWidth:true;Layout.minimumWidth:0;columns:width<Theme.px(600)?1:3;columnSpacing:Theme.gapXs;rowSpacing:Theme.gapXs
                            StudioComboBox {Layout.fillWidth:true;implicitWidth:Theme.px(80);model:Object.keys(panel.savedWorkspaces);displayText:currentText || qsTr("Load saved workspace");onActivated:panel.request("workspace.context",{name:currentText})}
                            StudioTextField {id:workspaceName;placeholderText:qsTr("Workspace name");Layout.fillWidth:true;Layout.minimumWidth:0;Layout.preferredWidth:Theme.px(180)}
                            StudioButton {text:qsTr("Save");enabled:!!panel.project.path && !!workspaceName.text;onClicked:panel.request("workspace.save",{name:workspaceName.text,path:projectPath.text,platform:platform.currentText,device:panel.selectedDevice,bundle_id:bundleId.text})}
                        }
                    }
                }
                // The phone's project page: a list of projects to open, or the open project with
                // its target, one primary action and the less common options folded away. It drives
                // the same fields as the desktop panels above, so both show the same state.
                ColumnLayout {
                    id:phoneProject;objectName:"projectPhone";visible:panel.phone;width:projectPage.availableWidth;spacing:0
                    readonly property bool listMode:!panel.project.path || panel.projectList
                    readonly property string query:projectSearch.text.trim().toLowerCase()
                    function matches(text) {const words=query.split(/\s+/).filter(w=>!!w);const hay=String(text).toLowerCase();return words.every(w=>hay.indexOf(w)>=0)}
                    readonly property var recent:Studio.recentProjects.map(p=>({path:p,name:String(p).replace(/\/+$/,"").split("/").pop()})).filter(p=>matches(p.name+" "+p.path))
                    readonly property var saved:Object.keys(panel.savedWorkspaces).filter(name=>matches(name+" "+JSON.stringify(panel.savedWorkspaces[name] || {})))
                    readonly property bool mobileTarget:platform.currentText==="android" || platform.currentText==="ios"
                    readonly property bool canRun:mobileTarget && !!panel.selectedDevice && panel.devices.some(x=>x.id===panel.selectedDevice)
                    readonly property var projectJobs:panel.jobs.filter(x=>x.plan && (x.plan.exclusive===panel.project.path || x.plan.cwd===panel.project.path)).slice(0,3)
                    property bool moreOptions:false
                    property bool pathEntry:false
                    component Heading: Text {
                        Layout.fillWidth:true;Layout.leftMargin:Theme.mobileGutter;Layout.rightMargin:Theme.mobileGutter;Layout.topMargin:Theme.gapXl;Layout.bottomMargin:Theme.gapSm
                        color:Theme.muted;font.pixelSize:Theme.mobileFontMeta;font.weight:Font.Medium;wrapMode:Text.Wrap
                    }
                    component Field: ColumnLayout {
                        property string label:""
                        Layout.fillWidth:true;Layout.leftMargin:Theme.mobileGutter;Layout.rightMargin:Theme.mobileGutter;Layout.topMargin:Theme.gapSm;spacing:Theme.gapXs
                        Text {Layout.fillWidth:true;text:parent.label;color:Theme.muted;font.pixelSize:Theme.mobileFontMeta;font.weight:Font.Medium;wrapMode:Text.Wrap}
                    }
                    // ── The list of projects
                    MobileTopBar {
                        objectName:"projectPhoneListBar";Layout.fillWidth:true;visible:phoneProject.listMode
                        showBack:panel.projectList && !!panel.project.path;title:qsTr("Projects")
                        backText:panel.project.name || qsTr("Back");onBack:panel.phoneBack()
                    }
                    StudioTextField {
                        id:projectSearch;objectName:"projectPhoneSearch";visible:phoneProject.listMode
                        Layout.fillWidth:true;Layout.margins:Theme.mobileGutter;Layout.bottomMargin:Theme.gapSm
                        Layout.preferredHeight:Math.max(implicitHeight,Theme.mobileTouch)
                        placeholderText:qsTr("Search projects and workspaces");Accessible.name:placeholderText
                    }
                    StudioButton {
                        objectName:"projectPhoneOpenFolder";visible:phoneProject.listMode;touch:true;accent:true;iconName:"files"
                        Layout.fillWidth:true;Layout.leftMargin:Theme.mobileGutter;Layout.rightMargin:Theme.mobileGutter
                        text:qsTr("Open folder");onClicked:projectFolder.open()
                    }
                    Heading {visible:phoneProject.listMode && phoneProject.recent.length>0;text:qsTr("Recent")}
                    Repeater {model:phoneProject.listMode?phoneProject.recent:[]
                        MobileRow {required property var modelData;objectName:"projectPhoneRecent-"+modelData.name;Layout.fillWidth:true
                            title:modelData.name;detail:modelData.path;monoDetail:true;showDot:modelData.path===panel.project.path
                            meta:modelData.path===panel.project.path?qsTr("Open","project"):""
                            onClicked:{panel.projectList=false;if(modelData.path!==panel.project.path)panel.openProject(modelData.path)}}
                    }
                    Heading {visible:phoneProject.listMode && phoneProject.saved.length>0;text:qsTr("Saved workspaces")}
                    Repeater {model:phoneProject.listMode?phoneProject.saved:[]
                        MobileRow {required property string modelData;objectName:"projectPhoneSaved-"+modelData;Layout.fillWidth:true;showDot:false
                            readonly property var context:panel.savedWorkspaces[modelData] || ({})
                            title:modelData;detail:[context.path || "",context.platform || "",context.device || ""].filter(x=>!!x).join(" · ");monoDetail:true
                            onClicked:{panel.projectList=false;panel.request("workspace.context",{name:modelData})}}
                    }
                    Text {
                        objectName:"projectPhoneNoMatch";Layout.fillWidth:true;Layout.margins:Theme.mobileGutter
                        visible:phoneProject.listMode && !!phoneProject.query && !phoneProject.recent.length && !phoneProject.saved.length
                        text:qsTr("No project matches “%1”.").arg(projectSearch.text.trim());color:Theme.muted;font.pixelSize:Theme.mobileFontSmall;wrapMode:Text.Wrap
                    }
                    Heading {visible:phoneProject.listMode;text:qsTr("More","projects")}
                    MobileRow {objectName:"projectPhoneNew";visible:phoneProject.listMode;Layout.fillWidth:true;showDot:false
                        title:qsTr("New project");detail:qsTr("Create an app from a template");onClicked:panel.showAction("project.create")}
                    MobileRow {objectName:"projectPhoneImportXcode";visible:phoneProject.listMode;Layout.fillWidth:true;showDot:false
                        title:qsTr("Import Xcode project");detail:qsTr("Reads an .xcodeproj and writes the Tomacco configuration");onClicked:panel.showAction("project.import_xcode")}
                    MobileRow {objectName:"projectPhoneByPath";visible:phoneProject.listMode;Layout.fillWidth:true;showDot:false;chevron:!phoneProject.pathEntry
                        title:qsTr("Open by path");detail:qsTr("Type the folder of a Swift, Kotlin or Flutter project");onClicked:{phoneProject.pathEntry=!phoneProject.pathEntry;if(phoneProject.pathEntry)phonePath.forceActiveFocus()}}
                    RowLayout {
                        visible:phoneProject.listMode && phoneProject.pathEntry;Layout.fillWidth:true;Layout.margins:Theme.mobileGutter;spacing:Theme.gapXs
                        StudioTextField {id:phonePath;objectName:"projectPhonePath";Layout.fillWidth:true;Layout.minimumWidth:0;Layout.preferredHeight:Math.max(implicitHeight,Theme.mobileTouch);font.family:Theme.mono;font.pixelSize:Theme.fontSmall;placeholderText:qsTr("Path to your Swift, Kotlin or Flutter project");onAccepted:if(text.trim()){panel.projectList=false;panel.openProject(text.trim())}}
                        StudioButton {touch:true;text:qsTr("Open");enabled:!!phonePath.text.trim();onClicked:{panel.projectList=false;panel.openProject(phonePath.text.trim())}}
                    }
                    // ── The open project
                    ColumnLayout {
                        objectName:"projectPhoneHeader";visible:!phoneProject.listMode;Layout.fillWidth:true
                        Layout.leftMargin:Theme.mobileGutter;Layout.rightMargin:Theme.mobileGutter;Layout.topMargin:Theme.gapLg;spacing:Theme.gapXs
                        Text {objectName:"projectPhoneName";Layout.fillWidth:true;text:panel.project.name || "";color:Theme.text;font.pixelSize:Theme.mobileFontTitle;font.weight:Font.DemiBold;wrapMode:Text.Wrap;lineHeight:1.2}
                        Text {objectName:"projectPhonePath";Layout.fillWidth:true;text:panel.project.path || "";color:Theme.muted;font.family:Theme.mono;font.pixelSize:Theme.mobileFontMeta;wrapMode:Text.WrapAnywhere}
                        Text {Layout.fillWidth:true;visible:!!text;text:panel.project.kinds?panel.project.kinds.join(" · "):"";color:Theme.faint;font.pixelSize:Theme.mobileFontMeta;wrapMode:Text.Wrap}
                        Flow {Layout.fillWidth:true;Layout.minimumWidth:0;spacing:Theme.gapXs
                            StudioButton {objectName:"projectPhoneSwitch";touch:true;quiet:true;iconName:"project";text:qsTr("Switch project");onClicked:panel.projectList=true}
                            StudioButton {touch:true;quiet:true;iconName:"files";text:qsTr("Open files");onClicked:panel.showTab("files")}
                        }
                    }
                    Heading {visible:!phoneProject.listMode;text:qsTr("Target")}
                    Field {visible:!phoneProject.listMode;label:qsTr("Platform")
                        StudioComboBox {objectName:"projectPhonePlatform";Layout.fillWidth:true;Layout.preferredHeight:Math.max(implicitHeight,Theme.mobileTouch);model:platform.model;currentIndex:platform.currentIndex
                            displayText:({android:"Android",ios:"iOS",web:"Web",linux:"Linux"})[currentText] || currentText
                            onActivated:index=>{platform.currentIndex=index;panel.rememberForProject({buildPlatform:platform.currentText})}}
                    }
                    Field {visible:!phoneProject.listMode;label:qsTr("Configuration")
                        StudioComboBox {objectName:"projectPhoneConfiguration";Layout.fillWidth:true;Layout.preferredHeight:Math.max(implicitHeight,Theme.mobileTouch);model:configuration.model;currentIndex:configuration.currentIndex
                            displayText:currentText==="debug"?"Debug":"Release"
                            onActivated:index=>{configuration.currentIndex=index;panel.rememberForProject({configuration:configuration.currentText})}}
                    }
                    Field {visible:!phoneProject.listMode && phoneProject.mobileTarget;label:qsTr("Package format")
                        StudioComboBox {objectName:"projectPhoneFormat";Layout.fillWidth:true;Layout.preferredHeight:Math.max(implicitHeight,Theme.mobileTouch);model:format.model;currentIndex:format.currentIndex
                            displayText:currentText.toUpperCase()
                            onActivated:index=>{format.currentIndex=index;panel.rememberForProject({format:format.currentText})}}
                    }
                    Field {visible:!phoneProject.listMode && phoneProject.mobileTarget;label:qsTr("Device")
                        StudioComboBox {objectName:"projectPhoneDevice";Layout.fillWidth:true;Layout.preferredHeight:Math.max(implicitHeight,Theme.mobileTouch)
                            model:panel.devices;textRole:"name";valueRole:"id";currentIndex:panel.devices.findIndex(x=>x.id===panel.selectedDevice)
                            displayText:currentIndex>=0?currentText:qsTr("Choose a smartphone or emulator")
                            onActivated:{panel.selectedDevice=currentValue;panel.selectedPlatform=panel.devices[currentIndex].platform;panel.rememberForProject({device:panel.selectedDevice,platform:panel.selectedPlatform})}}
                        Text {visible:!panel.selectedDevice;Layout.fillWidth:true;text:qsTr("Plug in a phone or start an emulator under Devices.");color:Theme.muted;font.pixelSize:Theme.mobileFontSmall;wrapMode:Text.Wrap}
                    }
                    // An iOS build runs on a Mac that was set up for it; nothing picks or finds one on its own.
                    Field {objectName:"projectPhoneHost";visible:!phoneProject.listMode && platform.currentText==="ios";label:qsTr("Build host")
                        Text {Layout.fillWidth:true;text:qsTr("iOS builds need a Mac host you set up yourself. Tomacco never chooses or searches for one.");color:Theme.text;font.pixelSize:Theme.mobileFontSmall;wrapMode:Text.Wrap}
                        StudioButton {objectName:"projectPhoneHosts";Layout.fillWidth:true;Layout.maximumWidth:naturalWidth;touch:true;quiet:true;text:qsTr("Mac hosts");onClicked:panel.showAction("remote.list")}
                    }
                    StudioButton {
                        objectName:"projectPhonePrimary";visible:!phoneProject.listMode;touch:true;accent:true;iconName:"play"
                        Layout.fillWidth:true;Layout.leftMargin:Theme.mobileGutter;Layout.rightMargin:Theme.mobileGutter;Layout.topMargin:Theme.gapXl
                        text:phoneProject.canRun?(panel.projectJob.id?qsTr("Job running"):qsTr("Build & run")):qsTr("Build")
                        enabled:!!panel.project.path && (phoneProject.canRun?!panel.projectRequestPending && !panel.projectJob.id:true)
                        onClicked:{if(phoneProject.canRun)panel.request("project.run",{path:projectPath.text,platform:panel.selectedPlatform,device:panel.selectedDevice,bundle_id:bundleId.text,configuration:configuration.currentText});else panel.build("build")}
                    }
                    Flow {
                        objectName:"projectPhoneSecondary";visible:!phoneProject.listMode;Layout.fillWidth:true;Layout.minimumWidth:0
                        Layout.leftMargin:Theme.mobileGutter;Layout.rightMargin:Theme.mobileGutter;Layout.topMargin:Theme.gapSm;spacing:Theme.gapXs
                        StudioButton {touch:true;quiet:true;visible:phoneProject.canRun;text:qsTr("Build only");enabled:!!panel.project.path;onClicked:panel.build("build")}
                        StudioButton {touch:true;quiet:true;text:"Tests";enabled:!!panel.project.path;onClicked:panel.build("test")}
                        StudioButton {touch:true;quiet:true;text:qsTr("Check code");enabled:!!panel.project.path;onClicked:panel.build("lint")}
                    }
                    MobileRow {
                        objectName:"projectPhoneRunning";visible:!phoneProject.listMode && !!panel.projectJob.id;Layout.fillWidth:true;Layout.topMargin:Theme.gapSm
                        title:panel.projectJob.label || "";detail:panel.projectJob.id?panel.jobStateLabel(panel.projectJob):"";dot:Theme.warning
                        onClicked:panel.selectJob(panel.projectJob)
                    }
                    StudioButton {
                        objectName:"projectPhoneMore";visible:!phoneProject.listMode;touch:true;quiet:true;alignLeft:true
                        Layout.fillWidth:true;Layout.leftMargin:Theme.gapXs;Layout.rightMargin:Theme.mobileGutter;Layout.topMargin:Theme.gapLg
                        iconName:phoneProject.moreOptions?"chevron":"chevronRight";text:qsTr("More build options")
                        Accessible.description:phoneProject.moreOptions?qsTr("Expanded"):qsTr("Collapsed")
                        onClicked:phoneProject.moreOptions=!phoneProject.moreOptions
                    }
                    ColumnLayout {
                        objectName:"projectPhoneOptions";visible:!phoneProject.listMode && phoneProject.moreOptions;Layout.fillWidth:true;spacing:0
                        Field {label:qsTr("Application ID");visible:phoneProject.mobileTarget
                            StudioTextField {objectName:"projectPhoneBundle";Layout.fillWidth:true;Layout.preferredHeight:Math.max(implicitHeight,Theme.mobileTouch);font.family:Theme.mono;font.pixelSize:Theme.fontSmall;text:bundleId.text;placeholderText:qsTr("Application ID, e.g. com.example.app");onTextEdited:bundleId.text=text}
                        }
                        StudioCheckBox {visible:platform.currentText==="ios";Layout.leftMargin:Theme.mobileGutter;Layout.topMargin:Theme.gapSm;Layout.preferredHeight:Math.max(implicitHeight,Theme.mobileTouch);text:qsTr("Sign build");checked:signBuild.checked;onToggled:signBuild.checked=checked}
                        Flow {Layout.fillWidth:true;Layout.minimumWidth:0;Layout.margins:Theme.mobileGutter;Layout.bottomMargin:0;spacing:Theme.gapXs
                            StudioButton {objectName:"projectPhonePlan";touch:true;quiet:true;text:qsTr("Show build steps");enabled:!!panel.project.path;onClicked:{panel.request("project.plan",{path:projectPath.text,platform:platform.currentText,configuration:configuration.currentText,format:format.currentText});panel.showResult()}}
                            StudioButton {touch:true;quiet:true;text:qsTr("SDK & tool paths");onClicked:panel.showAction("settings.configure")}
                            StudioButton {touch:true;quiet:true;text:"Toolchain";onClicked:{panel.showAction("doctor");panel.request("doctor",{})}}
                            StudioButton {touch:true;quiet:true;text:qsTr("New project");onClicked:panel.showAction("project.create")}
                        }
                        Field {label:qsTr("Save workspace")
                            Text {Layout.fillWidth:true;text:qsTr("Remember this project, platform and device together.");color:Theme.faint;font.pixelSize:Theme.mobileFontMeta;wrapMode:Text.Wrap}
                            RowLayout {Layout.fillWidth:true;spacing:Theme.gapXs
                                StudioTextField {id:phoneWorkspaceName;objectName:"projectPhoneWorkspaceName";Layout.fillWidth:true;Layout.minimumWidth:0;Layout.preferredHeight:Math.max(implicitHeight,Theme.mobileTouch);placeholderText:qsTr("Workspace name")}
                                StudioButton {touch:true;text:qsTr("Save");enabled:!!panel.project.path && !!phoneWorkspaceName.text;onClicked:panel.request("workspace.save",{name:phoneWorkspaceName.text,path:projectPath.text,platform:platform.currentText,device:panel.selectedDevice,bundle_id:bundleId.text})}
                            }
                        }
                    }
                    Heading {visible:!phoneProject.listMode && phoneProject.projectJobs.length>0;text:qsTr("Recent jobs")}
                    Repeater {model:phoneProject.listMode?[]:phoneProject.projectJobs
                        MobileRow {required property var modelData;objectName:"projectPhoneJob-"+modelData.id;Layout.fillWidth:true
                            title:modelData.label;detail:panel.jobStateLabel(modelData)+" · "+panel.jobDuration(modelData)
                            dot:({succeeded:Theme.success,failed:Theme.danger,timed_out:Theme.danger,queued:Theme.warning,running:Theme.warning})[modelData.state] || Theme.faint
                            onClicked:panel.selectJob(modelData)}
                    }
                    Item {Layout.preferredHeight:Theme.gapXxl}
                }
            }
            StudioScrollView {
                id:devicesPage;objectName:"devicesPage";clip:true;contentWidth:availableWidth
              ColumnLayout {
                // As tall as the page at least, so the lists keep filling it, and taller when its content
                // needs more (the virtual iPhone's card on the smallest window): then the page scrolls.
                width:devicesPage.availableWidth;height:Math.max(implicitHeight,devicesPage.availableHeight)
                spacing:Theme.gapMd
                RowLayout {Layout.fillWidth:true;spacing:Theme.gapXs;visible:!panel.phone
                    StudioSegmented {
                        current:panel.selectedPlatform
                        model:[{id:"android",label:"Android",objectName:"devices-android"},{id:"ios",label:"Apple",objectName:"devices-apple"}]
                        onChosen:id=>panel.chooseDevicePlatform(id)
                    }
                    Item {Layout.fillWidth:true}
                    StudioButton {objectName:"pairWifiButton";visible:panel.selectedPlatform==="android";text:qsTr("Connect via Wi-Fi");quiet:true;onClicked:androidLibrary.openPairing()}
                    StudioButton {visible:panel.selectedPlatform==="ios";text:qsTr("Set up iPhone");quiet:true;onClicked:panel.iosSetupRequested()}
                    StudioButton {text:qsTr("Find devices");iconName:"refresh";onClicked:panel.request("device.list",{})}
                }
                // The phone's list header: the platform, then the ways to find a device, each a 48 px target.
                ColumnLayout {objectName:"devicesPhoneHeader";Layout.fillWidth:true;Layout.margins:Theme.mobileGutter;Layout.bottomMargin:0;visible:panel.phone && !panel.phoneDevice;spacing:Theme.gapSm
                    StudioSegmented {
                        Layout.fillWidth:true;touch:true;current:panel.selectedPlatform
                        model:[{id:"android",label:"Android",objectName:"devicesPhone-android"},{id:"ios",label:"Apple",objectName:"devicesPhone-apple"}]
                        onChosen:id=>panel.chooseDevicePlatform(id)
                    }
                    Flow {Layout.fillWidth:true;Layout.minimumWidth:0;spacing:Theme.gapXs
                        StudioButton {objectName:"phoneFindDevices";touch:true;text:qsTr("Find devices");iconName:"refresh";onClicked:panel.request("device.list",{})}
                        StudioButton {objectName:"phonePairWifi";touch:true;visible:panel.selectedPlatform==="android";text:qsTr("Connect via Wi-Fi");quiet:true;onClicked:androidLibrary.openPairing()}
                        StudioButton {touch:true;visible:panel.selectedPlatform==="ios";text:qsTr("Set up iPhone");quiet:true;onClicked:panel.iosSetupRequested()}
                    }
                }
                AndroidDevices {id:androidLibrary;Layout.fillWidth:true;Layout.fillHeight:!panel.phone;visible:panel.selectedPlatform==="android" && !(panel.phone && !!panel.phoneDevice);workspace:panel}
                Loader {
                    id:iosResearchLoader;Layout.fillWidth:true;Layout.leftMargin:panel.phone?Theme.mobileGutter:0;Layout.rightMargin:Layout.leftMargin;visible:panel.selectedPlatform==="ios" && !(panel.phone && !!panel.phoneDevice)
                    active:!!panel.visited["ios-research"] || (!!panel.visited.devices && panel.selectedPlatform==="ios")
                    sourceComponent:Component {IosResearch {workspace:panel;guestSolo:panel.guestSolo;onGuestCloseRequested:panel.guestCloseRequested();onGuestDockRequested:panel.guestDockRequested()}}
                }
                ListView {
                    id:deviceList;Layout.fillWidth:true;Layout.fillHeight:true;Layout.minimumHeight:Theme.px(160)
                    visible:panel.selectedPlatform==="ios" && !(panel.phone && !!panel.phoneDevice)
                    model:panel.devices.filter(x=>x.platform===panel.selectedPlatform);clip:true;spacing:panel.phone?0:Theme.gapXs
                    // A phone draws each iPhone as a "Mobile / Row" that opens its page.
                    delegate:panel.phone?phoneDeviceRow:desktopDeviceRow
                    Component {id:phoneDeviceRow
                        MobileRow {
                            required property var modelData
                            objectName:"phoneRow-"+modelData.id;width:ListView.view?ListView.view.width:implicitWidth
                            title:modelData.name;detail:modelData.id;monoDetail:true
                            meta:modelData.state==="device"?qsTr("Connected"):modelData.state
                            dot:modelData.state==="device"?Theme.success:modelData.state==="unauthorized"?Theme.warning:Theme.faint
                            highlighted:panel.phoneDevice===modelData.id
                            onClicked:{panel.selectedDevice=modelData.id;panel.phoneDevice=modelData.id}
                        }
                    }
                    Component {id:desktopDeviceRow
                    ItemDelegate {
                        id:deviceRow
                        required property var modelData
                        width:ListView.view.width;height:Theme.rowHeight+Theme.gapLg+Theme.gapXs
                        highlighted:panel.selectedDevice===modelData.id
                        background:Rectangle {radius:Theme.radius;color:deviceRow.highlighted?Theme.selected:deviceRow.hovered?Theme.hover:Theme.surface;border.color:deviceRow.visualFocus?Theme.accentInk:Theme.subtle
                            Rectangle {visible:deviceRow.highlighted;width:2;height:parent.height;color:Theme.accent}
                        }
                        contentItem:RowLayout {spacing:Theme.gapMd
                            StudioIcon {name:"phone";color:Theme.muted;Layout.leftMargin:Theme.gapXs}
                            ColumnLayout {Layout.fillWidth:true;spacing:Theme.gapXxs
                                Text {Layout.fillWidth:true;text:deviceRow.modelData.name;color:Theme.text;font.pixelSize:Theme.fontBody;font.weight:Font.Medium;elide:Text.ElideRight}
                                Text {Layout.fillWidth:true;text:deviceRow.modelData.id+" · "+deviceRow.modelData.state;color:deviceRow.modelData.state==="unauthorized"?Theme.warning:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontMicro;elide:Text.ElideMiddle}
                            }
                            StudioIcon {name:"check";visible:deviceRow.highlighted;color:Theme.accentInk;Layout.rightMargin:Theme.gapSm}
                        }
                        onClicked:panel.selectedDevice=modelData.id
                    }
                    }
                    StudioEmptyState {
                        anchors.centerIn:parent;width:Math.min(parent.width-Theme.gapXxl,Theme.px(480));visible:deviceList.count===0
                        title:panel.selectedPlatform==="ios"?qsTr("No iPhone yet."):qsTr("No phone yet.")
                        body:panel.selectedPlatform==="ios"?qsTr("Plug it in, unlock it and trust this computer. Setup takes you through the rest."):qsTr("Plug in a phone with USB debugging on, or start an emulator.")
                        actionText:panel.selectedPlatform==="ios"?qsTr("Set up iPhone"):"";actionIcon:"devices"
                        onAction:panel.iosSetupRequested()
                    }
                    ScrollBar.vertical:StudioScrollBar {}
                }
                Rectangle {Layout.fillWidth:true;Layout.preferredHeight:1;color:Theme.subtle;visible:!panel.phone}
                Flow {id:deviceActions;objectName:"deviceActions";visible:!panel.phone;Layout.fillWidth:true;spacing:Theme.gapXs;enabled:panel.devices.some(x=>x.id===panel.selectedDevice && x.platform===panel.selectedPlatform)
                    StudioButton {text:qsTr("Open live device");iconName:"phone";accent:true;onClicked:panel.runtimeRequested(panel.selectedPlatform,panel.selectedDevice)}
                    StudioButton {text:panel.selectedPlatform==="ios"?qsTr("Install IPA"):qsTr("Install APK");onClicked:installFile.open()}
                    StudioButton {objectName:"deviceLogToggle";text:panel.selectedPlatform==="android"?qsTr("Live log"):"Logs";iconName:panel.selectedPlatform==="android"?"jobs":"";quiet:!panel.showDeviceLog;selected:panel.showDeviceLog && panel.selectedPlatform==="android";onClicked:if(panel.selectedPlatform==="android")panel.showDeviceLog=!panel.showDeviceLog;else panel.request("device.logs",{platform:panel.selectedPlatform,device:panel.selectedDevice,duration:60})}
                    StudioButton {text:"Screenshot";quiet:true;onClicked:screenshotFile.open()}
                    StudioButton {text:qsTr("Information");quiet:true;onClicked:{panel.request("device.info",{platform:panel.selectedPlatform,device:panel.selectedDevice});panel.showResult()}}
                    StudioButton {text:qsTr("Calibrate model");quiet:true;visible:panel.selectedPlatform==="android";onClicked:panel.showAction("android.calibrate")}
                }
                // A device's page on a phone: what it is and its state, the one thing to do with it, then the rest.
                ColumnLayout {
                    id:phoneDeviceDetail;objectName:"devicePhoneDetail";Layout.fillWidth:true;visible:panel.phone && !!panel.phoneDevice;spacing:Theme.gapMd
                    readonly property bool emulator:panel.phoneDevice.indexOf("avd:")===0
                    readonly property var avd:emulator?(androidLibrary.avds.find(a=>"avd:"+a.name===panel.phoneDevice) || null):null
                    readonly property var device:emulator?(avd?(androidLibrary.deviceFor(avd.name) || null):null):(panel.devices.find(d=>d.id===panel.phoneDevice) || null)
                    readonly property bool connected:!!device && device.state==="device" && device.booted!==false
                    readonly property bool starting:!!avd && !!androidLibrary.pendingBoots[avd.name]
                    readonly property bool missing:emulator?!avd:!device
                    readonly property string platformName:device?device.platform:(emulator?"android":"")
                    function use() {if(device){panel.selectedDevice=device.id;panel.selectedPlatform=device.platform}}
                    MobileTopBar {
                        objectName:"devicePhoneBar";Layout.fillWidth:true
                        title:phoneDeviceDetail.avd?(phoneDeviceDetail.avd.nickname || phoneDeviceDetail.avd.profile_name || phoneDeviceDetail.avd.name):phoneDeviceDetail.device?phoneDeviceDetail.device.name:qsTr("Device")
                        subtitle:phoneDeviceDetail.device?phoneDeviceDetail.device.id:phoneDeviceDetail.avd?phoneDeviceDetail.avd.name:"";monoSubtitle:true
                        backText:qsTr("All devices");onBack:panel.phoneBack()
                    }
                    component Fact: ColumnLayout {
                        property string label: ""
                        property string value: ""
                        property bool mono: false
                        property color tone: Theme.text
                        Layout.fillWidth:true;Layout.minimumWidth:0;spacing:Theme.gapXxs;visible:value.length>0
                        Text {Layout.fillWidth:true;text:parent.label;color:Theme.muted;font.pixelSize:Theme.mobileFontMeta;font.weight:Font.Medium;wrapMode:Text.Wrap}
                        Text {objectName:"fact";Layout.fillWidth:true;text:parent.value;color:parent.tone;font.family:parent.mono?Theme.mono:Theme.sans;font.pixelSize:parent.mono?Theme.mobileFontMeta:Theme.mobileFontBody;wrapMode:parent.mono?Text.WrapAnywhere:Text.Wrap;lineHeight:1.3}
                    }
                    ColumnLayout {Layout.fillWidth:true;Layout.leftMargin:Theme.mobileGutter;Layout.rightMargin:Theme.mobileGutter;spacing:Theme.gapMd
                        Fact {objectName:"devicePhoneState";label:qsTr("State")
                            value:phoneDeviceDetail.missing?qsTr("No longer listed. Find devices again, or go back to the list.")
                                :phoneDeviceDetail.connected?qsTr("Connected"):phoneDeviceDetail.starting?qsTr("Starting…")
                                :phoneDeviceDetail.device?(phoneDeviceDetail.device.state==="unauthorized"?qsTr("Not authorized: allow USB debugging on the phone"):phoneDeviceDetail.device.state):qsTr("Off")
                            tone:phoneDeviceDetail.missing || (!!phoneDeviceDetail.device && phoneDeviceDetail.device.state==="unauthorized")?Theme.warning:Theme.text}
                        Fact {label:qsTr("Kind");value:phoneDeviceDetail.missing?"":phoneDeviceDetail.emulator?qsTr("Android emulator"):phoneDeviceDetail.platformName==="ios"?"iPhone":qsTr("Android phone")}
                        Fact {label:qsTr("Display");value:phoneDeviceDetail.avd?(phoneDeviceDetail.avd.display && phoneDeviceDetail.avd.display.width?phoneDeviceDetail.avd.display.width+" × "+phoneDeviceDetail.avd.display.height:qsTr("External profile")):""}
                        Fact {label:qsTr("Performance");value:phoneDeviceDetail.avd?qsTr("Graphics: %1").arg(phoneDeviceDetail.avd.gpu || "auto")+(phoneDeviceDetail.avd.memory_mb?" · "+phoneDeviceDetail.avd.memory_mb+" MB":""):""}
                    }
                    StudioButton {
                        objectName:"devicePhonePrimary";Layout.fillWidth:true;Layout.leftMargin:Theme.mobileGutter;Layout.rightMargin:Theme.mobileGutter
                        touch:true;accent:true;visible:!phoneDeviceDetail.missing
                        iconName:phoneDeviceDetail.connected?"phone":"play"
                        text:phoneDeviceDetail.connected?qsTr("Open live device"):phoneDeviceDetail.starting?qsTr("Starting…"):phoneDeviceDetail.emulator?qsTr("Start"):qsTr("Open live device")
                        enabled:phoneDeviceDetail.emulator?!phoneDeviceDetail.starting:phoneDeviceDetail.connected
                        onClicked:{if(phoneDeviceDetail.emulator)androidLibrary.start(phoneDeviceDetail.avd);else{phoneDeviceDetail.use();panel.runtimeRequested(phoneDeviceDetail.device.platform,phoneDeviceDetail.device.id)}}
                    }
                    StudioButton {objectName:"devicePhoneFind";visible:phoneDeviceDetail.missing;Layout.leftMargin:Theme.mobileGutter;Layout.rightMargin:Theme.mobileGutter;Layout.fillWidth:true;Layout.maximumWidth:naturalWidth;touch:true;text:qsTr("Find devices");iconName:"refresh";onClicked:{androidLibrary.refresh();panel.request("device.list",{})}}
                    Text {
                        Layout.fillWidth:true;Layout.leftMargin:Theme.mobileGutter;Layout.rightMargin:Theme.mobileGutter
                        visible:!phoneDeviceDetail.missing && !phoneDeviceDetail.connected
                        text:phoneDeviceDetail.emulator?qsTr("Start the emulator to install apps, read its log or take a screenshot."):qsTr("Connect the phone to install apps, read its log or take a screenshot.")
                        color:Theme.muted;font.pixelSize:Theme.mobileFontSmall;wrapMode:Text.Wrap
                    }
                    // What a running device offers; each acts on this device.
                    Flow {
                        objectName:"devicePhoneActions";Layout.fillWidth:true;Layout.minimumWidth:0;Layout.minimumHeight:implicitHeight;Layout.leftMargin:Theme.mobileGutter;Layout.rightMargin:Theme.mobileGutter;spacing:Theme.gapXs
                        visible:phoneDeviceDetail.connected
                        StudioButton {touch:true;text:phoneDeviceDetail.platformName==="ios"?qsTr("Install IPA"):qsTr("Install APK");onClicked:{phoneDeviceDetail.use();installFile.open()}}
                        StudioButton {objectName:"devicePhoneLog";touch:true;text:phoneDeviceDetail.platformName==="android"?qsTr("Live log"):"Logs";quiet:!panel.showDeviceLog;selected:panel.showDeviceLog && phoneDeviceDetail.platformName==="android";onClicked:{phoneDeviceDetail.use();if(phoneDeviceDetail.platformName==="android")panel.showDeviceLog=!panel.showDeviceLog;else panel.request("device.logs",{platform:panel.selectedPlatform,device:panel.selectedDevice,duration:60})}}
                        StudioButton {touch:true;text:"Screenshot";quiet:true;onClicked:{phoneDeviceDetail.use();screenshotFile.open()}}
                        StudioButton {touch:true;text:qsTr("Information");quiet:true;onClicked:{phoneDeviceDetail.use();panel.request("device.info",{platform:panel.selectedPlatform,device:panel.selectedDevice});panel.showResult()}}
                        StudioButton {touch:true;text:qsTr("Calibrate model");quiet:true;visible:phoneDeviceDetail.platformName==="android";onClicked:{phoneDeviceDetail.use();panel.showAction("android.calibrate")}}
                    }
                    // Less common: order, name, copies and performance, each behind its own confirmation where it changes something.
                    ColumnLayout {
                        objectName:"devicePhoneManage";Layout.fillWidth:true;spacing:0;visible:!phoneDeviceDetail.missing && phoneDeviceDetail.platformName!=="ios"
                        Text {Layout.fillWidth:true;Layout.leftMargin:Theme.mobileGutter;Layout.topMargin:Theme.gapMd;Layout.bottomMargin:Theme.gapXs;text:qsTr("Manage");color:Theme.muted;font.pixelSize:Theme.mobileFontMeta;font.weight:Font.Medium}
                        MobileRow {objectName:"devicePhonePin";Layout.fillWidth:true;showDot:false;chevron:false
                            readonly property bool pinned:phoneDeviceDetail.avd?!!phoneDeviceDetail.avd.pinned:(!!phoneDeviceDetail.device && !!phoneDeviceDetail.device.pinned)
                            title:pinned?qsTr("Unpin"):qsTr("Pin");detail:pinned?qsTr("Unpin this device"):qsTr("Pin this device to the top")
                            onClicked:androidLibrary.pin(phoneDeviceDetail.avd?"avd:"+phoneDeviceDetail.avd.name:phoneDeviceDetail.device.id,!pinned)}
                        MobileRow {objectName:"devicePhoneRename";Layout.fillWidth:true;showDot:false
                            title:qsTr("Nickname");detail:qsTr("Give this device a nickname")
                            onClicked:{if(phoneDeviceDetail.avd)androidLibrary.rename("avd:"+phoneDeviceDetail.avd.name,phoneDeviceDetail.avd.nickname,phoneDeviceDetail.avd.profile_name || phoneDeviceDetail.avd.name);else androidLibrary.rename(phoneDeviceDetail.device.id,phoneDeviceDetail.device.nickname,phoneDeviceDetail.device.original_name || phoneDeviceDetail.device.name)}}
                        MobileRow {objectName:"devicePhoneManageAvd";Layout.fillWidth:true;showDot:false;visible:!!phoneDeviceDetail.avd
                            title:qsTr("Manage");detail:qsTr("Duplicate, rename or wipe this emulator")
                            onClicked:androidLibrary.manage(phoneDeviceDetail.avd)}
                        MobileRow {objectName:"devicePhonePerformance";Layout.fillWidth:true;showDot:false;visible:!!phoneDeviceDetail.avd
                            title:qsTr("Performance");detail:qsTr("Set graphics, memory and CPU")
                            onClicked:androidLibrary.performance(phoneDeviceDetail.avd)}
                    }
                    Text {
                        objectName:"devicePhoneNotice";Layout.fillWidth:true;Layout.leftMargin:Theme.mobileGutter;Layout.rightMargin:Theme.mobileGutter
                        visible:!!androidLibrary.notice && phoneDeviceDetail.emulator;text:androidLibrary.notice
                        color:androidLibrary.failed?Theme.danger:Theme.muted;font.pixelSize:Theme.mobileFontSmall;wrapMode:Text.Wrap
                    }
                }
                DeviceLogPane {
                    Layout.fillWidth:true;Layout.preferredHeight:Theme.px(320);Layout.minimumHeight:Theme.px(200)
                    visible:panel.opened && panel.section==="devices" && panel.showDeviceLog && panel.selectedPlatform==="android" && panel.devices.some(x=>x.id===panel.selectedDevice && x.platform==="android") && (!panel.phone || !!panel.phoneDevice)
                    device:panel.selectedDevice
                }
              }
            }
            Item {
              RowLayout {
                anchors.fill: parent; visible: panel.jobs.length>0
                spacing: 0
                ColumnLayout {
                  objectName: "jobListColumn"
                  // On a phone the list fills the width and gives way to the chosen job.
                  visible: !panel.phone || !panel.selectedJob
                  Layout.fillWidth: panel.phone
                  Layout.preferredWidth: panel.width<Theme.px(760)?Theme.px(216):Theme.px(296); Layout.fillHeight: true; spacing: 0
                  StudioSegmented {
                    objectName: "jobFilter"; Layout.fillWidth: true; Layout.margins: Theme.gapSm; touch: panel.phone
                    current: panel.jobFilter
                    model: [{id:"all",label:qsTr("All"),objectName:"jobFilter-all"},{id:"running",label:qsTr("Running","jobs filter"),objectName:"jobFilter-running"},{id:"failed",label:qsTr("Failed","jobs filter"),objectName:"jobFilter-failed"}]
                    onChosen: id=>panel.jobFilter=id
                  }
                  Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.subtle }
                  ListView {
                    id: jobList
                    objectName: "jobList"
                    Layout.fillWidth: true; Layout.fillHeight: true; model: panel.shownJobs; clip: true
                    // Tab reaches the list; arrows move, Enter or Space opens the job.
                    activeFocusOnTab: true
                    Keys.onReturnPressed: if(currentItem)currentItem.clicked()
                    Keys.onSpacePressed: if(currentItem)currentItem.clicked()
                    Accessible.role: Accessible.List; Accessible.name: qsTr("Jobs")
                    Text { anchors.centerIn: parent; width: parent.width-Theme.gapXl; visible: jobList.count===0; text: panel.jobFilter==="running"?qsTr("Nothing running."):qsTr("No failed job."); color: Theme.muted; font.pixelSize: Theme.fontSmall; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter }
                    // The phone draws each job as a Bogen "Mobile / Row"; the desktop keeps its compact row.
                    delegate: panel.phone ? phoneJobRow : desktopJobRow
                    Component {
                      id: phoneJobRow
                      MobileRow {
                        id: mobileJob
                        required property var modelData
                        objectName: "jobRow-"+modelData.id
                        width: ListView.view ? ListView.view.width : implicitWidth
                        readonly property string project: panel.jobProject(modelData)
                        readonly property string tone: ({queued:"busy",running:"busy",succeeded:"ok",failed:"failed",timed_out:"failed"})[modelData.state] || "idle"
                        title: modelData.label
                        detail: panel.jobStateLabel(modelData)+" · "+panel.jobDuration(modelData)+(project?"\n"+project:"")
                        meta: ""
                        dot: tone==="ok"?Theme.success:tone==="failed"?Theme.danger:tone==="busy"?Theme.warning:Theme.faint
                        highlighted: panel.selectedJob===modelData.id
                        Accessible.name: modelData.label+", "+panel.jobStateLabel(modelData)+", "+panel.jobDuration(modelData)+(project?", "+project:"")
                        onClicked: panel.selectJob(modelData)
                      }
                    }
                    Component {
                    id: desktopJobRow
                    ItemDelegate {
                        id: jobRow
                        required property var modelData
                        objectName: "jobRow-"+modelData.id
                        width: ListView.view.width; height: Theme.rowHeight*2+Theme.gapXs
                        padding: 0; leftPadding: Theme.gapLg; rightPadding: Theme.gapMd
                        background: Rectangle {
                            color: panel.selectedJob===jobRow.modelData.id?Theme.selected:jobRow.hovered?Theme.hover:"transparent"
                            border.width: jobRow.ListView.isCurrentItem && jobList.activeFocus?2:0; border.color: Theme.accentInk
                            Rectangle { visible: panel.selectedJob===jobRow.modelData.id; width: 2; height: parent.height; color: Theme.accent }
                            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.subtle }
                        }
                        readonly property string project: panel.jobProject(modelData)
                        contentItem: ColumnLayout {
                            spacing: Theme.gapXxs
                            Text { Layout.fillWidth: true; text: jobRow.modelData.label; color: Theme.text; font.pixelSize: Theme.fontBody; elide: Text.ElideRight }
                            StudioStatus {
                                Layout.fillWidth: true
                                tone: ({queued:"busy",running:"busy",succeeded:"ok",failed:"failed",timed_out:"failed"})[jobRow.modelData.state] || "idle"
                                text: panel.jobStateLabel(jobRow.modelData)+" · "+panel.jobDuration(jobRow.modelData)
                            }
                            Text { objectName: "jobProject"; Layout.fillWidth: true; visible: !!jobRow.project; text: jobRow.project; color: Theme.faint; font.family: Theme.mono; font.pixelSize: Theme.fontMicro; elide: Text.ElideMiddle }
                        }
                        Accessible.name: modelData.label+", "+panel.jobStateLabel(modelData)+", "+panel.jobDuration(modelData)+(project?", "+project:"")
                        onClicked: panel.selectJob(modelData)
                    }
                    }
                    ScrollBar.vertical: StudioScrollBar {}
                  }
                }
                Rectangle { visible: !panel.phone; Layout.fillHeight: true; Layout.preferredWidth: 1; color: Theme.subtle }
                ColumnLayout {
                    id:jobDetails;Layout.minimumWidth:0
                    visible: !panel.phone || !!panel.selectedJob
                    Layout.fillWidth: true; Layout.fillHeight: true; spacing: 0
                    // On a phone: the job's name in full with its state, a 48 px back target, and the actions on their own row.
                    MobileTopBar {
                        objectName: "jobPhoneBar"; Layout.fillWidth: true; visible: panel.phone
                        title: panel.activeJob.label || ""
                        subtitle: panel.activeJob.id ? panel.jobStateLabel(panel.activeJob)+" · "+panel.jobDuration(panel.activeJob) : ""
                        backText: qsTr("All jobs"); backObjectName: "jobBack"
                        onBack: panel.phoneBack()
                    }
                    Flow {
                        objectName: "jobPhoneActions"; visible: panel.phone; Layout.fillWidth: true; Layout.minimumWidth: 0; Layout.minimumHeight: implicitHeight; Layout.margins: Theme.gapSm; spacing: Theme.gapXs
                        StudioButton { objectName: "jobCancelPhone"; touch: true; text: qsTr("Cancel"); iconName: "stop"; visible: panel.activeJob.state==="running" || panel.activeJob.state==="queued"; onClicked: panel.request("job.cancel",{job_id:panel.selectedJob}) }
                        StudioButton { touch: true; text: "Hot Reload"; quiet: true; iconName: "refresh"; visible: panel.activeJob.state==="running" && panel.activeJob.interactive===true && ["ready","session"].indexOf(panel.activeJob.phase)>=0; onClicked: panel.request("job.send",{job_id:panel.selectedJob,command:"reload"}) }
                        StudioButton { objectName: "jobRerunPhone"; touch: true; text: qsTr("Run again"); iconName: "play"; quiet: true; visible: panel.canRerun(panel.activeJob); onClicked: panel.rerunJob(panel.activeJob) }
                        StudioButton { touch: true; text: "Details"; quiet: true; enabled: panel.selectedJob.length>0; onClicked: { panel.request("job.get",{job_id:panel.selectedJob}); panel.showResult() } }
                        StudioButton { touch: true; text: qsTr("Open log file"); iconName: "external"; quiet: true; visible: !!panel.activeJob.log_path; onClicked: Studio.openJobLog(panel.activeJob.log_path) }
                    }
                    RowLayout {
                        visible: !panel.phone
                        Layout.fillWidth: true; Layout.preferredHeight: Theme.barHeight; Layout.leftMargin: Theme.gapMd; Layout.rightMargin: Theme.gapSm; spacing: Theme.gapXs
                        Text { Layout.fillWidth: true; text: panel.activeJob.label || ""; color: Theme.text; font.pixelSize: Theme.fontBody; font.weight: Font.Medium; elide: Text.ElideRight }
                        StudioButton { text: "Hot Reload"; quiet: true; iconName: "refresh"; visible: panel.activeJob.state==="running" && panel.activeJob.interactive===true && ["ready","session"].indexOf(panel.activeJob.phase)>=0; onClicked: panel.request("job.send",{job_id:panel.selectedJob,command:"reload"}) }
                        StudioButton { text: "Details"; quiet: true; enabled: panel.selectedJob.length>0; onClicked: { panel.request("job.get",{job_id:panel.selectedJob}); panel.showResult() } }
                        StudioButton { objectName: "jobLogFile"; text: qsTr("Open log file"); displayText: ""; iconName: "external"; quiet: true; tip: text; enabled: !!panel.activeJob.log_path; onClicked: Studio.openJobLog(panel.activeJob.log_path) }
                        StudioButton { objectName: "jobRerun"; text: qsTr("Run again"); displayText: panel.width<Theme.px(1000)?"":text; tip: panel.canRerun(panel.activeJob)?text:qsTr("Run again · only for jobs this window started"); iconName: "play"; quiet: true; enabled: panel.canRerun(panel.activeJob); onClicked: panel.rerunJob(panel.activeJob) }
                        StudioButton { objectName: "jobCancel"; text: qsTr("Cancel"); displayText: panel.width<Theme.px(1000)?"":text; tip: text; iconName: "stop"; enabled: panel.activeJob.state==="running" || panel.activeJob.state==="queued"; onClicked: panel.request("job.cancel",{job_id:panel.selectedJob}) }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.subtle }
                    Flow {
                        objectName: "logFilterBar"
                        Layout.fillWidth: true; Layout.minimumWidth:0; Layout.margins: Theme.gapSm; Layout.leftMargin: Theme.gapMd; spacing: Theme.gapXs
                        visible: panel.selectedJob.length>0
                        StudioTextField { id: logSearch; objectName: "logSearch"; width:Math.min(parent.width,Theme.px(220)); placeholderText: qsTr("Filter the log"); text: panel.logQuery; onTextEdited: panel.logQuery=text }
                        StudioSegmented {
                            id:logLevels
                            objectName: "logLevel"; current: panel.logLevel
                            visible:implicitWidth<=parent.width
                            model: [{id:"all",label:qsTr("All lines")},{id:"warning",label:qsTr("Warnings"),objectName:"logLevel-warning"},{id:"error",label:qsTr("Errors"),objectName:"logLevel-error"}]
                            onChosen: id=>panel.logLevel=id
                        }
                        StudioComboBox {
                            objectName:"logLevelPicker";visible:!logLevels.visible;width:Math.min(parent.width,Theme.px(220))
                            textRole:"label";valueRole:"id"
                            model:[{id:"all",label:qsTr("All lines")},{id:"warning",label:qsTr("Warnings")},{id:"error",label:qsTr("Errors")}]
                            currentIndex:["all","warning","error"].indexOf(panel.logLevel)
                            onActivated:panel.logLevel=currentValue
                        }
                        Text { objectName: "logMatches"; visible: panel.logFiltered && panel.width>=Theme.px(760); text: qsTr("%n line(s)","",panel.logLines.length); color: Theme.faint; font.family: Theme.mono; font.pixelSize: Theme.fontMicro;height:Theme.controlHeight;verticalAlignment:Text.AlignVCenter }
                        StudioButton { objectName: "logCopy"; text: panel.logFiltered?qsTr("Copy these lines"):qsTr("Copy log"); displayText: panel.width<Theme.px(1000)?"":text; tip: text; iconName: "copy"; quiet: true; enabled: panel.shownLog.length>0; onClicked: Studio.copy(panel.shownLog) }
                    }
                    StudioCodeView { objectName: "jobLog"; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; text: panel.shownLog; accessibleName: panel.activeJob.label ? qsTr("Log of %1").arg(panel.activeJob.label) : qsTr("Job log"); placeholderText: panel.logFiltered && panel.logText.length ? qsTr("No line of this log matches.") : qsTr("Choose a job to read its log.") }
                    Repeater {
                        model: panel.activeJob.result && panel.activeJob.result.artifacts ? panel.activeJob.result.artifacts : []
                        RowLayout {
                            required property var modelData
                            Layout.fillWidth: true; Layout.margins: Theme.gapSm; Layout.leftMargin: Theme.gapMd; spacing: Theme.gapXs
                            Text { text: modelData.path; elide: Text.ElideMiddle; Layout.fillWidth: true; color: Theme.muted; font.family: Theme.mono; font.pixelSize: Theme.fontSmall }
                            StudioButton { text: qsTr("Copy path"); quiet: true; iconName: "copy"; onClicked: Studio.copy(modelData.path) }
                            StudioButton { text: qsTr("Install"); visible: /\.(apk|ipa)$/.test(modelData.path); enabled: panel.selectedDevice.length>0; onClicked: panel.request("device.install",{platform:panel.selectedPlatform,device:panel.selectedDevice,paths:[modelData.path]}) }
                        }
                    }
                }
            }
              Rectangle {
                anchors.fill:parent;visible:panel.jobs.length===0;color:Theme.green
                StudioEmptyState {
                    anchors.left:parent.left;anchors.leftMargin:Math.max(Theme.gapXl,parent.width*.06);anchors.verticalCenter:parent.verticalCenter;width:Math.min(parent.width-Theme.gapXxxl,Theme.px(860))
                    onBlock:true;ink:Theme.ink;showMark:false
                    title:qsTr("Your next build starts here")
                    body:qsTr("Build or test from the project. Your agent's jobs show up here too, with logs and results.")
                    actionText:qsTr("Go to project");actionIcon:"project"
                    onAction:panel.showTab("project")
                }
              }
            }
            RowLayout {
                spacing: 0
                ColumnLayout {
                    objectName: "toolListColumn"; visible: !panel.phone || panel.toolPane!=="form"; Layout.fillWidth: panel.phone; Layout.preferredWidth: panel.width<Theme.px(760)?Theme.px(216):Theme.px(296); Layout.maximumWidth: panel.phone?Number.POSITIVE_INFINITY:Theme.px(296); Layout.fillHeight: true; spacing: 0
                    StudioTextField { id: actionFilter; objectName: "toolSearch"; Layout.fillWidth: true; Layout.margins: Theme.gapSm; Layout.preferredHeight: panel.phone?Math.max(implicitHeight,Theme.mobileTouch):implicitHeight; placeholderText: qsTr("Search tools…") }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.subtle }
                    ListView {
                        id: toolList; objectName: "toolList"
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                        activeFocusOnTab: true
                        Keys.onReturnPressed: if(currentItem)currentItem.clicked()
                        Keys.onSpacePressed: if(currentItem)currentItem.clicked()
                        Accessible.role: Accessible.List; Accessible.name: qsTr("Tools")
                        model: panel.operations.filter(x=>(OperationTitles.title(x)+" "+x.title+" "+x.name).toLowerCase().indexOf(actionFilter.text.toLowerCase())>=0)
                        delegate: ItemDelegate {
                            id: toolRow
                            required property var modelData
                            width: ListView.view.width; height: panel.phone?Math.max(Theme.rowHeight,Theme.mobileTouch):Theme.rowHeight
                            padding: 0; leftPadding: Theme.gapLg; rightPadding: Theme.gapMd
                            readonly property bool current: !!panel.selectedOperation && panel.selectedOperation.name===modelData.name
                            Accessible.name: OperationTitles.title(modelData)
                            background: Rectangle {
                                color: toolRow.current?Theme.selected:toolRow.hovered?Theme.hover:"transparent"
                                border.width: toolRow.ListView.isCurrentItem && toolList.activeFocus?2:0; border.color: Theme.accentInk
                                Rectangle { visible: toolRow.current; width: 2; height: parent.height; color: Theme.accent }
                            }
                            contentItem: Text { text: OperationTitles.title(toolRow.modelData); color: toolRow.current?Theme.text:Theme.muted; font.pixelSize: Theme.fontBody; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter }
                            onClicked: panel.chooseOperation(modelData)
                        }
                        ScrollBar.vertical: StudioScrollBar {}
                        // No tools: the control program has not answered, and says why.
                        StudioEmptyState {
                            objectName: "toolsUnavailable"; actionName: "toolsRetry"
                            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: Theme.gapLg
                            visible: panel.operations.length===0
                            showMark: false
                            title: qsTr("No tools yet.")
                            body: Agent.cliError || qsTr("The control program has not listed its tools yet.")
                            actionText: qsTr("Try again"); actionIcon: "refresh"
                            onAction: Agent.request("schema")
                        }
                        Text {
                            objectName: "toolsNoMatch"
                            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: Theme.gapLg
                            visible: panel.operations.length>0 && toolList.count===0
                            text: qsTr("No tool matches “%1”.").arg(actionFilter.text)
                            color: Theme.muted; font.pixelSize: Theme.fontSmall; wrapMode: Text.WordWrap
                        }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.subtle }
                    StudioButton { objectName: "toolsConnectAgent"; text: qsTr("Connect your coding agent"); displayText: panel.width<Theme.px(760)?qsTr("Agent"):text; tip: text; iconName: "plus"; quiet: true; touch: panel.phone; Layout.fillWidth: true; Layout.margins: Theme.gapSm; onClicked: panel.connectAgentRequested() }
                }
                Rectangle { visible: !panel.phone; Layout.fillHeight: true; Layout.preferredWidth: 1; color: Theme.subtle }
                ColumnLayout {
                    objectName: "toolFormColumn"; visible: !panel.phone || panel.toolPane==="form"
                    Layout.fillWidth: true; Layout.fillHeight: true; Layout.margins: Theme.gapLg; Layout.topMargin: panel.phone?0:Theme.gapLg; spacing: Theme.gapSm
                    // On a phone the form has the whole width; back returns to the list of tools.
                    MobileTopBar { objectName: "toolPhoneBar"; Layout.fillWidth: true; visible: panel.phone; title: qsTr("Agents & tools"); backText: qsTr("All tools"); onBack: panel.phoneBack() }
                    Text { Layout.fillWidth: true; text: panel.selectedOperation?OperationTitles.title(panel.selectedOperation):qsTr("Result"); color: Theme.text; font.pixelSize: Theme.fontDisplay; font.weight: Font.Black; font.letterSpacing: -Theme.fontDisplay*.038; elide: Text.ElideRight }
                    Text { Layout.fillWidth: true; visible: !!panel.selectedOperation; text: panel.selectedOperation?panel.selectedOperation.name:""; color: Theme.faint; font.family: Theme.mono; font.pixelSize: Theme.fontSmall; elide: Text.ElideRight }
                    Text { Layout.fillWidth: true; text: panel.selectedOperation?panel.selectedOperation.description:qsTr("Your agent can call every one of these over MCP or the command line."); wrapMode: Text.Wrap; color: Theme.muted; font.pixelSize: Theme.fontBody; maximumLineCount: 4; elide: Text.ElideRight }
                    StudioScrollView { id: toolForm; Layout.fillWidth: true; Layout.preferredHeight: Math.min(formColumn.implicitHeight,Math.max(120,panel.height*.42)); clip: true; contentWidth: availableWidth; visible: panel.selectedOperation!==null
                        ColumnLayout { id: formColumn; width: Math.min(toolForm.availableWidth-Theme.gapMd,Theme.px(720)); spacing: Theme.gapSm
                            Repeater {
                                model: panel.selectedOperation ? Object.keys(panel.selectedOperation.inputSchema.properties) : []
                                ColumnLayout {
                                    id: field
                                    required property string modelData
                                    property var spec: panel.selectedOperation.inputSchema.properties[modelData]
                                    Layout.fillWidth: true; spacing: Theme.gapXs
                                    Text { text: field.modelData+(panel.selectedOperation.inputSchema.required.indexOf(field.modelData)>=0?" *":"")+(panel.fieldLimits(field.spec)?"  ·  "+panel.fieldLimits(field.spec):""); color: Theme.muted; font.family: Theme.mono; font.pixelSize: Theme.fontSmall }
                                    Text { objectName: "toolFieldHelp-"+field.modelData; Layout.fillWidth: true; visible: !!field.spec.description; text: field.spec.description || ""; color: Theme.faint; font.pixelSize: Theme.fontSmall; wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideRight }
                                    Loader {
                                        Layout.fillWidth: true
                                        sourceComponent: field.spec.type==="boolean"?booleanField:field.spec.enum?enumField:textField
                                        Component { id: booleanField; StudioCheckBox { checked: panel.formValues[field.modelData]===true; text: checked?qsTr("Yes"):qsTr("No"); onToggled: panel.formValues[field.modelData]=checked } }
                                        Component { id: enumField; StudioComboBox { model: ["—"].concat(field.spec.enum); currentIndex: panel.formValues[field.modelData]===undefined?0:field.spec.enum.indexOf(panel.formValues[field.modelData])+1; onActivated: panel.formValues[field.modelData]=currentIndex>0?currentText:undefined } }
                                        Component { id: textField; StudioTextField { text: panel.formValues[field.modelData]===undefined?"":typeof panel.formValues[field.modelData]==="object"?JSON.stringify(panel.formValues[field.modelData]):String(panel.formValues[field.modelData]); placeholderText: field.spec.type==="object"?"{}":field.spec.type==="array"?'["…"]':field.spec.description||""; selectByMouse: true; onTextEdited: panel.formValues[field.modelData]=text } }
                                    }
                                }
                            }
                        }
                    }
                    Rectangle {
                        objectName: "toolConfirm"; Layout.fillWidth: true; visible: panel.confirmingRun
                        implicitHeight: confirmRow.implicitHeight+Theme.gapSm*2; color: Theme.dangerSurface
                        Rectangle { width: Theme.gapXs; height: parent.height; color: Theme.danger }
                        GridLayout { id: confirmRow; columns: panel.phone?1:3; anchors.fill: parent; anchors.margins: Theme.gapSm; anchors.leftMargin: Theme.gapLg; columnSpacing: Theme.gapSm; rowSpacing: Theme.gapSm
                            Text { Layout.fillWidth: true; text: qsTr("This tool changes or removes something and cannot be undone. Run it?"); color: Theme.danger; wrapMode: Text.Wrap; font.pixelSize: Theme.fontSmall }
                            StudioButton { objectName: "toolConfirmRun"; Layout.fillWidth: panel.phone; Layout.maximumWidth: naturalWidth; touch: panel.phone; text: qsTr("Run anyway"); danger: true; onClicked: panel.submitForm(true) }
                            StudioButton { text: qsTr("Cancel"); quiet: true; onClicked: panel.confirmingRun=false }
                        }
                    }
                    Flow { Layout.fillWidth:true; Layout.minimumWidth:0; visible: panel.selectedOperation!==null; spacing: Theme.gapSm
                        StudioButton { objectName: "toolRun"; touch: panel.phone; text: qsTr("Run"); iconName: "play"; accent: true; tip: panel.destructiveOperation?qsTr("Asks before it runs: this tool changes or removes something"):""; onClicked: panel.submitForm() }
                        StudioButton { objectName: "toolCopyCommand"; touch: panel.phone; text: qsTr("Copy as command"); iconName: "copy"; quiet: true; tip: qsTr("Copy this call as a tomaccoctl command for a terminal or a script"); onClicked: panel.copyCommand() }
                    }
                    StudioCodeView { Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: Theme.px(80); clip: true; placeholderText: qsTr("The result shows up here."); accessibleName: qsTr("Result"); text: panel.resultText }
                }
            }
            Loader { id: debugPage; active: !!panel.visited.debug; sourceComponent: Component { DebugWorkspace { workspace: panel } } }
            Loader { active: !!panel.visited.testing; sourceComponent: Component { TestWorkspace { workspace: panel } } }
            Loader { active: !!panel.visited.profiling; sourceComponent: Component { ProfileWorkspace { workspace: panel } } }
            FileWorkspace { id: fileWorkspace; workspace: panel }
            Loader { active: !!panel.visited.release; sourceComponent: Component { ReleaseWorkspace { workspace: panel } } }
            Loader { id:demoPage; active: !!panel.visited.demo; sourceComponent: Component { DemoWorkspace { workspace: panel } } }
        }
    }
    FolderDialog { id: projectFolder; title: qsTr("Choose project"); onAccepted: panel.openProject(decodeURIComponent(selectedFolder.toString().replace(/^file:\/\//,""))) }
    FileDialog { id: installFile; title: qsTr("Install app on device"); nameFilters: [qsTr("Mobile apps (*.apk *.ipa)")]; onAccepted: panel.request("device.install",{platform:panel.selectedPlatform,device:panel.selectedDevice,paths:[decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,""))]}) }
    FileDialog { id: screenshotFile; title: qsTr("Save device screenshot"); fileMode: FileDialog.SaveFile; nameFilters: ["PNG (*.png)"]; onAccepted: panel.request("device.screenshot",{platform:panel.selectedPlatform,device:panel.selectedDevice,path:decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,""))}) }
}
