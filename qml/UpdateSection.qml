import QtQuick
import QtQuick.Layouts
import "."

// The Updates part of the Settings dialog.
ColumnLayout {
    id: section
    property var model
    // What the settings' search finds this section by.
    readonly property var keywords: [qsTr("Updates"),qsTr("Off"),qsTr("Notify"),qsTr("Automatic"),qsTr("Channel"),qsTr("Stable"),qsTr("Pre-release"),qsTr("Check now"),qsTr("Roll back"),"GitHub"]
    spacing: Theme.gapSm
    Text {text:qsTr("Updates");color:Theme.muted;font.pixelSize:Theme.fontSmall}
    StudioSegmented {
        objectName:"updateModePicker";Layout.fillWidth:true;enabled:!section.model.forced;current:section.model.mode
        model:[{id:"off",label:qsTr("Off")},{id:"notify",label:qsTr("Notify")},{id:"auto",label:qsTr("Automatic")}]
        onChosen:id=>section.model.setMode(id)
    }
    // One line per choice, the chosen one stronger.
    Repeater {
        model:[{id:"off",text:qsTr("Off: nothing is sent unless you check now.")},
            {id:"notify",text:qsTr("Notify: checks GitHub once a day and shows a new version at the top of the window.")},
            {id:"auto",text:qsTr("Automatic: also installs it by itself when nothing is running, then offers a restart.")}]
        delegate:Text {
            required property var modelData
            objectName:"updateModeHelp-"+modelData.id
            Layout.fillWidth:true;wrapMode:Text.WordWrap;textFormat:Text.PlainText;font.pixelSize:Theme.fontSmall
            readonly property bool chosen:section.model.mode===modelData.id
            color:chosen?Theme.text:Theme.faint
            text:modelData.text
        }
    }
    Text {text:qsTr("Channel");color:Theme.muted;font.pixelSize:Theme.fontSmall;Layout.topMargin:Theme.gapXs}
    StudioSegmented {
        objectName:"updateChannelPicker";Layout.fillWidth:true;enabled:!section.model.forced;current:section.model.channel
        model:[{id:"stable",label:qsTr("Stable")},{id:"prerelease",label:qsTr("Pre-release")}]
        onChosen:id=>section.model.setChannel(id)
    }
    Text {
        Layout.fillWidth:true;color:Theme.faint;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap
        text:section.model.channel==="prerelease"?qsTr("Also offers test versions before their release. They can have more bugs."):qsTr("Offers only finished releases.")
    }
    Text {objectName:"updateState";Layout.fillWidth:true;color:section.model.error?Theme.danger:Theme.text;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap;textFormat:Text.PlainText;maximumLineCount:4;elide:Text.ElideRight;text:section.model.stateText()}
    Text {objectName:"updateBlocked";Layout.fillWidth:true;visible:!!text;color:Theme.faint;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap;text:section.model.blockedText()}
    Flow {
        Layout.fillWidth:true;spacing:Theme.gapSm
        StudioButton {objectName:"updateCheckNow";text:qsTr("Check now");quiet:true;enabled:!section.model.checking && !section.model.forced;onClicked:section.model.checkNow()}
        StudioButton {objectName:"updateWhatsNew";visible:!!section.model.notesUrl;text:qsTr("What's new ↗");quiet:true;onClicked:Studio.openExternal(section.model.notesUrl)}
        StudioButton {objectName:"updateInstall";visible:section.model.canInstall;enabled:!section.model.working;text:qsTr("Install");onClicked:section.model.install()}
        StudioButton {objectName:"updateRestart";visible:section.model.restartNeeded;text:qsTr("Restart");onClicked:section.model.restartRequested()}
        StudioButton {objectName:"updateRollBack";visible:section.model.canRollBack;enabled:!section.model.working;text:qsTr("Roll back");quiet:true;onClicked:section.model.rollBack()}
    }
}
