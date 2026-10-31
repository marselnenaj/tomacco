import QtQuick
import QtQuick.Window
import QtQuick.Layouts
import QtCore
import "."

// A frameless, device-shaped companion window. The live display item is
// reparented into `host`; no second video stream or frame copy is created.
Window {
    id: win
    objectName: "deviceWindow"
    property real deviceWidth: 320
    property real deviceHeight: 640
    property real zoom: 1
    // Largest zoom offered at all. A live picture sets it to its panel's
    // pixels per point, so the device can be shown pixel for pixel.
    property real maxZoom: 1.5
    property bool showBack: false
    property bool showRotate: false
    property bool showHome: true
    property bool showPower: false
    // The device's back: offered where its profile describes one.
    property bool showFlip: false
    property bool flipped: false
    property bool controlsEnabled: true
    property bool forwardHardwareKeys: false
    // Keyboard shortcuts the device's owner offers for these buttons, shown
    // in their tooltips; empty where there is none.
    property string homeShortcut: ""
    property string powerShortcut: ""
    property string backShortcut: ""
    // Solo: this window is the whole app, so closing it ends the app instead
    // of returning the device to a workspace that is not on screen.
    property bool solo: false
    // Qt asks every visible window before the app quits. Set while the app
    // itself is quitting so this window does not veto its own shutdown.
    property bool allowClose: false
    property string caption: ""
    readonly property alias host: deviceHost
    readonly property real pad: 12
    // Largest zoom at which the whole device still fits on the monitor this
    // window is on (not the whole desktop, which spans every monitor).
    readonly property real fitZoom: Math.max(.3,Math.min(maxZoom,(Screen.height-120)/Math.max(1,deviceHeight),(Screen.width-160)/Math.max(1,deviceWidth)))
    onFitZoomChanged: if(visible && zoom>fitZoom)setZoom(fitZoom)
    signal dockRequested()
    signal closeRequested()
    signal homeRequested()
    signal powerRequested()
    signal backRequested()
    signal hardwareKeyRequested(int key, int modifiers)
    signal rotateRequested()
    signal screenshotRequested()
    // The user chose a zoom (Ctrl+wheel, Ctrl+plus/minus/0 or the rail), as
    // applied. The window's own zooms, the opening zoom and the clamp to the
    // monitor it lands on, do not say so: only a chosen zoom is worth keeping.
    signal zoomChosen(real value)
    // Physical pixels per zoom unit on the monitor this window is on.
    readonly property real pixelRatio: Screen.devicePixelRatio
    // In steps, except the largest zoom: it is the exact pixel ratio on a scaled monitor.
    function setZoom(value) {zoom=Math.max(.3,Math.min(Math.min(maxZoom,fitZoom),Math.abs(value-maxZoom)<.001?value:Math.round(value*20)/20))}
    function chooseZoom(value) {const before=zoom;setZoom(value);if(zoom!==before)zoomChosen(zoom)}
    function move() {startSystemMove()}
    title: caption ? "Tomacco – "+caption : qsTr("Tomacco – Device")
    flags: Qt.Window | Qt.FramelessWindowHint
    // Independent of the main window: Qt would otherwise keep this window
    // hidden for as long as its transient parent is not shown.
    transientParent: null
    color: "transparent"
    // The compositor must not stretch a device: one fixed size per zoom step.
    readonly property int wantedWidth: Math.ceil(deviceWidth*zoom)+pad*3+rail.width
    readonly property int wantedHeight: Math.max(Math.ceil(deviceHeight*zoom),rail.height)+pad*2
    width: wantedWidth; height: wantedHeight
    minimumWidth: wantedWidth; maximumWidth: wantedWidth
    minimumHeight: wantedHeight; maximumHeight: wantedHeight
    onWantedWidthChanged: width=wantedWidth
    onWantedHeightChanged: height=wantedHeight
    onVisibleChanged: if(visible && zoom>fitZoom)setZoom(fitZoom)
    // Qt also asks this window while it is hidden, as part of ending the app
    // after the last visible window closed. Only a window on screen answers.
    onClosing: close=>{if(win.allowClose || !win.visible)return;close.accepted=false;if(win.solo)win.closeRequested();else win.dockRequested()}

    // Keys are handled only while this window has focus; an application-wide
    // shortcut would take Escape away from the main window's dialogs. Every
    // other item lives inside this one, so a key the device view does not
    // use itself (it forwards Escape and text to a live device) arrives here.
    Item {
        id: surface
        objectName: "deviceWindowSurface"
        anchors.fill: parent; focus: true
        Keys.onPressed: event=>{
            const control=event.modifiers & Qt.ControlModifier
            if(event.key===Qt.Key_Escape && !win.solo)win.dockRequested()
            else if(control && (event.key===Qt.Key_Plus || event.key===Qt.Key_Equal))win.chooseZoom(win.zoom+.1)
            else if(control && event.key===Qt.Key_Minus)win.chooseZoom(win.zoom-.1)
            else if(control && event.key===Qt.Key_0)win.chooseZoom(Math.min(1,win.fitZoom))
            else if(control && (event.modifiers & Qt.ShiftModifier) && event.key===Qt.Key_S && win.controlsEnabled)win.screenshotRequested()
            else if(win.forwardHardwareKeys) {
                win.hardwareKeyRequested(event.key,event.modifiers);event.accepted=true;return
            } else return
            event.accepted=true
        }
        // Empty margins and the control rail move the window; the device's own
        // bezel forwards its presses through move().
        MouseArea {anchors.fill:parent;acceptedButtons:Qt.LeftButton;onPressed:win.startSystemMove()}
        Item {
            id: deviceHost
            objectName: "deviceWindowHost"
            x: win.pad; y: Math.round((win.wantedHeight-height)/2)
            width: Math.ceil(win.deviceWidth*win.zoom); height: Math.ceil(win.deviceHeight*win.zoom)
            WheelHandler {
                acceptedModifiers: Qt.ControlModifier
                onWheel: event=>win.chooseZoom(win.zoom+(event.angleDelta.y>0?.05:-.05))
            }
        }
        Rectangle {
            id: rail
            objectName: "deviceWindowRail"
            x: deviceHost.x+deviceHost.width+win.pad; y: Math.round((win.wantedHeight-height)/2)
            width: Theme.controlHeight+Theme.gapSm*2; height: buttons.implicitHeight+Theme.gapMd
            radius: Theme.radiusLarge; color: Theme.surface; border.color: Theme.border
            MouseArea {anchors.fill:parent;onPressed:win.startSystemMove()}
            ColumnLayout {
                id: buttons
                anchors.centerIn: parent; spacing: 2
                StudioButton {objectName:"deviceWindowDock";iconName:"close";quiet:true;tip:win.solo?qsTr("Close device window · the device keeps running"):qsTr("Bring back into the workspace");onClicked:{if(win.solo)win.closeRequested();else win.dockRequested()}}
                StudioButton {objectName:"deviceWindowWorkspace";iconName:"project";quiet:true;tip:qsTr("Open in the workspace");visible:win.solo;onClicked:win.dockRequested()}
                Rectangle {Layout.alignment:Qt.AlignHCenter;width:Theme.gapXl;height:1;color:Theme.subtle}
                StudioButton {objectName:"deviceWindowHome";displayText:"";text:"Home";iconName:"home";quiet:true;tip:"Home"+(win.homeShortcut?" · "+win.homeShortcut:"");visible:win.showHome;enabled:win.controlsEnabled;onClicked:win.homeRequested()}
                StudioButton {objectName:"deviceWindowPower";displayText:"";text:qsTr("Side button");iconName:"power";quiet:true;tip:qsTr("Side button · wake or lock the display")+(win.powerShortcut?" · "+win.powerShortcut:"");visible:win.showPower;enabled:win.controlsEnabled;onClicked:win.powerRequested()}
                StudioButton {objectName:"deviceWindowBack";iconName:"back";quiet:true;tip:qsTr("Back")+(win.backShortcut?" · "+win.backShortcut:"");visible:win.showBack;enabled:win.controlsEnabled;onClicked:win.backRequested()}
                StudioButton {iconName:"rotate";quiet:true;tip:qsTr("Rotate");visible:win.showRotate;enabled:win.controlsEnabled;onClicked:win.rotateRequested()}
                StudioButton {objectName:"deviceWindowFlip";iconName:"flip";quiet:true;selected:win.flipped;tip:win.flipped?qsTr("Show the front"):qsTr("Show the back");visible:win.showFlip;onClicked:win.flipped=!win.flipped}
                StudioButton {iconName:"camera";quiet:true;objectName:"deviceWindowScreenshot";tip:qsTr("Save and copy a screenshot · Ctrl+Shift+S");enabled:win.controlsEnabled;onClicked:win.screenshotRequested()}
                Rectangle {Layout.alignment:Qt.AlignHCenter;width:Theme.gapXl;height:1;color:Theme.subtle}
                StudioButton {objectName:"deviceWindowZoomIn";iconName:"plus";quiet:true;tip:qsTr("Larger · Ctrl+mouse wheel");enabled:win.zoom<Math.min(win.maxZoom,win.fitZoom);onClicked:win.chooseZoom(win.zoom+.1)}
                Text {Layout.alignment:Qt.AlignHCenter;text:Math.round(win.zoom*100)+"%";color:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontMicro}
                StudioButton {objectName:"deviceWindowZoomOut";displayText:"";text:"−";iconName:"minus";quiet:true;tip:qsTr("Smaller · Ctrl+mouse wheel");enabled:win.zoom>.3;onClicked:win.chooseZoom(win.zoom-.1)}
            }
        }
    }
}
