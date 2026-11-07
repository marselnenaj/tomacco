import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// The project folder chooser of every entry point (one instance in NativeWorkspace,
// which Workspace opens through its projectChooser): the app's own dialog, so it
// follows the interface size, theme, language and phone layout. Qt's stock FolderDialog fallback kept a fixed 600x400 window with 16 px
// icons and one-line labels at any interface size, so at 175 % long folder names and
// the path were cut off. Here the location shows whole (wrapped) and can be typed or
// pasted, every folder name wraps, symbolic links name their target, and the dialog
// stays inside its window and scrolls. Open chooses the folder the location names;
// Cancel and Escape leave the current project as it is. Backend::folderListing reads
// the folders.
StudioDialog {
    id: dialog
    // The open project: the chooser starts in the folder that holds it and marks it.
    property string currentProject: ""
    // Free room at the window's edges (status bar, keyboard) a phone sheet keeps clear.
    property real insetLeft: 0
    property real insetTop: 0
    property real insetRight: 0
    property real insetBottom: 0
    // The folder shown and the listing of it (Backend::folderListing).
    property var listing: ({path:"",parent:"",home:"",error:"",folders:[]})
    // The project folder Open would choose: the location's canonical folder, or "" when it is none.
    readonly property string target: visible ? Studio.projectFolder(Studio.resolveFolderPath(location.text,listing.path)) : ""
    readonly property int rowHeight: sheet ? Math.max(Theme.mobileTouch,Theme.controlHeight) : Theme.rowHeight
    // On a narrow dialog the parent and home buttons show only their icons (named for assistive technology).
    readonly property bool narrow: availableWidth < Theme.px(440)
    // The title unless an entry point names its own (openWith).
    property string defaultTitle: ""
    property string caption: ""
    title: caption || defaultTitle
    signal chosen(string path)
    // Opens with an entry point's own title; open() shows defaultTitle.
    function openWith(text) { caption=text || ""; open() }
    onClosed: caption=""

    parent: Overlay.overlay
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    standardButtons: Dialog.Open | Dialog.Cancel
    readonly property real roomWidth: (parent ? parent.width : 0)-insetLeft-insetRight
    readonly property real roomHeight: (parent ? parent.height : 0)-insetTop-insetBottom
    width: sheet ? roomWidth : Math.min(roomWidth-Theme.gapXl*2,Theme.px(760))
    height: sheet ? roomHeight : Math.min(roomHeight-Theme.gapXl*2,Theme.px(640))
    x: insetLeft+Math.round((roomWidth-width)/2)
    y: insetTop+Math.round((roomHeight-height)/2)

    function go(path) {
        const from=listing.path
        const next=Studio.folderListing(path,from,hidden.checked)
        listing=next
        location.text=next.path
        // The folder just left (after going up), otherwise the open project, is the current row.
        let mark=-1
        for(let i=0;i<next.folders.length && mark<0;++i)if(from && next.folders[i].path===from)mark=i
        for(let i=0;i<next.folders.length && mark<0;++i)if(next.folders[i].path===currentProject || next.folders[i].target===currentProject)mark=i
        folders.currentIndex=mark>=0?mark:(next.folders.length?0:-1)
        folders.positionViewAtIndex(Math.max(0,folders.currentIndex),ListView.Contain)
    }
    function goUp() { if(listing.parent)go(listing.parent) }
    function choose() { if(target)accept() }
    function startFolder() {
        const project=currentProject || (Studio.recentProjects.length?Studio.recentProjects[0]:"")
        if(!project)return "~"
        const parentFolder=Studio.folderListing(project).parent
        return parentFolder || project
    }

    // Open is only offered for a folder that can be a project.
    function bindOpen() { const open=standardButton(Dialog.Open); if(open)open.enabled=Qt.binding(()=>dialog.target!=="") }
    onAboutToShow: {
        bindOpen()
        listing={path:"",parent:"",home:"",error:"",folders:[]}
        go(startFolder())
    }
    onOpened: folders.forceActiveFocus()
    onAccepted: { const chosenFolder=target; if(chosenFolder)chosen(chosenFolder) }
    Component.onCompleted: bindOpen()
    Shortcut { sequence: "Ctrl+L"; enabled: dialog.visible; onActivated: { location.forceActiveFocus(); location.selectAll() } }
    Shortcut { sequence: "Alt+Up"; enabled: dialog.visible; onActivated: dialog.goUp() }
    Shortcut { sequences: ["Ctrl+Return","Ctrl+Enter"]; enabled: dialog.visible; onActivated: dialog.choose() }

    // Open and Cancel stay in the footer. Above them, when even one line of the location and
    // two folder rows do not fit (a short window at a large interface size), everything scrolls.
    contentItem: StudioScrollView {
        id: body
        objectName: dialog.objectName+"Body"
        contentWidth: availableWidth
        contentHeight: column.height
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ColumnLayout {
            id: column
            width: body.availableWidth
            height: Math.max(body.availableHeight,implicitHeight)
            spacing: Theme.gapSm
            // The location: the whole path, wrapped onto up to three lines (fewer when the dialog is short) and scrolled beyond them.
            // Typing or pasting a path and pressing Enter shows that folder; Ctrl+Enter or Open chooses it.
            StudioScrollView {
                id: locationScroll
                objectName: dialog.objectName+"LocationScroll"
                readonly property int lines: Math.max(1,Math.min(3,Math.floor((body.availableHeight-tools.height-column.spacing*2-dialog.rowHeight*2-2-location.topPadding-location.bottomPadding)/location.lineHeight)))
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(location.implicitHeight,location.lineHeight*lines+location.topPadding+location.bottomPadding)
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                TextArea {
                    id: location
                    objectName: dialog.objectName+"Location"
                    readonly property real lineHeight: Math.ceil(fontMetrics.lineSpacing)
                    FontMetrics { id: fontMetrics; font: location.font }
                    wrapMode: TextEdit.WrapAnywhere
                    // A path reads left to right also in a right-to-left interface (as StudioTextField machine).
                    horizontalAlignment: LayoutMirroring.enabled ? TextEdit.AlignRight : TextEdit.AlignLeft
                    font.family: Theme.mono
                    font.pixelSize: dialog.sheet ? Theme.mobileFontBody : Theme.fontBody
                    color: Theme.text
                    selectByMouse: true
                    selectionColor: Theme.accent
                    selectedTextColor: Theme.accentText
                    placeholderText: qsTranslate("Workspace","Type the folder of a Swift, Kotlin or Flutter project")
                    placeholderTextColor: Theme.faint
                    leftPadding: Theme.gapMd; rightPadding: Theme.gapMd
                    topPadding: Theme.gapSm; bottomPadding: Theme.gapSm
                    background: Rectangle {
                        radius: Theme.radiusSmall
                        color: Theme.inset
                        border.color: location.activeFocus ? Theme.accentInk : Theme.border
                        border.width: location.activeFocus ? 2 : 1
                    }
                    // One line: a pasted line break would be part of no path.
                    onTextChanged: if(/[\r\n]/.test(text)) { const at=cursorPosition; text=text.replace(/[\r\n]+/g,""); cursorPosition=Math.min(at,length) }
                    Keys.onReturnPressed: event => { if(event.modifiers & Qt.ControlModifier)dialog.choose(); else dialog.go(text); event.accepted=true }
                    Keys.onEnterPressed: event => { if(event.modifiers & Qt.ControlModifier)dialog.choose(); else dialog.go(text); event.accepted=true }
                    // Tab moves on (a path holds no tab character).
                    Keys.onTabPressed: event => { const next=nextItemInFocusChain(true); if(next)next.forceActiveFocus(Qt.TabFocusReason); event.accepted=true }
                    Keys.onBacktabPressed: event => { const previous=nextItemInFocusChain(false); if(previous)previous.forceActiveFocus(Qt.BacktabFocusReason); event.accepted=true }
                    Accessible.name: placeholderText
                    Accessible.role: Accessible.EditableText
                }
            }
            Flow {
                id: tools
                Layout.fillWidth: true
                spacing: Theme.gapXs
                StudioButton {
                    id: parentButton
                    objectName: dialog.objectName+"Parent"
                    text: qsTr("Parent folder"); displayText: dialog.narrow ? "" : text; tip: text
                    iconName: "stepOut"; quiet: true; touch: dialog.sheet
                    enabled: !!dialog.listing.parent
                    onClicked: dialog.goUp()
                }
                StudioButton {
                    objectName: dialog.objectName+"Home"
                    text: qsTr("Home folder"); displayText: dialog.narrow ? "" : text; tip: text
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
                    model: dialog.listing.folders
                    boundsBehavior: Flickable.StopAtBounds
                    keyNavigationEnabled: true
                    highlightMoveDuration: 0
                    activeFocusOnTab: true
                    ScrollBar.vertical: StudioScrollBar {}
                    Keys.onReturnPressed: if(currentItem)dialog.go(currentItem.modelData.path)
                    Keys.onEnterPressed: if(currentItem)dialog.go(currentItem.modelData.path)
                    Keys.onRightPressed: if(currentItem && !LayoutMirroring.enabled)dialog.go(currentItem.modelData.path)
                    Keys.onLeftPressed: if(LayoutMirroring.enabled) { if(currentItem)dialog.go(currentItem.modelData.path) } else dialog.goUp()
                    Keys.onPressed: event => { if(event.key===Qt.Key_Backspace) { dialog.goUp(); event.accepted=true } }
                    Accessible.role: Accessible.List
                    Accessible.name: dialog.title
                    delegate: ItemDelegate {
                        id: row
                        required property var modelData
                        required property int index
                        objectName: dialog.objectName+"Folder-"+index
                        width: ListView.view.width
                        implicitHeight: Math.max(dialog.rowHeight,rowContent.implicitHeight+topPadding+bottomPadding)
                        leftPadding: Theme.gapMd; rightPadding: Theme.gapMd
                        topPadding: Theme.gapXs; bottomPadding: Theme.gapXs
                        focusPolicy: Qt.NoFocus
                        highlighted: ListView.isCurrentItem
                        readonly property bool current: modelData.path===dialog.currentProject || modelData.target===dialog.currentProject
                        onClicked: { folders.currentIndex=index; dialog.go(modelData.path) }
                        Accessible.name: modelData.link ? modelData.name+" → "+modelData.target : modelData.name
                        background: Rectangle {
                            color: row.highlighted && folders.activeFocus ? Theme.selected : row.down || row.hovered ? Theme.hover : row.highlighted ? Theme.hover : "transparent"
                            Rectangle { visible: row.highlighted; width: 3; height: parent.height; color: Theme.accent; x: row.mirrored ? parent.width-width : 0 }
                        }
                        contentItem: RowLayout {
                            id: rowContent
                            spacing: Theme.gapSm
                            StudioIcon { name: "files"; color: row.current ? Theme.accentInk : Theme.muted; Layout.alignment: Qt.AlignTop; Layout.topMargin: Math.max(0,(nameText.lineHeightPx-height)/2) }
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 0
                                // The whole name, on as many lines as it needs.
                                Text {
                                    id: nameText
                                    objectName: "projectFolderName"
                                    readonly property real lineHeightPx: Math.ceil(nameMetrics.lineSpacing)
                                    FontMetrics { id: nameMetrics; font: nameText.font }
                                    Layout.fillWidth: true
                                    text: row.modelData.name
                                    wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                                    color: Theme.text
                                    font.pixelSize: dialog.sheet ? Theme.mobileFontBody : Theme.fontBody
                                    font.weight: row.current ? Font.DemiBold : Font.Normal
                                }
                                // Where a symbolic link leads.
                                Text {
                                    objectName: "projectFolderLink"
                                    Layout.fillWidth: true
                                    visible: !!row.modelData.link
                                    text: "→ "+(row.modelData.target || "")
                                    wrapMode: Text.WrapAnywhere
                                    color: Theme.muted
                                    font.family: Theme.mono
                                    font.pixelSize: dialog.sheet ? Theme.mobileFontSmall : Theme.fontSmall
                                }
                            }
                            StudioIcon { name: "chevronRight"; color: Theme.faint; Layout.alignment: Qt.AlignVCenter }
                        }
                    }
                    // An empty or unreadable folder says so instead of showing nothing.
                    Text {
                        objectName: dialog.objectName+"Status"
                        anchors.top: parent.top; anchors.left: parent.left; anchors.right: parent.right
                        anchors.margins: Theme.gapMd
                        visible: !folders.count
                        text: dialog.listing.error ? qsTranslate("Backend","%1 cannot be read.").arg(dialog.listing.path) : qsTr("No folders here")
                        wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                        color: dialog.listing.error ? Theme.danger : Theme.muted
                        font.pixelSize: dialog.sheet ? Theme.mobileFontSmall : Theme.fontSmall
                    }
                }
            }
        }
    }
}
