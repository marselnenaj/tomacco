import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "OperationTitles.js" as OperationTitles
import "Fuzzy.js" as Fuzzy

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
    // Everything the palette finds: views, commands and every operation, each with a key of its own.
    readonly property var entries: {
        let pages=navigation.map(x=>({key:"page:"+x.id,id:x.id,title:x.label,detail:qsTr("View"),icon:x.icon,key2:qsTr("Ctrl")+"+"+x.key,page:true}))
        let actions=commands.map(x=>Object.assign({},x,{key:"command:"+x.id,key2:x.key}))
        // Operations match by their shown title, their English title and their name.
        let operations=Agent.operations.map(x=>({key:"operation:"+x.name,id:x.name,title:OperationTitles.title(x),detail:x.name,search:x.title || "",icon:"agent",operation:x}))
        return pages.concat(actions).concat(operations)
    }
    // What was chosen lately, newest first; it comes first and ranks higher.
    property var recent: { try { const value=JSON.parse(Studio.setting("palette/recent","[]"));return Array.isArray(value)?value:[] } catch(e) { return [] } }
    readonly property string query: search.text.trim()
    readonly property var results: {
        const all=entries
        if(!query) {
            const first=recent.map(key=>all.find(x=>x.key===key)).filter(x=>!!x).map(x=>Object.assign({},x,{recent:true}))
            return first.concat(all.filter(x=>recent.indexOf(x.key)<0)).slice(0,60)
        }
        let scored=[]
        for(const entry of all) {
            let score=Fuzzy.score(query,[[entry.title,1],[entry.detail,.8],[entry.search || "",.9]])
            if(score<0)continue
            const rank=recent.indexOf(entry.key)
            if(rank>=0)score+=40*(1-rank/10)
            scored.push({entry:entry,score:score,recent:rank>=0})
        }
        scored.sort((a,b)=>b.score-a.score)
        return scored.slice(0,60).map(x=>Object.assign({},x.entry,{recent:x.recent}))
    }
    function remember(key) {
        let keys=[key].concat(recent.filter(x=>x!==key)).slice(0,8)
        recent=keys
        Studio.saveSetting("palette/recent",JSON.stringify(keys))
    }
    function choose(index) {
        let entry=results[index]
        if(!entry)return
        remember(entry.key)
        close()
        if(entry.page)navigate(entry.id)
        else if(entry.run)entry.run()
        else {workspace.showTab("tools");workspace.chooseOperation(entry.operation)}
    }
    function move(step) {list.currentIndex=Math.max(0,Math.min(list.currentIndex+step,results.length-1));list.positionViewAtIndex(list.currentIndex,ListView.Contain)}
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
            Keys.onDownPressed: palette.move(1)
            Keys.onUpPressed: palette.move(-1)
            Keys.onPressed: event=>{
                if(event.key===Qt.Key_PageDown){palette.move(8);event.accepted=true}
                else if(event.key===Qt.Key_PageUp){palette.move(-8);event.accepted=true}
            }
            onAccepted: palette.choose(list.currentIndex)
        }
        Rectangle {Layout.fillWidth:true;Layout.preferredHeight:1;color:Theme.subtle}
        ListView {
            id: list
            objectName: "commandResults"
            // Read in the rows, where "palette" names a control's colours.
            readonly property string query: palette.query
            function choose(index) { palette.choose(index) }
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
                    // The letters the query matched stand out.
                    Text {objectName:"commandTitle";text:list.query?Fuzzy.highlight(row.modelData.title,list.query,Theme.accentInk):row.modelData.title;textFormat:list.query?Text.StyledText:Text.PlainText;color:Theme.text;font.pixelSize:Theme.fontBody;elide:Text.ElideRight;Layout.fillWidth:true;Layout.preferredWidth:2}
                    Text {objectName:"commandRecent";visible:!!row.modelData.recent && !list.query;text:qsTr("Recent");color:Theme.accentInk;font.family:Theme.mono;font.pixelSize:Theme.fontMicro}
                    Text {Layout.fillWidth:true;Layout.preferredWidth:1;horizontalAlignment:Text.AlignRight;text:row.modelData.key2 || row.modelData.detail;color:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontMicro;elide:Text.ElideLeft}
                }
                onClicked: list.choose(index)
            }
            Text {anchors.centerIn:parent;visible:palette.results.length===0;text:qsTr("No matching views or tools");color:Theme.muted;font.pixelSize:Theme.fontBody}
            ScrollBar.vertical: StudioScrollBar {}
        }
        Rectangle {Layout.fillWidth:true;Layout.preferredHeight:1;color:Theme.subtle}
        Text {Layout.margins:Theme.gapSm;Layout.leftMargin:Theme.gapLg;text:qsTr("↑ ↓  Select     PgUp PgDn  Page     ↵  Open     Esc  Close");color:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontMicro}
    }
}
