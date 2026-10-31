import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// The first start: what is left to set up on this computer, from doctor's
// next_steps in plain words, and the way to connect a coding agent. Shown once
// by itself; the command palette opens it again.
StudioDialog {
    id: sheet
    objectName: "onboardingSheet"
    title: qsTr("Welcome to Tomacco")
    modal: true
    property var steps: []
    property bool checking: false
    property bool copied: false
    property int request: -1
    // The doctor's steps say what to run for an agent; these say what it means for a person.
    function plain(step) {
        const what=step.what || ""
        if(step.goal==="desktop")return qsTr("This system lacks libraries the desktop needs. Your agent can still use Tomacco through tomaccoctl.")
        if(step.goal==="android" && what.indexOf("JDK")>=0)return qsTr("Install Java (JDK 17 or newer) and Gradle to build Android apps.")
        if(step.goal==="android")return qsTr("Install the Android SDK and an emulator. This downloads about 5 GB.")
        if(step.goal==="ios")return qsTr("Set up iOS builds. This needs an Apple account.")
        if(step.goal==="virtual_ios")return qsTr("Optional: the virtual iPhone. It needs your own iPhone restore image, about 100 GiB of disk space and a graphics card with 16 GiB.")
        return what
    }
    function check() {
        checking=true;steps=[]
        request=Agent.request("doctor",{})
    }
    onAboutToShow: {copied=false;Studio.saveSetting("onboarding/shown",true);check()}
    Connections {
        target: Agent
        function onApiFinished(id,op,response) {
            if(id!==sheet.request)return
            sheet.request=-1;sheet.checking=false
            sheet.steps=response.ok?(response.result.next_steps || []).map(step=>sheet.plain(step)):[]
        }
    }
    contentItem: StudioScrollView {
        clip: true; contentWidth: availableWidth
        ColumnLayout {
            width: parent.width-Theme.gapMd; spacing: Theme.gapMd
            Text {Layout.fillWidth:true;text:qsTr("Tomacco lets your coding agent build, run and test Android and iOS apps on this computer.");color:Theme.text;font.pixelSize:Theme.fontBody;wrapMode:Text.WordWrap;lineHeight:1.3}
            StudioStatus {objectName:"onboardingChecking";visible:sheet.checking;tone:"busy";text:qsTr("Checking this computer…")}
            Text {visible:!sheet.checking && sheet.steps.length>0;Layout.fillWidth:true;text:qsTr("What is left to set up:");color:Theme.muted;font.pixelSize:Theme.fontSmall}
            Repeater {
                model: sheet.checking ? [] : sheet.steps
                delegate: Text {
                    required property var modelData
                    required property int index
                    objectName: "onboardingStep"
                    Layout.fillWidth: true
                    text: (index+1)+"  "+modelData
                    color: Theme.text; font.pixelSize: Theme.fontSmall; wrapMode: Text.WordWrap; textFormat: Text.PlainText
                }
            }
            StudioStatus {objectName:"onboardingReady";visible:!sheet.checking && sheet.steps.length===0;tone:"ok";text:qsTr("Everything for Android and iOS builds is in place.")}
            Text {Layout.fillWidth:true;text:qsTr("Your agent can do these steps for you. Connect it, then ask it to set Tomacco up.");color:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            Flow {
                Layout.fillWidth: true; spacing: Theme.gapSm
                StudioButton {
                    objectName: "onboardingConnect"; accent: true
                    text: sheet.copied ? qsTr("Copied") : qsTr("Connect your coding agent")
                    iconName: sheet.copied ? "check" : "copy"
                    onClicked: {Studio.copy(JSON.stringify({mcpServers:{[Agent.cliPath.split("/").pop()]:{command:Agent.cliPath,args:["mcp"]}}},null,2));sheet.copied=true}
                }
                StudioButton {objectName:"onboardingSkip";text:qsTr("Skip");quiet:true;onClicked:sheet.close()}
            }
            Text {objectName:"onboardingCopied";Layout.fillWidth:true;visible:sheet.copied;text:qsTr("The MCP configuration is on the clipboard. Paste it into your agent's MCP settings.");color:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
        }
    }
}
