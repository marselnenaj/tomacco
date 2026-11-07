import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// A phone page's top bar, as the Bogen component "Mobile / Top bar": a back
// target of at least 48 logical px, the title in full (it wraps, it is never
// cut), an optional line below it (mono for paths) and trailing actions. The
// bar grows with its text and the interface size, so at 175 or 200 % the back
// target keeps its size instead of being squeezed by the row. Only phone-wide
// layouts (Workspace.phone) use it; the desktop keeps its own headers.
Rectangle {
    id: bar
    property string title: ""
    property string subtitle: ""
    property bool monoSubtitle: false
    // What the back target says to assistive technology, such as "All jobs".
    property string backText: qsTr("Back")
    property bool showBack: true
    // The back target's own name, where a page already has one (such as jobBack).
    property string backObjectName: ""
    // Buttons at the far end; give them touch: true.
    default property alias actions: trailing.data
    signal back()
    color: Theme.background
    implicitWidth: Theme.px(393)
    implicitHeight: Math.max(Theme.mobileBar,Theme.mobileTouch+Theme.gapXs*2,content.implicitHeight+Theme.gapSm*2)
    // A column short of room shrinks the page below, never the bar and its back target.
    Layout.minimumHeight: implicitHeight
    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.subtle }
    RowLayout {
        id: content
        anchors.fill: parent; anchors.leftMargin: bar.showBack ? Theme.gapXs : Theme.mobileGutter; anchors.rightMargin: Theme.gapSm
        spacing: Theme.gapXs
        StudioButton {
            objectName: bar.backObjectName || (bar.objectName || "mobileTopBar")+"Back"
            visible: bar.showBack; touch: true; quiet: true
            iconName: "back"; text: bar.backText; displayText: ""; tip: text
            onClicked: bar.back()
        }
        ColumnLayout {
            Layout.fillWidth: true; Layout.minimumWidth: 0; spacing: Theme.gapXxs
            Layout.topMargin: Theme.gapSm; Layout.bottomMargin: Theme.gapSm
            Text {
                objectName: (bar.objectName || "mobileTopBar")+"Title"
                Layout.fillWidth: true; text: bar.title; color: Theme.text
                font.family: Theme.sans; font.pixelSize: Theme.mobileFontLead; font.weight: Font.DemiBold
                wrapMode: Text.Wrap; lineHeight: 1.2
            }
            Text {
                objectName: (bar.objectName || "mobileTopBar")+"Subtitle"
                Layout.fillWidth: true; visible: bar.subtitle.length>0; text: bar.subtitle; color: Theme.muted
                font.family: bar.monoSubtitle ? Theme.mono : Theme.sans; font.pixelSize: Theme.mobileFontMeta
                // Paths break anywhere rather than being cut.
                wrapMode: bar.monoSubtitle ? Text.WrapAnywhere : Text.Wrap; lineHeight: 1.3
            }
        }
        RowLayout { id: trailing; spacing: Theme.gapXs }
    }
    Accessible.role: Accessible.Pane
    Accessible.name: title
}
