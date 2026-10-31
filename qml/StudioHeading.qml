import QtQuick
import "."

// The display type: Archivo at its heaviest, slightly wide, set tight. The
// tracking follows the size, as it does on the product's site.
Text {
    property int size: Theme.fontDisplay
    property bool wide: false
    color: Theme.text
    font.family: Theme.sans
    font.pixelSize: size
    font.weight: Font.Black
    font.letterSpacing: -size*.038
    font.variableAxes: ({"wdth": wide ? 125 : 108})
    lineHeight: .96
    wrapMode: Text.WordWrap
}
