import QtQuick
import QtQuick.Controls
import "."

// Read-only monospace output: logs, JSON results, reports. It follows its end
// while the reader is at the end.
StudioScrollView {
    id: view
    property alias text: area.text
    property alias placeholderText: area.placeholderText
    property alias textColor: area.color
    TextArea {
        id: area
        objectName: view.objectName ? view.objectName+"-text" : ""
        readOnly: true
        selectByMouse: true
        wrapMode: TextEdit.Wrap
        font.family: Theme.mono
        font.pixelSize: Theme.fontSmall
        color: Theme.muted
        placeholderTextColor: Theme.faint
        selectionColor: Theme.accent
        selectedTextColor: Theme.accentText
        leftPadding: Theme.gapMd
        rightPadding: Theme.gapMd
        topPadding: Theme.gapSm
        bottomPadding: Theme.gapSm
        background: Rectangle { color: Theme.inset; radius: Theme.radiusSmall }
    }
}
