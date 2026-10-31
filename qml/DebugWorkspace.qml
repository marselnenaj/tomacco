import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "."

StudioScrollView {
    id: view
    objectName: "debugWorkspace"
    required property var workspace
    clip: true
    implicitWidth: 0
    implicitHeight: 0
    contentWidth: availableWidth
    property string session: ""
    property var state: ({})
    property var sessions: []
    property var threads: []
    property var frames: []
    property var variables: []
    property string threadId: ""
    property string frameId: ""
    property string sourcePath: ""
    property int sourceLine: 1
    property string details: ""
    property string failure: ""
    property var existingSession: ({})
    property var requests: ({})
    property int cursor: 0
    property string events: ""
    property bool starting: false
    property bool controlling: false
    property bool refreshing: false
    property bool showEvents: false
    readonly property bool active: !!session && !state.terminated
    readonly property bool android: backend.currentIndex!==2
    readonly property bool nativeAndroid: backend.currentIndex===1
    readonly property var androidDevices: workspace.devices.filter(x=>x.platform==="android")
    readonly property bool hasAndroid: workspace.selectedPlatform==="android" && androidDevices.some(x=>x.id===workspace.selectedDevice)
    readonly property string stateLabel: starting ? qsTr("Connecting…") : ({starting:qsTr("Connecting…"),stopped:qsTr("Paused"),running:qsTr("Running"),terminated:qsTr("Ended"),disconnected:qsTr("Disconnected"),worker_exited:qsTr("Debugger exited")})[state.state] || qsTr("No session")

    function call(op,args) {
        let quiet=["debug.state","debug.events","debug.threads","debug.stack","debug.variables","debug.list"].indexOf(op)>=0
        let id=workspace.request(op,args,quiet)
        requests[id]={operation:op,session:args.session || "",reference:args.reference || "",frame:frameId,thread:args.thread_id || ""}
        return id
    }
    function clearFrames() { frames=[];variables=[];frameId="";sourcePath="";details="" }
    function follow(value) {
        session=value.session;state=value;cursor=0;events="";threads=[];threadId="";refreshing=false
        clearFrames();failure="";existingSession=({});refresh()
    }
    function control(action) { controlling=true;failure="";call("debug.control",{session:session,action:action,thread_id:threadId}) }
    function refresh() { if(session && !refreshing) { refreshing=true;call("debug.state",{session:session}) } }
    function start() {
        if(starting || active) return
        let args=android ? {backend:nativeAndroid?"android-lldb":"jdwp",device:workspace.selectedDevice,bundle_id:application.text.trim(),launch:launch.checked} : {backend:"lldb",program:program.text.trim(),stop_on_entry:true}
        if(nativeAndroid) args.program=program.text.trim()
        starting=true;failure="";existingSession=({});call("debug.start",args)
    }
    function selectFrame(frame) {
        frameId=String(frame.id);sourcePath=frame.source ? frame.source.path || "" : "";sourceLine=frame.line || 1
        call("debug.variables",{session:session,thread_id:threadId,frame_id:frameId})
    }
    function useProject() { if(workspace.project.configuration && workspace.project.configuration.applicationId) application.text=workspace.project.configuration.applicationId }
    onVisibleChanged: if(visible) { call("debug.list",{});if(!application.text)useProject() }
    Connections { target: view.workspace; function onProjectChanged() { if(!view.active)view.useProject() } }
    Connections {
        target: Agent
        function onApiFinished(id,op,response) {
            let request=view.requests[id]
            if(!request) return
            delete view.requests[id]
            if(request.session && request.session!==view.session) return
            if(op==="debug.variables" && (request.frame!==view.frameId || !view.state.stopped))return
            if(op==="debug.stack" && (request.thread!==view.threadId || !view.state.stopped))return
            if(op==="debug.start")view.starting=false
            if(op==="debug.control")view.controlling=false
            if(op==="debug.state")view.refreshing=false
            if(!response.ok) {
                view.failure=response.error.message || qsTr("Debugger request failed")
                if(response.error.code==="device_debug_busy")view.existingSession=response.error.details || ({})
                return
            }
            let r=response.result
            if(op==="debug.list") view.sessions=r.sessions
            if(op==="debug.start") { view.follow(r);view.call("debug.list",{}) }
            if(op==="debug.state") {
                let changed=r.event_cursor!==view.state.event_cursor
                view.state=r
                if(r.thread_id) view.threadId=String(r.thread_id)
                if(r.stopped && !r.terminated && (changed || !view.frames.length)) { view.call("debug.threads",{session:view.session});view.call("debug.stack",{session:view.session,thread_id:view.threadId}) }
                if(!r.stopped || r.terminated)view.clearFrames()
                if(changed)view.call("debug.events",{session:view.session,after:view.cursor})
            }
            if(op==="debug.threads") view.threads=r.threads
            if(op==="debug.stack") { view.frames=r.frames;if(r.frames.length)view.selectFrame(r.frames[0]) }
            if(op==="debug.variables") {
                view.variables=r.variables || r.scopes || []
                if(r.scopes && !request.reference && r.scopes.length) {
                    let locals=r.scopes.find(x=>!x.expensive) || r.scopes[0]
                    view.call("debug.variables",{session:view.session,reference:String(locals.variablesReference)})
                }
            }
            if(op==="debug.evaluate") view.details=r.result===undefined?JSON.stringify(r,null,2):r.result
            if(op==="debug.breakpoints") view.details=r.breakpoints.map(x=>(x.verified?"✓ ":"○ ")+qsTr("Line %1").arg(x.line || "?")+(x.message?" · "+x.message:"")).join("\n")
            if(op==="debug.events") { view.cursor=r.cursor;view.events=(view.events+r.events.map(x=>JSON.stringify(x)).join("\n")+"\n").slice(-60000) }
            if(op==="debug.control") view.refresh()
        }
    }
    Timer { interval:900;running:view.visible && view.active;repeat:true;onTriggered:view.refresh() }

    ColumnLayout {
        width:view.availableWidth-Theme.gapMd;spacing:Theme.gapMd
        StudioPanel {
            id:setup
            visible:!view.active
            Layout.fillWidth:true
            title:qsTr("Attach to your app")
                Label { objectName:"debugStatus";visible:false;text:view.stateLabel }
                GridLayout {
                    Layout.fillWidth:true;columns:view.availableWidth<650?1:2;columnSpacing:Theme.gapXs;rowSpacing:Theme.gapXs;enabled:!view.starting && !view.active
                    StudioComboBox { id:backend;objectName:"debugBackend";Layout.fillWidth:true;model:["Android · Java / Kotlin","Android · C / C++ (NDK)","Linux · Swift / C / C++"] }
                    StudioComboBox {
                        objectName:"debugDevice";visible:view.android;Layout.fillWidth:true;model:view.androidDevices;textRole:"name"
                        currentIndex:view.androidDevices.findIndex(x=>view.hasAndroid && x.id===workspace.selectedDevice)
                        displayText:currentIndex<0?qsTr("Choose Android device"):currentText
                        onActivated:{workspace.selectedDevice=view.androidDevices[currentIndex].id;workspace.selectedPlatform="android"}
                    }
                }
                RowLayout {
                    Layout.fillWidth:true;visible:view.android;enabled:!view.starting && !view.active
                    StudioTextField { id:application;objectName:"debugApplication";Layout.fillWidth:true;placeholderText:qsTr("Application ID · e.g. dev.example.app") }
                    StudioCheckBox { id:launch;text:qsTr("Launch app");checked:true;ToolTip.visible:hovered;ToolTip.text:view.nativeAndroid?qsTr("Launches the app if it is not running yet."):qsTr("Restarts the app and waits for the Java debugger.") }
                }
                RowLayout {
                    Layout.fillWidth:true;visible:view.nativeAndroid || !view.android;enabled:!view.starting && !view.active
                    StudioTextField { id:program;objectName:"debugProgram";Layout.fillWidth:true;placeholderText:view.nativeAndroid?qsTr("Library with debug symbols (.so)"):qsTr("Executable with debug symbols") }
                    StudioButton { text:qsTr("Choose file");onClicked:programDialog.open() }
                }
                RowLayout {
                    Layout.fillWidth:true;spacing:12
                    Label {
                        Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;font.pixelSize:Theme.fontSmall
                        text:view.nativeAndroid?qsTr("Needs a debug build and the NDK. The library must match your app and its processor."):view.android?qsTr("Needs a debug build on a connected phone or emulator."):qsTr("A program on this computer, inspected with LLDB.")
                    }
                    StudioButton {
                        objectName:"debugStart";text:view.starting?qsTr("Connecting…"):view.active?qsTr("Session active"):qsTr("Connect");iconName:"debug";accent:true
                        enabled:!view.starting && !view.active && (view.android?(view.hasAndroid && application.text.trim().length>0 && (!view.nativeAndroid || program.text.trim().length>0)):program.text.trim().length>0)
                        onClicked:view.start()
                    }
                }
        }
        Label { objectName:"debugError";Layout.fillWidth:true;visible:!!view.failure;text:view.failure;wrapMode:Text.Wrap;color:Theme.danger }
        StudioButton { visible:!!view.existingSession.session;text:qsTr("Open existing session");onClicked:view.follow(view.existingSession) }
        RowLayout {
            Layout.fillWidth:true;visible:view.sessions.length>0
            StudioComboBox {
                Layout.fillWidth:true;model:view.sessions.map(x=>({label:(x.bundle_id || x.program || x.backend)+" · "+x.session.slice(0,8)+(x.terminated?" · "+qsTr("ended"):""),session:x.session}));textRole:"label"
                currentIndex:view.sessions.findIndex(x=>x.session===view.session);displayText:currentIndex<0?qsTr("Take over your agent's session"):currentText
                enabled:!view.starting && !view.controlling
                onActivated:view.follow(view.sessions[currentIndex])
            }
            StudioButton { text:qsTr("Refresh");onClicked:view.call("debug.list",{}) }
        }
        Flow {
            Layout.fillWidth:true;spacing:Theme.gapXs;visible:view.active;enabled:view.active && !view.controlling
            StudioButton { text:qsTr("Continue");iconName:"play";enabled:view.state.stopped===true;onClicked:view.control("continue") }
            StudioButton { text:"Pause";iconName:"pause";enabled:view.state.stopped===false;onClicked:view.control("pause") }
            StudioButton { text:qsTr("Step over");iconName:"stepOver";tip:qsTr("Run the next source line");enabled:view.state.stopped===true;onClicked:view.control("next") }
            StudioButton { text:qsTr("Step in");iconName:"stepIn";enabled:view.state.stopped===true;onClicked:view.control("stepIn") }
            StudioButton { text:qsTr("Step out");iconName:"stepOut";enabled:view.state.stopped===true;onClicked:view.control("stepOut") }
            StudioButton { text:qsTr("Disconnect");iconName:"unplug";quiet:true;onClicked:view.control("disconnect") }
        }
        RowLayout {
            Layout.fillWidth:true;visible:view.active
            StudioTextField { id:source;Layout.fillWidth:true;placeholderText:qsTr("Source file or Java class") }
            StudioTextField { id:lines;Layout.preferredWidth:115;placeholderText:qsTr("Lines: 12, 24") }
            StudioButton {
                text:qsTr("Breakpoints");enabled:view.active && source.text.trim().length>0
                onClicked:{let values=lines.text.trim()?lines.text.split(",").map(x=>Number(x.trim())):[];if(values.some(x=>!Number.isInteger(x)||x<1)){view.failure=qsTr("Enter valid line numbers");return}view.call("debug.breakpoints",{session:view.session,source:source.text.trim(),lines:values})}
            }
        }
        Rectangle {
            Layout.fillWidth:true;implicitHeight:Math.max(300,view.availableHeight-Theme.barHeight*3);visible:view.active;color:Theme.surface;radius:Theme.radius;border.color:Theme.subtle
            RowLayout {
                anchors.fill:parent;anchors.margins:Theme.gapMd;spacing:Theme.gapMd
                ColumnLayout {
                    Layout.fillWidth:true;Layout.fillHeight:true;Layout.preferredWidth:1
                    StudioSection { text:qsTr("CALL STACK") }
                    StudioComboBox { Layout.fillWidth:true;model:view.threads;textRole:"name";displayText:count?currentText:qsTr("No paused thread yet");onActivated:{view.threadId=String(view.threads[currentIndex].id);view.call("debug.stack",{session:view.session,thread_id:view.threadId})} }
                    ListView {
                        Layout.fillWidth:true;Layout.fillHeight:true;clip:true;model:view.frames
                        delegate:ItemDelegate {
                            required property var modelData
                            width:ListView.view.width;height:Theme.rowHeight
                            contentItem:Label { text:(modelData.name || modelData.method || "Frame")+" : "+(modelData.line || "—");color:Theme.text;elide:Text.ElideRight;font.family:Theme.mono;font.pixelSize:Theme.fontSmall;verticalAlignment:Text.AlignVCenter }
                            background:Rectangle { radius:Theme.radiusSmall;color:parent.highlighted?Theme.selected:parent.hovered?Theme.hover:"transparent" }
                            highlighted:String(modelData.id)===view.frameId
                            onClicked:view.selectFrame(modelData)
                        }
                    }
                    StudioButton {
                        text:qsTr("Open source");quiet:true;enabled:!!view.sourcePath && !!workspace.project.path && view.sourcePath.startsWith(workspace.project.path+"/")
                        onClicked:{workspace.revealFile(view.sourcePath.slice(workspace.project.path.length+1),view.sourceLine);workspace.showTab("files")}
                    }
                }
                Rectangle { Layout.fillHeight:true;implicitWidth:1;color:Theme.subtle }
                ColumnLayout {
                    Layout.fillWidth:true;Layout.fillHeight:true;Layout.preferredWidth:1
                    RowLayout {
                        Layout.fillWidth:true
                        StudioSection { text:qsTr("VARIABLES");Layout.fillWidth:true }
                        StudioButton { text:qsTr("Locals");quiet:true;implicitHeight:Theme.controlSmall;enabled:!!view.frameId && view.state.stopped===true;onClicked:view.call("debug.variables",{session:view.session,thread_id:view.threadId,frame_id:view.frameId}) }
                    }
                    ListView {
                        Layout.fillWidth:true;Layout.fillHeight:true;clip:true;model:view.variables
                        delegate:ItemDelegate {
                            required property var modelData
                            width:ListView.view.width;height:Theme.rowHeight
                            contentItem:Label { text:modelData.name+"   "+(modelData.value===undefined?"…":String(modelData.value));elide:Text.ElideRight;color:Theme.text;font.family:Theme.mono;font.pixelSize:Theme.fontSmall;verticalAlignment:Text.AlignVCenter }
                            background:Rectangle { radius:Theme.radiusSmall;color:parent.hovered?Theme.hover:"transparent" }
                            onClicked:{let ref=String(modelData.reference || modelData.variablesReference || "");if(ref && ref!=="0")view.call("debug.variables",{session:view.session,reference:ref});else view.details=JSON.stringify(modelData,null,2)}
                        }
                    }
                    RowLayout {
                        Layout.fillWidth:true
                        StudioTextField { id:expression;Layout.fillWidth:true;placeholderText:qsTr("Evaluate expression");onAccepted:if(evaluate.enabled)evaluate.clicked() }
                        StudioButton { id:evaluate;text:"↵";tip:qsTr("Evaluate the expression in the selected frame");enabled:view.state.stopped===true && !!expression.text.trim();onClicked:view.call("debug.evaluate",{session:view.session,thread_id:view.threadId,frame_id:view.frameId,expression:expression.text}) }
                    }
                }
            }
        }
        Label { Layout.fillWidth:true;visible:!view.frames.length;font.pixelSize:Theme.fontBody;text:view.active?qsTr("Pause your app or set a breakpoint to see the stack and variables."):qsTr("Attach here, or take over a session your agent started.");wrapMode:Text.Wrap;color:Theme.muted }
        StudioCodeView { Layout.fillWidth:true;Layout.preferredHeight:80;visible:!!view.details;text:view.details;textColor:Theme.text }
        StudioButton { visible:view.active;text:(view.showEvents?qsTr("Hide events"):qsTr("Show events"))+" · "+view.cursor;quiet:true;onClicked:view.showEvents=!view.showEvents }
        StudioCodeView { Layout.fillWidth:true;Layout.preferredHeight:140;visible:view.showEvents;text:view.events || qsTr("No debugger events yet.") }
    }
    FileDialog { id:programDialog;title:view.nativeAndroid?qsTr("Choose library with debug symbols"):qsTr("Choose debug program");nameFilters:view.nativeAndroid?[qsTr("Android libraries (*.so)"),qsTr("All files (*)")]:[qsTr("All files (*)")];onAccepted:program.text=decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,"")) }
}
