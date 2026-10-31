import QtQuick
import "."
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
StudioScrollView {
    // The form keeps clear of the overlay scroll bar at the right edge.
    clip: true; rightPadding: 16; contentWidth: availableWidth
    id: view
    required property var workspace
    property string result: ""
    property var requests: ({})
    function call(op,args) { let id=workspace.request(op,args); requests[id]=true }
    function selector() { let s={}; if(matchText.text.length) s.text=matchText.text; if(resource.text.length) s.resource_id=resource.text; if(description.text.length) s.description=description.text; if(!Object.keys(s).length) throw new Error(qsTr("Enter a text, resource ID or description")); return s }
    function action(kind) { try { let args={device:workspace.selectedDevice,selector:selector()}; if(kind==="find") call("testing.find",args); else {args.action=kind; if(kind==="fill") args.text=inputText.text;call("testing.action",args)} } catch(e) { workspace.status=e.message;workspace.failed=true } }
    Connections { target: Agent; function onApiFinished(id,op,response) { if(!view.requests[id]) return; delete view.requests[id]; view.result=JSON.stringify(response.ok?response.result:response.error,null,2) } }
    ColumnLayout {
    width: Math.min(parent.width-Theme.gapLg,1240); spacing: Theme.gapXs
    StudioPanel {
        Layout.fillWidth: true
        title: qsTr("Test your Android app")
        lead: workspace.selectedDevice || qsTr("Pick an emulator or phone under Devices first.")
        StudioTextField { id: matchText; Layout.fillWidth: true; placeholderText: qsTr("Visible text · exact") }
        StudioTextField { id: resource; Layout.fillWidth: true; placeholderText: qsTr("Resource ID · e.g. app.example:id/submit") }
        StudioTextField { id: description; Layout.fillWidth: true; placeholderText: qsTr("Accessibility description · exact") }
        Flow {
            Layout.fillWidth: true; spacing: Theme.gapXs; enabled: workspace.selectedDevice.length>0 && workspace.selectedPlatform==="android"
            StudioButton { text: qsTr("Find & wait"); accent: true; onClicked: view.action("find") }
            StudioButton { text: qsTr("Tap"); onClicked: view.action("click") }
            StudioButton { text: qsTr("Long press"); onClicked: view.action("long_click") }
            StudioTextField { id: inputText; width: 180; placeholderText: qsTr("Input text") }
            StudioButton { text: qsTr("Fill in"); onClicked: view.action("fill") }
        }
        StudioButton { text: qsTr("Set up text input on the device"); quiet: true; enabled: workspace.selectedDevice.length>0 && workspace.selectedPlatform==="android"; onClicked: view.call("testing.setup",{device:workspace.selectedDevice}) }
    }
    StudioPanel {
        Layout.fillWidth: true
        title: qsTr("Run on several devices")
        lead: qsTr("JUnit results and the failure screenshot stay, even when a step fails.")
        RowLayout { Layout.fillWidth: true; spacing: Theme.gapXs
            StudioTextField { id: scenario; Layout.fillWidth: true; placeholderText: qsTr("JSON scenario") }
            StudioButton { text: qsTr("Choose"); onClicked: scenarioDialog.open() }
        }
        StudioTextField { id: serials; Layout.fillWidth: true; placeholderText: qsTr("Device IDs separated by commas; empty = selected device") }
        StudioTextField { id: output; Layout.fillWidth: true; placeholderText: qsTr("New, empty directory for JUnit, JSON and screenshots") }
        StudioButton { text: qsTr("Start scenario"); iconName: "play"; accent: true; enabled: scenario.text.length>0 && output.text.length>0; onClicked: { let devices=serials.text.trim()?serials.text.split(",").map(x=>x.trim()):[workspace.selectedDevice]; view.call("testing.run",{path:scenario.text,directory:output.text,devices:devices}) } }
    }
    StudioCodeView { Layout.fillWidth: true; Layout.preferredHeight: 180; visible: !!view.result; text: view.result }
    }
    FileDialog { id: scenarioDialog; nameFilters: ["JSON (*.json)"]; onAccepted: scenario.text=decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,"")) }
}
