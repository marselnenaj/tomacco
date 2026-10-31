import QtQuick
import QtQuick.Controls
import "."

CheckBox {
    id: control
    font.pixelSize: Theme.fontBody
    spacing: Theme.gapSm
    implicitHeight: Theme.controlHeight
    topPadding: 0
    bottomPadding: 0
    leftPadding: 0
    indicator: Rectangle {
        implicitWidth: Theme.iconSize; implicitHeight: Theme.iconSize
        x: control.leftPadding; y: (control.height-height)/2
        radius: Theme.radiusSmall
        color: control.checked ? Theme.accent : Theme.inset
        border.color: control.visualFocus ? Theme.accentInk : control.checked ? Theme.accent : Theme.faint
        border.width: control.visualFocus ? 2 : 1
        StudioIcon { anchors.centerIn: parent; width: Theme.gapMd; height: Theme.gapMd; name: "check"; visible: control.checked; color: Theme.accentText }
    }
    contentItem: Text {
        text: control.text; font: control.font; color: control.enabled ? Theme.text : Theme.faint
        leftPadding: control.indicator.width+control.spacing
        verticalAlignment: Text.AlignVCenter
    }
}
