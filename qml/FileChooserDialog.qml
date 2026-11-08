import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// The app's own file and folder chooser: every entry point that opens, imports, saves or
// exports a file or chooses a folder uses it (ProjectFolderDialog is its project form), so it
// follows the interface size, theme, language and phone layout. Qt's stock FileDialog and
// FolderDialog fallback kept a fixed 600x400 or 600x497 window with 16 px icons and one-line
// fields at any interface size, so at 175 % paths, names and filters were cut off. Here the
// location and the file name show whole (wrapped) and can be typed or pasted, every entry name
// wraps, symbolic links name their target, and the dialog stays inside its window and scrolls.
//   mode "open":   an existing, readable file. Open, a double click or Enter on a file chooses it.
//   mode "save":   the file name typed in the shown folder; defaultSuffix is added to a name
//                  without a dot (as Qt's dialogs do). An existing file is chosen only after Yes
//                  in the replace question; No or Escape there keeps the chooser open.
//   mode "folder": the folder the location names.
// Choosing sets selectedPath (selectedFile/selectedFolder as file: URLs) and emits accepted.
// Cancel and Escape emit rejected and choose nothing; browsing, selecting and highlighting never
// accept. Backend::fileListing and Backend::folderListing read the folders and files.
StudioDialog {
    id: dialog
    property string mode: "open"
    // As Qt's FileDialog: "Videos (*.mp4 *.mov)"; the chosen one stays chosen for the next opening.
    property var nameFilters: []
    property int filterIndex: 0
    property string defaultSuffix: ""
    // The folder the next opening starts in (the home folder when empty); it follows the browsing.
    property string currentFolder: ""
    // An entry point's own start (a function returning a path), instead of currentFolder.
    property var startAt: null
    // What Open chooses for a folder the location names (folder mode): the folder, or "" when it cannot be chosen.
    property var folderTarget: path => Studio.pathInfo(path).folder ? path : ""
    // A path whose row is marked (the open project) and the object-name prefix of every row's name and link text.
    property string markPath: ""
    property string namePrefix: "fileChooserEntry"
    property string placeholder: qsTr("Type or paste a folder path")
    // The choice: a local path, and the same as a file: URL (percent-encoded where a URL needs it).
    property string selectedPath: ""
    readonly property url selectedFile: mode!=="folder" && selectedPath ? Studio.fileUrl(selectedPath) : ""
    readonly property url selectedFolder: mode==="folder" && selectedPath ? Studio.fileUrl(selectedPath) : ""
    // The title unless an entry point names its own (title or openWith).
    property string defaultTitle: mode==="save" ? qsTranslate("StudioDialog","Save") : mode==="folder" ? qsTranslate("FileWorkspace","Choose folder") : qsTranslate("DebugWorkspace","Choose file")
    property string caption: ""
    title: caption || defaultTitle
    function openWith(text) { caption=text || ""; open() }
    onClosed: caption=""

    // A sheet over the whole window below 600 logical px of width (as NativeWorkspace.phone) and in a
    // window under 480 logical px high (a phone held sideways, a short window at a large interface
    // size), where a centred dialog with desktop margins would leave the list almost no room.
    sheet: (parent ? parent.width : 0) < Theme.px(600) || (parent ? parent.height : 0) < Theme.px(480)
    // Free room at the window's edges a phone sheet keeps clear: the system bars and cut-outs
    // (the window's safe area, read through an invisible item that covers the window) and the keyboard.
    readonly property real keyboardInset: Qt.inputMethod.visible && Qt.inputMethod.keyboardRectangle.height>0 && parent ? Math.max(0,parent.height-Qt.inputMethod.keyboardRectangle.y) : 0
    // The probe is a child of the window's content item, never of the overlay: an overlay with
    // child items takes the input of the whole window.
    property Item safeArea: Item { id: safe; parent: dialog.parent ? dialog.parent.parent : null; anchors.fill: parent; visible: false; enabled: false }
    property real insetLeft: sheet ? safe.SafeArea.margins.left : 0
    property real insetTop: sheet ? safe.SafeArea.margins.top : 0
    property real insetRight: sheet ? safe.SafeArea.margins.right : 0
    property real insetBottom: sheet ? Math.max(safe.SafeArea.margins.bottom,keyboardInset) : 0

    // The folder shown and what it holds (Backend::fileListing; no files in folder mode).
    property var listing: ({path:"",parent:"",home:"",error:"",folders:[],files:[]})
    readonly property var entries: (listing.folders || []).map(entry => Object.assign({folder:true},entry))
        .concat((listing.files || []).map(entry => Object.assign({folder:false},entry)))
    // The file the name names (open and save modes), with the default suffix when saving.
    readonly property string typedTarget: visible && mode!=="folder" ? Studio.saveTarget(fileName.text,listing.path,mode==="save" ? defaultSuffix : "") : ""
    readonly property var targetInfo: typedTarget ? Studio.pathInfo(typedTarget) : ({})
    readonly property bool targetFolderReady: !!typedTarget && Studio.pathInfo(folderOf(typedTarget)).folder
    // What Open or Save would choose now ("" when nothing can be): a folder named there is entered instead.
    readonly property string target: {
        if(!visible)return ""
        if(mode==="folder")return folderTarget(Studio.resolveFolderPath(location.text,listing.path)) || ""
        if(!typedTarget)return ""
        if(targetInfo.folder)return typedTarget
        if(mode==="open")return targetInfo.file && targetInfo.readable ? typedTarget : ""
        return targetFolderReady ? typedTarget : ""
    }
    // Why a typed name cannot be chosen: an unreadable file, or a save folder that does not exist.
    readonly property string problem: !visible || mode==="folder" || !typedTarget || targetInfo.folder ? ""
        : mode==="open" ? (targetInfo.file && !targetInfo.readable ? qsTranslate("Backend","%1 cannot be read.").arg(isolate(typedTarget)) : "")
        : !targetFolderReady ? qsTranslate("Backend","%1 cannot be read.").arg(isolate(folderOf(typedTarget))) : ""
    readonly property int rowHeight: sheet ? Math.max(Theme.mobileTouch,Theme.controlHeight) : Theme.rowHeight
    readonly property int bodySize: sheet ? Theme.mobileFontBody : Theme.fontBody
    readonly property int smallSize: sheet ? Theme.mobileFontSmall : Theme.fontSmall
    // On a narrow dialog the parent and home buttons show only their icons (named for assistive technology).
    readonly property bool narrow: availableWidth < Theme.px(440)
    // Set while go() puts the highlight on a row, so only the person's own highlighting fills the name.
    property bool placing: false

    parent: Overlay.overlay
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    readonly property real roomWidth: (parent ? parent.width : 0)-insetLeft-insetRight
    readonly property real roomHeight: (parent ? parent.height : 0)-insetTop-insetBottom
    width: sheet ? roomWidth : Math.min(roomWidth-Theme.gapXl*2,Theme.px(760))
    height: sheet ? roomHeight : Math.min(roomHeight-Theme.gapXl*2,Theme.px(640))
    x: insetLeft+Math.round((roomWidth-width)/2)
    y: insetTop+Math.round((roomHeight-height)/2)

    // A path inside a sentence reads left to right also in a right-to-left language (isolated there).
    function isolate(path) { return mirrored ? "\u2066"+path+"\u2069" : path }
    // A file type's wildcards read left to right also in a right-to-left sentence: "(*.json)".
    function filterText(text) { return mirrored ? String(text).replace(/\(([^()]*)\)\s*$/,"(\u2066$1\u2069)") : text }
    function folderOf(path) { const at=path.lastIndexOf("/"); return at>0 ? path.substring(0,at) : "/" }
    // The wildcards of the chosen file type, read when listing (also before the dialog shows).
    function patterns() { return nameFilters && nameFilters.length ? Studio.filterPatterns(nameFilters[Math.max(0,Math.min(filterIndex,nameFilters.length-1))]) : [] }
    function go(path) {
        const from=listing.path
        const next=mode==="folder" ? Studio.folderListing(path,from,hidden.checked) : Studio.fileListing(path,from,hidden.checked,patterns())
        placing=true
        listing=next
        location.text=next.path
        if(next.error!=="missing" && next.error!=="not-folder")currentFolder=next.path
        // The folder just left (after going up), otherwise the marked path, otherwise the typed file is the current row.
        const rows=entries
        let mark=-1
        for(let i=0;i<rows.length && mark<0;++i)if(from && rows[i].folder && rows[i].path===from)mark=i
        for(let i=0;i<rows.length && mark<0;++i)if(markPath && (rows[i].path===markPath || rows[i].target===markPath))mark=i
        for(let i=0;i<rows.length && mark<0 && fileName.text;++i)if(!rows[i].folder && rows[i].name===fileName.text)mark=i
        folders.currentIndex=mark>=0?mark:(rows.length?0:-1)
        folders.positionViewAtIndex(Math.max(0,folders.currentIndex),ListView.Contain)
        placing=false
    }
    function goUp() { if(listing.parent)go(listing.parent) }
    // The location as typed: a folder is shown; a file (open and save) is shown in its folder and named.
    function goTyped(text) {
        const path=Studio.resolveFolderPath(text,listing.path)
        if(mode!=="folder" && Studio.pathInfo(path).file) { fileName.text=path.substring(path.lastIndexOf("/")+1); go(folderOf(path)) }
        else go(path)
    }
    // A row: a folder is entered; a file is named (and with `activate` chosen, as a double click).
    function use(entry,activate) {
        if(!entry)return
        if(entry.folder) { go(entry.path); return }
        fileName.text=entry.name
        if(activate)choose()
    }
    // Open or Save: a named folder is entered, an existing file is replaced only after Yes.
    function choose() {
        if(replace.visible)return
        if(mode==="folder") { const folder=target; if(folder)finish(folder); return }
        const path=typedTarget
        if(!path)return
        const info=Studio.pathInfo(path)
        if(info.folder) { fileName.text=""; go(path); return }
        if(mode==="open") { if(info.file && info.readable)finish(path); return }
        if(!Studio.pathInfo(folderOf(path)).folder)return
        if(info.exists) { replace.path=path; replace.open(); return }
        finish(path)
    }
    function finish(path) { selectedPath=path; accept() }
    function startFolder() {
        const start=typeof startAt==="function" ? startAt() : ""
        return start || currentFolder || "~"
    }
    // Scrolls the body so a field that takes the focus (a phone keyboard opening) stays in view.
    function reveal(item) {
        const flick=body.contentItem
        if(!flick || !item)return
        const top=item.mapToItem(column,0,0).y,bottom=top+item.height
        if(top<flick.contentY)flick.contentY=Math.max(0,top)
        else if(bottom>flick.contentY+flick.height)flick.contentY=Math.min(Math.max(0,column.height-flick.height),bottom-flick.height)
    }

    onAboutToShow: {
        selectedPath=""
        fileName.text=""
        listing={path:"",parent:"",home:"",error:"",folders:[],files:[]}
        go(startFolder())
    }
    onOpened: { if(mode==="save") { fileName.forceActiveFocus() } else folders.forceActiveFocus() }
    onAboutToHide: replace.close()
    Shortcut { sequence: "Ctrl+L"; enabled: dialog.visible && !replace.visible; onActivated: { location.forceActiveFocus(); location.selectAll() } }
    Shortcut { sequence: "Alt+Up"; enabled: dialog.visible && !replace.visible; onActivated: dialog.goUp() }
    Shortcut { sequences: ["Ctrl+Return","Ctrl+Enter"]; enabled: dialog.visible && !replace.visible; onActivated: dialog.choose() }

    // A one-line path or name field that wraps: the whole text on up to `lines` lines, scrolled beyond them.
    component PathField: TextArea {
        id: field
        property int lines: 3
        readonly property real lineHeight: Math.ceil(metrics.lineSpacing)
        signal submitted(bool control)
        FontMetrics { id: metrics; font: field.font }
        wrapMode: TextEdit.WrapAnywhere
        // A path reads left to right also in a right-to-left interface (as StudioTextField machine).
        horizontalAlignment: LayoutMirroring.enabled ? TextEdit.AlignRight : TextEdit.AlignLeft
        textFormat: TextEdit.PlainText
        font.family: Theme.mono
        font.pixelSize: dialog.bodySize
        color: Theme.text
        selectByMouse: true
        selectionColor: Theme.accent
        selectedTextColor: Theme.accentText
        placeholderTextColor: Theme.faint
        leftPadding: Theme.gapMd; rightPadding: Theme.gapMd
        // On a phone sheet one line is still a 48 px touch target.
        topPadding: dialog.sheet ? Math.max(Theme.gapSm,Math.ceil((Theme.mobileTouch-lineHeight)/2)) : Theme.gapSm
        bottomPadding: topPadding
        background: Rectangle {
            radius: Theme.radiusSmall
            color: Theme.inset
            border.color: field.activeFocus ? Theme.accentInk : Theme.border
            border.width: field.activeFocus ? 2 : 1
        }
        // One line: a pasted line break would be part of no path.
        onTextChanged: if(/[\r\n]/.test(text)) { const at=cursorPosition; text=text.replace(/[\r\n]+/g,""); cursorPosition=Math.min(at,length) }
        Keys.onReturnPressed: event => { submitted(!!(event.modifiers & Qt.ControlModifier)); event.accepted=true }
        Keys.onEnterPressed: event => { submitted(!!(event.modifiers & Qt.ControlModifier)); event.accepted=true }
        // Tab moves on (a path holds no tab character).
        Keys.onTabPressed: event => { const next=nextItemInFocusChain(true); if(next)next.forceActiveFocus(Qt.TabFocusReason); event.accepted=true }
        Keys.onBacktabPressed: event => { const previous=nextItemInFocusChain(false); if(previous)previous.forceActiveFocus(Qt.BacktabFocusReason); event.accepted=true }
        Accessible.name: placeholderText
        Accessible.role: Accessible.EditableText
    }

    // Cancel and Open (or Save) stay at the foot, at the end side, each at its natural width; in a
    // narrow sheet with long labels the second wraps onto its own line rather than squeezing a
    // label. Choosing goes through choose(), never straight to accept, so a missing target or the
    // replace question can keep the chooser open.
    footer: Item {
        implicitHeight: actions.implicitHeight+Theme.gapXl+Theme.gapLg
        implicitWidth: actions.implicitWidth+(dialog.sheet ? Theme.mobileGutter : Theme.gapXl)*2
        Flow {
            id: actions
            anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
            anchors.leftMargin: dialog.sheet ? Theme.mobileGutter : Theme.gapXl
            anchors.rightMargin: dialog.sheet ? Theme.mobileGutter : Theme.gapXl
            anchors.bottomMargin: Theme.gapLg
            spacing: Theme.gapSm
            // From the end side: the accent action outermost (mirrored with the layout).
            layoutDirection: Qt.RightToLeft
            StudioButton {
                id: chooseButton
                objectName: dialog.objectName+"-button-0"
                text: dialog.mode==="save" ? qsTranslate("StudioDialog","Save") : qsTranslate("StudioDialog","Open")
                accent: true
                touch: dialog.sheet
                enabled: dialog.target!==""
                onClicked: dialog.choose()
            }
            StudioButton {
                objectName: dialog.objectName+"-button-1"
                text: qsTranslate("StudioDialog","Cancel")
                touch: dialog.sheet
                onClicked: dialog.reject()
            }
        }
    }

    // Above the foot, when even one line of the location and two rows do not fit (a short window
    // at a large interface size, or a phone keyboard), everything scrolls.
    contentItem: StudioScrollView {
        id: body
        objectName: dialog.objectName+"Body"
        contentWidth: availableWidth
        contentHeight: column.height
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        // The focused file name stays in view when the room changes (a phone keyboard opening, a
        // rotation, the first layout after opening).
        onHeightChanged: if(fileName.activeFocus)Qt.callLater(dialog.reveal,fileBlock)
        ColumnLayout {
            id: column
            width: body.availableWidth
            height: Math.max(body.availableHeight,implicitHeight)
            spacing: Theme.gapSm
            onHeightChanged: if(fileName.activeFocus)Qt.callLater(dialog.reveal,fileBlock)
            // The location: the whole path, wrapped onto up to three lines (fewer when the dialog is short) and scrolled beyond them.
            // Typing or pasting a path and pressing Enter shows that folder; Ctrl+Enter or Open chooses.
            StudioScrollView {
                id: locationScroll
                objectName: dialog.objectName+"LocationScroll"
                readonly property real below: (fileBlock.visible ? fileBlock.height+column.spacing : 0)+(filterBlock.visible ? filterBlock.height+column.spacing : 0)+(problemText.visible ? problemText.height+column.spacing : 0)
                readonly property int lines: Math.max(1,Math.min(3,Math.floor((body.availableHeight-tools.height-below-column.spacing*2-dialog.rowHeight*2-2-location.topPadding-location.bottomPadding)/location.lineHeight)))
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(location.implicitHeight,location.lineHeight*lines+location.topPadding+location.bottomPadding)
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                PathField {
                    id: location
                    objectName: dialog.objectName+"Location"
                    placeholderText: dialog.placeholder
                    onSubmitted: control => { if(control)dialog.choose(); else dialog.goTyped(text) }
                }
            }
            Flow {
                id: tools
                Layout.fillWidth: true
                spacing: Theme.gapXs
                StudioButton {
                    objectName: dialog.objectName+"Parent"
                    text: qsTranslate("ProjectFolderDialog","Parent folder"); displayText: dialog.narrow ? "" : text; tip: text
                    iconName: "stepOut"; quiet: true; touch: dialog.sheet
                    enabled: !!dialog.listing.parent
                    onClicked: dialog.goUp()
                }
                StudioButton {
                    objectName: dialog.objectName+"Home"
                    text: qsTranslate("ProjectFolderDialog","Home folder"); displayText: dialog.narrow ? "" : text; tip: text
                    iconName: "home"; quiet: true; touch: dialog.sheet
                    onClicked: dialog.go("~")
                }
                StudioCheckBox {
                    id: hidden
                    objectName: dialog.objectName+"Hidden"
                    height: dialog.sheet ? Math.max(implicitHeight,Theme.mobileTouch) : implicitHeight
                    text: qsTranslate("FileWorkspace","Hidden")
                    onToggled: dialog.go(dialog.listing.path)
                    Accessible.name: text
                }
            }
            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: dialog.rowHeight*2+2
                color: Theme.inset
                border.color: Theme.border
                radius: Theme.radiusSmall
                ListView {
                    id: folders
                    objectName: dialog.objectName+"Folders"
                    anchors.fill: parent
                    anchors.margins: 1
                    clip: true
                    model: dialog.entries
                    boundsBehavior: Flickable.StopAtBounds
                    keyNavigationEnabled: true
                    highlightMoveDuration: 0
                    activeFocusOnTab: true
                    ScrollBar.vertical: StudioScrollBar {}
                    readonly property var current: currentIndex>=0 && currentIndex<dialog.entries.length ? dialog.entries[currentIndex] : null
                    // Moving the highlight onto a file names it (nothing is chosen). Read from the index itself:
                    // the binding of `current` may not have followed it yet when this handler runs.
                    onCurrentIndexChanged: {
                        const entry=currentIndex>=0 && currentIndex<dialog.entries.length ? dialog.entries[currentIndex] : null
                        if(!dialog.placing && dialog.mode!=="folder" && entry && !entry.folder)fileName.text=entry.name
                    }
                    Keys.onReturnPressed: dialog.use(current,true)
                    Keys.onEnterPressed: dialog.use(current,true)
                    Keys.onRightPressed: if(current && current.folder && !LayoutMirroring.enabled)dialog.go(current.path)
                    Keys.onLeftPressed: if(LayoutMirroring.enabled) { if(current && current.folder)dialog.go(current.path) } else dialog.goUp()
                    Keys.onPressed: event => { if(event.key===Qt.Key_Backspace) { dialog.goUp(); event.accepted=true } }
                    Accessible.role: Accessible.List
                    Accessible.name: dialog.title
                    delegate: ItemDelegate {
                        id: row
                        required property var modelData
                        required property int index
                        objectName: dialog.objectName+(modelData.folder ? "Folder-"+index : "File-"+(index-(dialog.listing.folders || []).length))
                        width: ListView.view.width
                        implicitHeight: Math.max(dialog.rowHeight,rowContent.implicitHeight+topPadding+bottomPadding)
                        leftPadding: Theme.gapMd; rightPadding: Theme.gapMd
                        topPadding: Theme.gapXs; bottomPadding: Theme.gapXs
                        focusPolicy: Qt.NoFocus
                        highlighted: ListView.isCurrentItem
                        readonly property bool current: !!dialog.markPath && (modelData.path===dialog.markPath || modelData.target===dialog.markPath)
                        onClicked: { folders.currentIndex=index; dialog.use(modelData,false) }
                        onDoubleClicked: if(!modelData.folder) { folders.currentIndex=index; dialog.use(modelData,true) }
                        Accessible.name: modelData.link ? modelData.name+" → "+modelData.target : modelData.name
                        background: Rectangle {
                            color: row.highlighted && folders.activeFocus ? Theme.selected : row.down || row.hovered ? Theme.hover : row.highlighted ? Theme.hover : "transparent"
                            Rectangle { visible: row.highlighted; width: 3; height: parent.height; color: Theme.accent; x: row.mirrored ? parent.width-width : 0 }
                        }
                        contentItem: RowLayout {
                            id: rowContent
                            spacing: Theme.gapSm
                            StudioIcon { name: row.modelData.folder ? "files" : "file"; color: row.current ? Theme.accentInk : Theme.muted; Layout.alignment: Qt.AlignTop; Layout.topMargin: Math.max(0,(nameText.lineHeightPx-height)/2) }
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 0
                                // The whole name, on as many lines as it needs, as plain text.
                                Text {
                                    id: nameText
                                    objectName: dialog.namePrefix+"Name"
                                    readonly property real lineHeightPx: Math.ceil(nameMetrics.lineSpacing)
                                    FontMetrics { id: nameMetrics; font: nameText.font }
                                    Layout.fillWidth: true
                                    text: row.modelData.name
                                    textFormat: Text.PlainText
                                    // At the start side, beside the icon, whatever the name's own direction.
                                    horizontalAlignment: Text.AlignLeft
                                    wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                                    color: Theme.text
                                    font.pixelSize: dialog.bodySize
                                    font.weight: row.current ? Font.DemiBold : Font.Normal
                                }
                                // Where a symbolic link leads.
                                Text {
                                    objectName: dialog.namePrefix+"Link"
                                    Layout.fillWidth: true
                                    visible: !!row.modelData.link
                                    text: "→ "+(row.modelData.target || "")
                                    textFormat: Text.PlainText
                                    horizontalAlignment: Text.AlignLeft
                                    wrapMode: Text.WrapAnywhere
                                    color: Theme.muted
                                    font.family: Theme.mono
                                    font.pixelSize: dialog.smallSize
                                }
                            }
                            StudioIcon { visible: row.modelData.folder; name: "chevronRight"; color: Theme.faint; Layout.alignment: Qt.AlignVCenter }
                        }
                    }
                    // An empty or unreadable folder says so instead of showing nothing.
                    Text {
                        objectName: dialog.objectName+"Status"
                        anchors.top: parent.top; anchors.left: parent.left; anchors.right: parent.right
                        anchors.margins: Theme.gapMd
                        visible: !folders.count
                        text: dialog.listing.error ? qsTranslate("Backend","%1 cannot be read.").arg(dialog.isolate(dialog.listing.path))
                            : dialog.mode==="folder" ? qsTranslate("ProjectFolderDialog","No folders here") : qsTr("No files here")
                        textFormat: Text.PlainText
                        wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                        color: dialog.listing.error ? Theme.danger : Theme.muted
                        font.pixelSize: dialog.smallSize
                    }
                }
            }
            // The file name (open and save): a name in the shown folder, or a whole path. Enter chooses.
            ColumnLayout {
                id: fileBlock
                Layout.fillWidth: true
                visible: dialog.mode!=="folder"
                spacing: Theme.gapXxs
                Text {
                    objectName: dialog.objectName+"FileNameLabel"
                    Layout.fillWidth: true
                    text: qsTr("File name")
                    wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                    color: Theme.muted
                    font.pixelSize: dialog.smallSize
                }
                StudioScrollView {
                    objectName: dialog.objectName+"FileNameScroll"
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(fileName.implicitHeight,fileName.lineHeight*fileName.lines+fileName.topPadding+fileName.bottomPadding)
                    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    PathField {
                        id: fileName
                        objectName: dialog.objectName+"FileName"
                        placeholderText: qsTr("File name")
                        onSubmitted: dialog.choose()
                        onActiveFocusChanged: if(activeFocus)Qt.callLater(dialog.reveal,fileBlock)
                    }
                }
            }
            // The file type: which files the list shows (folders always show).
            ColumnLayout {
                id: filterBlock
                Layout.fillWidth: true
                visible: dialog.mode!=="folder" && !!dialog.nameFilters && dialog.nameFilters.length>0
                spacing: Theme.gapXxs
                Text {
                    Layout.fillWidth: true
                    text: qsTr("File type")
                    wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                    color: Theme.muted
                    font.pixelSize: dialog.smallSize
                }
                StudioComboBox {
                    id: filter
                    objectName: dialog.objectName+"Filter"
                    Layout.fillWidth: true
                    model: dialog.nameFilters
                    currentIndex: Math.max(0,Math.min(dialog.filterIndex,count-1))
                    font.pixelSize: dialog.bodySize
                    implicitHeight: Math.max(dialog.sheet ? Math.max(Theme.mobileTouch,Theme.controlHeight) : Theme.controlHeight,filterLabel.implicitHeight+Theme.gapSm*2)
                    // The whole filter, wrapped, and every choice whole in the list.
                    contentItem: Text {
                        id: filterLabel
                        text: dialog.filterText(filter.displayText)
                        textFormat: Text.PlainText
                        color: filter.enabled ? Theme.text : Theme.faint
                        font: filter.font
                        verticalAlignment: Text.AlignVCenter
                        wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                    }
                    delegate: ItemDelegate {
                        id: choice
                        required property int index
                        width: filter.popup.width-filter.popup.leftPadding-filter.popup.rightPadding
                        implicitHeight: Math.max(dialog.sheet ? Theme.mobileTouch : Theme.controlHeight,choiceText.implicitHeight+Theme.gapSm*2)
                        padding: 0; leftPadding: Theme.gapSm; rightPadding: Theme.gapSm
                        text: dialog.filterText(filter.textAt(index))
                        highlighted: filter.highlightedIndex===index
                        contentItem: Text { id: choiceText; text: choice.text; textFormat: Text.PlainText; font: filter.font; color: Theme.text; verticalAlignment: Text.AlignVCenter; wrapMode: Text.WrapAtWordBoundaryOrAnywhere }
                        background: Rectangle { radius: Theme.radiusSmall; color: choice.highlighted || choice.hovered ? Theme.hover : "transparent" }
                    }
                    // Its list stays inside the window.
                    Binding { target: filter.popup; property: "margins"; value: Theme.gapSm }
                    onActivated: index => { dialog.filterIndex=index; dialog.go(dialog.listing.path) }
                    Accessible.name: qsTr("File type")
                }
            }
            Text {
                id: problemText
                objectName: dialog.objectName+"Problem"
                Layout.fillWidth: true
                visible: !!dialog.problem
                text: dialog.problem
                textFormat: Text.PlainText
                wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                color: Theme.danger
                font.pixelSize: dialog.smallSize
            }
        }
    }

    // Saving over an existing file asks first; No and Escape keep the chooser and the name.
    property Dialog replaceQuestion: StudioDialog {
        id: replace
        objectName: dialog.objectName+"Replace"
        property string path: ""
        parent: Overlay.overlay
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        sheet: dialog.sheet
        standardButtons: Dialog.Yes | Dialog.No
        width: Math.min(dialog.roomWidth-(dialog.sheet ? Theme.mobileGutter : Theme.gapXl)*2,Theme.px(560))
        x: dialog.insetLeft+Math.round((dialog.roomWidth-width)/2)
        y: dialog.insetTop+Math.max(0,Math.round((dialog.roomHeight-height)/2))
        contentItem: Text {
            objectName: dialog.objectName+"ReplaceText"
            text: qsTr("%1 already exists. Do you want to replace it?").arg(dialog.isolate(replace.path))
            textFormat: Text.PlainText
            wrapMode: Text.WrapAtWordBoundaryOrAnywhere
            color: Theme.text
            font.pixelSize: dialog.bodySize
        }
        // The safe answer has the focus, with its ring showing.
        onOpened: { const no=standardButton(Dialog.No); if(no)no.forceActiveFocus(Qt.TabFocusReason) }
        onAccepted: dialog.finish(path)
        onRejected: fileName.forceActiveFocus()
    }
}
