import QtQuick
import "."
import QtQuick.Controls
import QtQuick.Layouts
StudioScrollView {
    // The form keeps clear of the overlay scroll bar at the right edge.
    clip: true; rightPadding: 16; contentWidth: availableWidth
    id: view
    required property var workspace
    property string result: ""
    property var requests: ({})
    function call(op,args) {let id=workspace.request(op,args);requests[id]=true}
    Connections { target: Agent; function onApiFinished(id,op,response) {if(!view.requests[id])return;delete view.requests[id];view.result=JSON.stringify(response.ok?response.result:response.error,null,2)} }
    ColumnLayout {
    width: Math.min(parent.width-Theme.gapLg,1240); spacing: Theme.gapXs
    StudioPanel {
        Layout.fillWidth: true
        title: qsTr("Sign your app")
        lead: qsTr("Your keys stay on this computer. Nothing is released until the check passes.")
        RowLayout { Layout.fillWidth: true; spacing: Theme.gapXs
            StudioTextField { id: key; Layout.fillWidth: true; placeholderText: "Keystore (.p12 / .jks)" }
            StudioTextField { id: alias; Layout.preferredWidth: 160; placeholderText: qsTr("Key alias") }
        }
        StudioTextField { id: password; Layout.fillWidth: true; placeholderText: qsTr("Protected password file · readable by its owner only") }
        StudioTextField { id: input; Layout.fillWidth: true; placeholderText: qsTr("APK / AAB to sign") }
        StudioTextField { id: output; Layout.fillWidth: true; placeholderText: qsTr("New signed output file") }
        Flow {
            Layout.fillWidth: true; spacing: Theme.gapXs
            StudioButton { text: qsTr("Sign & verify"); accent: true; enabled: key.text.length>0 && password.text.length>0 && alias.text.length>0 && input.text.length>0 && output.text.length>0; onClicked: view.call("release.sign",{path:input.text,output:output.text,keystore:key.text,password_file:password.text,alias:alias.text}) }
            StudioButton { text: qsTr("Verify signature"); enabled: input.text.length>0; onClicked: view.call("release.verify",{path:input.text}) }
            StudioButton { text: qsTr("Create new keystore"); quiet: true; enabled: key.text.length>0 && password.text.length>0 && alias.text.length>0; onClicked: view.call("release.keystore",{output:key.text,password_file:password.text,alias:alias.text}) }
        }
    }
    StudioPanel {
        Layout.fillWidth: true
        title: qsTr("Version & build number")
        RowLayout { Layout.fillWidth: true; spacing: Theme.gapXs
            StudioTextField { id: version; placeholderText: "1.0.0"; Layout.preferredWidth: 120 }
            StudioSpinBox { id: buildNumber; from: 1; to: 2100000000; value: 1; editable: true; Layout.preferredWidth: 140; Accessible.name: qsTr("Build number") }
            StudioButton { text: qsTr("Set in the open project"); enabled: !!workspace.project.path && version.text.length>0; onClicked: view.call("release.version",{path:workspace.project.path,version:version.text,build_number:buildNumber.value}) }
        }
    }
    StudioPanel {
        Layout.fillWidth: true
        title: "Stores & Apple"
        lead: qsTr("Google Play needs an authorized service account. Apple needs a Mac with Xcode and your signing keys.")
        Flow { Layout.fillWidth: true; spacing: Theme.gapXs
            StudioButton { text: qsTr("Prepare Play upload"); onClicked: workspace.showAction("play.prepare") }
            StudioButton { text: qsTr("Commit Play change"); onClicked: workspace.showAction("play.commit") }
            StudioButton { text: qsTr("Build iOS archive"); onClicked: workspace.showAction("apple.archive") }
            StudioButton { text: qsTr("Export IPA"); onClicked: workspace.showAction("apple.export") }
            StudioButton { text: "App Store Upload"; onClicked: workspace.showAction("apple.store") }
        }
    }
    StudioCodeView { Layout.fillWidth: true; Layout.preferredHeight: 180; visible: !!view.result; text: view.result }
    }
}
