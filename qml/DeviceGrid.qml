import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// Every running Android device and emulator side by side, each with a picture
// of its screen from android.screen that refreshes every few seconds. A click
// on one chooses it.
Item {
    id: grid
    objectName: "deviceGrid"
    // device.list entries of running devices.
    property var devices: []
    property string selected: ""
    property int interval: 2500
    // Device ID -> {path, stamp, width, height} of its latest picture, or {error}.
    property var shots: ({})
    property var requests: ({})
    signal chosen(var device)
    function refresh() {
        for(const device of devices) {
            if(Object.keys(requests).some(id=>requests[id].device===device.id))continue
            const path=Studio.thumbnailPath(device.id)
            if(!path)continue
            requests[Agent.request("android.screen",{device:device.id,path:path})]={device:device.id}
        }
    }
    Timer {objectName:"deviceGridTimer";interval:grid.interval;repeat:true;triggeredOnStart:true;running:grid.visible && grid.devices.length>0;onTriggered:grid.refresh()}
    Connections {
        target:Agent
        function onApiFinished(id,operation,response) {
            const context=grid.requests[id];if(!context)return;delete grid.requests[id]
            let next=Object.assign({},grid.shots)
            if(response.ok)next[context.device]={path:response.result.path,stamp:Date.now(),width:response.result.width,height:response.result.height}
            else next[context.device]=Object.assign({},next[context.device] || {},{error:response.error.message})
            grid.shots=next
        }
    }
    GridView {
        id:tiles;objectName:"deviceGridView"
        anchors.fill:parent;clip:true;visible:grid.devices.length>0
        readonly property int columns:Math.max(1,Math.floor(width/190))
        cellWidth:Math.floor(width/columns);cellHeight:Math.round(cellWidth*1.9)+Theme.gapXxl
        model:grid.devices
        delegate:ItemDelegate {
            id:tile
            required property var modelData
            readonly property var shot:grid.shots[modelData.id] || ({})
            objectName:"deviceTile-"+modelData.id
            width:tiles.cellWidth-Theme.gapSm;height:tiles.cellHeight-Theme.gapSm
            Accessible.name:modelData.name
            background:Rectangle {radius:Theme.radius;color:tile.hovered?Theme.hover:Theme.surface;border.color:grid.selected===tile.modelData.id || tile.visualFocus?Theme.accentInk:Theme.subtle}
            contentItem:ColumnLayout {spacing:Theme.gapXs
                Item {Layout.fillWidth:true;Layout.fillHeight:true
                    Image {
                        id:picture;objectName:"deviceThumb-"+tile.modelData.id
                        anchors.fill:parent;fillMode:Image.PreserveAspectFit;asynchronous:true;cache:false;smooth:true
                        sourceSize.height:640
                        // The stamp makes a new picture at the same path load again.
                        source:tile.shot.path?"file://"+tile.shot.path+"?"+tile.shot.stamp:""
                    }
                    Text {anchors.centerIn:parent;width:parent.width-Theme.gapMd;visible:picture.status!==Image.Ready;horizontalAlignment:Text.AlignHCenter;wrapMode:Text.WordWrap
                        text:tile.shot.error || qsTr("Waiting for the screen…");color:tile.shot.error?Theme.danger:Theme.faint;font.pixelSize:Theme.fontSmall}
                }
                Text {Layout.fillWidth:true;text:(tile.modelData.pinned?"★ ":"")+tile.modelData.name;color:Theme.text;font.pixelSize:Theme.fontBody;font.weight:Font.DemiBold;elide:Text.ElideRight}
                Text {Layout.fillWidth:true;text:tile.modelData.id;color:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontMicro;elide:Text.ElideMiddle}
            }
            onClicked:grid.chosen(modelData)
        }
        ScrollBar.vertical:StudioScrollBar {}
    }
    StudioEmptyState {
        anchors.centerIn:parent;width:Math.min(parent.width-Theme.gapXxl,480);visible:!grid.devices.length
        title:qsTr("No device running.");body:qsTr("Start an emulator or plug in a phone with USB debugging on; each one shows here with its screen.")
    }
}
