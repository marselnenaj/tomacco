import QtQuick
import QtQuick.Layouts
import "."

// The band at the top of the main window while an agent waits for the person
// to act on the device: why, what to do, and Done once it is done. Done is the
// person's answer; the agent then looks at the screen again instead of
// replaying anything. Cancel withdraws the request.
Rectangle {
    id: banner
    objectName: "handoffBanner"
    property var model
    readonly property var handoff: model ? model.current : null
    visible: !!handoff
    implicitHeight: visible ? column.implicitHeight+Theme.gapSm*2 : 0
    color: Theme.accent
    ColumnLayout {
        id: column
        anchors.fill: parent
        anchors.leftMargin: Theme.gapLg
        anchors.rightMargin: Theme.gapSm
        anchors.topMargin: Theme.gapSm
        anchors.bottomMargin: Theme.gapSm
        spacing: Theme.gapXs
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.gapSm
            Text {
                objectName: "handoffBannerTitle"
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                text: banner.handoff ? (banner.model.count>1
                    ? qsTr("%1 · an agent is waiting for you (%2 more)").arg(banner.model.kindText(banner.handoff.kind)).arg(banner.model.count-1)
                    : qsTr("%1 · an agent is waiting for you").arg(banner.model.kindText(banner.handoff.kind))) : ""
                color: Theme.accentText
                font.pixelSize: Theme.fontBody
                font.weight: Font.DemiBold
                wrapMode: Text.WordWrap
                textFormat: Text.PlainText
            }
            StudioButton {
                objectName: "handoffBannerDone"
                text: qsTr("Done")
                inverted: true; invertedFill: Theme.accentText; invertedText: Theme.accent
                Accessible.name: qsTr("Done: I did it on the device")
                onClicked: banner.model.complete(banner.handoff.id)
            }
            StudioButton {
                objectName: "handoffBannerCancel"
                text: qsTr("Cancel")
                inverted: true; invertedFill: Theme.accent; invertedText: Theme.accentText
                onClicked: banner.model.cancel(banner.handoff.id)
            }
        }
        Text {
            objectName: "handoffBannerReason"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            text: banner.handoff ? (banner.handoff.instructions ? banner.handoff.reason+" · "+banner.handoff.instructions : banner.handoff.reason) : ""
            color: Theme.accentText
            font.pixelSize: Theme.fontSmall
            wrapMode: Text.WordWrap
            maximumLineCount: 3
            elide: Text.ElideRight
            textFormat: Text.PlainText
        }
        Text {
            objectName: "handoffBannerNote"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            text: qsTr("Do it on the device itself. Tomacco never asks for your passcode or password.")
            color: Theme.accentText
            opacity: 0.85
            font.pixelSize: Theme.fontMicro
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
        }
    }
}
