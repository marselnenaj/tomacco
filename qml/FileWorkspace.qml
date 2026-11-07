import QtQuick
import "."
import QtQuick.Controls
import QtQuick.Layouts
Item {
    id: view
    objectName: "fileWorkspace"
    required property var workspace
    property string root: workspace.project.path || ""
    property var entries: []
    property var expanded: ({})
    property var documents: ({})
    property var tabs: []
    property string current: ""
    property string loadedRoot: ""
    property var projectDocuments: ({})
    property int generation: 0
    property var requests: ({})
    property var matches: []
    property bool searching: false
    property bool updating: false
    property int selectedLine: 0
    property string notice: ""
    // A phone shows one pane at a time: the files and search (browse) or the open file.
    // Kept as state, so the same file comes back after a wider window or another section.
    readonly property bool phone: workspace.phone===true
    property string phonePane: "browse"
    property bool listing: false
    property string lastQuery: ""
    readonly property string rootName: root ? root.replace(/\/+$/,"").split("/").pop() : ""
    function fileName(file) { return String(file || "").split("/").pop() }
    function folderOf(file) { const parts=String(file || "").split("/");parts.pop();return parts.join("/") }
    function copyPath() { if(current){Studio.copy(root.replace(/\/+$/,"")+"/"+current);notice=qsTr("Path copied")} }
    // The browser and its controls use the same scale as the source text.
    // Below their combined readable width, the browser moves above the editor.
    readonly property bool sideBySide: width>=Theme.px(640)
    readonly property real browserWidth: Math.max(Theme.px(240),Math.min(Theme.px(280),width*.34))
    readonly property real browserStackMinimumHeight: browserSearch.implicitHeight+browserOptions.implicitHeight+browserActions.implicitHeight+Theme.gapSm*4+Theme.px(72)
    // A phone's file pane: its bar, its actions and at least 120 logical px of code; a shorter window scrolls the pane.
    readonly property real phoneEditorMinimumHeight: filePhoneBar.implicitHeight+filePhoneActions.implicitHeight+Theme.gapSm*2+1+Theme.px(120)+(fileStatus.visible?fileStatus.implicitHeight+Theme.gapXs*2+1:0)+2
    readonly property real editorReadableMinimumHeight: (tabs.length?Theme.controlHeight+1:0)+editorToolbar.implicitHeight+Theme.gapSm*2+(fileStatus.visible?fileStatus.implicitHeight+Theme.gapXs*2+1:0)+Theme.px(120)+2
    function dirtyFiles() { return Object.keys(documents).filter(file=>documents[file].dirty) }
    function hasUnsaved() {
        if(dirtyFiles().length)return true
        for(let project in projectDocuments)if(project!==root && Object.keys(projectDocuments[project].documents).some(file=>projectDocuments[project].documents[file].dirty))return true
        return false
    }
    function request(op,args,context) { let id=workspace.request(op,args,true);requests[id]=Object.assign({operation:op,root:root,generation:generation},context||{});return id }
    function load(directory,depth) { if(!directory)listing=true;request("files.list",{path:root,directory:directory,hidden:hidden.checked,generated:generated.checked},{directory:directory,depth:depth}) }
    function refresh() { generation++; if(root) { expanded=({});load("",0) } }
    function openFile(file,line) {
        selectedLine=line||0
        if(documents[file]) { activate(file);return }
        request("files.read",{path:root,file:file},{file:file,line:line||0})
    }
    function activate(file) {
        current=file;updating=true;editor.text=documents[file].text;updating=false
        // The file opened last is the phone's detail, also when it was opened in a wider window.
        phonePane="file";if(phone && fileCodeScroll.contentItem)fileCodeScroll.contentItem.contentX=0
        if(selectedLine>0) {let lines=editor.text.split("\n"),pos=0;for(let i=0;i<Math.min(selectedLine-1,lines.length);i++)pos+=lines[i].length+1;editor.select(pos,pos+(lines[selectedLine-1]||"").length);selectedLine=0}
        // A phone opens a file to read it: no keyboard until the text is tapped. A new file is for typing.
        if(!phone || !documents[file].sha256)editor.forceActiveFocus();Qt.callLater(revealEditor)
    }
    function revealEditor() {
        if(sideBySide || !current || !editor.activeFocus || !fileScroll.contentItem)return
        // On a phone the pane is the file alone: show its code, below the bar and the actions.
        const target=phone?editorPane.y+fileCodeScroll.y:editorPane.y
        fileScroll.contentItem.contentY=Math.max(0,Math.min(target,fileLayout.height-fileScroll.availableHeight))
    }
    onSideBySideChanged:Qt.callLater(revealEditor)
    function save() { if(!current)return;request("files.write",{path:root,file:current,text:editor.text,if_match:documents[current].sha256},{file:current,text:editor.text}) }
    function newFile(file) { if(!file.trim()) return; if(!documents[file]) { let d=Object.assign({},documents);d[file]={text:"",saved:"",sha256:"",dirty:true};documents=d;tabs=tabs.concat([file]) }activate(file) }
    function toggle(entry) {
        if(!entry.accessible) {notice=qsTr("The link leads outside this project.");return}
        if(!entry.directory){openFile(entry.file);return}
        let state=Object.assign({},expanded)
        if(state[entry.file]) {delete state[entry.file];entries=entries.filter(x=>!x.file.startsWith(entry.file+"/"));expanded=state}
        else {state[entry.file]=true;expanded=state;load(entry.file,entry.depth+1)}
    }
    function closeFile(file) {
        if(documents[file].dirty){notice=qsTr("Save the file first or discard it with Reload.");return}
        let d=Object.assign({},documents);delete d[file];tabs=tabs.filter(x=>x!==file);documents=d
        if(current===file){current="";updating=true;editor.text="";updating=false;if(tabs.length)activate(tabs[tabs.length-1])}
    }
    onRootChanged: {
        updating=true
        if(loadedRoot)projectDocuments[loadedRoot]={documents:documents,tabs:tabs,current:current}
        current="";tabs=[];documents=({});editor.text="";entries=[];matches=[];searching=false
        loadedRoot=root
        if(projectDocuments[root]) {let saved=projectDocuments[root];documents=saved.documents;tabs=saved.tabs;current=saved.current;if(current)editor.text=documents[current].text}
        updating=false
        if(root)refresh()
    }
    onVisibleChanged: if(visible && root && !entries.length)refresh()
    Connections {
        target: Agent
        function onApiFinished(id,op,response) {
            let c=view.requests[id];if(!c)return;delete view.requests[id]
            if(c.root!==view.root)return
            if(op==="files.list" && !c.directory)view.listing=false
            if(!response.ok){view.notice=response.error.message;return}
            let r=response.result;view.notice=""
            if(op==="files.list") {
                if(c.generation!==view.generation)return
                let children=r.entries.map(x=>Object.assign({},x,{depth:c.depth}))
                if(!c.directory)view.entries=children
                else if(view.expanded[c.directory]) {let index=view.entries.findIndex(x=>x.file===c.directory);let rows=view.entries.slice();rows.splice(index+1,0,...children);view.entries=rows}
                if(r.truncated)view.notice=qsTr("This folder contains more than 2,000 entries. Use search for targeted results.")
            }
            if(op==="files.read") {let d=Object.assign({},view.documents);d[c.file]={text:r.text,saved:r.text,sha256:r.sha256,dirty:false};view.documents=d;if(view.tabs.indexOf(c.file)<0)view.tabs=view.tabs.concat([c.file]);view.selectedLine=c.line||0;view.activate(c.file)}
            if(op==="files.write") {
                let d=Object.assign({},view.documents),doc=d[c.file];doc.sha256=r.sha256;doc.saved=c.text;doc.dirty=doc.text!==c.text;d[c.file]=doc;view.documents=d;view.notice=qsTr("Saved · %1").arg(c.file);view.refresh()
            }
            if(op==="files.search") {view.lastQuery=c.query || "";view.matches=r.matches;view.searching=true;if(r.truncated)view.notice=qsTr("More matches available; narrow the search term.")}
            if(op==="files.mkdir")view.refresh()
        }
    }
    Shortcut { sequence: "Ctrl+S"; enabled: view.visible && view.current.length>0; onActivated: view.save() }
    Shortcut { sequence: "Ctrl+Shift+F"; enabled: view.visible; onActivated: search.forceActiveFocus() }
    StudioScrollView {
        id:fileScroll;objectName:"fileWorkspaceScroll";anchors.fill:parent
        contentWidth:availableWidth;contentHeight:fileLayout.height
    GridLayout {
        id:fileLayout;width:fileScroll.availableWidth
        onHeightChanged:Qt.callLater(view.revealEditor)
        // Only short stacked layouts scroll as a page. The editor still owns
        // its bounded code viewport, regardless of the document's line count.
        height:Math.max(fileScroll.availableHeight,view.phone?(editorPane.visible?view.phoneEditorMinimumHeight:0):view.sideBySide || !view.root?0:browserPane.Layout.preferredHeight+editorPane.Layout.preferredHeight+1)
        columns:view.sideBySide?3:1;columnSpacing:0;rowSpacing:0
        Rectangle {
            id: browserPane;objectName:"fileBrowserPane"
            visible:!view.phone || view.phonePane!=="file" || !view.current
            Layout.preferredWidth:view.sideBySide?view.browserWidth:0;Layout.minimumWidth:0;Layout.fillWidth:!view.sideBySide
            Layout.fillHeight:view.sideBySide || view.phone || !editorPane.visible
            Layout.minimumHeight:view.sideBySide || view.phone?0:view.browserStackMinimumHeight
            Layout.preferredHeight:view.sideBySide || view.phone?-1:Math.max(view.browserStackMinimumHeight,Math.min(view.height*.45,Theme.px(300)))
            Layout.maximumHeight:view.sideBySide || view.phone || !editorPane.visible?-1:Layout.preferredHeight
            color: Theme.surface
            ColumnLayout {
                anchors.fill: parent; spacing: 0
                // On a phone: where the list is, the project and then the folder or search, in full.
                ColumnLayout {
                    objectName:"filePhoneCrumbs";visible:view.phone && !!view.root;Layout.fillWidth:true;Layout.minimumWidth:0
                    Layout.leftMargin:Theme.mobileGutter;Layout.rightMargin:Theme.mobileGutter;Layout.topMargin:Theme.gapMd;spacing:Theme.gapXxs
                    Text {objectName:"filePhoneProject";Layout.fillWidth:true;text:view.rootName;color:Theme.text;font.pixelSize:Theme.mobileFontLead;font.weight:Font.DemiBold;wrapMode:Text.Wrap}
                    Text {objectName:"filePhoneRoot";Layout.fillWidth:true;text:view.searching?qsTr("Search results for “%1” · %n match(es)","",view.matches.length).arg(view.lastQuery):view.root;color:Theme.muted;font.family:view.searching?Theme.sans:Theme.mono;font.pixelSize:Theme.mobileFontMeta;wrapMode:view.searching?Text.Wrap:Text.WrapAnywhere}
                }
                RowLayout { id:browserSearch;Layout.fillWidth:true;Layout.minimumWidth:0;Layout.margins:Theme.gapSm;spacing:Theme.gapXs
                    StudioTextField { id: search;objectName:"fileSearchField";Layout.minimumWidth:0; Layout.fillWidth: true; implicitWidth: Theme.px(80); placeholderText: qsTr("Search source code…");onActiveFocusChanged:if(activeFocus && !view.sideBySide && fileScroll.contentItem)fileScroll.contentItem.contentY=0; onAccepted: {if(text.trim())view.request("files.search",{path:view.root,query:text},{query:text})} }
                    StudioButton { objectName:"fileReloadList"; iconName: "refresh"; quiet: true; touch: view.phone; text: view.searching?qsTr("Show all files"):qsTr("Reload files"); displayText: ""; tip: text; onClicked: {view.searching=false;view.refresh()} }
                }
                Flow { id:browserOptions;objectName:"fileBrowserOptions";Layout.fillWidth:true;Layout.minimumWidth:0;Layout.leftMargin:Theme.gapMd;Layout.rightMargin:Theme.gapSm;spacing:Theme.gapSm
                    StudioCheckBox { id: hidden; height: view.phone?Math.max(implicitHeight,Theme.mobileTouch):implicitHeight; text: qsTr("Hidden"); onToggled: view.refresh(); font.pixelSize: Theme.fontSmall }
                    StudioCheckBox { id: generated; height: view.phone?Math.max(implicitHeight,Theme.mobileTouch):implicitHeight; text: "Builds"; onToggled: view.refresh(); font.pixelSize: Theme.fontSmall }
                }
                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.subtle }
                ListView {
                    id: entryList; objectName: "fileList"
                    Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                    // Tab reaches the tree; arrows move, Enter or Space opens.
                    activeFocusOnTab: true
                    Keys.onReturnPressed: if(currentItem)currentItem.clicked()
                    Keys.onSpacePressed: if(currentItem)currentItem.clicked()
                    Accessible.role: Accessible.List; Accessible.name: view.searching?qsTr("Search results"):qsTr("Project files")
                    model: view.searching ? view.matches : view.entries
                    // A phone row shows the whole name and, for a match, its folder and line; nothing is cut.
                    delegate: view.phone ? phoneEntry : desktopEntry
                    Component {
                        id: phoneEntry
                        MobileRow {
                            required property var modelData
                            objectName: "filePhoneRow-"+modelData.file+(view.searching?":"+modelData.line:"")
                            width: ListView.view ? ListView.view.width : implicitWidth
                            leftPadding: Theme.mobileGutter+(view.searching?0:Math.min((modelData.depth || 0)*Theme.gapLg,Math.max(0,width/3)))
                            showDot: false
                            title: view.searching ? view.fileName(modelData.file)+":"+modelData.line : (modelData.directory?modelData.name+"/":modelData.name)
                            detail: view.searching ? (view.folderOf(modelData.file)?view.folderOf(modelData.file)+"/\n":"")+String(modelData.text || "").trim() : (modelData.accessible===false?qsTr("The link leads outside this project."):"")
                            monoDetail: view.searching
                            meta: !view.searching && modelData.directory ? (view.expanded[modelData.file]?qsTr("Open","folder"):"") : ""
                            chevron: !modelData.directory || view.searching
                            highlighted: modelData.file===view.current
                            Accessible.name: view.searching?modelData.file+":"+modelData.line:modelData.name
                            onClicked: view.searching?view.openFile(modelData.file,modelData.line):view.toggle(modelData)
                        }
                    }
                    Component {
                    id: desktopEntry
                    ItemDelegate {
                        id: entryRow
                        required property var modelData
                        width: ListView.view.width; height: view.searching?Theme.barHeight:Theme.controlSmall
                        padding: 0
                        leftPadding:view.searching?Theme.gapMd:Theme.gapMd+Math.min(modelData.depth*Theme.gapMd,Math.max(0,entryList.width/3))
                        rightPadding: Theme.gapSm
                        highlighted: modelData.file===view.current
                        Accessible.name: view.searching?modelData.file+":"+modelData.line:modelData.name
                        background: Rectangle {
                            color: entryRow.highlighted?Theme.selected:entryRow.hovered?Theme.hover:"transparent"
                            border.width: entryRow.ListView.isCurrentItem && entryList.activeFocus?2:0; border.color: Theme.accentInk
                            Rectangle { visible: entryRow.highlighted; width: 2; height: parent.height; color: Theme.accent }
                        }
                        contentItem: Column {
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: Theme.gapXxs
                            Row {
                                spacing: Theme.gapXs
                                width: parent.width
                                StudioIcon { visible: !view.searching; width: Theme.gapMd; height: Theme.gapMd; anchors.verticalCenter: parent.verticalCenter; name: entryRow.modelData.directory?(view.expanded[entryRow.modelData.file]?"chevron":"chevronRight"):""; color: Theme.faint }
                                Text { width: parent.width-(view.searching?0:Theme.gapMd+Theme.gapXs); text: view.searching?entryRow.modelData.file+":"+entryRow.modelData.line:entryRow.modelData.name; color: entryRow.modelData.accessible===false?Theme.faint:entryRow.modelData.directory||entryRow.highlighted?Theme.text:Theme.muted; elide: Text.ElideMiddle; font.pixelSize: Theme.fontSmall }
                            }
                            Text { width: parent.width; visible: view.searching; text: entryRow.modelData.text || ""; color: Theme.faint; elide: Text.ElideRight; font.family: Theme.mono; font.pixelSize: Theme.fontMicro }
                        }
                        onClicked: view.searching?view.openFile(modelData.file,modelData.line):view.toggle(modelData)
                    }
                    }
                    // A phone also says when the list is loading, and when a search found nothing.
                    Text { objectName: "filesLoading"; anchors.centerIn: parent; width: parent.width-Theme.gapXl; visible: view.phone && !!view.root && view.listing && !view.searching && !view.entries.length; text: qsTr("Loading files…"); color: Theme.muted; font.pixelSize: Theme.mobileFontSmall; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap }
                    Text { objectName: "filesNoMatch"; anchors.top: parent.top; anchors.left: parent.left; anchors.right: parent.right; anchors.margins: Theme.mobileGutter; visible: view.phone && view.searching && !view.matches.length; text: qsTr("No matches for “%1”.").arg(view.lastQuery); color: Theme.muted; font.pixelSize: Theme.mobileFontSmall; wrapMode: Text.Wrap }
                    Text { objectName: "filesPhoneError"; anchors.top: parent.top; anchors.left: parent.left; anchors.right: parent.right; anchors.margins: Theme.mobileGutter; visible: view.phone && !!view.root && !!view.notice && !view.listing && !view.searching && !view.entries.length; text: view.notice; color: Theme.danger; font.pixelSize: Theme.mobileFontSmall; wrapMode: Text.Wrap }
                    StudioEmptyState { objectName: "filesEmpty"; actionName: "filesChooseProject"; anchors.centerIn: parent; width: parent.width-Theme.gapXl; visible: !view.root; showMark: false; title: ""; body: qsTr("Open a project first"); actionText: qsTr("Choose folder"); actionIcon: "files"; onAction: view.workspace.chooseProjectFolder() }
                    ScrollBar.vertical: StudioScrollBar {}
                }
                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.subtle }
                Flow { id:browserActions;objectName:"fileBrowserActions";Layout.fillWidth:true;Layout.minimumWidth:0;Layout.margins:Theme.gapSm;spacing:Theme.gapXs
                    StudioButton { touch:view.phone;width:Math.min(implicitWidth,parent.width);text:qsTr("New file"); iconName: "plus"; quiet: true; enabled: view.root.length>0; onClicked: {newPath.title=qsTr("New file");newPath.folder=false;newPath.open()} }
                    StudioButton { touch:view.phone;width:Math.min(implicitWidth,parent.width);text:qsTr("Folder"); iconName: "plus"; quiet: true; enabled: view.root.length>0; onClicked: {newPath.title=qsTr("New folder");newPath.folder=true;newPath.open()} }
                }
            }
        }
        Rectangle { Layout.fillHeight:view.sideBySide;Layout.fillWidth:!view.sideBySide;Layout.preferredWidth:view.sideBySide?1:-1;Layout.preferredHeight:view.sideBySide?-1:1;color:Theme.subtle;visible:editorPane.visible }
        ColumnLayout {
            id:editorPane;objectName:"fileEditorPane";visible:view.phone?view.phonePane==="file" && !!view.current:view.sideBySide || !!view.root
            Layout.minimumWidth:0;Layout.fillWidth:true;Layout.fillHeight:true;spacing:0
            Layout.minimumHeight:view.phone?view.phoneEditorMinimumHeight:view.sideBySide?0:view.editorReadableMinimumHeight
            Layout.preferredHeight:view.phone?Math.max(view.phoneEditorMinimumHeight,fileScroll.availableHeight):view.sideBySide?-1:Math.max(view.editorReadableMinimumHeight,fileScroll.availableHeight-browserPane.Layout.preferredHeight-1)
            Layout.maximumHeight:view.sideBySide?-1:Layout.preferredHeight
            // On a phone: the file's name and folder in full, back to the files, then its actions.
            MobileTopBar {
                id:filePhoneBar;objectName:"filePhoneBar";Layout.fillWidth:true;visible:view.phone
                title:(view.documents[view.current] && view.documents[view.current].dirty?"● ":"")+view.fileName(view.current)
                subtitle:view.folderOf(view.current)?view.folderOf(view.current)+"/":view.rootName;monoSubtitle:true
                backText:view.searching?qsTr("Search results"):qsTr("Files");onBack:view.workspace.phoneBack()
            }
            Flow {
                id:filePhoneActions;objectName:"filePhoneActions";visible:view.phone;Layout.fillWidth:true;Layout.minimumWidth:0;Layout.minimumHeight:implicitHeight;Layout.margins:Theme.gapSm;Layout.leftMargin:Theme.mobileGutter;spacing:Theme.gapXs
                StudioButton {objectName:"filePhoneSave";touch:true;accent:true;text:qsTr("Save");enabled:view.current.length>0 && !!view.documents[view.current] && view.documents[view.current].dirty;onClicked:view.save()}
                StudioButton {objectName:"filePhoneCopyPath";touch:true;quiet:true;iconName:"copy";text:qsTr("Copy path");onClicked:view.copyPath()}
                StudioButton {objectName:"filePhoneReload";touch:true;quiet:true;iconName:"refresh";text:qsTr("Reload");enabled:view.current.length>0;onClicked:{if(view.documents[view.current].dirty)reloadDialog.open();else view.request("files.read",{path:view.root,file:view.current},{file:view.current})}}
            }
            // Open files, then the current path with its actions, then the text.
            StudioScrollView { Layout.fillWidth: true; Layout.preferredHeight: view.tabs.length && !view.phone?Theme.controlHeight:0; visible: view.tabs.length>0 && !view.phone; contentWidth: tabRow.implicitWidth; clip: true
                Row { id: tabRow
                    Repeater { model: view.tabs
                        Rectangle {
                            required property string modelData
                            readonly property bool current: view.current===modelData
                            width: tabContent.implicitWidth; height: Theme.controlHeight
                            color: current?Theme.background:"transparent"
                            Rectangle { visible: parent.current; width: parent.width; height: 2; color: Theme.accent }
                            Rectangle { anchors.right: parent.right; width: 1; height: parent.height; color: Theme.subtle }
                            Row { id: tabContent
                                StudioButton { implicitHeight: Theme.controlHeight; quiet: true; size: Theme.fontSmall; text: (view.documents[parent.parent.modelData].dirty?"● ":"")+parent.parent.modelData.split("/").pop(); onClicked: view.activate(parent.parent.modelData) }
                                StudioButton { iconName: "close"; quiet: true; tip: qsTr("Close file"); width: Theme.controlSmall; onClicked: view.closeFile(parent.parent.modelData) }
                            }
                        }
                    }
                }
            }
            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.subtle; visible: view.tabs.length>0 }
            GridLayout {
                id:editorToolbar;objectName:"fileEditorToolbar";visible:!view.phone;Layout.fillWidth:true;Layout.minimumWidth:0;Layout.margins:Theme.gapSm;Layout.leftMargin:Theme.gapMd
                columns:editorPane.width>=Theme.px(560)?2:1;columnSpacing:Theme.gapXs;rowSpacing:Theme.gapXs
                Label {objectName:"fileCurrentPath";text:view.current || qsTr("Choose a file in the project tree");Layout.fillWidth:true;Layout.minimumWidth:0;Layout.preferredWidth:0;elide:view.current?Text.ElideMiddle:Text.ElideRight;color:Theme.muted;font.family:Theme.mono;font.pixelSize:Theme.fontSmall}
                Flow {
                    id:editorActions;objectName:"fileEditorActions";Layout.fillWidth:true;Layout.minimumWidth:0
                    Layout.preferredWidth:reloadFile.implicitWidth+saveFile.implicitWidth+spacing;Layout.maximumWidth:Layout.preferredWidth;Layout.alignment:Qt.AlignRight;spacing:Theme.gapXs
                    StudioButton {id:reloadFile;objectName:"fileReload";width:Math.min(implicitWidth,parent.width);text:qsTr("Reload");tip:text;quiet:true;enabled:view.current.length>0;onClicked:{if(view.documents[view.current].dirty)reloadDialog.open();else view.request("files.read",{path:view.root,file:view.current},{file:view.current})}}
                    StudioButton {id:saveFile;objectName:"fileSave";width:Math.min(implicitWidth,parent.width);text:qsTr("Save · Ctrl+S");tip:text;enabled:view.current.length>0 && !!view.documents[view.current] && view.documents[view.current].dirty;onClicked:view.save()}
                }
            }
            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.subtle }
            StudioScrollView {
                id:fileCodeScroll
                objectName:"fileCodeScroll";Layout.minimumHeight:0
                Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                TextArea {
                    id: editor; objectName: "sourceEditor"; leftPadding: Theme.gapLg; topPadding: Theme.gapMd; bottomPadding: Theme.gapMd; font.family: Theme.mono; font.pixelSize: Theme.fontBody; color: Theme.text; selectByMouse: true
                    selectionColor: Theme.accent; selectedTextColor: Theme.accentText
                    wrapMode: TextEdit.NoWrap; readOnly: view.current.length===0
                    background: Rectangle { color: Theme.background }
                    Accessible.name: view.current ? qsTr("Source of %1").arg(view.current) : qsTr("Source editor")
                    onTextChanged: {if(view.updating||!view.current)return;let d=Object.assign({},view.documents),doc=d[view.current];doc.text=text;doc.dirty=text!==doc.saved;d[view.current]=doc;view.documents=d}
                    Keys.onPressed: function(event) { if(event.key===Qt.Key_Tab && !(event.modifiers&Qt.ControlModifier)) {editor.insert(editor.cursorPosition,"    ");event.accepted=true} }
                }
            }
            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.subtle; visible: fileStatus.visible }
            Label { id: fileStatus; visible: !!text; Layout.fillWidth: true; Layout.margins: Theme.gapXs; Layout.leftMargin: Theme.gapMd; text: view.notice || (view.current ? qsTr("Line %1 · UTF-8 · changes are checked on save").arg(editor.text.slice(0,editor.cursorPosition).split("\n").length) : ""); color: Theme.faint; font.family: Theme.mono; font.pixelSize: Theme.fontMicro; wrapMode: Text.Wrap }
        }
    }
    }
    StudioDialog {
        id: newPath; property bool folder: false; modal: true; anchors.centerIn: parent; standardButtons: Dialog.Ok | Dialog.Cancel
        StudioTextField { id: filename; width: Math.min(view.width-Theme.gapXxxl*2,Theme.px(400)); placeholderText: qsTr("Path relative to the project, e.g. lib/example.dart"); selectByMouse: true }
        onOpened: {filename.text="";filename.forceActiveFocus()}
        onAccepted: {if(folder)view.request("files.mkdir",{path:view.root,directory:filename.text},{});else view.newFile(filename.text)}
    }
    StudioDialog { id: reloadDialog; modal: true; anchors.centerIn: parent; title: qsTr("Discard unsaved changes?"); standardButtons: Dialog.Yes | Dialog.Cancel; Label { text: qsTr("The file is read again from disk."); color: Theme.muted } onAccepted: view.request("files.read",{path:view.root,file:view.current},{file:view.current}) }
}
