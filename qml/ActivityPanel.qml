import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// What is happening right now, next to the device. It shows what the control
// plane records and this window sees: the jobs of every client (job.list), the
// newest running job's log, the calls agents make to this window through its
// preview socket, and, when an agent writes a session log (TOMACCO_SESSION_LOG)
// that this panel is told about, every operation it calls. Other calls an agent
// makes to tomaccoctl leave no record unless they start a job.
Rectangle {
    id: panel
    required property var workspace
    property string stateLabel: ""
    property string stateTone: "idle"
    property string deviceName: ""
    property string deviceMetrics: ""
    // The recent notifications (NotificationCentre) and where a click on one leads.
    property var notifications: null
    signal notificationChosen(var entry)
    readonly property var notificationEntries: notifications ? notifications.entries : []
    property bool allNotifications: false
    readonly property var shownNotifications: allNotifications ? notificationEntries : notificationEntries.slice(0,6)
    function linked(entry) { return !!entry.job || !!entry.device || entry.update===true }
    // What agents did, for the notifications: a call that failed, and a job one started.
    function noteAgent(operation,ok,error,job) {
        if(!notifications)return
        if(!ok)notifications.add("agent","failed",qsTr("An agent's call failed: %1").arg(operation),error,{job:job})
        else if(job)notifications.add("agent","ok",qsTr("An agent started a job: %1").arg(operation),"",{job:job})
    }
    color: Theme.surface
    implicitWidth: Theme.panelWidth

    readonly property var running: workspace.jobs.filter(x=>["queued","running"].indexOf(x.state)>=0)
    readonly property var finished: workspace.jobs.filter(x=>["queued","running"].indexOf(x.state)<0).slice(0,6)
    readonly property var followed: running.length ? running[0] : null
    property var calls: []
    property bool copied: false
    Timer {id:copiedTimer;interval:2000;onTriggered:panel.copied=false}
    readonly property bool firstRun: stateTone==="idle" && !workspace.jobs.length && !calls.length && !sessionEntries.length && !notificationEntries.length
    property real now: Date.now()/1000

    // Older calls drop off; repeats of the same call fold into one line, which
    // shows how the latest of them ended and how long it took.
    property var callStarts: ({})
    function noteCall(id,method) {
        const at=new Date()
        callStarts[id]=at.getTime()
        let rows=calls.slice()
        if(rows.length && rows[0].method===method)rows[0]=Object.assign({},rows[0],{at:at,count:rows[0].count+1,ids:rows[0].ids.concat([id]),pending:true})
        else rows.unshift({method:method,at:at,count:1,ids:[id],pending:true,ms:-1,error:""})
        calls=rows.slice(0,20)
    }
    function finishCall(id,error) {
        const started=callStarts[id]
        if(started===undefined)return
        delete callStarts[id]
        if(error){const row=calls.find(row=>row.ids.indexOf(id)>=0);if(row)noteAgent("preview."+row.method,false,error,"")}
        calls=calls.map(row=>row.ids.indexOf(id)<0?row:Object.assign({},row,{pending:false,ms:Date.now()-started,error:error || ""}))
    }
    // An agent's session log: a file it appends each operation it calls to.
    property string sessionLog: ""
    property var sessionEntries: []
    property real sessionOffset: 0
    property string sessionError: ""
    // What a session log already held when it was named is not news.
    property bool sessionPrimed: false
    onSessionLogChanged: {sessionEntries=[];sessionOffset=0;sessionError="";sessionPrimed=false;readSession()}
    function readSession() {
        if(!sessionLog){sessionError="";return}
        const read=Studio.readSessionLog(sessionLog,sessionOffset)
        sessionError=read.error
        if(read.reset)sessionEntries=[]
        sessionOffset=read.next_offset
        if(read.entries.length)sessionEntries=sessionEntries.concat(read.entries).slice(-200)
        if(sessionPrimed && !read.reset)for(const x of read.entries)noteAgent(String(x.operation || ""),!!x.ok,x.ok?"":(x.error && x.error.message) || "",(x.result || {}).job_id || "")
        if(!read.error)sessionPrimed=true
    }
    Timer {interval:2000;repeat:true;running:panel.visible && !!panel.sessionLog;onTriggered:panel.readSession()}
    // Both, newest first: what was called, when, how long it took and how it ended.
    readonly property var timeline: {
        let rows=calls.map(x=>({key:"call-"+x.ids[0],operation:"preview."+x.method,at:x.at.getTime(),count:x.count,ms:x.pending?-1:x.ms,
                                state:x.pending?"busy":x.error?"failed":"ok",error:x.error,job:""}))
        for(let i=0;i<sessionEntries.length;++i) {
            const x=sessionEntries[i]
            const result=x.result || {}
            rows.push({key:"log-"+i,operation:String(x.operation || ""),at:Number(x.time || 0)*1000,count:1,ms:x.duration_seconds===undefined?-1:Math.round(Number(x.duration_seconds)*1000),
                       state:x.ok?"ok":"failed",error:x.ok?"":(x.error && x.error.message) || "",job:result.job_id || ""})
        }
        return rows.sort((a,b)=>b.at-a.at)
    }
    property string activityFilter: "all"
    property string activityQuery: ""
    readonly property var shownActivity: timeline.filter(x=>(activityFilter==="all" || x.state==="failed")
        && (!activityQuery || (x.operation+" "+x.error).toLowerCase().indexOf(activityQuery.toLowerCase())>=0)).slice(0,40)
    // The MCP server entry; with a session log set, the agent writes its calls there.
    function mcpServer() {
        let entry={command:Agent.cliPath,args:["mcp"]}
        if(sessionLog)entry.env={TOMACCO_SESSION_LOG:sessionLog}
        return entry
    }
    function duration(ms) {
        if(ms<0)return ""
        if(ms<1000)return qsTr("%1 ms").arg(ms)
        return ms<60000?qsTr("%1 s").arg((ms/1000).toFixed(1)):elapsed(ms/1000)
    }
    function elapsed(seconds) {
        const s=Math.max(0,Math.round(seconds))
        if(s<60)return s+" s"
        if(s<3600)return Math.floor(s/60)+" min "+(s%60)+" s"
        return Math.floor(s/3600)+" h "+Math.floor(s%3600/60)+" min"
    }
    function commandOf(job) {
        const steps=job.plan && job.plan.steps ? job.plan.steps : []
        const argv=steps.length ? steps[0].argv || [] : []
        return argv.map(x=>String(x).split("/").pop()).slice(0,5).join(" ")
    }
    function toneOf(job) {
        return ({queued:"busy",running:"busy",succeeded:"ok",failed:"failed",timed_out:"failed"})[job.state] || "idle"
    }
    function openJob(job) {workspace.selectJob(job);workspace.showTab("jobs")}
    function finishedAgo(job) {return qsTr("%1 ago").arg(elapsed(now-(job.updated || now)))}

    // The followed job's log, read onward from where the last read stopped; only the end is kept.
    property string logJob: ""
    property int logOffset: 0
    property string logTail: ""
    property int logRequest: 0
    function readLog() {
        if(!followed){logJob="";logTail="";return}
        if(logRequest)return
        if(logJob!==followed.id){logJob=followed.id;logOffset=0;logTail=""}
        logRequest=Agent.request("job.logs",{job_id:logJob,offset:logOffset})
    }
    Connections {
        target: Agent
        function onPreviewRequest(id,method,args) {panel.noteCall(id,method)}
        function onPreviewFinished(id,error) {panel.finishCall(id,error)}
        function onApiFinished(id,operation,response) {
            if(id!==panel.logRequest)return
            panel.logRequest=0
            if(!response.ok || response.result.id!==panel.logJob || response.result.offset!==panel.logOffset)return
            panel.logOffset=response.result.next_offset
            if(response.result.text)panel.logTail=(panel.logTail+response.result.text).slice(-1200)
            if(!response.result.eof)panel.readLog()
        }
    }
    Timer {interval:1000;repeat:true;running:panel.visible;onTriggered:panel.now=Date.now()/1000}
    Timer {interval:1800;repeat:true;running:panel.visible && !!panel.followed;triggeredOnStart:true;onTriggered:panel.readLog()}
    onFollowedChanged: if(!followed){logJob="";logTail=""}

    Rectangle {width:1;height:parent.height;color:Theme.subtle}
    StudioScrollView {
        anchors.fill: parent
        anchors.leftMargin: 1
        clip: true
        contentWidth: availableWidth
        ColumnLayout {
            width: parent.width
            spacing: 0
            // First run: what agents can do here, in the brand's slab.
            Rectangle {
                id: slab
                objectName: "activitySlab"
                visible: panel.firstRun
                Layout.fillWidth: true
                Layout.preferredHeight: Math.max(panel.height,slabColumn.implicitHeight+Theme.gapXl*2)
                color: Theme.text
                ColumnLayout {
                    id: slabColumn
                    anchors {left: parent.left; right: parent.right; top: parent.top; margins: Theme.gapLg; topMargin: Theme.gapXl}
                    spacing: Theme.gapXs
                    StudioHeading {Layout.fillWidth:true;text:qsTr("Connect your agent.");color:Theme.background;size:Theme.fontHero}
                    Text {Layout.fillWidth:true;Layout.topMargin:Theme.gapSm;text:qsTr("Claude Code, Codex or any MCP client. It can then do everything this window can.");color:Theme.background;font.pixelSize:Theme.fontLead;font.weight:Font.Medium;lineHeight:1.15;wrapMode:Text.WordWrap}
                    StudioButton {
                        objectName:"activityCopyMcp";Layout.topMargin:Theme.gapMd;inverted:true;invertedFill:Theme.background;invertedText:Theme.text
                        text:panel.copied?qsTr("Copied"):qsTr("Copy MCP configuration");iconName:panel.copied?"check":"copy"
                        onClicked:{Studio.copy(JSON.stringify({mcpServers:{[Agent.cliPath.split("/").pop()]:panel.mcpServer()}},null,2));panel.copied=true;copiedTimer.restart()}
                    }
                    Text {Layout.fillWidth:true;Layout.topMargin:Theme.gapMd;text:qsTr("1  Copy the configuration.")+"\n"+qsTr("2  Paste it into your agent's MCP settings.")+"\n"+qsTr("3  Ask it to run your app.");color:Theme.background;font.family:Theme.mono;font.pixelSize:Theme.fontSmall;lineHeight:1.5;wrapMode:Text.WordWrap}
                }
            }
            // The device's state, big enough to read from across the room.
            Rectangle {
                objectName: "activityDeviceBlock"
                visible: panel.stateTone!=="idle"
                Layout.fillWidth: true
                Layout.preferredHeight: stateColumn.implicitHeight+Theme.gapLg*2
                color: panel.stateTone==="live"?Theme.accent:panel.stateTone==="failed"?Theme.dangerSurface:Theme.text
                readonly property color ink: panel.stateTone==="live"?Theme.accentText:panel.stateTone==="failed"?Theme.danger:Theme.background
                ColumnLayout {
                    id: stateColumn
                    anchors {left: parent.left; right: parent.right; top: parent.top; margins: Theme.gapLg}
                    spacing: Theme.gapXxs
                    StudioHeading {
                        objectName: "activityDeviceState"
                        Layout.fillWidth: true
                        text: panel.stateLabel
                        color: parent.parent.ink
                        size: Theme.fontGiant-Theme.gapSm
                        wrapMode: Text.NoWrap
                        elide: Text.ElideRight
                    }
                    Text {Layout.fillWidth:true;visible:!!panel.deviceName;text:panel.deviceName;color:parent.parent.ink;font.pixelSize:Theme.fontLead;font.weight:Font.Medium;elide:Text.ElideRight}
                    Text {Layout.fillWidth:true;visible:!!panel.deviceMetrics;text:panel.deviceMetrics;color:parent.parent.ink;font.family:Theme.mono;font.pixelSize:Theme.fontSmall;elide:Text.ElideRight}
                }
                // Starting has no measure of progress, only that it is still going.
                Item {
                    visible: panel.stateTone==="busy"
                    anchors.bottom: parent.bottom; width: parent.width; height: 3; clip: true
                    Rectangle {
                        width: parent.width/3; height: parent.height; color: Theme.accent
                        XAnimator on x {from:-parent.width/3;to:parent.width;duration:1400;loops:Animation.Infinite;running:parent.visible && !Theme.reducedMotion}
                    }
                }
            }

            // Now.
            StudioSection {
                visible: !panel.firstRun
                Layout.fillWidth: true; Layout.leftMargin: Theme.gapLg; Layout.topMargin: Theme.gapMd; Layout.bottomMargin: Theme.gapXs
                text: qsTr("Running")+(panel.running.length?" · "+panel.running.length:"")
            }
            Text {
                objectName: "activityIdle"
                Layout.fillWidth: true; Layout.leftMargin: Theme.gapLg; Layout.rightMargin: Theme.gapLg; Layout.bottomMargin: Theme.gapMd
                visible: panel.running.length===0 && !panel.firstRun
                text: qsTr("Nothing running. Builds and agent jobs show up here as they start.")
                color: Theme.muted
                font.pixelSize: Theme.fontSmall
                wrapMode: Text.WordWrap
                lineHeight: 1.3
            }
            Repeater {
                model: panel.running
                ItemDelegate {
                    id: runningRow
                    required property var modelData
                    objectName: "activityJob-"+modelData.id
                    Layout.fillWidth: true
                    focusPolicy: Qt.StrongFocus
                    Accessible.name: modelData.label+", "+panel.workspace.jobStateLabel(modelData)
                    padding: 0
                    leftPadding: Theme.gapLg
                    rightPadding: Theme.gapSm
                    topPadding: Theme.gapXs+Theme.gapXxs
                    bottomPadding: Theme.gapXs+Theme.gapXxs
                    background: Rectangle {color:runningRow.hovered?Theme.hover:"transparent";border.width:runningRow.visualFocus?2:0;border.color:Theme.accentInk}
                    contentItem: RowLayout {
                        spacing: Theme.gapSm
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: Theme.gapXxs
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Theme.gapSm
                                StudioStatus {tone:panel.toneOf(runningRow.modelData)}
                                Text {Layout.fillWidth:true;text:runningRow.modelData.label;color:Theme.text;font.pixelSize:Theme.fontBody;elide:Text.ElideRight}
                            }
                            Text {
                                Layout.fillWidth: true
                                text: panel.workspace.jobStateLabel(runningRow.modelData)+" · "+panel.elapsed(panel.now-runningRow.modelData.created)
                                color: Theme.muted
                                font.pixelSize: Theme.fontSmall
                                elide: Text.ElideRight
                            }
                            Text {
                                Layout.fillWidth: true
                                visible: !!text
                                text: panel.commandOf(runningRow.modelData)
                                color: Theme.faint
                                font.family: Theme.mono
                                font.pixelSize: Theme.fontMicro
                                elide: Text.ElideMiddle
                            }
                        }
                        StudioButton {
                            iconName: "stop"; quiet: true
                            implicitWidth: Theme.controlSmall; implicitHeight: Theme.controlSmall
                            tip: qsTr("Stop this job")
                            onClicked: Agent.request("job.cancel",{job_id:runningRow.modelData.id})
                        }
                    }
                    onClicked: panel.openJob(modelData)
                }
            }
            Text {
                objectName: "activityLog"
                Layout.fillWidth: true; Layout.margins: Theme.gapMd
                visible: !!panel.followed && !!panel.logTail
                // The last lines of the job's log, whole lines only.
                text: {const lines=panel.logTail.trim().split("\n");return lines.slice(-6).join("\n")}
                color: Theme.faint
                font.family: Theme.mono
                font.pixelSize: Theme.fontMicro
                wrapMode: Text.WrapAnywhere
                maximumLineCount: 8
                elide: Text.ElideLeft
            }

            // Recent notifications: jobs that ended, updates, devices and agents, each leading back to its job, device or settings.
            Rectangle {visible:panel.notificationEntries.length>0;Layout.fillWidth:true;Layout.preferredHeight:1;color:Theme.subtle}
            RowLayout {
                visible: panel.notificationEntries.length>0
                Layout.fillWidth: true; Layout.leftMargin: Theme.gapLg; Layout.rightMargin: Theme.gapSm; Layout.topMargin: Theme.gapSm
                spacing: Theme.gapXs
                StudioSection {Layout.fillWidth:true;text:qsTr("Notifications")+" · "+panel.notificationEntries.length}
                StudioButton {
                    objectName:"notificationsMore";quiet:true;size:Theme.fontSmall;visible:panel.notificationEntries.length>6
                    text:panel.allNotifications?qsTr("Fewer"):qsTr("All %1").arg(panel.notificationEntries.length)
                    onClicked:panel.allNotifications=!panel.allNotifications
                }
                StudioButton {objectName:"notificationsClear";quiet:true;size:Theme.fontSmall;text:qsTr("Clear");tip:qsTr("Clear the notifications");onClicked:{panel.allNotifications=false;panel.notifications.clear()}}
            }
            Repeater {
                model: panel.shownNotifications
                ItemDelegate {
                    id: noteRow
                    required property var modelData
                    objectName: "notification-"+modelData.kind
                    Layout.fillWidth: true
                    focusPolicy: panel.linked(modelData) ? Qt.StrongFocus : Qt.NoFocus
                    Accessible.name: modelData.title+(modelData.body?", "+modelData.body:"")
                    padding: 0; leftPadding: Theme.gapLg; rightPadding: Theme.gapLg; topPadding: Theme.gapXs; bottomPadding: Theme.gapXs
                    background: Rectangle {color:noteRow.hovered && panel.linked(noteRow.modelData)?Theme.hover:"transparent";border.width:noteRow.visualFocus?2:0;border.color:Theme.accentInk}
                    contentItem: ColumnLayout {
                        spacing: 0
                        RowLayout {
                            Layout.fillWidth: true; spacing: Theme.gapSm
                            StudioStatus {tone:noteRow.modelData.tone}
                            Text {objectName:"notificationTitle";Layout.fillWidth:true;text:noteRow.modelData.title;color:Theme.text;font.pixelSize:Theme.fontSmall;elide:Text.ElideRight}
                            Text {text:panel.elapsed(panel.now-noteRow.modelData.at/1000);color:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontMicro}
                        }
                        Text {
                            Layout.fillWidth: true; Layout.leftMargin: Theme.gapMd+Theme.gapXs
                            visible: !!noteRow.modelData.body
                            text: noteRow.modelData.body; color: Theme.muted; font.pixelSize: Theme.fontMicro
                            wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight
                        }
                    }
                    onClicked: if(panel.linked(modelData))panel.notificationChosen(modelData)
                }
            }
            Item {visible:panel.notificationEntries.length>0;Layout.preferredHeight:Theme.gapSm}

            // What agents called: this window, and the operations of a session log.
            Rectangle {visible:panel.timeline.length>0 || !!panel.sessionError;Layout.fillWidth:true;Layout.preferredHeight:1;color:Theme.subtle}
            StudioSection {
                visible: panel.timeline.length>0 || !!panel.sessionError
                Layout.fillWidth: true; Layout.leftMargin: Theme.gapLg; Layout.topMargin: Theme.gapMd; Layout.bottomMargin: Theme.gapXs
                text: qsTr("Agent activity")+(panel.timeline.length?" · "+panel.timeline.length:"")
            }
            Text {
                objectName: "activitySessionError"
                visible: !!panel.sessionError
                Layout.fillWidth: true; Layout.leftMargin: Theme.gapLg; Layout.rightMargin: Theme.gapLg; Layout.bottomMargin: Theme.gapXs
                text: panel.sessionError; color: Theme.warning; font.pixelSize: Theme.fontSmall; wrapMode: Text.WordWrap
            }
            RowLayout {
                visible: panel.timeline.length>0
                Layout.fillWidth: true; Layout.leftMargin: Theme.gapLg; Layout.rightMargin: Theme.gapLg; Layout.bottomMargin: Theme.gapXs
                spacing: Theme.gapXs
                StudioSegmented {
                    objectName: "activityFilter"
                    current: panel.activityFilter
                    model: [{id:"all",label:qsTr("All"),objectName:"activityFilter-all"},{id:"failed",label:qsTr("Failed"),objectName:"activityFilter-failed"}]
                    onChosen: id=>panel.activityFilter=id
                }
                StudioTextField {objectName:"activitySearch";Layout.fillWidth:true;placeholderText:qsTr("Find an operation");onTextChanged:panel.activityQuery=text}
            }
            Text {
                objectName: "activityNoMatch"
                visible: panel.timeline.length>0 && panel.shownActivity.length===0
                Layout.fillWidth: true; Layout.leftMargin: Theme.gapLg; Layout.rightMargin: Theme.gapLg
                text: panel.activityFilter==="failed" && !panel.activityQuery ? qsTr("No call failed.") : qsTr("No call matches.")
                color: Theme.muted; font.pixelSize: Theme.fontSmall
            }
            Repeater {
                model: panel.shownActivity
                ItemDelegate {
                    id: callRow
                    required property var modelData
                    objectName: "activityCall"
                    Layout.fillWidth: true
                    focusPolicy: callRow.modelData.job ? Qt.StrongFocus : Qt.NoFocus
                    Accessible.name: modelData.operation+", "+(modelData.state==="failed"?qsTr("failed"):modelData.state==="busy"?qsTr("running"):qsTr("done"))
                    padding: 0; leftPadding: Theme.gapLg; rightPadding: Theme.gapLg; topPadding: Theme.gapXxs; bottomPadding: Theme.gapXxs
                    background: Rectangle {color:callRow.hovered && !!callRow.modelData.job?Theme.hover:"transparent";border.width:callRow.visualFocus?2:0;border.color:Theme.accentInk}
                    contentItem: ColumnLayout {
                        spacing: 0
                        RowLayout {
                            Layout.fillWidth: true; spacing: Theme.gapSm
                            StudioStatus {tone:callRow.modelData.state}
                            Text {objectName:"activityCallName";Layout.fillWidth:true;text:callRow.modelData.operation+(callRow.modelData.count>1?" ×"+callRow.modelData.count:"");color:Theme.muted;font.family:Theme.mono;font.pixelSize:Theme.fontSmall;elide:Text.ElideRight}
                            Text {objectName:"activityCallDuration";text:panel.duration(callRow.modelData.ms);color:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontMicro}
                            Text {text:Qt.formatTime(new Date(callRow.modelData.at),"HH:mm:ss");color:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontMicro}
                        }
                        Text {
                            objectName: "activityCallError"
                            Layout.fillWidth: true; Layout.leftMargin: Theme.gapMd+Theme.gapXs
                            visible: !!callRow.modelData.error
                            text: callRow.modelData.error; color: Theme.danger; font.pixelSize: Theme.fontMicro
                            wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight
                        }
                    }
                    onClicked: {
                        if(!modelData.job)return
                        const job=panel.workspace.jobs.find(x=>x.id===modelData.job) || {id:modelData.job}
                        panel.openJob(job)
                    }
                }
            }
            Item {visible:panel.timeline.length>0;Layout.preferredHeight:Theme.gapSm}

            // Lately.
            Rectangle {visible:panel.finished.length>0;Layout.fillWidth:true;Layout.preferredHeight:1;color:Theme.subtle}
            StudioSection {
                visible: panel.finished.length>0
                Layout.fillWidth: true; Layout.leftMargin: Theme.gapLg; Layout.topMargin: Theme.gapMd; Layout.bottomMargin: Theme.gapXs
                text: qsTr("Recent")
            }
            Repeater {
                model: panel.finished
                ItemDelegate {
                    id: finishedRow
                    required property var modelData
                    Layout.fillWidth: true
                    focusPolicy: Qt.StrongFocus
                    Accessible.name: modelData.label+", "+panel.workspace.jobStateLabel(modelData)
                    padding: 0
                    leftPadding: Theme.gapLg
                    rightPadding: Theme.gapLg
                    topPadding: Theme.gapXs
                    bottomPadding: Theme.gapXs
                    background: Rectangle {color:finishedRow.hovered?Theme.hover:"transparent";border.width:finishedRow.visualFocus?2:0;border.color:Theme.accentInk}
                    contentItem: RowLayout {
                        spacing: Theme.gapSm
                        StudioStatus {tone:panel.toneOf(finishedRow.modelData)}
                        Text {Layout.fillWidth:true;text:finishedRow.modelData.label;color:Theme.muted;font.pixelSize:Theme.fontSmall;elide:Text.ElideRight}
                        Text {text:panel.elapsed(panel.now-(finishedRow.modelData.updated || panel.now));color:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontMicro}
                    }
                    onClicked: panel.openJob(modelData)
                }
            }
            Item {visible:!panel.firstRun;Layout.preferredHeight:Theme.gapMd}

        }
    }
}
