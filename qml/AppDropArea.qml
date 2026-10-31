import QtQuick
import "."
import "AppFiles.js" as AppFiles

// Apps dragged onto a device view. Any drag of app files is taken, so a file
// the device cannot install is explained instead of silently refused; other
// files pass on to whatever lies beneath.
DropArea {
    id: area
    // The kinds this device installs ("apk", "ipa", "app"); empty: none.
    property var kinds: []
    // The device's name, for the invitation.
    property string targetName: ""
    // Why the dragged files do not fit, in a sentence.
    property string refusal: ""
    property var draggedPaths: []
    readonly property bool fits: AppFiles.installable(draggedPaths,kinds)
    signal appsDropped(var paths)
    signal refused(string reason)
    onEntered: drag=>{
        draggedPaths=AppFiles.paths(drag.urls)
        if(!AppFiles.allApps(draggedPaths)){draggedPaths=[];drag.accepted=false;return}
        drag.accept(Qt.CopyAction)
    }
    onExited: draggedPaths=[]
    onDropped: drop=>{
        const list=AppFiles.paths(drop.urls)
        draggedPaths=[]
        if(!AppFiles.allApps(list)){drop.accepted=false;return}
        drop.accept(Qt.CopyAction)
        if(AppFiles.installable(list,kinds))appsDropped(list);else refused(refusal)
    }
    Rectangle {
        objectName: "appDropOverlay"
        anchors.fill: parent
        visible: area.containsDrag
        color: Theme.scrim
        border.width: Theme.gapXxs
        border.color: area.fits ? Theme.accent : Theme.danger
        Rectangle {
            anchors.centerIn: parent
            width: Math.min(parent.width-Theme.gapXl,label.implicitWidth+Theme.gapXl*2)
            height: label.implicitHeight+Theme.gapLg*2
            radius: Theme.radiusLarge
            color: Theme.elevated
            border.color: area.fits ? Theme.accent : Theme.danger
            StudioHeading {
                id: label
                anchors.centerIn: parent
                width: Math.min(implicitWidth,parent.parent.width-Theme.gapXl*3)
                horizontalAlignment: Text.AlignHCenter
                size: Theme.fontTitle
                color: area.fits ? Theme.text : Theme.danger
                text: area.fits ? qsTr("Drop to install on %1").arg(area.targetName) : area.refusal
            }
        }
    }
}
