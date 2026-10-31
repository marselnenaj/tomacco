import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// What is happening right now, next to the device. It shows what the control
// plane records and this window sees: the jobs of every client (job.list), the
// newest running job's log, and the calls agents make to this window through
// its preview socket. Calls an agent makes to tomaccoctl directly leave no record
// unless they start a job, so they cannot appear here.
Rectangle {
    id: panel
    required property var workspace
    property string stateLabel: ""
    property string stateTone: "idle"
    property string deviceName: ""
    property string deviceMetrics: ""
    color: Theme.surface
    implicitWidth: Theme.panelWidth

    readonly property var running: workspace.jobs.filter(x=>["queued","running"].indexOf(x.state)>=0)
    readonly property var finished: workspace.jobs.filter(x=>["queued","running"].indexOf(x.state)<0).slice(0,6)
    readonly property var followed: running.length ? running[0] : null
    property var calls: []
    property bool copied: false
    Timer {id:copiedTimer;interval:2000;onTriggered:panel.copied=false}
    readonly property bool firstRun: stateTone==="idle" && !workspace.jobs.length && !calls.length
    property real now: Date.now()/1000

    // Older calls drop off; repeats of the same call fold into one line.
    function noteCall(method) {
        const at=new Date()
        let rows=calls.slice()
        if(rows.length && rows[0].method===method)rows[0]={method:method,at:at,count:rows[0].count+1}
        else rows.unshift({method:method,at:at,count:1})
        calls=rows.slice(0,12)
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
        function onPreviewRequest(id,method,args) {panel.noteCall(method)}
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
                        onClicked:{Studio.copy(JSON.stringify({mcpServers:{[Agent.cliPath.split("/").pop()]:{command:Agent.cliPath,args:["mcp"]}}},null,2));panel.copied=true;copiedTimer.restart()}
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

            // Calls agents made to this window.
            Rectangle {visible:panel.calls.length>0;Layout.fillWidth:true;Layout.preferredHeight:1;color:Theme.subtle}
            StudioSection {
                visible: panel.calls.length>0
                Layout.fillWidth: true; Layout.leftMargin: Theme.gapLg; Layout.topMargin: Theme.gapMd; Layout.bottomMargin: Theme.gapXs
                text: qsTr("Agent calls to this window")
            }
            Repeater {
                model: panel.calls
                RowLayout {
                    required property var modelData
                    objectName: "activityCall"
                    Layout.fillWidth: true; Layout.leftMargin: Theme.gapLg; Layout.rightMargin: Theme.gapLg; Layout.preferredHeight: Theme.controlSmall-Theme.gapXs
                    spacing: Theme.gapSm
                    Text {Layout.fillWidth:true;text:"preview."+parent.modelData.method+(parent.modelData.count>1?" ×"+parent.modelData.count:"");color:Theme.muted;font.family:Theme.mono;font.pixelSize:Theme.fontSmall;elide:Text.ElideRight}
                    Text {text:Qt.formatTime(parent.modelData.at,"HH:mm:ss");color:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontMicro}
                }
            }
            Item {visible:panel.calls.length>0;Layout.preferredHeight:Theme.gapSm}

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
