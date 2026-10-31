import QtQuick
import "."

// A state at a glance: a small square and its word. tone is one of idle, busy,
// live, ok, warn, failed. Only "busy" moves, and only without reduced motion.
Row {
    id: status
    property string tone: "idle"
    property string text: ""
    property int size: Theme.fontSmall
    property bool mono: false
    readonly property color toneColor: tone==="live" ? Theme.accent
        : tone==="busy" ? Theme.warning
        : tone==="ok" ? Theme.success
        : tone==="warn" ? Theme.warning
        : tone==="failed" ? Theme.danger
        : Theme.faint
    spacing: Theme.gapSm
    Rectangle {
        id: square
        width: 7; height: 7
        anchors.verticalCenter: parent.verticalCenter
        color: status.toneColor
        SequentialAnimation on opacity {
            running: status.tone==="busy" && !Theme.reducedMotion && status.visible
            loops: Animation.Infinite
            onRunningChanged: if(!running)square.opacity=1
            NumberAnimation { to: .25; duration: 600 }
            NumberAnimation { to: 1; duration: 600 }
        }
    }
    Text {
        visible: !!status.text
        text: status.text
        color: Theme.muted
        font.family: status.mono ? Theme.mono : Theme.sans
        font.pixelSize: status.size
        anchors.verticalCenter: parent.verticalCenter
    }
}
