import QtQuick
import QtQuick.Controls
import "."

ScrollBar {
    id: control
    padding: Theme.gapXxs
    minimumSize: 0.05
    implicitWidth: Theme.gapMd
    implicitHeight: Theme.gapMd
    hoverEnabled: true
    contentItem: Rectangle {
        implicitWidth: Theme.gapXs+Theme.gapXxs; implicitHeight: Theme.gapXs+Theme.gapXxs
        color: control.pressed ? Theme.text : control.hovered ? Theme.muted : Theme.faint
        opacity: control.size < 1 ? (control.active || control.hovered ? 0.8 : 0.3) : 0
        Behavior on opacity { NumberAnimation { duration: Theme.motionFast } }
    }
    background: Item {}
}
