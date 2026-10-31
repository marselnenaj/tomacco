import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "LogFilter.js" as LogFilter

// The live log of one Android device: a device.logs job read as it grows,
// for one app (by its user ID from Android 12) and from a level up. A capture
// that ends is started again while the pane follows.
ColumnLayout {
    id: pane
    objectName: "deviceLogPane"
    property string device: ""
    property string platform: "android"
    // Follows while shown; a hidden pane ends its capture.
    property bool following: visible && !!device
    property string bundleId: ""
    // The lowest logcat level shown: V, I, W or E.
    property string level: "V"
    property string query: ""
    property string job: ""
    property int offset: 0
    property string text: ""
    property string error: ""
    property bool paused: false
    property var requests: ({})
    // Each capture's number: a reply for an earlier one is ended, not shown.
    property int generation: 0
    // Bytes kept of the log; older lines go first.
    readonly property int limit: 400000
    readonly property var lines: LogFilter.filter(text,query,"all")
    spacing: Theme.gapXs
    function request(operation,args) {const id=Agent.request(operation,args);requests[id]={op:operation,args:args,job:job,generation:generation};return id}
    function stopCapture() {if(job)request("job.cancel",{job_id:job});job="";offset=0}
    function restart() {
        stopCapture();text="";error="";generation+=1
        if(!following || paused)return
        let args={platform:platform,device:device,duration:3600}
        if(bundleId)args.bundle_id=bundleId
        if(level!=="V")args.filter="*:"+level
        request("device.logs",args)
    }
    // A new device, app or level starts a new capture.
    onFollowingChanged:restart()
    onDeviceChanged:if(following)restart()
    onLevelChanged:if(following)restart()
    onPausedChanged:restart()
    Component.onDestruction:stopCapture()
    Timer {
        interval:700;repeat:true;running:pane.following && !!pane.job
        onTriggered:if(!Object.keys(pane.requests).some(id=>pane.requests[id].op==="job.logs"))pane.request("job.logs",{job_id:pane.job,offset:pane.offset})
    }
    Connections {
        target:Agent
        function onApiFinished(id,operation,response) {
            const context=pane.requests[id];if(!context)return;delete pane.requests[id]
            if(operation==="job.cancel")return
            if(operation==="device.logs") {
                const current=context.generation===pane.generation && pane.following && !pane.paused
                if(!current){if(response.ok)Agent.request("job.cancel",{job_id:response.result.id});return}
                if(!response.ok){pane.error=response.error.message;return}
                pane.job=response.result.id;pane.offset=0;return
            }
            if(operation!=="job.logs" || context.job!==pane.job || !response.ok)return
            const value=response.result
            if(value.text) {
                let next=pane.text+value.text
                if(next.length>pane.limit){next=next.slice(next.length-pane.limit);next=next.slice(next.indexOf("\n")+1)}
                pane.text=next
            }
            pane.offset=value.next_offset
            // The capture ended (its hour, or adb lost the device): start another.
            if(value.eof && ["succeeded","failed","cancelled","timed_out"].indexOf(value.state)>=0){pane.job="";if(pane.following && !pane.paused)pane.restart()}
        }
    }
    RowLayout {Layout.fillWidth:true;spacing:Theme.gapXs
        StudioTextField {id:appField;objectName:"deviceLogApp";Layout.fillWidth:true;Layout.maximumWidth:280;placeholderText:qsTr("App ID, all apps when empty");font.family:Theme.mono
            onEditingFinished:{const value=text.trim();if(value!==pane.bundleId){pane.bundleId=value;if(pane.following)pane.restart()}}}
        StudioSegmented {
            objectName:"deviceLogLevel";current:pane.level
            model:[{id:"V",label:qsTr("All"),objectName:"deviceLogLevel-V"},{id:"I",label:qsTr("Info"),objectName:"deviceLogLevel-I"},{id:"W",label:qsTr("Warnings"),objectName:"deviceLogLevel-W"},{id:"E",label:qsTr("Errors"),objectName:"deviceLogLevel-E"}]
            onChosen:id=>pane.level=id
        }
        StudioTextField {objectName:"deviceLogSearch";Layout.fillWidth:true;placeholderText:qsTr("Find in the log");onTextChanged:pane.query=text}
        StudioButton {objectName:"deviceLogPause";text:pane.paused?qsTr("Resume"):qsTr("Pause");displayText:"";iconName:pane.paused?"play":"pause";quiet:true;tip:text;onClicked:pane.paused=!pane.paused}
        StudioButton {objectName:"deviceLogClear";text:qsTr("Clear");displayText:"";iconName:"close";quiet:true;tip:qsTr("Clear the lines shown");onClicked:pane.text=""}
    }
    Rectangle {
        Layout.fillWidth:true;Layout.fillHeight:true;radius:Theme.radiusSmall;color:Theme.inset;border.color:Theme.subtle
        ListView {
            id:logView;objectName:"deviceLogLines"
            anchors.fill:parent;anchors.margins:Theme.gapXs;clip:true
            model:pane.lines
            // Stays at the newest line unless the reader scrolled up.
            property bool atEnd:true
            onMovementEnded:atEnd=atYEnd
            onCountChanged:if(atEnd)positionViewAtEnd()
            delegate:Text {
                required property string modelData
                width:ListView.view.width;text:modelData;wrapMode:Text.WrapAnywhere;textFormat:Text.PlainText
                font.family:Theme.mono;font.pixelSize:Theme.fontMicro
                color:{const level=LogFilter.level(modelData);return level==="error"?Theme.danger:level==="warning"?Theme.warning:Theme.text}
            }
            ScrollBar.vertical:StudioScrollBar {}
        }
        Text {
            anchors.centerIn:parent;width:parent.width-Theme.gapXl;horizontalAlignment:Text.AlignHCenter;wrapMode:Text.WordWrap
            visible:!pane.lines.length;objectName:"deviceLogState"
            text:pane.error || (pane.paused?qsTr("Paused"):pane.query && pane.text?qsTr("No line contains this text."):qsTr("Waiting for log lines…"))
            color:pane.error?Theme.danger:Theme.faint;font.pixelSize:Theme.fontSmall
        }
    }
}
