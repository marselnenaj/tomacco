import QtQuick
import QtQuick.Layouts
import "."

// A page's header: a full-bleed block of colour with one huge word and one calm
// sentence. tone is red, green or inverse (the theme turned around). The type
// shrinks to fit the width and takes less room in a short window.
Rectangle {
    id: band
    property string title: ""
    property string lead: ""
    property string tone: "inverse"
    property string detail: ""
    property bool roomy: true
    default property alias trailing: extras.data
    readonly property color ink: tone==="inverse" ? Theme.background : Theme.ink
    color: tone==="red" ? Theme.accent : tone==="green" ? Theme.green : Theme.text
    readonly property int pad: roomy ? Theme.gapXl : Theme.gapLg
    readonly property int fitted: Math.floor((width-pad*2-extras.width)/Math.max(6,title.length*.66))
    implicitHeight: column.implicitHeight+pad*2
    ColumnLayout {
        id: column
        anchors {left: parent.left; right: parent.right; top: parent.top; margins: band.pad}
        spacing: band.roomy ? Theme.gapMd : Theme.gapXs
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.gapMd
            StudioHeading {
                objectName: "bandTitle"
                Layout.fillWidth: true
                text: band.title
                color: band.ink
                size: Math.max(Theme.fontDisplay,Math.min(band.roomy?Theme.fontMega:Theme.fontGiant-Theme.gapMd,band.fitted))
                wrapMode: Text.NoWrap
                elide: Text.ElideRight
            }
            RowLayout {
                id: extras
                Layout.alignment: Qt.AlignTop
                spacing: Theme.gapXs
            }
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.gapMd
            Text {
                Layout.fillWidth: true
                text: band.lead
                color: band.ink
                font.pixelSize: band.roomy ? Theme.fontLarge : Theme.fontLead
                font.weight: Font.Medium
                font.letterSpacing: -.2
                wrapMode: Text.WordWrap
                maximumLineCount: 2
                elide: Text.ElideRight
            }
            Text {
                objectName: "bandDetail"
                visible: !!band.detail
                text: band.detail
                color: band.ink
                font.family: Theme.mono
                font.pixelSize: Theme.fontSmall
                font.weight: Font.Bold
            }
        }
    }
}
