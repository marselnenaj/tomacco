import QtQuick
import QtQuick.Layouts
import "."

// The band at the top of the main window that says a new version is there, or
// that it is installed and waits for a restart. Later hides it for that version.
Rectangle {
    id: banner
    objectName: "updateBanner"
    property var model
    readonly property string kind: model ? model.bannerKind : ""
    visible: !!kind
    implicitHeight: visible ? Math.max(Theme.controlHeight+Theme.gapSm*2,row.implicitHeight+Theme.gapSm*2) : 0
    color: Theme.accent
    RowLayout {
        id: row
        anchors.fill: parent
        anchors.leftMargin: Theme.gapLg
        anchors.rightMargin: Theme.gapSm
        spacing: Theme.gapSm
        Text {
            objectName: "updateBannerText"
            Layout.fillWidth: true
            text: banner.kind==="restart" ? qsTr("Updated to %1 · Restart to use it").arg(banner.model.status.current)
                : banner.kind==="installing" ? qsTr("Installing Tomacco %1…").arg(banner.model.latest)
                : qsTr("Tomacco %1 is available").arg(banner.model.latest)
            color: Theme.accentText
            font.pixelSize: Theme.fontBody
            font.weight: Font.DemiBold
            elide: Text.ElideRight
            textFormat: Text.PlainText
        }
        StudioButton {
            objectName: "updateBannerNotes"
            visible: !!banner.model.notesUrl && banner.kind==="available"
            text: qsTr("What's new ↗")
            inverted: true; invertedFill: Theme.accent; invertedText: Theme.accentText
            onClicked: Studio.openExternal(banner.model.notesUrl)
        }
        StudioButton {
            objectName: "updateBannerInstall"
            visible: banner.kind==="available" && banner.model.canInstall
            enabled: !banner.model.working
            text: qsTr("Install")
            inverted: true; invertedFill: Theme.accentText; invertedText: Theme.accent
            onClicked: banner.model.install()
        }
        StudioButton {
            objectName: "updateBannerRestart"
            visible: banner.kind==="restart"
            text: qsTr("Restart now")
            inverted: true; invertedFill: Theme.accentText; invertedText: Theme.accent
            onClicked: banner.model.restartRequested()
        }
        StudioButton {
            objectName: "updateBannerLater"
            visible: banner.kind!=="installing"
            text: qsTr("Later")
            inverted: true; invertedFill: Theme.accent; invertedText: Theme.accentText
            onClicked: banner.model.later()
        }
    }
}
