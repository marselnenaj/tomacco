import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// A log beside the live device: the device's own log (device.logs, as under
// Devices) or the log of a job, the app's launch first, then the newest
// running job, then the newest one.
Rectangle {
    id: pane
    objectName: "stageLogPane"
    required property var workspace
    // The live device; empty while none is live.
    property string device: ""
    property string platform: "android"
    property string launchJob: ""
    // "device" or "job".
    property string mode: "device"
    signal closeRequested()
    color: Theme.surface

    readonly property var followedJob: {
        const jobs=workspace.jobs
        const launch=jobs.find(x=>x.id===launchJob)
        if(launch)return launch
        // Just started, and not listed yet.
        if(launchJob)return {id:launchJob,label:qsTr("App launch"),state:"queued"}
        const running=jobs.find(x=>["queued","running"].indexOf(x.state)>=0)
        return running || (jobs.length ? jobs[0] : null)
    }
    // The followed job's log, read on from where the last read stopped; the end is kept.
    property string jobId: ""
    property int jobOffset: 0
    property string jobText: ""
    property string jobError: ""
    property int jobRequest: 0
    function readJob() {
        if(mode!=="job" || !followedJob){jobId="";jobText="";return}
        if(jobRequest)return
        if(jobId!==followedJob.id){jobId=followedJob.id;jobOffset=0;jobText="";jobError=""}
        jobRequest=Agent.request("job.logs",{job_id:jobId,offset:jobOffset})
    }
    Connections {
        target: Agent
        function onApiFinished(id,operation,response) {
            if(id!==pane.jobRequest)return
            pane.jobRequest=0
            if(!response.ok){pane.jobError=response.error.message+(response.error.remedy?" · "+response.error.remedy:"");return}
            if(response.result.id!==pane.jobId || response.result.offset!==pane.jobOffset)return
            pane.jobError=""
            pane.jobOffset=response.result.next_offset
            if(response.result.text)pane.jobText=(pane.jobText+response.result.text).slice(-200000)
        }
    }
    Timer {interval:1500;repeat:true;triggeredOnStart:true;running:pane.visible && pane.mode==="job" && !!pane.followedJob;onTriggered:pane.readJob()}
    onFollowedJobChanged: if(followedJob && followedJob.id!==jobId)readJob()

    Rectangle {width:1;height:parent.height;color:Theme.subtle}
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Theme.gapSm
        anchors.leftMargin: Theme.gapSm+1
        spacing: Theme.gapXs
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.gapXs
            StudioSegmented {
                objectName: "stageLogMode"
                current: pane.mode
                model: [{id:"device",label:qsTr("Device log"),objectName:"stageLogMode-device"},{id:"job",label:qsTr("Job log"),objectName:"stageLogMode-job"}]
                onChosen: id=>pane.mode=id
            }
            Text {
                Layout.fillWidth: true
                visible: pane.mode==="job" && !!pane.followedJob
                text: pane.followedJob ? pane.followedJob.label+" · "+pane.workspace.jobStateLabel(pane.followedJob) : ""
                color: Theme.muted; font.pixelSize: Theme.fontSmall; elide: Text.ElideRight
            }
            Item {Layout.fillWidth:true;visible:!(pane.mode==="job" && !!pane.followedJob)}
            StudioButton {
                objectName: "stageLogOpenJob"; iconName: "jobs"; quiet: true; visible: pane.mode==="job" && !!pane.followedJob
                text: qsTr("Open under Builds & jobs"); displayText: ""; tip: text
                onClicked: {pane.workspace.selectJob(pane.followedJob);pane.workspace.showTab("jobs")}
            }
            StudioButton {objectName:"stageLogClose";iconName:"close";quiet:true;text:qsTr("Hide the log");displayText:"";tip:text;onClicked:pane.closeRequested()}
        }
        Loader {
            objectName: "stageDeviceLogHost"
            Layout.fillWidth: true; Layout.fillHeight: true
            active: pane.visible && pane.mode==="device" && !!pane.device
            visible: active
            sourceComponent: DeviceLogPane {objectName:"stageDeviceLog";device:pane.device;platform:pane.platform}
        }
        StudioEmptyState {
            objectName: "stageDeviceLogEmpty"
            Layout.fillWidth: true; Layout.fillHeight: true
            visible: pane.mode==="device" && !pane.device
            showMark: false
            title: qsTr("No device is live.")
            body: qsTr("Connect a device and its log runs here, beside its screen.")
        }
        RowLayout {
            objectName: "stageJobLogError"
            Layout.fillWidth: true; spacing: Theme.gapSm
            visible: pane.mode==="job" && !!pane.followedJob && !!pane.jobError
            Text {Layout.fillWidth:true;text:pane.jobError;color:Theme.danger;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            StudioButton {objectName:"stageJobLogRetry";text:qsTr("Try again");iconName:"refresh";onClicked:{pane.jobError="";pane.readJob()}}
        }
        StudioCodeView {
            objectName: "stageJobLog"
            Layout.fillWidth: true; Layout.fillHeight: true
            visible: pane.mode==="job" && !!pane.followedJob
            text: pane.jobText
            textColor: pane.jobError ? Theme.danger : Theme.muted
            placeholderText: qsTr("Waiting for log lines…")
        }
        StudioEmptyState {
            objectName: "stageJobLogEmpty"
            Layout.fillWidth: true; Layout.fillHeight: true
            visible: pane.mode==="job" && !pane.followedJob
            showMark: false
            title: qsTr("No job yet.")
            body: qsTr("Run your app or start a build, and its log follows here.")
        }
    }
}
