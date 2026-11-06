import QtQuick
import QtQuick.Controls
import "."

TextField {
    id: control
    // A field for a path, address, identifier or number: its text stays at the left edge and reads
    // left to right also in a right-to-left interface. Under LayoutMirroring an explicit alignment is
    // mirrored, so the left edge is asked for as AlignRight there.
    property bool machine: false
    horizontalAlignment: machine && control.LayoutMirroring.enabled ? TextInput.AlignRight : TextInput.AlignLeft
    implicitHeight: Theme.controlHeight
    implicitWidth: Theme.px(160)
    leftPadding: Theme.gapMd
    rightPadding: Theme.gapMd
    topPadding: 0
    bottomPadding: 0
    verticalAlignment: TextInput.AlignVCenter
    font.pixelSize: Theme.fontBody
    color: enabled ? Theme.text : Theme.faint
    placeholderTextColor: Theme.faint
    selectionColor: Theme.accent
    selectedTextColor: Theme.accentText
    selectByMouse: true
    background: Rectangle {
        radius: Theme.radiusSmall
        color: control.enabled ? Theme.inset : "transparent"
        border.color: control.activeFocus ? Theme.accentInk : control.hovered && control.enabled ? Theme.faint : control.enabled ? Theme.border : Theme.subtle
        border.width: control.activeFocus ? 2 : 1
    }
    Accessible.name: placeholderText
}
