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
    DropOverlay {
        objectName: "appDropOverlay"
        anchors.fill: parent
        visible: area.containsDrag
        fits: area.fits
        text: area.fits ? qsTr("Drop to install on %1").arg(area.targetName) : area.refusal
    }
}
