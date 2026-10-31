import QtQuick
import "."

// One screen recording as a job: android.record or ios.virtual.record started
// with the longest time they allow and ended early with job.send stop. When the
// video is saved, the file manager shows it.
QtObject {
    id: recorder
    property string job: ""
    property string path: ""
    property bool pending: false
    property bool stopping: false
    property string notice: ""
    property bool failed: false
    readonly property bool recording: !!job
    property var requests: ({})
    property real started: 0
    property real now: 0
    // Whole seconds since the recording started, for the button.
    readonly property int elapsed: recording ? Math.max(0,Math.floor((now-started)/1000)) : 0
    signal saved(string path)
    function request(operation,args) {let id=Agent.request(operation,args);requests[id]={op:operation,args:args};return id}
    function start(operation,args) {
        if(recording || pending)return
        pending=true;failed=false;path=args.path;notice=qsTr("Starting the recording…")
        request(operation,args)
    }
    function stop() {
        if(!recording || stopping)return
        stopping=true;notice=qsTr("Saving the recording…")
        request("job.send",{job_id:job,command:"stop"})
    }
    function toggle(operation,args) {if(recording)stop();else start(operation,args)}
    property Timer poll: Timer {
        interval:1000;repeat:true;running:recorder.recording
        onTriggered:{recorder.now=Date.now();recorder.request("job.get",{job_id:recorder.job})}
    }
    property Connections replies: Connections {
        target:Agent
        function onApiFinished(id,operation,response) {
            let context=recorder.requests[id];if(!context)return;delete recorder.requests[id]
            if(!response.ok) {
                if(operation==="job.get")return
                if(operation==="job.send")recorder.stopping=false
                else recorder.pending=false
                recorder.notice=response.error.message;recorder.failed=true
                return
            }
            let result=response.result
            if(operation==="job.get") {
                if(result.id!==recorder.job || ["succeeded","failed","cancelled","timed_out"].indexOf(result.state)<0)return
                recorder.job="";recorder.stopping=false
                if(result.state==="succeeded") {
                    recorder.failed=false;recorder.notice=qsTr("Recording saved · %1").arg(recorder.path)
                    Studio.revealFile(recorder.path);recorder.saved(recorder.path)
                } else {recorder.failed=true;recorder.notice=qsTr("The recording did not finish · details under Builds & jobs")}
                return
            }
            if(operation==="job.send")return
            recorder.pending=false;recorder.job=result.id;recorder.started=Date.now();recorder.now=recorder.started
            recorder.notice=qsTr("Recording · press again to stop")
        }
    }
}
