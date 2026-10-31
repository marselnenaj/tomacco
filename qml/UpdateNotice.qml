import QtQuick
import QtQuick.Controls
import "."

// The status line's word about an update; a click opens the Settings.
Item {
    id: notice
    property var model
    signal opened()
    objectName: "statusUpdate"
    visible: !!model.notice
    implicitWidth: word.implicitWidth
    implicitHeight: word.implicitHeight
    StudioStatus {
        id: word
        anchors.verticalCenter: parent.verticalCenter
        tone: notice.model.restartNeeded?"live":"busy"
        mono: true
        size: Theme.fontMicro
        text: notice.model.notice
    }
    MouseArea {id:area;anchors.fill:parent;hoverEnabled:true;cursorShape:Qt.PointingHandCursor;onClicked:notice.opened()}
    ToolTip.visible:area.containsMouse;ToolTip.delay:550;ToolTip.text:qsTr("Open the update settings")
}
