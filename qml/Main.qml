import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import QtWebEngine
import "."
import "ColorNames.js" as ColorNames

ApplicationWindow {
    id: root
    objectName: "studioWindow"
    width: 1380; height: 970
    minimumWidth: 560; minimumHeight: 480
    // In device-only mode the detached device is the whole app. Closing this
    // window while a device is detached keeps that device window and turns the
    // app into device-only mode; closing the device window then ends the app.
    visible: !initialDeviceOnly
    Connections {target:nativeWorkspace;function onSoloChanged() {root.visible=!nativeWorkspace.solo}}
    title: "Tomacco · Mobile Workspace"
    color: Theme.background
    font.family: Theme.sans
    font.pixelSize: Theme.fontBody
    palette.window: Theme.surface
    palette.base: Theme.inset
    palette.text: Theme.text
    palette.windowText: Theme.text
    palette.placeholderText: Theme.faint
    palette.button: Theme.elevated
    palette.buttonText: Theme.text
    palette.highlight: Theme.accent
    palette.toolTipBase: Theme.elevated
    palette.toolTipText: Theme.text
    palette.highlightedText: Theme.accentText
    property var devices: Studio.models
    property int selectedIndex: 0
    property var currentModel: devices[selectedIndex]
    property string deviceCategory: "ios"
    property var deviceSearches: ({ios:"",android:"",generic:""})
    property var categorySelections: ({})
    readonly property var deviceCategories: [
        {id:"ios",label:"Apple"}, {id:"android",label:"Android"}
    ].concat(devices.some(d=>d.platform==="generic") ? [{id:"generic",label:qsTr("Custom","device category")}] : [])
    readonly property var categoryDevices: devices.filter(d=>(d.platform || "ios")===deviceCategory)
    readonly property var visibleDevices: categoryDevices.filter(d=>(d.name+" "+(d.family || "")).toLowerCase().indexOf((deviceSearches[deviceCategory] || "").toLowerCase())>=0)
    property int finishIndex: 0
    property bool rotated: false
    property bool foldedOpen: false
    property bool safeMode: true
    property bool guidesOn: false
    property bool muted: false
    property real requestedZoom: 0
    property string notice: ""
    property bool nativeMode: initialWorkspace!=="preview"
    property int testStep: 0
    property bool testFailed: false
    property var testResults: []
    readonly property bool compact: width < 1190
    readonly property string finishName: ColorNames.name(currentModel.colorNames[finishIndex])
    readonly property string stateName: (currentModel.openWidth ? (foldedOpen ? qsTr("Open · %1") : qsTr("Folded · %1")) : "%1").arg(rotated ? qsTr("Landscape") : qsTr("Portrait"))
    readonly property bool isDemo: phone.browser.url.toString().indexOf("qrc:/assets/demo.html")===0
    readonly property bool sdkMissing: Studio.sdk.toLowerCase().indexOf("not installed")>=0 || Studio.sdk===qsTranslate("Backend","Not installed")
    readonly property real fitScale: Math.max(0.2,Math.min(2.5,(stage.width-Theme.gapXxl)/phone.width,(stage.height-Theme.gapXl)/phone.height))
    readonly property real actualScale: requestedZoom>0 ? requestedZoom : fitScale

    function toast(message) { notice=message; toastTimer.restart() }
    // Display sizes are written with a decimal comma (assets/models.json).
    function inches(text) { return Studio.uiLanguage==="de" ? String(text) : String(text).replace(/(\d),(\d)/g,"$1.$2") }
    function showWorkspace(tab, projectPath, file, line) {
        if(projectPath) workspace.openProject(projectPath)
        if(file) workspace.revealFile(file,line || 1)
        nativeWorkspace.solo=false
        root.showNormal(); root.raise(); root.requestActivate()
        nativeMode=tab!=="preview"
        if(tab==="preview" || tab==="runtime") workspace.close()
        else workspace.showTab(tab)
    }
    function workspaceState() { return workspace.viewState() }
    function configureNative(args) { nativeMode=true;nativeWorkspace.configure(args) }
    function nativeState() { return nativeWorkspace.state() }
    function configureIosGuest(args) { workspace.configureIosGuest(args) }
    function nativeCaptureItem(includeFrame) { return nativeWorkspace.captureItem(includeFrame) }
    property bool discardOnClose: false
    // The window opens at the size it had when it was last closed, maximised
    // if it was; visual checks and the smoke test keep the default size.
    readonly property bool keepsGeometry: !captureMode && !smokeTest
    function saveGeometry() {
        if(!keepsGeometry) return
        const maximized=root.visibility===Window.Maximized || root.visibility===Window.FullScreen
        Studio.saveSetting("window/maximized",maximized)
        if(!maximized) {Studio.saveSetting("window/width",root.width);Studio.saveSetting("window/height",root.height)}
    }
    function restoreGeometry() {
        if(!keepsGeometry) return
        const w=Number(Studio.setting("window/width",0)),h=Number(Studio.setting("window/height",0))
        if(w>=root.minimumWidth && h>=root.minimumHeight) {
            root.width=Math.min(w,Math.max(root.minimumWidth,Screen.desktopAvailableWidth))
            root.height=Math.min(h,Math.max(root.minimumHeight,Screen.desktopAvailableHeight))
        }
        if(String(Studio.setting("window/maximized",false))==="true" && root.visible) root.showMaximized()
    }
    onClosing: function(close) {
        root.saveGeometry()
        // With a device window on screen the app continues as that window.
        // Unsaved edits stay in the hidden main window, which returns before
        // the app ends, so nothing is asked and nothing is discarded here.
        if(!nativeWorkspace.quitting && nativeWorkspace.deviceWindowVisible) {close.accepted=false;discardOnClose=false;nativeWorkspace.solo=true;return}
        if(!discardOnClose && workspace.hasUnsaved()) {close.accepted=false;unsavedClose.open()}
    }
    function selectDevice(index) {
        selectedIndex=index; finishIndex=0; foldedOpen=false
        deviceCategory=currentModel.platform || "ios"
        categorySelections=Object.assign({},categorySelections,{[deviceCategory]:currentModel.id})
        if(!smokeTest && !captureMode) Studio.saveSetting("device",currentModel.id)
    }
    function selectCategory(category) {
        let wanted=categorySelections[category]
        let index=devices.findIndex(d=>(d.platform || "ios")===category && d.id===wanted)
        if(index<0) index=devices.findIndex(d=>(d.platform || "ios")===category)
        if(index>=0) selectDevice(index)
        else deviceCategory=category
    }
    function selectDuoState(opened, landscape) {
        foldedOpen=opened; rotated=landscape; requestedZoom=0
        stage.contentX=0; stage.contentY=0
    }
    function checkDuoState(name,screenW,screenH,viewportW,viewportH,punch) {
        phone.browser.runJavaScript("({width:innerWidth,height:innerHeight,layout:window.__tomaccoViewport})",function(result) {
            let pass=result.width===viewportW && result.height===viewportH
                && phone.screenWidth===screenW && phone.screenHeight===screenH
                && !phone.islandVisible && phone.punchVisible===punch
                && result.layout.barEdge===phone.barEdge && result.layout.width===screenW && result.layout.height===screenH
                && result.layout.safeArea.right===phone.safeRight && result.layout.safeArea.left===phone.safeLeft
            root.testResults.push({check:"duo-"+name,pass:pass,result:result,
                screen:[phone.screenWidth,phone.screenHeight],island:phone.islandVisible,punch:phone.punchVisible})
            if(!pass) root.testFailed=true
        })
    }
    function openUrl(text) {
        let next=Studio.normalizeUrl(text)
        if(next.toString().length) {
            phone.browser.url=next; phone.loadError=""; phone.browser.forceActiveFocus()
            if(!smokeTest && !captureMode && next.toString().indexOf("http")===0) Studio.saveSetting("lastUrl",next.toString())
        }
    }
    function toggleFullscreen() {
        if(root.visibility===Window.FullScreen) root.showNormal(); else root.showFullScreen()
    }
    function snapshot() {
        if(nativeMode){nativeWorkspace.capture();return}
        let path=Studio.screenshotPath(currentModel.id+"-"+(currentModel.id==="duo"?(foldedOpen?"offen-":"gefaltet-"):"")+(rotated?"quer":"hoch"))
        if(!path.length) return
        phone.grabToImage(function(result) {
            root.toast(!result.saveToFile(path) ? qsTr("Screenshot could not be saved.") : (Studio.copyImage(path) ? qsTr("Screenshot saved and copied · %1") : qsTr("Screenshot saved · %1")).arg(path))
        },Qt.size(phone.width*2,phone.height*2))
    }
    Component.onCompleted: {
        root.restoreGeometry()
        if(!captureMode && !smokeTest) {
            let saved=Studio.setting("device","18pro")
            for(let i=0;i<devices.length;i++) if(devices[i].id===saved) selectedIndex=i
        }
        if(initialDevice.length) {
            for(let i=0;i<devices.length;i++) if(devices[i].id===initialDevice) selectedIndex=i
        }
        deviceCategory=currentModel.platform || "ios"
        categorySelections=Object.assign({},categorySelections,{[deviceCategory]:currentModel.id})
        root.rotated=initialLandscape; root.foldedOpen=initialUnfolded && !!root.currentModel.openWidth
        if(Studio.initialUrl.length) openUrl(Studio.initialUrl)
        if(initialProject.length) { workspace.initialPath=initialProject; workspace.openProject(initialProject) }
        if(initialWorkspace!=="preview" && initialWorkspace!=="runtime") workspace.showTab(initialWorkspace)
        if(Agent.socketError.length) toast(Agent.socketError)
    }
    Connections { target: Studio; function onMessage(text) { root.toast(text) } }
    Connections {
        target: phone.browser
        function onUrlChanged() { address.text=root.isDemo ? "tomacco://demo" : phone.browser.url.toString() }
    }
    Shortcut { sequence: "Ctrl+L"; enabled:!root.nativeMode; onActivated: { address.forceActiveFocus(); address.selectAll() } }
    Shortcut { sequence: "Ctrl+R"; enabled:!root.nativeMode; onActivated: phone.browser.reload() }
    Shortcut { sequence: "Ctrl+Shift+R"; enabled:!root.nativeMode; onActivated: root.rotated=!root.rotated }
    Shortcut { sequence: "Ctrl+Shift+S"; onActivated: root.snapshot() }
    Shortcut { sequence: "F11"; onActivated: root.toggleFullscreen() }
    Shortcut { sequence: "F12"; enabled:!root.nativeMode; onActivated: devTools.visible=!devTools.visible }
    Shortcut { sequence: "Escape"; enabled:root.visibility===Window.FullScreen; onActivated: root.showNormal() }

    ColumnLayout {
        anchors.fill: parent; spacing: 0; visible:!root.nativeMode
        Rectangle {
            Layout.fillWidth: true; Layout.preferredHeight: Theme.barHeight; color: Theme.surface
            RowLayout {
                anchors.fill: parent; anchors.leftMargin: Theme.gapMd+Theme.gapXxs; anchors.rightMargin: Theme.gapSm; spacing: Theme.gapXs
                Row { spacing: Theme.gapXs+1
                    BrandMark { size: Theme.gapXl; anchors.verticalCenter: parent.verticalCenter }
                    StudioHeading { text: Qt.application.name; size: Theme.fontTitle; wide: true; wrapMode: Text.NoWrap; anchors.verticalCenter: parent.verticalCenter }
                }
                StudioSection { text: qsTr("Web preview"); visible: root.width>=1000; Layout.leftMargin: Theme.gapSm; Layout.rightMargin: Theme.gapSm }
                StudioButton { iconName: "back"; quiet: true; tip: qsTr("Back"); enabled: phone.browser.canGoBack; onClicked: phone.browser.goBack() }
                StudioButton { iconName: "refresh"; quiet: true; tip: qsTr("Reload · Ctrl+R"); onClicked: phone.browser.reload() }
                StudioTextField {
                    id: address; objectName: "addressField"
                    Layout.fillWidth: true; implicitWidth: 120
                    text: "tomacco://demo"; placeholderText: "http://localhost:5173"
                    font.family: Theme.mono; font.pixelSize: Theme.fontSmall
                    onAccepted: root.openUrl(text)
                }
                StudioButton { text: qsTr("Open"); visible: root.width>=880; onClicked: root.openUrl(address.text) }
                StudioButton { text: qsTr("Open folder"); displayText: root.compact?"":text; iconName: "files"; quiet: true; tip: qsTr("Load a local web build with index.html"); onClicked: folderDialog.open() }
                StudioButton { text: "Demo"; quiet: true; selected: root.isDemo; visible: root.width>=880; onClicked: root.openUrl("demo") }
                Rectangle { Layout.preferredWidth: 1; Layout.preferredHeight: Theme.iconSize; color: Theme.border; Layout.leftMargin: Theme.gapXs; Layout.rightMargin: Theme.gapXs }
                StudioButton { text: qsTr("Live device"); displayText: root.compact?"":text; iconName: "phone"; tip: text; onClicked: {workspace.close();root.nativeMode=true} }
                StudioButton { text: qsTr("Workspace"); displayText: root.compact?"":text; iconName: "project"; quiet: true; tip: text; onClicked: workspace.open() }
                StudioButton { iconName: "camera"; quiet: true; tip: qsTr("Save device as PNG · Ctrl+Shift+S"); onClicked: root.snapshot() }
                StudioButton { iconName: "fullscreen"; quiet: true; tip: qsTr("Full screen · F11"); onClicked: root.toggleFullscreen() }
                StudioButton { iconName: "keyboard"; quiet: true; tip: qsTr("Keyboard shortcuts and tips"); onClicked: help.open() }
            }
            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.subtle }
            Rectangle { anchors.bottom: parent.bottom; width: parent.width*phone.browser.loadProgress/100; height: Theme.gapXxs; color: Theme.accent; visible: phone.browser.loading }
        }
        RowLayout {
            Layout.fillHeight: true; Layout.fillWidth: true; spacing: 0
            Rectangle {
                visible: root.width>=860
                Layout.preferredWidth: Theme.railWideWidth+Theme.gapXxl; Layout.fillHeight: true; color: Theme.surface
                ColumnLayout {
                    anchors.fill: parent; anchors.margins: Theme.gapSm; spacing: Theme.gapSm
                    StudioSegmented {
                        Layout.fillWidth: true; current: root.deviceCategory
                        model: root.deviceCategories.map(x=>({id:x.id,label:x.label,objectName:"category-"+x.id}))
                        onChosen: id=>root.selectCategory(id)
                    }
                    StudioTextField {
                        id: deviceFilter; Layout.fillWidth: true
                        placeholderText: root.deviceCategory==="ios"?qsTr("Search Apple devices…"):root.deviceCategory==="android"?qsTr("Search Android devices…"):qsTr("Search custom devices…")
                        text: root.deviceSearches[root.deviceCategory] || ""
                        onTextEdited: root.deviceSearches=Object.assign({},root.deviceSearches,{[root.deviceCategory]:text})
                    }
                    StudioSection { Layout.leftMargin: Theme.gapXs; Layout.topMargin: Theme.gapXs; text: qsTr("%n model(s)","",root.categoryDevices.length) }
                    ListView {
                        id: deviceList; objectName: "deviceList"
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                        model: root.visibleDevices
                        delegate: Rectangle {
                            id: deviceRow
                            required property int index
                            required property var modelData
                            readonly property bool current: root.currentModel.id===modelData.id
                            width: deviceList.width; height: Theme.barHeight
                            color: current ? Theme.selected : modelHover.containsMouse ? Theme.hover : "transparent"
                            Rectangle { visible: deviceRow.current; width: 2; height: parent.height; color: Theme.accent }
                            Column { x: Theme.gapMd; width: parent.width-Theme.gapMd*2; anchors.verticalCenter: parent.verticalCenter; spacing: Theme.gapXxs
                                Text { width: parent.width; text: deviceRow.modelData.name; color: deviceRow.current?Theme.text:Theme.muted; font.pixelSize: Theme.fontBody; font.weight: Font.Medium; elide: Text.ElideRight }
                                Text { width: parent.width; text: (deviceRow.modelData.display ? root.inches(deviceRow.modelData.display) : qsTr("Custom size"))+" · "+(({ios:"iOS",android:"Android"})[deviceRow.modelData.platform || "ios"] || qsTr("Custom","device category")); color: Theme.faint; font.family: Theme.mono; font.pixelSize: Theme.fontMicro; elide: Text.ElideRight }
                            }
                            MouseArea { id: modelHover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: root.selectDevice(root.devices.findIndex(d=>d.id===deviceRow.modelData.id)) }
                            Accessible.role: Accessible.Button; Accessible.name: modelData.name; Accessible.onPressAction: root.selectDevice(root.devices.findIndex(d=>d.id===modelData.id))
                        }
                        Text { anchors.centerIn: parent; visible: deviceList.count===0; text: qsTr("No matching model"); color: Theme.muted; font.pixelSize: Theme.fontSmall }
                        ScrollBar.vertical: StudioScrollBar { policy: ScrollBar.AsNeeded }
                    }
                    RowLayout { Layout.fillWidth: true; spacing: Theme.gapXs
                        StudioButton { text: qsTr("Import"); quiet: true; size: Theme.fontSmall; Layout.fillWidth: true; onClicked: profileImport.open() }
                        StudioButton { text: qsTr("Export"); quiet: true; size: Theme.fontSmall; Layout.fillWidth: true; onClicked: profileExport.open() }
                    }
                    Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.subtle }
                    StudioSection { Layout.leftMargin: Theme.gapXs; text: qsTr("QUICK OPEN") }
                    RowLayout { Layout.fillWidth: true; spacing: Theme.gapXs
                        Repeater { model: [5173,3000,8080]
                            StudioButton { required property int modelData; text: ":"+modelData; size: Theme.fontSmall; Layout.fillWidth: true; implicitWidth: Theme.gapXxxl; quiet: true; leftPadding: 0; rightPadding: 0; onClicked: root.openUrl("http://localhost:"+modelData) }
                        }
                    }
                    ItemDelegate {
                        id: toolLine
                        Layout.fillWidth: true; Layout.preferredHeight: Theme.barHeight
                        padding: 0; leftPadding: Theme.gapXs; rightPadding: Theme.gapXs
                        background: Rectangle { color: toolLine.hovered?Theme.hover:"transparent" }
                        contentItem: Column { anchors.verticalCenter: parent.verticalCenter; spacing: Theme.gapXxs
                            StudioStatus { width: parent.width; tone: Studio.xtool.indexOf("xtool ")===0?"ok":"warn"; text: Studio.xtool }
                            Text { width: parent.width; text: root.sdkMissing?qsTr("iOS SDK not set up yet"):qsTr("View build tools →"); color: Theme.faint; font.pixelSize: Theme.fontMicro; elide: Text.ElideRight }
                        }
                        onClicked: workspace.open()
                    }
                }
                Rectangle { anchors.right: parent.right; height: parent.height; width: 1; color: Theme.subtle }
            }
            ColumnLayout {
                Layout.fillWidth: true; Layout.fillHeight: true; spacing: 0
                RowLayout {
                    Layout.fillWidth: true; Layout.preferredHeight: Theme.barHeight; Layout.leftMargin: Theme.gapMd; Layout.rightMargin: Theme.gapSm; spacing: Theme.gapXs
                    // Without the device list, the model is chosen here.
                    StudioComboBox {
                        visible: root.width<860; Layout.preferredWidth: 200; model: root.visibleDevices; textRole: "name"; valueRole: "id"
                        currentIndex: root.visibleDevices.findIndex(d=>d.id===root.currentModel.id)
                        onActivated: root.selectDevice(root.devices.findIndex(d=>d.id===currentValue))
                    }
                    Text { visible: root.width>=860; text: root.currentModel.name; color: Theme.text; font.pixelSize: Theme.fontBody; font.weight: Font.DemiBold; elide: Text.ElideRight }
                    Text { Layout.fillWidth: true; text: root.finishName+" · "+root.stateName; color: Theme.faint; font.pixelSize: Theme.fontSmall; elide: Text.ElideRight }
                    StudioButton { iconName: "rotate"; quiet: true; tip: qsTr("Rotate · Ctrl+Shift+R"); selected: root.rotated; onClicked: root.rotated=!root.rotated }
                    StudioButton { text: root.foldedOpen?qsTr("Fold"):qsTr("Unfold"); quiet: true; visible: !!root.currentModel.openWidth; onClicked: root.selectDuoState(!root.foldedOpen,root.rotated) }
                    StudioButton { text: qsTr("Safe area"); quiet: true; visible: root.compact; selected: root.guidesOn; onClicked: root.guidesOn=!root.guidesOn }
                }
                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.subtle }
                RowLayout {
                    visible: !!root.currentModel.openWidth && root.width>=1000
                    Layout.fillWidth: true; Layout.preferredHeight: Theme.barHeight
                    Layout.leftMargin: Theme.gapMd; Layout.rightMargin: Theme.gapMd; spacing: Theme.gapXs
                    Repeater {
                        model: [
                            {label:qsTr("Folded · Portrait"),open:false,landscape:false},
                            {label:qsTr("Folded · Landscape"),open:false,landscape:true},
                            {label:qsTr("Open · Portrait"),open:true,landscape:false},
                            {label:qsTr("Open · Landscape"),open:true,landscape:true}
                        ]
                        StudioButton {
                            required property var modelData
                            text: modelData.label; size: Theme.fontSmall; quiet: true; Layout.fillWidth: true
                            selected: root.foldedOpen===modelData.open && root.rotated===modelData.landscape
                            onClicked: root.selectDuoState(modelData.open,modelData.landscape)
                        }
                    }
                }
                Flickable {
                    id: stage; objectName: "deviceStage"
                    Layout.fillWidth: true; Layout.fillHeight: true
                    contentWidth: Math.max(width,phone.width*root.actualScale+Theme.gapXxl)
                    contentHeight: Math.max(height,phone.height*root.actualScale+Theme.gapXl)
                    clip: true; boundsBehavior: Flickable.StopAtBounds
                    // Empty canvas can be dragged; WebEngine receives input on the display.
                    Device {
                        id: phone; objectName: "phoneDevice"
                        model: root.currentModel; landscape: root.rotated; unfolded: root.foldedOpen
                        finish: root.currentModel.colors[root.finishIndex]
                        safeLayout: root.safeMode; guides: root.guidesOn; muted: root.muted
                        x: (stage.contentWidth-width)/2; y: (stage.contentHeight-height)/2
                        scale: root.actualScale
                        onStatusMessage: function(text) { root.toast(text) }
                    }
                    ScrollBar.vertical: StudioScrollBar { policy: ScrollBar.AsNeeded }
                    ScrollBar.horizontal: StudioScrollBar { policy: ScrollBar.AsNeeded }
                }
            }
            Rectangle {
                visible: !root.compact; Layout.preferredWidth: Theme.railWideWidth+Theme.gapXxxl; Layout.fillHeight: true; color: Theme.surface
                Rectangle { height: parent.height; width: 1; color: Theme.subtle }
                StudioScrollView {
                    anchors.fill: parent; anchors.leftMargin: Theme.gapLg; anchors.rightMargin: Theme.gapMd; anchors.topMargin: Theme.gapLg; contentWidth: availableWidth; clip: true
                    ColumnLayout {
                        width: parent.width; spacing: Theme.gapSm
                        StudioSection { text: qsTr("DISPLAY") }
                        Text { text: root.inches(root.currentModel.id==="duo" ? (root.foldedOpen?"7,6″":"5,4″") : root.currentModel.display); color: Theme.text; font.pixelSize: Theme.fontDisplay; font.weight: Font.DemiBold; font.letterSpacing: Theme.trackingDisplay }
                        Text { Layout.fillWidth: true; text: qsTr("%1 panel pixels").arg(root.foldedOpen ? root.currentModel.openPixels : root.currentModel.pixels); color: Theme.muted; font.pixelSize: Theme.fontSmall; wrapMode: Text.Wrap }
                        Text { Layout.fillWidth: true; visible: root.currentModel.id==="duo"; text: root.foldedOpen?qsTr("Inner · Under-display camera"):qsTr("Outer · Round front camera"); color: Theme.faint; font.pixelSize: Theme.fontSmall; wrapMode: Text.Wrap }
                        Rectangle { Layout.fillWidth: true; Layout.topMargin: Theme.gapSm; Layout.bottomMargin: Theme.gapXs; implicitHeight: 1; color: Theme.subtle }
                        StudioSection { text: qsTr("CASE") }
                        Flow { Layout.fillWidth: true; spacing: Theme.gapXs
                            Repeater { model: root.currentModel.colors
                                Rectangle {
                                    required property int index; required property string modelData
                                    width: Theme.gapXl; height: Theme.gapXl; color: "transparent"
                                    border.width: root.finishIndex===index?2:0; border.color: Theme.accentInk
                                    Rectangle { anchors.centerIn: parent; width: Theme.iconSize; height: Theme.iconSize; color: modelData; border.width: 1; border.color: Theme.border }
                                    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.finishIndex=index }
                                    Accessible.role: Accessible.Button; Accessible.name: ColorNames.name(root.currentModel.colorNames[index])
                                }
                            }
                        }
                        Text { text: root.finishName; color: Theme.faint; font.pixelSize: Theme.fontSmall }
                        Rectangle { Layout.fillWidth: true; Layout.topMargin: Theme.gapSm; Layout.bottomMargin: Theme.gapXs; implicitHeight: 1; color: Theme.subtle }
                        StudioSection { text: qsTr("VIEW") }
                        StudioSegmented {
                            Layout.fillWidth: true; current: root.rotated?"landscape":"portrait"
                            model: [{id:"portrait",label:qsTr("Portrait","orientation button")},{id:"landscape",label:qsTr("Landscape","orientation button")}]
                            onChosen: id=>root.rotated=id==="landscape"
                        }
                        StudioCheckBox { text: qsTr("Safe area metadata"); Layout.fillWidth: true; checked: root.safeMode; ToolTip.visible: hovered; ToolTip.delay: 550; ToolTip.text: qsTr("Send layout hints to the app; the background always stays edge-to-edge"); onToggled: root.safeMode=checked }
                        StudioCheckBox { text: qsTr("Show safe area"); Layout.fillWidth: true; checked: root.guidesOn; onToggled: root.guidesOn=checked }
                        StudioCheckBox { text: qsTr("System bars"); Layout.fillWidth: true; checked: phone.showChrome; onToggled: phone.showChrome=checked }
                        StudioCheckBox { text: qsTr("Sound"); Layout.fillWidth: true; checked: !root.muted; onToggled: root.muted=!checked }
                        StudioSection { Layout.topMargin: Theme.gapXs; text: qsTr("System bar color") }
                        StudioSegmented {
                            Layout.fillWidth: true; current: phone.chromeTheme
                            model: [{id:"auto",label:qsTr("Auto")},{id:"light",label:qsTr("Light")},{id:"dark",label:qsTr("Dark")}]
                            onChosen: id=>phone.chromeTheme=id
                        }
                        Rectangle { Layout.fillWidth: true; Layout.topMargin: Theme.gapSm; Layout.bottomMargin: Theme.gapXs; implicitHeight: 1; color: Theme.subtle }
                        StudioSection { text: qsTr("TOOLS") }
                        StudioButton { text: qsTr("Web inspector"); iconName: "terminal"; quiet: true; alignLeft: true; Layout.fillWidth: true; tip: qsTr("Developer tools · F12"); onClicked: { devTools.visible=true; devTools.raise(); devTools.requestActivate() } }
                        StudioButton { text: qsTr("Builds & devices"); iconName: "jobs"; quiet: true; alignLeft: true; Layout.fillWidth: true; onClicked: workspace.open() }
                        StudioButton { text: qsTr("Screenshots"); iconName: "camera"; quiet: true; alignLeft: true; Layout.fillWidth: true; onClicked: Studio.openScreenshots() }
                        Text {
                            Layout.fillWidth: true; Layout.topMargin: Theme.gapXs; Layout.bottomMargin: Theme.gapLg
                            text: qsTr("Chromium preview. Frames are approximate.")
                            color: Theme.faint; font.pixelSize: Theme.fontMicro; lineHeight: 1.3; wrapMode: Text.WordWrap
                        }
                    }
                }
            }
        }
        Rectangle {
            Layout.fillWidth: true; Layout.preferredHeight: Theme.statusHeight; color: Theme.surface
            Rectangle { width: parent.width; height: 1; color: Theme.subtle }
            RowLayout { anchors.fill: parent; anchors.topMargin: 1; anchors.rightMargin: Theme.gapMd; spacing: Theme.gapMd
                Rectangle {
                    Layout.fillHeight: true; Layout.preferredWidth: previewState.implicitWidth+Theme.gapLg
                    color: phone.loadError.length?Theme.danger:phone.browser.loading?Theme.warning:Theme.accent
                    Text { id: previewState; anchors.centerIn: parent; text: phone.loadError.length?qsTr("Unreachable"):phone.browser.loading?qsTr("Loading…"):root.isDemo?qsTr("Interactive demo"):qsTr("Web app connected"); color: Theme.accentText; font.family: Theme.mono; font.pixelSize: Theme.fontMicro; font.weight: Font.Bold }
                }
                Text { text: phone.viewportWidth+"×"+phone.viewportHeight+" CSS px"; color: Theme.muted; font.family: Theme.mono; font.pixelSize: Theme.fontMicro }
                Text { Layout.fillWidth: true; visible: root.width>=1100; text: qsTr("Ctrl+L  Address     Ctrl+Shift+R  Rotate     F11  Full screen"); color: Theme.faint; font.family: Theme.mono; font.pixelSize: Theme.fontMicro; elide: Text.ElideRight }
                Item { Layout.fillWidth: true; visible: root.width<1100 }
                Row { spacing: 0
                    StudioButton { iconName: "minus"; quiet: true; implicitWidth: Theme.statusHeight-1; implicitHeight: Theme.statusHeight-1; tip: qsTr("Zoom out"); onClicked: root.requestedZoom=Math.max(0.25,root.actualScale-0.1) }
                    StudioButton { text: Math.round(root.actualScale*100)+" %"; quiet: true; size: Theme.fontMicro; implicitHeight: Theme.statusHeight-1; leftPadding: Theme.gapXs; rightPadding: Theme.gapXs; tip: qsTr("View at 100 %"); onClicked: root.requestedZoom=1 }
                    StudioButton { iconName: "plus"; quiet: true; implicitWidth: Theme.statusHeight-1; implicitHeight: Theme.statusHeight-1; tip: qsTr("Zoom in"); onClicked: root.requestedZoom=Math.min(1.5,root.actualScale+0.1) }
                    StudioButton { text: qsTr("Fit"); quiet: true; size: Theme.fontMicro; selected: root.requestedZoom===0; implicitHeight: Theme.statusHeight-1; leftPadding: Theme.gapXs; rightPadding: Theme.gapXs; onClicked: { root.requestedZoom=0; stage.contentX=0; stage.contentY=0 } }
                }
            }
        }
    }
    Rectangle {
        anchors.horizontalCenter: parent.horizontalCenter; anchors.bottom: parent.bottom; anchors.bottomMargin: Theme.gapXxxl
        width: Math.min(noticeText.implicitWidth+Theme.gapXl,root.width-Theme.gapXxxl); height: noticeText.implicitHeight+Theme.gapMd*2
        color: Theme.elevated; border.color: Theme.border; radius: Theme.radius; visible: root.notice.length>0; z: 10
        Rectangle { width: Theme.gapXxs; height: parent.height; color: Theme.accent }
        Text { id: noticeText; anchors.centerIn: parent; width: Math.min(implicitWidth,root.width-Theme.gapXxxl*2); text: root.notice; wrapMode: Text.Wrap; color: Theme.text; font.pixelSize: Theme.fontSmall }
    }
    Timer { id: toastTimer; interval: 5000; onTriggered: root.notice="" }
    FolderDialog { id: folderDialog; title: qsTr("Choose a web build"); onAccepted: { let url=Studio.serveFolder(selectedFolder); if(url.toString().length) phone.browser.url=url } }

    StudioDialog {
        id: help; anchors.centerIn: parent; width: Math.min(root.width-Theme.gapXxxl,520); modal: true
        contentItem: ColumnLayout { spacing: Theme.gapMd
            Text { Layout.fillWidth: true; text: qsTr("Your web app, on every phone."); color: Theme.text; font.pixelSize: Theme.fontTitle; font.weight: Font.DemiBold; font.letterSpacing: Theme.trackingTitle; wrapMode: Text.WordWrap }
            Text { Layout.fillWidth: true; text: qsTr("Open it by URL, or load a finished build as a folder. Switch phone, color and orientation without reloading."); color: Theme.muted; wrapMode: Text.WordWrap; font.pixelSize: Theme.fontBody; lineHeight: 1.3 }
            Text { Layout.fillWidth: true; text: qsTr("Ctrl+L                  Address\nCtrl+R                  Reload\nCtrl+Shift+R         Rotate\nCtrl+Shift+S         Screenshot\nF11 / Esc              Full screen / back\nF12                        Web inspector"); color: Theme.text; font.family: Theme.mono; font.pixelSize: Theme.fontSmall; lineHeight: 1.4 }
            Text { Layout.fillWidth: true; text: qsTr("The layout preview uses Chromium. Build and run native Android and iOS apps from the workspace on an emulator or a connected device. The profiles contain the manufacturers' resolutions and approximated bodies, cutouts and safe areas. CSS env(safe-area-inset-*) and Safari are not emulated here."); wrapMode: Text.WordWrap; color: Theme.faint; font.pixelSize: Theme.fontSmall; lineHeight: 1.3 }
            RowLayout { Layout.fillWidth: true; spacing: Theme.gapXs
                StudioButton { text: qsTr("Model specifications ↗"); quiet: true; onClicked: Studio.openExternal(root.currentModel.source) }
                Item { Layout.fillWidth: true }
                StudioButton { text: qsTr("Got it"); accent: true; onClicked: help.close() }
            }
        }
    }
    StudioDialog {
        id: toolchain; anchors.centerIn: parent; width: Math.min(root.width-Theme.gapXxxl,520); modal: true
        contentItem: ColumnLayout { spacing: Theme.gapMd
            Text { text: qsTr("Native build tools"); color: Theme.text; font.pixelSize: Theme.fontTitle; font.weight: Font.DemiBold; font.letterSpacing: Theme.trackingTitle }
            Text { Layout.fillWidth: true; text: Studio.xtool+"\n"+Studio.swift+"\nSDK: "+Studio.sdk; color: Theme.text; font.family: Theme.mono; font.pixelSize: Theme.fontSmall; lineHeight: 1.4; wrapMode: Text.Wrap }
            Text { Layout.fillWidth: true; text: qsTr("xtool builds and signs SwiftPM apps for iOS. This needs Apple's SDK from Xcode and a separate sign-in for signing."); wrapMode: Text.WordWrap; color: Theme.muted; font.pixelSize: Theme.fontBody; lineHeight: 1.3 }
            Rectangle { Layout.fillWidth: true; implicitHeight: setupCommands.implicitHeight+Theme.gapMd*2; color: Theme.inset; radius: Theme.radiusSmall
                Text { id: setupCommands; anchors.fill: parent; anchors.margins: Theme.gapMd; text: qsTr("xtool sdk install /path/to/Xcode.xip\nxtool setup"); font.family: Theme.mono; color: Theme.text; font.pixelSize: Theme.fontSmall; lineHeight: 1.4 }
            }
            Text { Layout.fillWidth: true; text: qsTr("The web preview works independently of this. Credentials are entered only in the interactive xtool setup."); wrapMode: Text.WordWrap; color: Theme.faint; font.pixelSize: Theme.fontSmall; lineHeight: 1.3 }
            RowLayout { Layout.fillWidth: true; spacing: Theme.gapXs
                StudioButton { text: qsTr("Apple download ↗"); quiet: true; onClicked: Studio.openExternal("https://developer.apple.com/download/all/?q=Xcode") }
                StudioButton { text: qsTr("Copy setup"); quiet: true; onClicked: Studio.copy("xtool setup") }
                Item { Layout.fillWidth: true }
                StudioButton { text: qsTr("Close"); accent: true; onClicked: toolchain.close() }
            }
        }
    }
    Window {
        id: devTools; width: 1050; height: 720; visible: false; title: qsTr("Tomacco · Web inspector"); color: Theme.background
        WebEngineView { anchors.fill: parent; inspectedView: devTools.visible ? phone.browser : null }
    }

    NativeWorkspace {id:nativeWorkspace;anchors.fill:parent;visible:root.nativeMode;workspace:workspace;externalDeviceWindow:workspace.guestWindowVisible;onWebRequested:{workspace.close();root.nativeMode=false}}
    Connections { target: workspace; function onOpenedChanged() { if(workspace.opened)root.nativeMode=true } }
    PreviewAutomation { app: root; device: phone }
    Workspace { id: workspace; parent: nativeWorkspace.workspaceHost; anchors.fill: parent; visible: opened && root.nativeMode;
        guestSolo: nativeWorkspace.solo
        onGuestCloseRequested: nativeWorkspace.quitApp()
        onGuestDockRequested: { nativeWorkspace.showMainWindow(); root.nativeMode=true; workspace.showTab("ios-research") } onProfilesChanged: { root.selectDevice(0); Studio.reloadModels() } }
    StudioDialog { id: unsavedClose; objectName: "unsavedCloseDialog"; anchors.centerIn: parent; modal: true; title: qsTr("Unsaved files"); standardButtons: Dialog.Discard | Dialog.Cancel; Label { text: qsTr("The workspace has unsaved changes."); color: Theme.muted } onDiscarded: {root.discardOnClose=true;root.close()} }
    FileDialog { id: profileImport; title: qsTr("Import device profiles"); nameFilters: [qsTr("Device profiles (*.json)")]; onAccepted: Agent.request("catalog.import",{path:decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,""))}) }
    FileDialog { id: profileExport; title: qsTr("Export device profile"); fileMode: FileDialog.SaveFile; nameFilters: ["JSON (*.json)"]; onAccepted: Agent.request("catalog.export",{profile_id:root.currentModel.id,path:decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,""))}) }
    Connections { target: Agent; function onApiFinished(id,operation,response) {
        if(operation==="catalog.import" || operation==="catalog.export") {
            if(response.ok) { root.selectDevice(0); Studio.reloadModels(); root.toast(qsTr("Device catalog updated")) }
            else root.toast(response.error.message)
        }
    } }

    // Deterministic integration checks exercise the same QML functions as the UI.
    Timer {
        interval: 900; repeat: true; running: smokeTest
        onTriggered: {
            if(phone.browser.loading) return
            if(root.testStep===0) {
                phone.browser.runJavaScript("({width:innerWidth,height:innerHeight,title:document.title,dpr:devicePixelRatio})",function(result) {
                    let pass=result && result.width===phone.viewportWidth && result.height===phone.viewportHeight
                        && result.dpr >= 2 && phone.browser.zoomFactor===phone.renderScale
                    root.testResults.push({check:"initial-viewport",pass:pass,result:result})
                    if(!pass) root.testFailed=true
                })
            } else if(root.testStep===1) {
                root.rotated=true
            } else if(root.testStep===2) {
                phone.browser.runJavaScript("({width:innerWidth,height:innerHeight})",function(result) {
                    let pass=result.width===phone.viewportWidth && result.height===phone.viewportHeight
                    root.testResults.push({check:"landscape-viewport",pass:pass,result:result})
                    if(!pass) root.testFailed=true
                })
            } else if(root.testStep===3) {
                root.selectDevice(2); root.selectDuoState(false,false); root.safeMode=true
            } else if(root.testStep===4) {
                root.checkDuoState("folded-portrait",466,678,466,678,true)
            } else if(root.testStep===5) {
                root.selectDuoState(false,true)
            } else if(root.testStep===6) {
                root.checkDuoState("folded-landscape",678,466,678,466,true)
            } else if(root.testStep===7) {
                root.selectDuoState(true,false)
            } else if(root.testStep===8) {
                const open=root.currentModel; root.checkDuoState("open-portrait",open.openHeight,open.openWidth,open.openHeight,open.openWidth,false)
            } else if(root.testStep===9) {
                root.selectDuoState(true,true)
            } else if(root.testStep===10) {
                const open=root.currentModel; root.checkDuoState("open-landscape",open.openWidth,open.openHeight,open.openWidth,open.openHeight,false)
            } else if(root.testStep===11) {
                root.safeMode=false
            } else if(root.testStep===12) {
                phone.browser.runJavaScript("({width:innerWidth,height:innerHeight})",function(result) {
                    let pass=result.width===root.currentModel.openWidth && result.height===root.currentModel.openHeight
                    root.testResults.push({check:"duo-edge-to-edge",pass:pass,result:result})
                    if(!pass) root.testFailed=true
                })
            } else if(root.testStep===13) {
                phone.browser.runJavaScript("document.getElementById('start').click(); document.getElementById('start').textContent",function(result) {
                    let pass=result.indexOf("Pause")>=0
                    root.testResults.push({check:"demo-interaction",pass:pass,result:result})
                    if(!pass) root.testFailed=true
                })
            } else if(root.testStep===14) {
                phone.grabToImage(function(result) {
                    let pass=result.saveToFile("/tmp/tomacco-device-test.png")
                    root.testResults.push({check:"device-screenshot",pass:pass})
                    if(!pass) root.testFailed=true
                },Qt.size(phone.width*2,phone.height*2))
            } else if(root.testStep===15) {
                root.toggleFullscreen()
            } else if(root.testStep===16) {
                let pass=root.visibility===Window.FullScreen
                root.testResults.push({check:"fullscreen",pass:pass})
                if(!pass) root.testFailed=true
                root.showNormal()
            } else if(root.testStep===17) {
                root.deviceSearches={ios:"17e",android:"pixel",generic:""}
                root.selectCategory("android")
            } else if(root.testStep===18) {
                let pass=root.currentModel.platform==="android" && root.visibleDevices.length>0 && root.visibleDevices.every(d=>d.platform==="android" && d.name.toLowerCase().indexOf("pixel")>=0)
                root.testResults.push({check:"android-category-filter",pass:pass})
                if(!pass) root.testFailed=true
                root.selectCategory("ios")
            } else if(root.testStep===19) {
                let pass=root.deviceCategory==="ios" && root.visibleDevices.length===1 && root.visibleDevices[0].id==="17e" && deviceFilter.text==="17e"
                root.testResults.push({check:"apple-category-search-preserved",pass:pass})
                if(!pass) root.testFailed=true
                root.selectDevice(root.devices.findIndex(d=>d.id==="17e")); root.rotated=false
            } else if(root.testStep===20) {
                let pass=phone.notchVisible && !phone.islandVisible && phone.bezel===21 && phone.screenWidth===390 && phone.screenHeight===844
                root.testResults.push({check:"17e-attached-notch-and-proportions",pass:pass})
                if(!pass) root.testFailed=true
            } else if(root.testStep===21) {
                Studio.testResult({passed:!root.testFailed,checks:root.testResults})
                Qt.exit(root.testFailed?4:0)
            }
            root.testStep++
        }
    }
}
