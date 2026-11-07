import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// A phone list row, as the Bogen component "Mobile / Row" draws it: a status
// square and the meta text sit on the title line, the detail wraps rather than
// being cut, and the whole row is one target of at least 56 logical px. Title
// 16 semibold, detail 14 regular muted (or mono 13 for paths and code), meta
// 13 faint on the right at most 40 % wide, a chevron where a tap opens more.
// Only phone-wide layouts (NativeWorkspace.phone) use it; the desktop keeps
// its own list delegates.
ItemDelegate {
    id: row
    property string title: ""
    property string detail: ""
    property string meta: ""
    property bool showDot: true
    property color dot: Theme.success
    property bool chevron: true
    // Paths, operation names and code read better in the mono face.
    property bool monoDetail: false
    // Narrower than 300 logical px (a 412 px phone at 200 %), the meta text moves
    // under the title instead of taking 40 % beside it, so the title keeps a
    // readable measure.
    readonly property bool stacked: availableWidth<Theme.px(300)
    implicitWidth: Theme.px(393)
    implicitHeight: Math.max(Theme.mobileRow, contentItem.implicitHeight+topPadding+bottomPadding)
    padding: 0
    leftPadding: Theme.mobileGutter; rightPadding: Theme.mobileGutter
    topPadding: Theme.gapMd; bottomPadding: Theme.gapMd
    background: Rectangle {
        color: row.down ? Theme.hover : row.highlighted ? Theme.selected : "transparent"
        Rectangle { visible: row.highlighted; width: Theme.px(2); height: parent.height; color: Theme.accent }
        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.subtle }
    }
    contentItem: RowLayout {
        spacing: Theme.gapMd
        Rectangle {
            objectName: "rowDot"; visible: row.showDot
            Layout.preferredWidth: Theme.px(8); Layout.preferredHeight: Theme.px(8)
            Layout.alignment: Qt.AlignTop; Layout.topMargin: Theme.px(6)
            color: row.dot
        }
        ColumnLayout {
            Layout.fillWidth: true; Layout.minimumWidth: 0; spacing: Theme.gapXxs
            Text {
                objectName: "rowTitle"; Layout.fillWidth: true
                text: row.title; color: Theme.text
                font.family: Theme.sans; font.pixelSize: Theme.mobileFontBody; font.weight: Font.DemiBold
                wrapMode: Text.Wrap; lineHeight: 1.3
            }
            Text {
                objectName: "rowMetaStacked"; visible: row.stacked && row.meta.length>0; Layout.fillWidth: true
                text: row.meta; color: Theme.faint
                font.family: Theme.sans; font.pixelSize: Theme.mobileFontMeta
                wrapMode: Text.Wrap
            }
            Text {
                objectName: "rowDetail"; visible: row.detail.length>0; Layout.fillWidth: true
                text: row.detail; color: Theme.muted
                font.family: row.monoDetail ? Theme.mono : Theme.sans
                font.pixelSize: row.monoDetail ? Theme.mobileFontMeta : Theme.mobileFontSmall
                wrapMode: row.monoDetail ? Text.WrapAnywhere : Text.Wrap; lineHeight: 1.4
            }
        }
        Text {
            objectName: "rowMeta"; visible: !row.stacked && row.meta.length>0
            text: row.meta; color: Theme.faint
            font.family: Theme.sans; font.pixelSize: Theme.mobileFontMeta
            Layout.alignment: Qt.AlignTop; Layout.topMargin: Theme.px(2)
            Layout.maximumWidth: Math.round(row.availableWidth*0.4); elide: Text.ElideRight
        }
        StudioIcon {
            objectName: "rowChevron"; visible: row.chevron; name: "chevronRight"; color: Theme.faint
            Layout.preferredWidth: Theme.px(20); Layout.preferredHeight: Theme.px(20); Layout.alignment: Qt.AlignTop
        }
    }
    Accessible.role: Accessible.Button
    Accessible.name: title+(detail ? " · "+detail : "")+(meta ? " · "+meta : "")
}
