import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "OperationTitles.js" as OperationTitles

StudioDialog {
    id: palette
    objectName: "commandPalette"
    required property var workspace
    required property var navigation
    property var commands: []
    signal navigate(string section)
    modal: true
    anchors.centerIn: parent
    width: Math.min(parent.width-Theme.gapXxxl,640)
    padding: 0
    topPadding: 0
    title: ""
    readonly property var results: {
        let query=search.text.toLowerCase().trim()
        let pages=navigation.map(x=>({id:x.id,title:x.label,detail:qsTr("View"),icon:x.icon,key:qsTr("Ctrl")+"+"+x.key,page:true}))
        let actions=commands.map(x=>Object.assign({},x))
        // Operations match by their shown title, their English title and their name.
        let operations=Agent.operations.map(x=>({id:x.name,title:OperationTitles.title(x),detail:x.name,search:x.title || "",icon:"agent",operation:x}))
        return pages.concat(actions).concat(operations).filter(x=>(x.title+" "+x.detail+" "+(x.search || "")).toLowerCase().includes(query)).slice(0,60)
    }
    function choose(index) {
        let entry=results[index]
        if(!entry)return
        close()
        if(entry.page)navigate(entry.id)
        else if(entry.run)entry.run()
        else {workspace.showTab("tools");workspace.chooseOperation(entry.operation)}
    }
    onOpened: {search.text="";list.currentIndex=0;search.forceActiveFocus()}
    contentItem: ColumnLayout {
        spacing: 0
        StudioTextField {
            id: search
            objectName: "commandSearch"
            Layout.fillWidth: true
            placeholderText: qsTr("Search views, commands or tools…")
            font.pixelSize: Theme.fontLead
            implicitHeight: Theme.barHeight
            leftPadding: Theme.gapLg
            background: Item {}
            onTextChanged: list.currentIndex=0
            Keys.onDownPressed: {list.currentIndex=Math.min(list.currentIndex+1,palette.results.length-1);list.positionViewAtIndex(list.currentIndex,ListView.Contain)}
            Keys.onUpPressed: {list.currentIndex=Math.max(0,list.currentIndex-1);list.positionViewAtIndex(list.currentIndex,ListView.Contain)}
            onAccepted: palette.choose(list.currentIndex)
        }
        Rectangle {Layout.fillWidth:true;Layout.preferredHeight:1;color:Theme.subtle}
        ListView {
            id: list
            objectName: "commandResults"
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(palette.parent.height-Theme.gapXxxl*3,Math.min(384,Math.max(Theme.rowHeight*3,contentHeight)))
            Layout.topMargin: Theme.gapXs
            Layout.bottomMargin: Theme.gapXs
            clip: true
            model: palette.results
            keyNavigationEnabled: false
            delegate: ItemDelegate {
                id: row
                required property var modelData
                required property int index
                width: list.width
                height: Theme.controlHeight+Theme.gapXs
                padding: 0
                leftPadding: Theme.gapLg
                rightPadding: Theme.gapLg
                highlighted: list.currentIndex===index
                background: Rectangle {
                    color: row.highlighted || row.hovered ? Theme.hover : "transparent"
                    Rectangle {visible:row.highlighted;width:2;height:parent.height;color:Theme.accent}
                }
                contentItem: RowLayout {
                    spacing: Theme.gapMd
                    StudioIcon {name:row.modelData.icon;color:row.highlighted?Theme.text:Theme.faint}
                    Text {text:row.modelData.title;color:Theme.text;font.pixelSize:Theme.fontBody;elide:Text.ElideRight;Layout.fillWidth:true;Layout.preferredWidth:2}
                    Text {Layout.fillWidth:true;Layout.preferredWidth:1;horizontalAlignment:Text.AlignRight;text:row.modelData.key || row.modelData.detail;color:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontMicro;elide:Text.ElideLeft}
                }
                onClicked: palette.choose(index)
            }
            Text {anchors.centerIn:parent;visible:palette.results.length===0;text:qsTr("No matching views or tools");color:Theme.muted;font.pixelSize:Theme.fontBody}
            ScrollBar.vertical: StudioScrollBar {}
        }
        Rectangle {Layout.fillWidth:true;Layout.preferredHeight:1;color:Theme.subtle}
        Text {Layout.margins:Theme.gapSm;Layout.leftMargin:Theme.gapLg;text:qsTr("↑ ↓  Select     ↵  Open     Esc  Close");color:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontMicro}
    }
}
