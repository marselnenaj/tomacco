import QtQuick
import QtQuick.Controls
import "."

// The one button: outlined by default, quiet for secondary actions, accent for
// the single action a view is about. Set displayText to "" with an iconName for
// an icon button that keeps its text for accessibility and tests.
Button {
    id: control
    property bool accent: false
    property bool selected: false
    property bool quiet: false
    property bool danger: false
    // Solid on a block of colour, as on the site: ink with paper text unless told otherwise.
    property bool inverted: false
    property color invertedFill: Theme.ink
    property color invertedText: Theme.paper
    // Navigation: the selected entry's icon sits in a filled accent block.
    property bool marker: false
    property string tip: ""
    property string iconName: ""
    property string displayText: text
    // A short key shown at the far end, in mono: "Ctrl K", "1".
    property string keyHint: ""
    property bool alignLeft: false
    property int size: Theme.fontBody
    readonly property bool iconOnly: !!iconName && !displayText
    readonly property int iconSpace: Theme.iconSize+(marker?Theme.gapLg:Theme.gapSm)
    readonly property int hintSpace: keyHint ? hint.implicitWidth+Theme.gapMd : 0
    readonly property color foreground: inverted ? invertedText : !enabled ? Theme.faint
        : accent ? Theme.accentText
        : danger ? Theme.danger
        : selected ? Theme.text
        : quiet && !hovered ? Theme.muted : Theme.text
    implicitHeight: Theme.controlHeight
    implicitWidth: iconOnly ? Theme.controlHeight : Math.max(Theme.controlHeight,label.implicitWidth+(iconName?iconSpace:0)+hintSpace+leftPadding+rightPadding)
    hoverEnabled: true
    focusPolicy: Qt.StrongFocus
    leftPadding: iconOnly ? 0 : marker ? Theme.gapSm : Theme.gapMd
    rightPadding: iconOnly ? 0 : Theme.gapMd
    topPadding: 0
    bottomPadding: 0
    contentItem: Item {
        implicitWidth: control.iconOnly ? Theme.iconSize : label.implicitWidth+(control.iconName?control.iconSpace:0)+control.hintSpace
        StudioIcon {
            id: glyph
            visible: !!control.iconName
            name: control.iconName
            x: control.iconOnly ? (parent.width-width)/2 : control.alignLeft ? 0 : Math.max(0,(parent.width-label.implicitWidth-control.iconSpace-control.hintSpace)/2)
            anchors.verticalCenter: parent.verticalCenter
            color: control.marker && control.selected ? Theme.accentText : control.foreground
            z: 1
        }
        Text {
            id: label
            visible: !control.iconOnly
            anchors.fill: parent
            anchors.leftMargin: control.iconName ? glyph.x+control.iconSpace : 0
            anchors.rightMargin: control.hintSpace
            text: control.displayText
            elide: Text.ElideRight
            color: control.foreground
            font.pixelSize: control.size
            font.weight: control.accent || control.inverted ? Font.Bold : Font.Medium
            horizontalAlignment: control.alignLeft || control.iconName ? Text.AlignLeft : Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        Text {
            id: hint
            visible: !!control.keyHint
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: control.keyHint
            color: control.accent ? Theme.accentText : Theme.faint
            font.family: Theme.mono
            font.pixelSize: Theme.fontMicro
        }
    }
    background: Rectangle {
        radius: Theme.radiusSmall
        color: control.inverted ? control.invertedFill : control.accent ? (!control.enabled ? Theme.selected : control.hovered && !control.down ? Theme.accentHover : Theme.accent)
            : control.selected ? (control.marker ? "transparent" : Theme.selected)
            : control.down || control.hovered ? Theme.hover
            : "transparent"
        border.width: 1
        border.color: control.visualFocus ? Theme.accentInk
            : control.inverted || control.accent || control.selected || control.quiet ? "transparent"
            : control.enabled ? Theme.border : Theme.subtle
        Rectangle {
            visible: control.marker && control.selected
            width: Theme.controlHeight; height: parent.height
            color: Theme.accent
        }
    }
    ToolTip.visible: hovered && tip.length > 0
    ToolTip.text: tip
    ToolTip.delay: 550
    Accessible.name: tip.length > 0 ? tip : displayText || text
}
