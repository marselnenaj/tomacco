import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// The phone's bottom navigation: four destinations side by side and the rest
// behind More, a sheet from the bottom edge. It replaces the desktop rail below
// 600 logical px (NativeWorkspace.phone) and takes the same navigation list,
// so every section stays reachable and keyboard shortcuts keep working.
// Targets are 48 logical px or more; labels are always shown.
Rectangle {
    id: nav
    // The sections, as NativeWorkspace.navigation: {id, label, icon, key, group}.
    property var navigation: []
    property string current: ""
    // Which sections sit in the bar itself; the others go into More.
    property var primary: ["project","devices","jobs","files"]
    signal navigate(string section)
    readonly property var primaryEntries: primary.map(id=>navigation.find(x=>x.id===id)).filter(x=>!!x)
    readonly property var moreEntries: navigation.filter(x=>primary.indexOf(x.id)<0)
    readonly property bool moreActive: moreEntries.some(x=>x.id===current)
    readonly property int itemHeight: Math.max(Theme.mobileNav,Theme.controlHeight*2)
    implicitHeight: itemHeight
    color: Theme.surface
    function openMore() { more.open() }
    Rectangle { width: parent.width; height: 1; color: Theme.subtle }
    RowLayout {
        anchors.fill: parent; spacing: 0
        Repeater {
            model: nav.primaryEntries.concat([{id:"more",label:qsTr("More","navigation"),icon:"grid"}])
            Item {
                id: item
                required property var modelData
                objectName: "mobileNav-"+modelData.id
                Layout.fillWidth: true; Layout.fillHeight: true
                readonly property bool active: modelData.id==="more" ? nav.moreActive : nav.current===modelData.id
                Rectangle { anchors.top: parent.top; anchors.horizontalCenter: parent.horizontalCenter; width: Math.max(Theme.gapXl,parent.width-Theme.gapXl); height: Theme.px(3); color: Theme.accent; visible: item.active }
                Column {
                    anchors.centerIn: parent; spacing: Theme.gapXs; width: parent.width-Theme.gapSm
                    StudioIcon { name: item.modelData.icon; color: item.active ? Theme.text : Theme.muted; anchors.horizontalCenter: parent.horizontalCenter }
                    Text { width: parent.width; horizontalAlignment: Text.AlignHCenter; text: item.modelData.label; color: item.active ? Theme.text : Theme.muted; font.pixelSize: Theme.fontSmall; font.weight: item.active ? Font.DemiBold : Font.Normal; elide: Text.ElideRight }
                }
                TapHandler { onTapped: item.modelData.id==="more" ? more.open() : nav.navigate(item.modelData.id) }
                Accessible.role: Accessible.Button
                Accessible.name: item.modelData.label
                Accessible.onPressAction: item.modelData.id==="more" ? more.open() : nav.navigate(item.modelData.id)
            }
        }
    }
    // The remaining sections, as a sheet. Back or a tap outside closes it.
    Popup {
        id: more; objectName: "mobileNavMore"
        parent: Overlay.overlay
        width: parent ? parent.width : nav.width
        y: parent ? parent.height-height : 0
        modal: true; focus: true; padding: 0
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: Rectangle { color: Theme.surface; Rectangle { width: parent.width; height: 1; color: Theme.border } }
        contentItem: ColumnLayout {
            spacing: 0
            Rectangle { Layout.alignment: Qt.AlignHCenter; Layout.topMargin: Theme.gapMd; Layout.bottomMargin: Theme.gapSm; width: Theme.px(36); height: Theme.px(4); color: Theme.border }
            Repeater {
                model: nav.moreEntries
                ItemDelegate {
                    id: row
                    required property var modelData
                    objectName: "mobileNavMore-"+modelData.id
                    Layout.fillWidth: true; implicitHeight: Theme.px(56)
                    padding: 0; leftPadding: Theme.gapLg; rightPadding: Theme.gapLg
                    highlighted: nav.current===modelData.id
                    background: Rectangle { color: row.highlighted ? Theme.selected : row.down ? Theme.hover : "transparent"
                        Rectangle { visible: row.highlighted; width: Theme.px(2); height: parent.height; color: Theme.accent }
                        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.subtle }
                    }
                    contentItem: RowLayout { spacing: Theme.gapMd
                        StudioIcon { name: row.modelData.icon; color: row.highlighted ? Theme.text : Theme.muted }
                        Text { Layout.fillWidth: true; text: row.modelData.label; color: Theme.text; font.pixelSize: Theme.fontLead; elide: Text.ElideRight }
                        Text { visible: !!row.modelData.key; text: qsTr("Ctrl")+"+"+row.modelData.key; color: Theme.faint; font.family: Theme.mono; font.pixelSize: Theme.fontMicro }
                    }
                    Accessible.name: modelData.label
                    onClicked: { more.close(); nav.navigate(modelData.id) }
                }
            }
            Item { Layout.preferredHeight: Theme.gapXl }
        }
    }
}
