import QtQuick
import QtQuick.Layouts
import "."

// One idea on a page: a heavy rule, its heading to the left and the controls to
// the right; stacked when narrow. `actions` sit under the heading.
Item {
    id: panel
    property string title: ""
    property string lead: ""
    property alias actions: actionRow.data
    default property alias content: body.data
    property alias spacing: body.spacing
    readonly property bool sideways: width>=Theme.px(720)
    implicitWidth: grid.implicitWidth
    implicitHeight: grid.implicitHeight+Theme.gapLg+Theme.gapXl
    Rectangle {width:parent.width;height:3;color:Theme.text}
    GridLayout {
        id: grid
        anchors {left: parent.left; right: parent.right; top: parent.top; topMargin: Theme.gapLg}
        columns: panel.sideways ? 2 : 1
        columnSpacing: Theme.gapXl
        rowSpacing: Theme.gapMd
        ColumnLayout {
            Layout.minimumWidth: 0
            Layout.alignment: Qt.AlignTop
            Layout.preferredWidth: panel.sideways ? Math.round(panel.width*.3) : panel.width
            Layout.maximumWidth: panel.sideways ? Math.round(panel.width*.3) : panel.width
            spacing: Theme.gapSm
            StudioHeading {
                Layout.fillWidth: true
                visible: !!panel.title
                text: panel.title
                size: Theme.fontDisplay
            }
            Text {
                Layout.fillWidth: true
                visible: !!panel.lead
                text: panel.lead
                color: Theme.muted
                font.pixelSize: Theme.fontBody
                lineHeight: 1.2
                wrapMode: Text.WordWrap
            }
            Flow { id: actionRow; Layout.fillWidth:true; Layout.minimumWidth:0; spacing: Theme.gapXs; visible: children.length>0 }
        }
        ColumnLayout {
            id: body
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.alignment: Qt.AlignTop
            spacing: Theme.gapMd
        }
    }
}
