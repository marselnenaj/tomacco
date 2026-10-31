import QtQuick
import QtQuick.Controls
import "."

ComboBox {
    id: control
    implicitHeight: Theme.controlHeight
    implicitWidth: 180
    font.pixelSize: Theme.fontBody
    leftPadding: Theme.gapMd
    rightPadding: Theme.gapXxl
    topPadding: 0
    bottomPadding: 0
    hoverEnabled: true
    contentItem: Text {
        text: control.displayText
        color: control.enabled ? Theme.text : Theme.faint
        font: control.font
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    indicator: StudioIcon {
        name: "chevron"
        width: Theme.gapMd; height: Theme.gapMd
        x: control.width-width-Theme.gapMd
        y: (control.height-height)/2
        color: control.enabled ? Theme.muted : Theme.faint
    }
    background: Rectangle {
        radius: Theme.radiusSmall
        color: control.hovered && control.enabled ? Theme.hover : "transparent"
        border.color: control.visualFocus ? Theme.accentInk : control.enabled ? Theme.border : Theme.subtle
        border.width: control.visualFocus ? 2 : 1
    }
    delegate: ItemDelegate {
        required property int index
        width: control.popup.width-control.popup.leftPadding-control.popup.rightPadding
        height: Theme.controlHeight
        padding: 0
        leftPadding: Theme.gapSm
        rightPadding: Theme.gapSm
        text: control.textAt(index)
        font: control.font
        highlighted: control.highlightedIndex===index
        contentItem: Text {
            text: parent.text
            font: control.font
            color: Theme.text
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle { radius: Theme.radiusSmall; color: parent.highlighted || parent.hovered ? Theme.hover : "transparent" }
    }
    popup: Popup {
        y: control.height+Theme.gapXs
        width: control.width
        padding: Theme.gapXs
        implicitHeight: Math.min(contentItem.implicitHeight+Theme.gapSm,320)
        contentItem: ListView {
            clip: true
            implicitHeight: contentHeight
            model: control.popup.visible ? control.delegateModel : null
            currentIndex: control.highlightedIndex
            ScrollIndicator.vertical: ScrollIndicator {}
        }
        background: Rectangle { color: Theme.elevated; radius: Theme.radius; border.color: Theme.border }
    }
}
