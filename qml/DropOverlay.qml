import QtQuick
import "."

// What a drop target shows while files are dragged over it: a frame and one
// sentence, in the accent when the drop will be taken, in red when not.
Rectangle {
    id: overlay
    property string text: ""
    property bool fits: true
    color: Theme.scrim
    border.width: Theme.gapXxs
    border.color: fits ? Theme.accent : Theme.danger
    Rectangle {
        anchors.centerIn: parent
        width: Math.min(overlay.width-Theme.gapXl,label.implicitWidth+Theme.gapXl*2)
        height: label.implicitHeight+Theme.gapLg*2
        radius: Theme.radiusLarge
        color: Theme.elevated
        border.color: overlay.border.color
        StudioHeading {
            id: label
            anchors.centerIn: parent
            width: Math.min(implicitWidth,overlay.width-Theme.gapXl*3)
            horizontalAlignment: Text.AlignHCenter
            size: Theme.fontTitle
            color: overlay.fits ? Theme.text : Theme.danger
            text: overlay.text
        }
    }
}
