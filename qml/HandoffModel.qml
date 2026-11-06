import QtQuick

// The handoffs waiting for the person: an agent asked for Face ID, a passcode,
// a sign-in, a purchase or a permission on the device, and waits. The list is
// tomaccoctl's (handoff.list); this only polls it while a window is open and
// answers Done or Cancel through the same operations a terminal would use.
// Nothing here types for the person or gets past a prompt.
Item {
    id: model
    objectName: "handoffModel"
    visible: false
    property var waiting: []
    property string error: ""
    property bool polling: true
    property int interval: 3000
    readonly property var current: waiting.length ? waiting[0] : null
    readonly property int count: waiting.length
    // Every request this model makes, for whoever wants to watch them (the tests do).
    signal asked(string operation,var arguments)
    // A handoff that was not waiting before: a notification once per id.
    signal arrived(var handoff)
    property var requests: ({})
    property var seen: ({})
    function kindText(kind) {
        return ({biometric:qsTr("Face ID or Touch ID"),passcode:qsTr("Passcode"),sign_in:qsTr("Sign-in"),purchase:qsTr("Purchase"),permission:qsTr("Permission")})[kind] || qsTr("Your turn on the device")
    }
    function ask(operation,arguments,then) {
        let id=Agent.request(operation,arguments||{});requests[id]={then:then||""}
        asked(operation,arguments||{})
        return id
    }
    function refresh() { ask("handoff.list",{state:"waiting",limit:20},"list") }
    function complete(id) { if(id)ask("handoff.complete",{id:id},"done") }
    function cancel(id) { if(id)ask("handoff.cancel",{id:id,reason:"cancelled at the desktop"},"done") }
    // The first look comes after one interval: a window that opens has other things to ask first.
    Timer { interval: model.interval; repeat: true; running: model.polling; onTriggered: model.refresh() }
    Connections {
        target: Agent
        function onApiFinished(id,op,response) {
            const request=model.requests[id]
            if(!request)return
            delete model.requests[id]
            if(!response.ok) {
                // A tomaccoctl from before handoffs has no such operation: stay quiet, keep polling.
                if(request.then==="list"){model.waiting=[];return}
                model.error=response.error.message;model.refresh();return
            }
            model.error=""
            if(request.then==="list") {
                const list=response.result.handoffs || []
                for(const h of list) if(!model.seen[h.id]){model.seen[h.id]=true;model.arrived(h)}
                model.waiting=list
            } else model.refresh()
        }
    }
}
