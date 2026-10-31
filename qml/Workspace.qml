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
        {id:"release",tone:"red",title:qsTr("Signing & release"),description:qsTr("Sign it, version it and ship it.")}
    ]
    readonly property string section: sections[currentIndex].id
    function open() { opened=true }
    function close() { opened=false }
    property var project: ({})
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
    property var pending: ({})
    property var formValues: ({})
    property var selectedOperation: null
    property string initialPath: ""
    property string selectedDevice: ""
    property string selectedPlatform: "android"
    signal profilesChanged()
    signal runtimeRequested(string platform, string device)
    signal iosSetupRequested()
    function chooseDevicePlatform(value) {
        selectedPlatform=value
        if(!devices.some(x=>x.id===selectedDevice && x.platform===value)) {
            let rows=devices.filter(x=>x.platform===value)
            selectedDevice=rows.length?rows[0].id:""
        }
    }
    function viewState() {
        return {visible:opened,tab:["project","devices","jobs","tools","debug","testing","profiling","files","release"][panel.currentIndex],path:project.path || "",file:fileWorkspace.current,files:fileWorkspace.tabs,dirty:fileWorkspace.dirtyFiles(),entries:fileWorkspace.entries.length,notice:fileWorkspace.notice,
            ios_research:{guest:iosResearch.guestState(),virtual_phone:iosResearch.virtualPhone,visible:iosResearch.opened,job_id:iosResearch.jobId,job_state:iosResearch.jobState,report_directory:iosResearch.reportDirectory,guest_observations_verified:iosResearch.report.guest_observations_verified===true,graphical_apps_verified:false}}
    }
    function hasUnsaved() { return fileWorkspace.hasUnsaved() }
    function configureIosGuest(args) { iosResearch.configureGuest(args) }
    // The virtual iPhone's own window takes part in the app's device-only mode.
    property bool guestSolo: false
    readonly property bool guestWindowVisible: iosResearch.guestWindowVisible
    signal guestCloseRequested()
    signal guestDockRequested()

    function showTab(name) {
        if(name==="ios-research") {currentIndex=1;chooseDevicePlatform("ios");open();Qt.callLater(()=>iosResearch.open());return}
        let index=["project","devices","jobs","tools","debug","testing","profiling","files","release"].indexOf(name)
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
        if(!quiet) { failed=false; status=qsTr("Running…"); running=true }
        return id
    }
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
    function showAction(name) {
        panel.currentIndex=3
        for(let operation of Agent.operations) if(operation.name===name) { chooseOperation(operation); break }
    }
    function submitForm() {
        try {
            let args={}
            for(let key in selectedOperation.inputSchema.properties) {
                let value=formValues[key], spec=selectedOperation.inputSchema.properties[key]
                if(value===undefined || value==="") { if(selectedOperation.inputSchema.required.indexOf(key)>=0) throw new Error(qsTr("%1 is missing").arg(key)); continue }
                if(spec.type==="integer" || spec.type==="number") value=Number(value)
                if((spec.type==="object" || spec.type==="array") && typeof value==="string") value=JSON.parse(value)
                args[key]=value
            }
            request(selectedOperation.name,args)
        } catch(error) { status=error.message; failed=true }
    }
    onOpenedChanged: {
        if(!opened)return
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
                if(!context.quiet) { panel.failed=true; panel.status=response.error.message+(response.error.remedy ? " · "+response.error.remedy : ""); panel.resultText=JSON.stringify(response,null,2) }
                if(response.error.code==="workspace_busy" && response.error.details.job){panel.selectJob(response.error.details.job);panel.status=qsTr("A job is already running for this project. Use the running session or end it first.")}
                return
            }
            let value=response.result
            if(operation==="workspace.list") { panel.savedWorkspaces=value
            } else if(operation==="workspace.save") { panel.request("workspace.list",{},true)
            } else if(operation==="workspace.context") { panel.openProject(value.project.path);panel.selectedDevice=value.workspace.device;panel.selectedPlatform=value.workspace.platform;bundleId.text=value.workspace.bundle_id
            } else if(operation==="job.list") {
                panel.jobs=value
                for(let job of value) if(job.id===panel.selectedJob) panel.activeJob=job
            } else if(operation==="job.logs") {
                if(value.id===panel.selectedJob && value.offset===panel.logOffset) { panel.logText=(panel.logText+value.text).slice(-200000); panel.logOffset=value.next_offset }
            } else if(operation==="project.inspect") {
                if(JSON.parse(context.signature).path!==projectPath.text)return
                projectPath.text=value.path
                panel.project=value; Studio.rememberProject(value.path); if(panel.revealPath) { fileWorkspace.openFile(panel.revealPath,panel.revealLine);panel.revealPath="" } panel.status=value.name+" · "+value.kinds.join(" / ")
                if(value.configuration.applicationId) bundleId.text=value.configuration.applicationId
                if(value.platforms.length) platform.currentIndex=Math.max(0,["android","ios","web","linux"].indexOf(value.platforms[0]))
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
    ColumnLayout {
        anchors.fill: parent
        spacing: 0
        StudioBand {
            id: band
            Layout.fillWidth: true
            visible: !panel.fullBleed
            roomy: panel.height>=680 && !(panel.section==="debug" && debugPage.active)
            title: panel.sections[panel.currentIndex].title
            lead: panel.sections[panel.currentIndex].description
            tone: panel.sections[panel.currentIndex].tone || "inverse"
            detail: panel.section==="debug" ? debugPage.stateLabel : ""
            StudioButton { text: "Toolchain"; iconName: "settings"; inverted: true; invertedFill: band.ink; invertedText: band.color; visible: panel.section==="project"; onClicked: { panel.showAction("doctor");panel.request("doctor",{}) } }
        }
        Rectangle {
            Layout.fillWidth: true; implicitHeight: feedback.implicitHeight+Theme.gapSm*2
            visible: panel.failed || panel.running
            color: panel.failed ? Theme.dangerSurface : Theme.elevated
            Rectangle { width: Theme.gapXs; height: parent.height; color: panel.failed ? Theme.danger : Theme.accent }
            Text { id: feedback; anchors.fill: parent; anchors.margins: Theme.gapSm; anchors.leftMargin: Theme.gapLg; text: panel.status; color: panel.failed ? Theme.danger : Theme.text; wrapMode: Text.Wrap; font.pixelSize: Theme.fontSmall }
        }
        StackLayout {
            currentIndex: panel.currentIndex; Layout.fillWidth: true; Layout.fillHeight: true
            Layout.margins: panel.fullBleed ? 0 : panel.width<850 ? Theme.gapLg : Theme.gapXl
            StudioScrollView {
                id:projectPage;objectName:"projectOverview";clip:true;contentWidth:availableWidth
                GridLayout {
                    width:Math.min(projectPage.availableWidth-Theme.gapLg,1240);columns:1;rowSpacing:Theme.gapXs
                    StudioPanel {
                        Layout.fillWidth:true;Layout.columnSpan:parent.columns
                        title:panel.project.name || qsTr("Start with a project")
                        actions:[StudioButton {text:qsTr("New project");quiet:true;iconName:"plus";onClicked:panel.showAction("project.create")}]
                        RowLayout {Layout.fillWidth:true;spacing:Theme.gapXs
                            StudioTextField {id:projectPath;Layout.fillWidth:true;font.family:Theme.mono;font.pixelSize:Theme.fontSmall;placeholderText:qsTr("Path to your Swift, Kotlin or Flutter project");onAccepted:panel.openProject(text)}
                            StudioButton {text:qsTr("Choose folder");accent:!panel.project.path;onClicked:projectFolder.open()}
                            StudioButton {text:qsTr("Open");visible:projectPath.text!==panel.project.path;enabled:!!projectPath.text;onClicked:panel.openProject(projectPath.text)}
                        }
                        RowLayout {visible:!!panel.project.path;Layout.fillWidth:true
                            Text {text:panel.project.kinds?panel.project.kinds.join("  ·  "):"";color:Theme.muted;font.family:Theme.mono;font.pixelSize:Theme.fontSmall;Layout.fillWidth:true}
                            StudioButton {text:qsTr("Open files");quiet:true;iconName:"files";onClicked:panel.showTab("files")}
                        }
                    }
                    StudioPanel {
                        Layout.fillWidth:true
                        visible:!!panel.project.path
                        title:qsTr("Build")
                        GridLayout {Layout.fillWidth:true;columns:width<340?1:(platform.currentText==="android" || platform.currentText==="ios"?3:2);columnSpacing:Theme.gapSm;rowSpacing:Theme.gapSm
                            ColumnLayout {Layout.fillWidth:true;Layout.preferredWidth:1;spacing:Theme.gapXs
                                StudioSection {text:qsTr("Platform")}
                                StudioComboBox {id:platform;model:["android","ios","web","linux"];Layout.fillWidth:true;implicitWidth:80;displayText:({android:"Android",ios:"iOS",web:"Web",linux:"Linux"})[currentText] || currentText}
                            }
                            ColumnLayout {Layout.fillWidth:true;Layout.preferredWidth:1;spacing:Theme.gapXs
                                StudioSection {text:qsTr("Configuration")}
                                StudioComboBox {id:configuration;model:["debug","release"];Layout.fillWidth:true;implicitWidth:80;displayText:currentText==="debug"?"Debug":"Release"}
                            }
                            ColumnLayout {visible:platform.currentText==="android" || platform.currentText==="ios";Layout.fillWidth:true;Layout.preferredWidth:1;spacing:Theme.gapXs
                                StudioSection {text:qsTr("Package format")}
                                StudioComboBox {id:format;Layout.fillWidth:true;implicitWidth:80;model:platform.currentText==="ios"?["ipa"]:platform.currentText==="android"?["apk","aab"]:["apk"];displayText:currentText.toUpperCase()}
                            }
                        }
                        StudioCheckBox {id:signBuild;visible:platform.currentText==="ios";text:qsTr("Sign build");checked:true}
                        Flow {Layout.fillWidth:true;spacing:Theme.gapXs
                            StudioButton {text:qsTr("Build");iconName:"play";accent:true;enabled:!!panel.project.path;onClicked:panel.build("build")}
                            StudioButton {text:"Tests";enabled:!!panel.project.path;onClicked:panel.build("test")}
                            StudioButton {text:qsTr("Check code");enabled:!!panel.project.path;onClicked:panel.build("lint")}
                            StudioButton {text:qsTr("Show build steps");quiet:true;enabled:!!panel.project.path;onClicked:{panel.request("project.plan",{path:projectPath.text,platform:platform.currentText,configuration:configuration.currentText,format:format.currentText});panel.currentIndex=3}}
                            StudioButton {text:qsTr("SDK & tool paths");quiet:true;onClicked:panel.showAction("settings.configure")}
                        }
                    }
                    StudioPanel {
                        Layout.fillWidth:true
                        visible:!!panel.project.path
                        title:qsTr("Run on device")
                        actions:[StudioButton {text:qsTr("Manage devices");quiet:true;onClicked:panel.showTab("devices")}]
                        StudioComboBox {Layout.fillWidth:true;model:panel.devices;textRole:"name";valueRole:"id";currentIndex:panel.devices.findIndex(x=>x.id===panel.selectedDevice);displayText:currentIndex>=0?currentText:qsTr("Choose a smartphone or emulator");onActivated:{panel.selectedDevice=currentValue;panel.selectedPlatform=panel.devices[currentIndex].platform}}
                        RowLayout {Layout.fillWidth:true;spacing:Theme.gapXs
                            StudioTextField {id:bundleId;Layout.fillWidth:true;font.family:Theme.mono;font.pixelSize:Theme.fontSmall;placeholderText:qsTr("Application ID, e.g. com.example.app")}
                            StudioButton {text:panel.projectJob.id?qsTr("Job running"):qsTr("Build & run");iconName:"play";accent:true;enabled:!!panel.selectedDevice && !!panel.project.path && !panel.projectRequestPending && !panel.projectJob.id;onClicked:panel.request("project.run",{path:projectPath.text,platform:panel.selectedPlatform,device:panel.selectedDevice,bundle_id:bundleId.text,configuration:configuration.currentText})}
                        }
                        Text {visible:!panel.selectedDevice;Layout.fillWidth:true;text:qsTr("Plug in a phone or start an emulator under Devices.");color:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
                    }
                    StudioPanel {
                        Layout.fillWidth:true;Layout.columnSpan:parent.columns
                        title:qsTr("Save workspace")
                        lead:qsTr("Remember this project, platform and device together.")
                        RowLayout {Layout.fillWidth:true;spacing:Theme.gapXs
                            StudioComboBox {Layout.fillWidth:true;implicitWidth:80;model:Object.keys(panel.savedWorkspaces);displayText:currentText || qsTr("Load saved workspace");onActivated:panel.request("workspace.context",{name:currentText})}
                            StudioTextField {id:workspaceName;placeholderText:qsTr("Workspace name");Layout.preferredWidth:180}
                            StudioButton {text:qsTr("Save");enabled:!!panel.project.path && !!workspaceName.text;onClicked:panel.request("workspace.save",{name:workspaceName.text,path:projectPath.text,platform:platform.currentText,device:panel.selectedDevice,bundle_id:bundleId.text})}
                        }
                    }
                }
            }
            StudioScrollView {
                id:devicesPage;objectName:"devicesPage";clip:true;contentWidth:availableWidth
              ColumnLayout {
                // As tall as the page at least, so the lists keep filling it, and taller when its content
                // needs more (the virtual iPhone's card on the smallest window): then the page scrolls.
                width:devicesPage.availableWidth;height:Math.max(implicitHeight,devicesPage.availableHeight)
                spacing:Theme.gapMd
                RowLayout {Layout.fillWidth:true;spacing:Theme.gapXs
                    StudioSegmented {
                        current:panel.selectedPlatform
                        model:[{id:"android",label:"Android",objectName:"devices-android"},{id:"ios",label:"Apple",objectName:"devices-apple"}]
                        onChosen:id=>panel.chooseDevicePlatform(id)
                    }
                    Item {Layout.fillWidth:true}
                    StudioButton {visible:panel.selectedPlatform==="android";text:qsTr("Connect via Wi-Fi");quiet:true;onClicked:panel.showAction("android.connect")}
                    StudioButton {visible:panel.selectedPlatform==="ios";text:qsTr("Set up iPhone");quiet:true;onClicked:panel.iosSetupRequested()}
                    StudioButton {text:qsTr("Find devices");iconName:"refresh";onClicked:panel.request("device.list",{})}
                }
                AndroidDevices {Layout.fillWidth:true;Layout.fillHeight:true;visible:panel.selectedPlatform==="android";workspace:panel}
                IosResearch {id:iosResearch;Layout.fillWidth:true;visible:panel.selectedPlatform==="ios";workspace:panel;guestSolo:panel.guestSolo;onGuestCloseRequested:panel.guestCloseRequested();onGuestDockRequested:panel.guestDockRequested()}
                ListView {
                    id:deviceList;Layout.fillWidth:true;Layout.fillHeight:true;Layout.minimumHeight:160
                    visible:panel.selectedPlatform==="ios"
                    model:panel.devices.filter(x=>x.platform===panel.selectedPlatform);clip:true;spacing:Theme.gapXs
                    delegate:ItemDelegate {
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
                    StudioEmptyState {
                        anchors.centerIn:parent;width:Math.min(parent.width-Theme.gapXxl,480);visible:deviceList.count===0
                        title:panel.selectedPlatform==="ios"?qsTr("No iPhone yet."):qsTr("No phone yet.")
                        body:panel.selectedPlatform==="ios"?qsTr("Plug it in, unlock it and trust this computer. Setup takes you through the rest."):qsTr("Plug in a phone with USB debugging on, or start an emulator.")
                        actionText:panel.selectedPlatform==="ios"?qsTr("Set up iPhone"):"";actionIcon:"devices"
                        onAction:panel.iosSetupRequested()
                    }
                    ScrollBar.vertical:StudioScrollBar {}
                }
                Rectangle {Layout.fillWidth:true;Layout.preferredHeight:1;color:Theme.subtle}
                Flow {id:deviceActions;objectName:"deviceActions";Layout.fillWidth:true;spacing:Theme.gapXs;enabled:panel.devices.some(x=>x.id===panel.selectedDevice && x.platform===panel.selectedPlatform)
                    StudioButton {text:qsTr("Open live device");iconName:"phone";accent:true;onClicked:panel.runtimeRequested(panel.selectedPlatform,panel.selectedDevice)}
                    StudioButton {text:panel.selectedPlatform==="ios"?qsTr("Install IPA"):qsTr("Install APK");onClicked:installFile.open()}
                    StudioButton {text:"Logs";quiet:true;onClicked:panel.request("device.logs",{platform:panel.selectedPlatform,device:panel.selectedDevice,duration:60})}
                    StudioButton {text:"Screenshot";quiet:true;onClicked:screenshotFile.open()}
                    StudioButton {text:qsTr("Information");quiet:true;onClicked:{panel.request("device.info",{platform:panel.selectedPlatform,device:panel.selectedDevice});panel.currentIndex=3}}
                    StudioButton {text:qsTr("Calibrate model");quiet:true;visible:panel.selectedPlatform==="android";onClicked:panel.showAction("android.calibrate")}
                }
              }
            }
            Item {
              RowLayout {
                anchors.fill: parent; visible: panel.jobs.length>0
                spacing: 0
                ColumnLayout {
                  Layout.preferredWidth: panel.width<760?216:296; Layout.fillHeight: true; spacing: 0
                  StudioSegmented {
                    objectName: "jobFilter"; Layout.fillWidth: true; Layout.margins: Theme.gapSm
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
                    delegate: ItemDelegate {
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
                    ScrollBar.vertical: StudioScrollBar {}
                  }
                }
                Rectangle { Layout.fillHeight: true; Layout.preferredWidth: 1; color: Theme.subtle }
                ColumnLayout {
                    Layout.fillWidth: true; Layout.fillHeight: true; spacing: 0
                    RowLayout {
                        Layout.fillWidth: true; Layout.preferredHeight: Theme.barHeight; Layout.leftMargin: Theme.gapMd; Layout.rightMargin: Theme.gapSm; spacing: Theme.gapXs
                        Text { Layout.fillWidth: true; text: panel.activeJob.label || ""; color: Theme.text; font.pixelSize: Theme.fontBody; font.weight: Font.Medium; elide: Text.ElideRight }
                        StudioButton { text: "Hot Reload"; quiet: true; iconName: "refresh"; visible: panel.activeJob.state==="running" && panel.activeJob.interactive===true && ["ready","session"].indexOf(panel.activeJob.phase)>=0; onClicked: panel.request("job.send",{job_id:panel.selectedJob,command:"reload"}) }
                        StudioButton { text: "Details"; quiet: true; enabled: panel.selectedJob.length>0; onClicked: { panel.request("job.get",{job_id:panel.selectedJob}); panel.currentIndex=3 } }
                        StudioButton { objectName: "jobLogFile"; text: qsTr("Open log file"); displayText: ""; iconName: "external"; quiet: true; tip: text; enabled: !!panel.activeJob.log_path; onClicked: Studio.openJobLog(panel.activeJob.log_path) }
                        StudioButton { objectName: "jobRerun"; text: qsTr("Run again"); displayText: panel.width<1000?"":text; tip: panel.canRerun(panel.activeJob)?text:qsTr("Run again · only for jobs this window started"); iconName: "play"; quiet: true; enabled: panel.canRerun(panel.activeJob); onClicked: panel.rerunJob(panel.activeJob) }
                        StudioButton { objectName: "jobCancel"; text: qsTr("Cancel"); displayText: panel.width<1000?"":text; tip: text; iconName: "stop"; enabled: panel.activeJob.state==="running" || panel.activeJob.state==="queued"; onClicked: panel.request("job.cancel",{job_id:panel.selectedJob}) }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.subtle }
                    RowLayout {
                        objectName: "logFilterBar"
                        Layout.fillWidth: true; Layout.margins: Theme.gapSm; Layout.leftMargin: Theme.gapMd; spacing: Theme.gapXs
                        visible: panel.selectedJob.length>0
                        StudioTextField { id: logSearch; objectName: "logSearch"; Layout.fillWidth: true; implicitWidth: 80; placeholderText: qsTr("Filter the log"); text: panel.logQuery; onTextEdited: panel.logQuery=text }
                        StudioSegmented {
                            objectName: "logLevel"; current: panel.logLevel
                            model: [{id:"all",label:qsTr("All lines")},{id:"warning",label:qsTr("Warnings"),objectName:"logLevel-warning"},{id:"error",label:qsTr("Errors"),objectName:"logLevel-error"}]
                            onChosen: id=>panel.logLevel=id
                        }
                        Text { objectName: "logMatches"; visible: panel.logFiltered && panel.width>=760; text: qsTr("%n line(s)","",panel.logLines.length); color: Theme.faint; font.family: Theme.mono; font.pixelSize: Theme.fontMicro }
                        StudioButton { objectName: "logCopy"; text: panel.logFiltered?qsTr("Copy these lines"):qsTr("Copy log"); displayText: panel.width<1000?"":text; tip: text; iconName: "copy"; quiet: true; enabled: panel.shownLog.length>0; onClicked: Studio.copy(panel.shownLog) }
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
                    anchors.left:parent.left;anchors.leftMargin:Math.max(Theme.gapXl,parent.width*.06);anchors.verticalCenter:parent.verticalCenter;width:Math.min(parent.width-Theme.gapXxxl,860)
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
                    Layout.preferredWidth: panel.width<760?216:296; Layout.maximumWidth: 296; Layout.fillHeight: true; spacing: 0
                    StudioTextField { id: actionFilter; Layout.fillWidth: true; Layout.margins: Theme.gapSm; placeholderText: qsTr("Search tools…") }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.subtle }
                    ListView {
                        id: toolList; objectName: "toolList"
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                        activeFocusOnTab: true
                        Keys.onReturnPressed: if(currentItem)currentItem.clicked()
                        Keys.onSpacePressed: if(currentItem)currentItem.clicked()
                        Accessible.role: Accessible.List; Accessible.name: qsTr("Tools")
                        model: Agent.operations.filter(x=>(OperationTitles.title(x)+" "+x.title+" "+x.name).toLowerCase().indexOf(actionFilter.text.toLowerCase())>=0)
                        delegate: ItemDelegate {
                            id: toolRow
                            required property var modelData
                            width: ListView.view.width; height: Theme.rowHeight
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
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.subtle }
                    StudioButton { text: qsTr("Copy MCP configuration"); displayText: panel.width<760?"MCP":text; tip: text; iconName: "copy"; quiet: true; Layout.fillWidth: true; Layout.margins: Theme.gapSm; onClicked: Studio.copy(JSON.stringify({mcpServers:{"tomacco":{command:Agent.cliPath,args:["mcp"]}}},null,2)) }
                }
                Rectangle { Layout.fillHeight: true; Layout.preferredWidth: 1; color: Theme.subtle }
                ColumnLayout {
                    Layout.fillWidth: true; Layout.fillHeight: true; Layout.margins: Theme.gapLg; spacing: Theme.gapSm
                    Text { Layout.fillWidth: true; text: panel.selectedOperation?OperationTitles.title(panel.selectedOperation):qsTr("Result"); color: Theme.text; font.pixelSize: Theme.fontDisplay; font.weight: Font.Black; font.letterSpacing: -Theme.fontDisplay*.038; elide: Text.ElideRight }
                    Text { Layout.fillWidth: true; visible: !!panel.selectedOperation; text: panel.selectedOperation?panel.selectedOperation.name:""; color: Theme.faint; font.family: Theme.mono; font.pixelSize: Theme.fontSmall; elide: Text.ElideRight }
                    Text { Layout.fillWidth: true; text: panel.selectedOperation?panel.selectedOperation.description:qsTr("Your agent can call every one of these over MCP or the command line."); wrapMode: Text.Wrap; color: Theme.muted; font.pixelSize: Theme.fontBody; maximumLineCount: 4; elide: Text.ElideRight }
                    StudioScrollView { id: toolForm; Layout.fillWidth: true; Layout.preferredHeight: Math.min(formColumn.implicitHeight,Math.max(120,panel.height*.42)); clip: true; contentWidth: availableWidth; visible: panel.selectedOperation!==null
                        ColumnLayout { id: formColumn; width: Math.min(toolForm.availableWidth-Theme.gapMd,720); spacing: Theme.gapSm
                            Repeater {
                                model: panel.selectedOperation ? Object.keys(panel.selectedOperation.inputSchema.properties) : []
                                ColumnLayout {
                                    id: field
                                    required property string modelData
                                    property var spec: panel.selectedOperation.inputSchema.properties[modelData]
                                    Layout.fillWidth: true; spacing: Theme.gapXs
                                    Text { text: field.modelData+(panel.selectedOperation.inputSchema.required.indexOf(field.modelData)>=0?" *":""); color: Theme.muted; font.family: Theme.mono; font.pixelSize: Theme.fontSmall }
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
                    StudioButton { text: qsTr("Run"); iconName: "play"; accent: true; visible: panel.selectedOperation!==null; onClicked: panel.submitForm() }
                    StudioCodeView { Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 80; clip: true; placeholderText: qsTr("The result shows up here."); accessibleName: qsTr("Result"); text: panel.resultText }
                }
            }
            DebugWorkspace { id: debugPage; workspace: panel }
            TestWorkspace { workspace: panel }
            ProfileWorkspace { workspace: panel }
            FileWorkspace { id: fileWorkspace; workspace: panel }
            ReleaseWorkspace { workspace: panel }
        }
    }
    FolderDialog { id: projectFolder; title: qsTr("Choose project"); onAccepted: panel.openProject(decodeURIComponent(selectedFolder.toString().replace(/^file:\/\//,""))) }
    FileDialog { id: installFile; title: qsTr("Install app on device"); nameFilters: [qsTr("Mobile apps (*.apk *.ipa)")]; onAccepted: panel.request("device.install",{platform:panel.selectedPlatform,device:panel.selectedDevice,paths:[decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,""))]}) }
    FileDialog { id: screenshotFile; title: qsTr("Save device screenshot"); fileMode: FileDialog.SaveFile; nameFilters: ["PNG (*.png)"]; onAccepted: panel.request("device.screenshot",{platform:panel.selectedPlatform,device:panel.selectedDevice,path:decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,""))}) }
}
