import QtQuick
import QtQuick.Layouts
import "."

// What an empty view shows: a plain sentence, what it needs, one obvious action.
ColumnLayout {
    id: emptyState
    property string title: ""
    property string body: ""
    property string actionName: ""
    property string actionText: ""
    property string actionIcon: ""
    property string actionKey: ""
    property string secondaryText: ""
    property string hint: ""
    property string errorText: ""
    property int topPadding: 0
    // On a block of colour: the type's colour, and a solid button.
    property color ink: Theme.text
    property bool onBlock: false
    property bool showMark: true
    signal action()
    signal secondary()
    spacing: Theme.gapMd
    Item {visible:emptyState.topPadding>0;Layout.preferredHeight:emptyState.topPadding}
    BrandMark {visible:!!emptyState.title && emptyState.showMark;size:Theme.gapXxxl}
    StudioHeading {
        Layout.fillWidth: true
        visible: !!emptyState.title
        text: emptyState.title
        color: emptyState.ink
        size: emptyState.width>=620 ? Theme.fontGiant : emptyState.width>=380 ? Theme.fontHero : Theme.fontDisplay
    }
    Text {
        Layout.fillWidth: true
        visible: !!emptyState.body
        text: emptyState.body
        color: emptyState.onBlock ? emptyState.ink : Theme.muted
        font.pixelSize: Theme.fontLarge
        font.weight: Font.Medium
        lineHeight: 1.15
        wrapMode: Text.WordWrap
    }
    Flow {
        Layout.fillWidth: true
        Layout.topMargin: Theme.gapSm
        spacing: Theme.gapSm
        visible: !!emptyState.actionText || !!emptyState.secondaryText
        StudioButton {
            objectName: emptyState.actionName
            visible: !!emptyState.actionText
            text: emptyState.actionText
            iconName: emptyState.actionIcon
            keyHint: emptyState.actionKey
            accent: !emptyState.onBlock
            inverted: emptyState.onBlock
            onClicked: emptyState.action()
        }
        StudioButton {
            visible: !!emptyState.secondaryText
            text: emptyState.secondaryText
            onClicked: emptyState.secondary()
        }
    }
    Text {
        Layout.fillWidth: true
        Layout.topMargin: Theme.gapXs
        visible: !!emptyState.hint
        text: emptyState.hint
        color: Theme.faint
        font.family: Theme.mono
        font.pixelSize: Theme.fontSmall
        wrapMode: Text.WordWrap
    }
    Text {
        Layout.fillWidth: true
        visible: !!emptyState.errorText
        text: emptyState.errorText
        color: Theme.danger
        font.pixelSize: Theme.fontSmall
        wrapMode: Text.Wrap
    }
}
