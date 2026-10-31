import QtQuick
import QtQuick.Effects
import QtQuick.Shapes
import QtWebEngine
import "NativeProfiles.js" as NativeProfiles

Item {
    id: device
    required property var model
    property bool landscape: false
    property bool unfolded: false
    property color finish: model.colors[0]
    property bool guides: false
    property bool safeLayout: true
    property bool muted: false
    property bool dimmed: false
    property bool showChrome: true
    property string chromeTheme: "auto"
    property string detectedChromeTheme: "light"
    readonly property string effectiveChromeTheme: chromeTheme === "auto" ? detectedChromeTheme : chromeTheme
    readonly property color chromeColor: effectiveChromeTheme === "dark" ? "#101116" : "#ffffff"
    function refreshChromeTheme() {
        if(chromeTheme!=="auto" || web.loading) return
        // Read presentation metadata only; pages receive no native command bridge.
        web.runJavaScript("(()=>{const meta=Array.from(document.querySelectorAll('meta[name=\"theme-color\"]')).find(m=>!m.media||matchMedia(m.media).matches);let c=meta?meta.content:getComputedStyle(document.body).backgroundColor;if(/^#[0-9a-f]{6}$/i.test(c)){return [parseInt(c.slice(1,3),16),parseInt(c.slice(3,5),16),parseInt(c.slice(5,7),16)]}let v=c.match(/^rgba?\\(([^)]+)\\)/);if(v){let n=v[1].split(',').map(Number);if(n.length===3||n[3]>.5)return n.slice(0,3)}return null})()",function(rgb) {
            if(rgb && rgb.length===3) device.detectedChromeTheme=(rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722)>150?"dark":"light"
        })
    }
    Timer { interval: 700; repeat: true; running: device.visible && device.chromeTheme==="auto" && device.showChrome; onTriggered: device.refreshChromeTheme() }
    readonly property string statusStyle: isDuo && barEdge !== "bottom" ? "compact-ring" : "horizontal"
    // Rasterize Chromium and the rounded display at the displayed pixel density.
    // Inverse item scaling keeps CSS layout and pointer coordinates unchanged.
    // The ratio is 0 until the window is on a screen.
    readonly property real screenRatio: Screen.devicePixelRatio > 0 ? Screen.devicePixelRatio : 1
    readonly property real renderScale: Math.max(1, (model.dpr || 3) / screenRatio)
    readonly property real textureScale: renderScale * screenRatio
    property alias browser: web
    property alias displayItem: screen
    property var consoleMessages: []
    // When the current page load started (ms since the epoch).
    property real loadStarted: 0
    readonly property int bezel: model.bezel === undefined ? 9 : model.bezel
    readonly property var profileInsets: (unfolded ? (landscape ? model.openSafeLandscape : model.openSafePortrait) : (landscape ? model.safeLandscape : model.safePortrait)) || {top:0,right:0,bottom:0,left:0}
    property int baseWidth: unfolded && model.openWidth ? model.openWidth : model.width
    property int baseHeight: unfolded && model.openHeight ? model.openHeight : model.height
    readonly property bool isDuo: model.id === "duo"
    readonly property string cutout: unfolded && model.openCutout ? model.openCutout : model.cutout
    readonly property bool islandVisible: cutout === "island" || cutout === "compact"
    readonly property bool notchVisible: cutout === "notch"
    readonly property bool punchVisible: cutout === "punch-right" || cutout === "punch"
    property int screenWidth: landscape ? Math.max(baseWidth,baseHeight) : Math.min(baseWidth,baseHeight)
    property int screenHeight: landscape ? Math.min(baseWidth,baseHeight) : Math.max(baseWidth,baseHeight)
    readonly property string barEdge: isDuo ? (unfolded && !landscape ? "bottom" : !unfolded && landscape ? "left" : "right") : "bottom"
    property int safeTop: profileInsets.top
    property int safeBottom: profileInsets.bottom
    property int safeLeft: profileInsets.left
    property int safeRight: profileInsets.right
    // Duo backgrounds fill the display. Apps place controls using the preview
    // metadata below, rather than receiving an artificially letterboxed page.
    readonly property bool cropViewport: false
    readonly property int viewportWidth: screenWidth - (cropViewport ? safeLeft + safeRight : 0)
    readonly property int viewportHeight: screenHeight - (cropViewport ? safeTop + safeBottom : 0)
    readonly property string viewportJson: JSON.stringify({
        version: 1, model: model.id, display: isDuo ? (unfolded ? "inner" : "outer") : "single",
        orientation: landscape ? "landscape" : "portrait",
        width: viewportWidth, height: viewportHeight,
        barEdge: isDuo ? barEdge : "automatic",
        controlsTop: isDuo && barEdge !== "bottom" ? (unfolded ? 86 : 126) : 0,
        safeArea: safeLayout ? {top:safeTop,right:safeRight,bottom:safeBottom,left:safeLeft} : {top:0,right:0,bottom:0,left:0}
    })
    readonly property string viewportScript: "(() => { const value = " + viewportJson + "; Object.freeze(value.safeArea); Object.freeze(value); Object.defineProperty(window, '__tomaccoViewport', {value, configurable:true}); window.dispatchEvent(new CustomEvent('tomacco:viewport', {detail:value})); })();"
    onViewportScriptChanged: metadataTimer.restart()
    Timer { id: metadataTimer; interval: 0; onTriggered: web.runJavaScript(device.viewportScript) }
    property webEngineScript viewportMetadata: ({
        name: "Tomacco viewport",
        injectionPoint: WebEngineScript.DocumentCreation,
        worldId: WebEngineScript.MainWorld,
        runsOnSubFrames: false,
        sourceCode: "window.addEventListener('flutter-first-frame',()=>{window.__tomaccoFirstFrame=true},{once:true});" + device.viewportScript
    })
    // Records the load's uncaught errors and failed requests for preview.errors.
    property webEngineScript pageErrors: ({
        name: "Tomacco page errors",
        injectionPoint: WebEngineScript.DocumentCreation,
        worldId: WebEngineScript.MainWorld,
        runsOnSubFrames: false,
        sourceUrl: "qrc:/assets/page-errors.js"
    })
    // Corners per state (the folded Duo's hinge side is tight); turned to
    // landscape the top right corner becomes the top left one.
    readonly property var cornerRadii: {
        const radii=unfolded ? model.openCornerRadii : model.cornerRadii
        const one=(unfolded && model.openRadius!==undefined ? model.openRadius : model.radius) || 0
        const shown=radii || [one,one,one,one]
        return landscape ? [shown[1],shown[2],shown[3],shown[0]] : shown
    }
    readonly property real topLeftCorner: cornerRadii[0]
    readonly property real topRightCorner: cornerRadii[1]
    readonly property real bottomRightCorner: cornerRadii[2]
    readonly property real bottomLeftCorner: cornerRadii[3]
    // Apple's corners are continuous curves, not arcs (see NativeProfiles).
    readonly property bool continuous: model.platform === "ios"
    // The opening: the mask the page is cut to.
    readonly property string screenPath: NativeProfiles.outline(0,0,screenWidth,screenHeight,cornerRadii,continuous)
    function bodyLine(inset) {return NativeProfiles.outline(inset,inset,width-inset*2,height-inset*2,cornerRadii.map(r=>Math.max(0,r+bezel-inset)),continuous)}
    // The metal band as wide as the model's, where its drawing gives it:
    // the housing's edge to the active area, less the glass border.
    readonly property real band: {
        const body=model.body,panel=NativeProfiles.dimensions(model,unfolded)
        if(!body || body.border===undefined || !model.ppi || unfolded)return 3
        const mm=Math.min(panel[0],panel[1])/model.ppi*25.4
        return Math.max(2,((body.width-mm)/2-body.border)*Math.min(baseWidth,baseHeight)/mm)
    }
    // The camera cutout in the display's upright coordinates (points).
    readonly property real naturalWidth: Math.min(baseWidth,baseHeight)
    readonly property real cutoutWidth: (unfolded ? model.openCutoutWidth : model.cutoutWidth) || (punchVisible ? 13 : 119)
    readonly property real cutoutHeight: (unfolded ? model.openCutoutHeight : model.cutoutHeight) || (punchVisible ? 13 : 35)
    readonly property real cutoutY: { const value=unfolded ? model.openCutoutOffset : model.cutoutOffset; return value===undefined ? 11 : value }
    readonly property real cutoutX: { const value=unfolded ? model.openCutoutX : model.cutoutX; return value!==undefined ? value : cutout==="punch-right" ? naturalWidth-cutoutWidth-16 : (naturalWidth-cutoutWidth)/2 }
    property string loadError: ""
    signal statusMessage(string text)
    width: screenWidth + bezel*2
    height: screenHeight + bezel*2
    WebEngineProfile {
        id: previewProfile
        storageName: "Tomacco"
        offTheRecord: false
        onDownloadRequested: function(download) {
            download.cancel()
            device.statusMessage(qsTr("Please open downloads in your browser. Device images can be saved with “Screenshot”."))
        }
    }

    Repeater {
        model: 5
        Rectangle {
            required property int index
            x: -6-index*5; y: 7-index*3
            width: device.width+12+index*10; height: device.height+12+index*8
            radius: Math.max(device.topLeftCorner,device.topRightCorner) + 16 + index*3
            color: "#000000"; opacity: 0.06
        }
    }
    Shape {
        objectName: "deviceBody"
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
            strokeWidth: 0
            fillGradient: LinearGradient {
                x1: 0; y1: 0; x2: device.width; y2: device.height
                GradientStop { position: 0; color: Qt.lighter(device.finish,1.3) }
                GradientStop { position: 0.28; color: device.finish }
                GradientStop { position: 0.55; color: Qt.darker(device.finish,1.12) }
                GradientStop { position: 0.8; color: device.finish }
                GradientStop { position: 1; color: Qt.lighter(device.finish,1.2) }
            }
            PathSvg { path: device.bodyLine(0) }
        }
        ShapePath {
            strokeWidth: 0; fillColor: "#050609"
            PathSvg { path: device.bodyLine(device.band) }
        }
        ShapePath {
            strokeWidth: device.band*.25; strokeColor: Qt.darker(device.finish,1.55); fillColor: "transparent"
            PathSvg { path: device.bodyLine(device.band*.875) }
        }
    }
    // Buttons where the profile places them (fractions of the body's edge);
    // a profile without them keeps the generic set.
    Repeater {
        model: device.landscape ? [] : (device.unfolded && device.model.openButtons ? device.model.openButtons : device.model.buttons) ||
               [{edge:"left",from:105/device.height,length:28/device.height},{edge:"left",from:157/device.height,length:48/device.height},{edge:"left",from:218/device.height,length:48/device.height},{edge:"right",from:192/device.height,length:73/device.height}]
        Rectangle {
            required property var modelData
            readonly property bool across: modelData.edge === "top" || modelData.edge === "bottom"
            readonly property bool flush: modelData.kind === "cameraControl"
            x: across ? modelData.from*device.width : modelData.edge === "left" ? (flush ? -1 : -3) : device.width-(flush ? 2 : 1)
            y: across ? (modelData.edge === "top" ? (flush ? -1 : -3) : device.height-1) : modelData.from*device.height
            width: across ? modelData.length*device.width : (flush ? 3 : 4); height: across ? (flush ? 3 : 4) : modelData.length*device.height
            radius: 2; color: flush ? Qt.darker(device.finish,1.6) : device.finish
        }
    }
    Rectangle {
        visible: device.notchVisible
        x: device.landscape ? device.bezel-8 : (device.width-60)/2
        y: device.landscape ? (device.height-60)/2 : device.bezel-8
        width: device.landscape ? 3 : 60; height: device.landscape ? 60 : 3
        radius: 1.5; color: "#111214"; border.width: .5; border.color: "#191a1d"
    }
    // The folded Duo's hinge is along the straight edge, not through its display.
    Rectangle {
        visible: device.isDuo && !device.unfolded
        x: device.landscape ? 12 : -4
        y: device.landscape ? device.height-2 : 12
        width: device.landscape ? device.width-24 : 7
        height: device.landscape ? 7 : device.height-24
        radius: 3; color: Qt.darker(device.finish,1.3); border.color: Qt.lighter(device.finish,1.6)
    }
    Repeater {
        model: device.isDuo && device.unfolded ? 2 : 0
        Rectangle {
            required property int index
            x: device.landscape ? (device.width-3)/2 : (index===0?0:device.width-4)
            y: device.landscape ? (index===0?0:device.height-4) : (device.height-3)/2
            width: device.landscape ? 3 : 4; height: device.landscape ? 4 : 3
            color: Qt.darker(device.finish,1.9)
        }
    }

    Shape {
        id: mask
        width: screen.width; height: screen.height
        visible: false; layer.enabled: true
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
            strokeWidth: 0; fillColor: "white"
            PathSvg { path: device.screenPath }
        }
        layer.textureSize: Qt.size(Math.ceil(width * device.textureScale), Math.ceil(height * device.textureScale))
        layer.smooth: true
    }
    Item {
        id: screen
        objectName: "deviceScreen"
        x: device.bezel; y: device.bezel; width: device.screenWidth; height: device.screenHeight
        layer.enabled: true
        layer.textureSize: Qt.size(Math.ceil(width * device.textureScale), Math.ceil(height * device.textureScale))
        layer.smooth: true
        layer.effect: MultiEffect { maskEnabled: true; maskSource: mask; maskThresholdMin: .5; maskSpreadAtMin: 1 }
        Rectangle { anchors.fill: parent; color: "#101017" }
        WebEngineView {
            id: web
            objectName: "previewBrowser"
            profile: previewProfile
            userScripts.collection: [viewportMetadata, pageErrors]
            x: device.cropViewport ? device.safeLeft : 0
            y: device.cropViewport ? device.safeTop : 0
            width: device.viewportWidth * device.renderScale
            height: device.viewportHeight * device.renderScale
            zoomFactor: device.renderScale
            scale: 1 / device.renderScale
            transformOrigin: Item.TopLeft
            url: Studio.demoUrl
            backgroundColor: "#101017"
            audioMuted: device.muted
            settings.localContentCanAccessFileUrls: false
            settings.localContentCanAccessRemoteUrls: false
            settings.javascriptCanOpenWindows: true
            settings.fullScreenSupportEnabled: true
            settings.playbackRequiresUserGesture: true
            onNavigationRequested: function(request) {
                if(!Studio.allowedUrl(request.url)) {
                    request.action=WebEngineView.IgnoreRequest
                    device.statusMessage(qsTr("This preview opens web addresses. Use “Open folder” for local builds."))
                }
            }
            onLoadingChanged: function(request) {
                if(request.status===WebEngineView.LoadStartedStatus) { device.loadError=""; device.consoleMessages=[]; device.loadStarted=Date.now() }
                if(request.status===WebEngineView.LoadSucceededStatus) { web.runJavaScript(device.viewportScript); device.refreshChromeTheme() }
                if(request.status===WebEngineView.LoadFailedStatus)
                    device.loadError=qsTr("Page not reachable. Is your dev server running?")
            }
            onJavaScriptConsoleMessage: function(level, message, lineNumber, sourceID) {
                device.consoleMessages=device.consoleMessages.slice(-199).concat([{level:level,message:message,line:lineNumber,source:sourceID,time:Date.now()}])
            }
            onNewWindowRequested: function(request) {
                if(request.userInitiated && Studio.allowedUrl(request.requestedUrl)) request.openIn(web)
            }
            onPermissionRequested: function(permission) {
                permission.deny()
                device.statusMessage(qsTr("Device permissions are disabled in this layout preview."))
            }
            onFullScreenRequested: function(request) { request.reject() }
            onRenderProcessTerminated: function(status, code) {
                device.loadError=qsTr("The web view was terminated. Please reload.")
            }
        }
        Text {
            visible: device.showChrome && !device.landscape && !device.isDuo
            x: 31; y: device.model.platform==="android" ? 9 : device.cutout === "notch" ? 14 : 20
            text: "9:41"; color: device.chromeColor; font.pixelSize: device.model.platform==="android"?12:15; font.weight: Font.DemiBold
        }
        SystemStatus {
            objectName: "horizontalSystemStatus"
            android: device.model.platform==="android"
            visible: device.showChrome && !device.landscape && !device.isDuo
            anchors.right: parent.right; anchors.rightMargin: 27
            y: device.model.platform==="android"?10:device.cutout==="notch"?16:22
            foreground: device.chromeColor
        }
        Rectangle {
            id: island
            objectName: "cameraIsland"
            visible: device.islandVisible
            width: device.landscape ? device.cutoutHeight : device.cutoutWidth
            height: device.landscape ? device.cutoutWidth : device.cutoutHeight
            x: device.landscape ? device.cutoutY : device.cutoutX
            y: device.landscape ? device.naturalWidth-device.cutoutX-device.cutoutWidth : device.cutoutY
            radius: Math.min(width,height)/2
            color: "#000000"
            Rectangle { width: 9; height: 9; radius: 5; color: "#0c1220"; border.color: "#101b2c"; anchors.right: parent.right; anchors.rightMargin: 11; anchors.verticalCenter: parent.verticalCenter; visible: !device.landscape }
        }
        Item {
            id: notch
            objectName: "cameraNotch"
            visible: device.notchVisible
            width: device.model.cutoutWidth || 164
            height: device.model.cutoutHeight || 34
            x: device.landscape ? (height-width)/2 : (parent.width-width)/2
            y: device.landscape ? (parent.height-height)/2 : 0
            rotation: device.landscape ? -90 : 0
            // Edge-connected TrueDepth housing: concave shoulders, straight
            // neck and rounded lower corners. The camera is on its left.
            Shape {
                anchors.fill: parent
                preferredRendererType: Shape.CurveRenderer
                ShapePath {
                    strokeWidth: 0; strokeColor: "transparent"; fillColor: "#000000"
                    startX: 0; startY: -1
                    PathLine { x: notch.width; y: -1 }
                    PathCubic { x: notch.width-5; y: 5; control1X: notch.width-4; control1Y: 0; control2X: notch.width-5; control2Y: 1 }
                    PathLine { x: notch.width-5; y: notch.height-20 }
                    PathCubic { x: notch.width-25; y: notch.height; control1X: notch.width-5; control1Y: notch.height-6; control2X: notch.width-11; control2Y: notch.height }
                    PathLine { x: 25; y: notch.height }
                    PathCubic { x: 5; y: notch.height-20; control1X: 11; control1Y: notch.height; control2X: 5; control2Y: notch.height-6 }
                    PathLine { x: 5; y: 5 }
                    PathCubic { x: 0; y: -1; control1X: 5; control1Y: 1; control2X: 4; control2Y: 0 }
                }
            }
            Rectangle { x: 23; y: 9; width: 9; height: 9; radius: 5; color: "#080e16"; border.color: "#101a26"
                Rectangle { anchors.centerIn: parent; width: 3; height: 3; radius: 2; color: "#152d49" }
            }
        }
        Rectangle {
            objectName: "duoOuterCamera"
            visible: device.punchVisible
            x: device.landscape ? device.cutoutY : device.cutoutX
            y: device.landscape ? device.naturalWidth-device.cutoutX-device.cutoutWidth : device.cutoutY
            width: device.cutoutWidth; height: width; radius: width/2; color: "#020205"
            Rectangle { anchors.centerIn: parent; width: 11; height: 11; radius: 6; color: "#070d18"; border.color: "#101b31"
                Rectangle { x: 3; y: 2; width: 3; height: 3; radius: 2; color: "#1c2c46"; opacity: 0.7 }
            }
        }
        Column {
            objectName: "duoCompactStatus"
            visible: device.showChrome && device.isDuo && device.barEdge !== "bottom"
            // Centred in the status column; folded, below the island that
            // runs down from the camera (37 x 137 points).
            x: device.barEdge === "left" ? (device.safeLeft-width)/2 : parent.width-device.safeRight+(device.safeRight-width)/2
            y: device.unfolded ? 24 : device.cutoutY+145; width: 38; spacing: 7
            Text { width: parent.width; horizontalAlignment: Text.AlignHCenter; text: "9:41"; color: device.chromeColor; font.pixelSize: 13; font.weight: Font.Medium }
            SystemStatus { anchors.horizontalCenter: parent.horizontalCenter; compact: true; foreground: device.chromeColor }
        }
        Text { visible: device.showChrome && device.isDuo && device.barEdge === "bottom"; x: 30; y: 7; text: "9:41"; color: device.chromeColor; font.pixelSize: 12; font.weight: Font.Medium }
        SystemStatus { objectName: "duoHorizontalStatus"; visible: device.showChrome && device.isDuo && device.barEdge === "bottom"; anchors.right: parent.right; anchors.rightMargin: 28; y: 8; foreground: device.chromeColor }
        Rectangle { visible: device.showChrome && (!device.isDuo || device.barEdge === "bottom"); anchors.horizontalCenter: parent.horizontalCenter; y: parent.height-12; width: Math.min(140,parent.width*0.35); height: 5; radius: 3; color: device.chromeColor }
        Item {
            visible: device.guides; anchors.fill: parent
            Rectangle { x: device.safeLeft; y: device.safeTop; width: parent.width-device.safeLeft-device.safeRight; height: parent.height-device.safeTop-device.safeBottom; color: "#123baaff"; border.width: 1; border.color: "#8bbdff" }
            Text { x: device.safeLeft+9; y: device.safeTop+8; text: qsTr("SAFE AREA · PREVIEW"); color: "#afd1ff"; font.pixelSize: 10; font.letterSpacing: 1 }
        }
        Rectangle {
            anchors.fill: parent; color: "#101017"; visible: device.loadError.length>0
            Column { anchors.centerIn: parent; width: parent.width-70; spacing: 18
                Text { width: parent.width; text: qsTr("No connection"); color: "#f0f0f7"; font.pixelSize: 23; font.bold: true; horizontalAlignment: Text.AlignHCenter }
                Text { width: parent.width; text: device.loadError; wrapMode: Text.WordWrap; color: "#9b9eae"; font.pixelSize: 15; horizontalAlignment: Text.AlignHCenter }
                StudioButton { anchors.horizontalCenter: parent.horizontalCenter; text: qsTr("Reload"); accent: true; onClicked: web.reload() }
            }
        }
        Rectangle { anchors.fill: parent; color: "black"; opacity: device.dimmed ? 0.9 : 0; Behavior on opacity { NumberAnimation { duration: 180 } } visible: opacity>0; MouseArea { anchors.fill: parent; onClicked: device.dimmed=false } }
    }
}
