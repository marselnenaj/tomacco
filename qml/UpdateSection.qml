import QtQuick
import QtQuick.Layouts
import "."

// The Updates part of the Settings dialog.
ColumnLayout {
    id: section
    property var model
    spacing: Theme.gapSm
    Text {text:qsTr("Updates");color:Theme.muted;font.pixelSize:Theme.fontSmall}
    StudioSegmented {
        objectName:"updateModePicker";Layout.fillWidth:true;enabled:!section.model.forced;current:section.model.mode
        model:[{id:"off",label:qsTr("Off")},{id:"notify",label:qsTr("Notify")},{id:"auto",label:qsTr("Automatic")}]
        onChosen:id=>section.model.setMode(id)
    }
    Text {
        Layout.fillWidth:true;color:Theme.faint;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap
        text:section.model.mode==="off"?qsTr("Nothing is sent unless you check now."):section.model.mode==="auto"?qsTr("Checks GitHub once a day and installs by itself when nothing is running."):qsTr("Checks GitHub once a day and tells you.")
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
