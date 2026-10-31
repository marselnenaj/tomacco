import QtQuick
import "."

// The product's mark. It is the SVG named by Theme.logo once there is one, and
// until then the block of the wordmark, in the accent colour.
Item {
    id: mark
    property int size: Theme.gapLg
    implicitWidth: Theme.logo ? size : Math.round(size*.66)
    implicitHeight: size
    Rectangle {
        anchors.fill: parent
        visible: !Theme.logo
        color: Theme.accent
    }
    Image {
        anchors.fill: parent
        visible: !!Theme.logo
        source: Theme.logo
        sourceSize: Qt.size(mark.size*2,mark.size*2)
        fillMode: Image.PreserveAspectFit
    }
}
