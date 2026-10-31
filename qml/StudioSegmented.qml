import QtQuick
import QtQuick.Layouts
import "."

// A choice among a few: joined buttons in one outline. model is a list of
// {id, label, objectName (optional)}.
Rectangle {
    id: control
    property var model: []
    property string current: ""
    signal chosen(string id)
    implicitHeight: Theme.controlHeight
    implicitWidth: buttons.implicitWidth+2
    color: "transparent"
    radius: Theme.radiusSmall
    border.color: Theme.border
    RowLayout {
        id: buttons
        anchors.fill: parent
        anchors.margins: 1
        spacing: 0
        Repeater {
            model: control.model
            StudioButton {
                required property var modelData
                objectName: modelData.objectName || ""
                text: modelData.label
                Layout.fillWidth: true
                Layout.fillHeight: true
                implicitHeight: Theme.controlHeight-2
                quiet: true
                selected: control.current===modelData.id
                onClicked: control.chosen(modelData.id)
            }
        }
    }
}
