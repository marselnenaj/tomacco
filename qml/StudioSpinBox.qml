import QtQuick
import QtQuick.Controls
import "."

SpinBox {
    id: control
    implicitHeight: Theme.controlHeight
    implicitWidth: 112
    font.pixelSize: Theme.fontBody
    leftPadding: Theme.controlHeight
    rightPadding: Theme.controlHeight
    topPadding: 0
    bottomPadding: 0
    hoverEnabled: true
    contentItem: TextInput {
        text: control.displayText
        font: control.font
        color: control.enabled ? Theme.text : Theme.faint
        selectionColor: Theme.accent
        selectedTextColor: Theme.accentText
        horizontalAlignment: Qt.AlignHCenter
        verticalAlignment: Qt.AlignVCenter
        readOnly: !control.editable
        validator: control.validator
        inputMethodHints: control.inputMethodHints
        clip: true
    }
    down.indicator: Item {
        x: 0; height: control.height; implicitWidth: Theme.controlHeight
        StudioIcon {name: "minus"; anchors.centerIn: parent; width: Theme.gapMd; height: Theme.gapMd; color: control.down.pressed ? Theme.text : Theme.muted}
    }
    up.indicator: Item {
        x: control.width-width; height: control.height; implicitWidth: Theme.controlHeight
        StudioIcon {name: "plus"; anchors.centerIn: parent; width: Theme.gapMd; height: Theme.gapMd; color: control.up.pressed ? Theme.text : Theme.muted}
    }
    background: Rectangle {
        radius: Theme.radiusSmall
        color: Theme.inset
        border.color: control.activeFocus ? Theme.accentInk : Theme.border
    }
}
