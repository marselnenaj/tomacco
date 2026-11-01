import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "OperationTitles.js" as OperationTitles

// Asks the person whether a destructive call from an agent may run. tomaccoctl
// sends "approve" over this window's agent socket when agent_policy.approval
// .destructive is on, with the operation, the agent's name and the arguments,
// secrets already masked. Only Approve answers yes. Deny, Escape, the countdown
// running out and the request going away are a no. Requests that arrive while
// one is shown wait their turn; each keeps its own deadline.
StudioDialog {
    id: dialog
    objectName: "approvalDialog"
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(parent ? parent.width-Theme.gapXxxl : 560, 560)
    modal: true
    closePolicy: Popup.CloseOnEscape
    title: qsTr("Allow this agent action?")

    property var waiting: []
    property var current: null
    property int remaining: 0
    readonly property var operation: current ? Agent.operations.find(x=>x.name===current.params.operation) || {name:current.params.operation,title:current.params.title} : null
    readonly property string agentName: current && current.params.client ? current.params.client : qsTr("An agent")

    function arrive(id,params) {
        const seconds=Math.max(1,Math.min(30,Number(params.timeout_seconds) || 30))
        waiting=waiting.concat([{id:id,params:params,deadline:Date.now()+seconds*1000}])
        if(!current)next()
    }
    function next() {
        while(waiting.length && waiting[0].deadline<=Date.now()) {
            Agent.complete(waiting[0].id,{approved:false,reason:"timeout"})
            waiting=waiting.slice(1)
        }
        if(!waiting.length){current=null;close();return}
        current=waiting[0];waiting=waiting.slice(1)
        remaining=Math.ceil((current.deadline-Date.now())/1000)
        open()
        denyButton.forceActiveFocus()
    }
    function answer(approved,reason) {
        if(!current)return
        const id=current.id
        current=null
        Agent.complete(id,{approved:approved,reason:reason})
        next()
    }
    function forget(id) {
        waiting=waiting.filter(x=>x.id!==id)
        if(current && current.id===id){current=null;next()}
    }
    onClosed: if(current)answer(false,"denied")

    Timer {
        interval: 250; repeat: true; running: dialog.current!==null
        onTriggered: {
            dialog.remaining=Math.max(0,Math.ceil((dialog.current.deadline-Date.now())/1000))
            if(dialog.remaining<=0)dialog.answer(false,"timeout")
        }
    }
    Connections {
        target: Agent
        function onPreviewRequest(id,method,args) { if(method==="approve")dialog.arrive(id,args) }
        // The request ended without this answer: tomaccoctl gave up or went away.
        function onPreviewFinished(id,error) { if(error)dialog.forget(id) }
    }

    contentItem: ColumnLayout {
        spacing: Theme.gapMd
        Text {
            objectName: "approvalSummary"
            Layout.fillWidth: true
            text: dialog.current ? qsTr("%1 asks to run %2 (%3). Tomacco marks it as destructive: it may delete or overwrite data.").arg(dialog.agentName).arg(OperationTitles.title(dialog.operation)).arg(dialog.current.params.operation) : ""
            color: Theme.text; wrapMode: Text.WordWrap; font.pixelSize: Theme.fontBody; lineHeight: 1.3
        }
        Text {
            Layout.fillWidth: true
            text: qsTr("Its arguments, with secrets masked:")
            color: Theme.muted; wrapMode: Text.WordWrap; font.pixelSize: Theme.fontSmall
        }
        StudioCodeView {
            objectName: "approvalArguments"
            Layout.fillWidth: true
            Layout.preferredHeight: 160
            text: dialog.current ? JSON.stringify(dialog.current.params.arguments || {},null,2) : ""
            accessibleName: qsTr("Arguments")
        }
        Text {
            objectName: "approvalCountdown"
            Layout.fillWidth: true
            text: qsTr("Seconds left before it is denied: %1").arg(dialog.remaining)
            color: Theme.faint; wrapMode: Text.WordWrap; font.pixelSize: Theme.fontSmall
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.gapXs
            Item { Layout.fillWidth: true }
            StudioButton { id: denyButton; objectName: "approvalDeny"; text: qsTr("Deny"); onClicked: dialog.answer(false,"denied") }
            StudioButton { objectName: "approvalApprove"; text: qsTr("Approve"); accent: true; onClicked: dialog.answer(true,"") }
        }
    }
}
