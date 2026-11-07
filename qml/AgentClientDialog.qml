import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// The one place to connect a coding agent: the onboarding, the activity panel
// and the tools page open it. Each client gets its own state and its own text.
StudioDialog {
    id: dialog
    objectName: "agentClientDialog"
    title: qsTr("Connect your coding agent")
    modal: true
    onAboutToShow: picker.refresh()
    contentItem: StudioScrollView {
        clip: true; contentWidth: availableWidth
        ColumnLayout {
            width: parent.width-Theme.gapMd; spacing: Theme.gapMd
            Text { Layout.fillWidth: true; text: qsTr("Pick your agent. Tomacco shows whether it is connected and the exact setting it reads."); color: Theme.muted; font.pixelSize: Theme.fontSmall; wrapMode: Text.Wrap }
            AgentClientPicker { id: picker; Layout.fillWidth: true }
            StudioButton { objectName: "agentClientDialogClose"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; Layout.maximumWidth: naturalWidth; touch: dialog.sheet; text: qsTr("Done"); onClicked: dialog.close() }
        }
    }
}
