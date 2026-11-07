import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
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
    // A phone target: at least 48 logical px high (and wide for an icon), scaled
    // with the interface size like every other length, and never squeezed below
    // that by a layout. Only phone-wide layouts set it.
    property bool touch: false
    readonly property int touchSize: touch ? Math.max(Theme.controlHeight,Theme.mobileTouch) : Theme.controlHeight
    readonly property bool iconOnly: !!iconName && !displayText
    readonly property int iconSpace: Theme.iconSize+(marker?Theme.gapLg:Theme.gapSm)
    readonly property int hintSpace: keyHint ? hint.implicitWidth+Theme.gapMd : 0
    readonly property color foreground: inverted ? invertedText : !enabled ? Theme.faint
        : accent ? Theme.accentText
        : danger ? Theme.danger
        : selected ? Theme.text
        : quiet && !hovered ? Theme.muted : Theme.text
    // A phone text button keeps its whole label readable: the label wraps onto
    // more lines instead of being cut off and the button grows to fit. In a Flow
    // (the phone's action rows) it is never wider than the row. In a layout, a
    // button that should keep its own width but may shrink on a narrow screen
    // sets Layout.fillWidth: true; Layout.maximumWidth: naturalWidth, so the
    // layout narrows it instead of being pushed past the screen. Desktop and
    // icon buttons keep their single line and fixed height.
    readonly property bool wraps: touch && !iconOnly
    readonly property real naturalWidth: Math.max(touchSize,label.implicitWidth+(iconName?iconSpace:0)+hintSpace+leftPadding+rightPadding)
    readonly property Flow parentFlow: wraps ? parent as Flow : null
    readonly property real room: parentFlow ? parentFlow.width-parentFlow.leftPadding-parentFlow.rightPadding : Infinity
    implicitHeight: wraps ? Math.max(touchSize,Math.ceil(label.implicitHeight)+Theme.gapSm*2) : touchSize
    implicitWidth: iconOnly ? touchSize : room>0 && room<naturalWidth ? Math.max(touchSize,room) : naturalWidth
    Layout.minimumHeight: touch ? touchSize : 0
    Layout.minimumWidth: touch ? touchSize : 0
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
            objectName: "studioButtonIcon"
            visible: !!control.iconName
            name: control.iconName
            // Measured from the start edge, which is the right one when mirrored.
            readonly property real start: control.iconOnly ? (parent.width-width)/2 : control.alignLeft ? 0 : Math.max(0,(parent.width-label.implicitWidth-control.iconSpace-control.hintSpace)/2)
            x: control.mirrored ? parent.width-width-start : start
            anchors.verticalCenter: parent.verticalCenter
            color: control.marker && control.selected ? Theme.accentText : control.foreground
            z: 1
        }
        Text {
            id: label
            objectName: "studioButtonLabel"
            visible: !control.iconOnly
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: control.iconName ? glyph.start+control.iconSpace : 0
            anchors.rightMargin: control.hintSpace
            text: control.displayText
            wrapMode: control.wraps ? Text.Wrap : Text.NoWrap
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
        // A keyboard focus ring wide enough to see on every fill.
        border.width: control.visualFocus ? 2 : 1
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
    // The full label, also for an icon button; the tip, often with its
    // shortcut, is the description. A button with only a tip is named by it.
    Accessible.name: text || tip
    Accessible.description: tip !== Accessible.name ? tip : ""
}
