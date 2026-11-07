import QtQuick
import QtQuick.Controls
import "."

Dialog {
    id: control
    // A phone sheet: the dialog fills the area it is given (its owner sets x, y,
    // width and height), with the 16 px phone gutter and a smaller title.
    property bool sheet: false
    padding: sheet ? Theme.mobileGutter : Theme.gapXl
    topPadding: sheet ? Theme.gapMd : Theme.gapLg
    font.pixelSize: Theme.fontBody
    background: Rectangle {
        color: Theme.surface; border.color: Theme.border
        Rectangle { width: parent.width; height: 3; color: Theme.accent }
    }
    // The header's implicit width is the title's own width, measured apart
    // from the wrapped label: that label's layout follows the dialog's width,
    // which follows this implicit width, so a new title (another language)
    // would otherwise loop through it.
    header: Item {
        implicitWidth: Math.ceil(titleMetrics.advanceWidth) + titleLabel.leftPadding + titleLabel.rightPadding
        implicitHeight: titleLabel.implicitHeight
        visible: control.title.length>0
        TextMetrics { id: titleMetrics; text: control.title; font: titleLabel.font }
        Label {
            id: titleLabel
            width: parent.width
            text: control.title
            leftPadding: control.sheet ? Theme.mobileGutter : Theme.gapXl
            rightPadding: control.sheet ? Theme.mobileGutter : Theme.gapXl
            topPadding: control.sheet ? Theme.gapLg : Theme.gapXl
            color: Theme.text
            font.pixelSize: control.sheet ? Theme.mobileFontTitle : Theme.fontDisplay
            font.weight: control.sheet ? Font.DemiBold : Font.Black
            font.letterSpacing: control.sheet ? 0 : -Theme.fontDisplay*.038
            wrapMode: Text.WordWrap
        }
    }
    footer: DialogButtonBox {
        visible: count>0
        implicitHeight: count>0 ? (control.sheet ? Math.max(Theme.controlHeight,Theme.mobileTouch) : Theme.controlHeight)+Theme.gapXl+Theme.gapLg : 0
        leftPadding: control.sheet ? Theme.mobileGutter : Theme.gapXl
        rightPadding: control.sheet ? Theme.mobileGutter : Theme.gapXl
        topPadding: Theme.gapSm
        bottomPadding: Theme.gapLg
        spacing: Theme.gapSm
        alignment: Qt.AlignRight
        delegate: StudioButton {
            objectName: control.objectName+"-button-"+DialogButtonBox.buttonRole
            // Qt's own button texts are English unless its translations are installed.
            displayText: ({"Close":qsTr("Close"),"Cancel":qsTr("Cancel"),"Yes":qsTr("Yes"),"No":qsTr("No"),"Discard":qsTr("Discard"),"Save":qsTr("Save"),"Open":qsTr("Open"),"Apply":qsTr("Apply"),"Reset":qsTr("Reset")})[text] || text
            accent: DialogButtonBox.buttonRole===DialogButtonBox.AcceptRole || DialogButtonBox.buttonRole===DialogButtonBox.YesRole
            touch: control.sheet
        }
        background: Item {}
    }
    Overlay.modal: Rectangle { color: Theme.scrim }
}
