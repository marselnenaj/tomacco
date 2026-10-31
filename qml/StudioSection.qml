import QtQuick
import "."

// A small lowercase mono heading above a group, the way a terminal labels things.
Text {
    color: Theme.faint
    font.family: Theme.mono
    font.pixelSize: Theme.fontMicro
    font.capitalization: Font.AllLowercase
    elide: Text.ElideRight
}
