import QtQuick

// The desktop's view of updates. The decisions are tomaccoctl's: whether a check
// may use the network, whether an installation is allowed now. This only asks,
// in the background, so that nothing here can delay the start.
Item {
    id: model
    objectName: "updateModel"
    visible: false
    property var status: ({})
    property bool checking: false
    property bool working: false
    property string error: ""
    signal restartRequested()
    // Every request this model makes, for whoever wants to watch them (the tests do).
    signal asked(string operation,var arguments)
    property var requests: ({})
    readonly property string mode: status.mode || "notify"
    // stable: GitHub's latest release; prerelease: pre-releases too.
    readonly property string channel: status.channel || "stable"
    readonly property bool forced: !!status.forced
    readonly property bool installing: !!status.job
    readonly property bool updateAvailable: status.update_available===true
    readonly property bool canInstall: updateAvailable && status.can_apply===true && !installing && !restartNeeded
    readonly property bool canRollBack: status.rollback_available===true && !installing
    readonly property string notesUrl: status.notes_url || ""
    readonly property string latest: status.latest || ""
    // The control program is already the new version while this desktop is not.
    readonly property bool restartNeeded: !!status.install && status.install.kind==="user_install" && !!status.current && !!Qt.application.version && status.current!==Qt.application.version
    // The version whose banner Later put away; a newer one shows it again.
    property string dismissed: String(Studio.setting("updates/dismissedVersion",""))
    readonly property string bannerKind: !updateAvailable || restartNeeded || latest===dismissed ? ""
        : installing ? "installing"
        : mode!=="off" ? "available" : ""
    function later() {
        dismissed=latest
        Studio.saveSetting("updates/dismissedVersion",dismissed)
    }
    readonly property string notice: restartNeeded?qsTr("Updated · restart"):installing?qsTr("Installing update"):updateAvailable && mode!=="off"?qsTr("Update %1").arg(latest):""

    function ask(operation,arguments,quiet,then) {
        let id=Agent.request(operation,arguments||{});requests[id]={quiet:!!quiet,then:then||""}
        asked(operation,arguments||{})
        return id
    }
    function refresh() {ask("update.status",{},true)}
    function scheduledCheck() {ask("update.check",{},true)}
    function checkNow() {checking=true;error="";ask("update.check",{manual:true})}
    function install() {working=true;error="";ask("update.apply",{manual:true})}
    function rollBack() {working=true;error="";ask("update.rollback",{})}
    function setMode(choice) {error="";ask("settings.configure",{updates:{mode:choice}})}
    // A check from the other channel no longer counts, so a new one follows (none when off).
    function setChannel(choice) {error="";ask("settings.configure",{updates:{channel:choice}},false,"check")}

    function checkText() {
        switch(status.state) {
        case "up_to_date":return qsTr("Version %1 is up to date.").arg(status.current)
        case "update_available":return qsTr("Version %1 is available. You have %2.").arg(latest).arg(status.current)
        case "no_release":return qsTr("No release is available yet.")
        case "no_package":return qsTr("Version %1 has no package for this system.").arg(latest)
        case "offline":return qsTr("GitHub could not be reached. This computer may be offline.")
        case "error":return qsTr("The check failed: %1").arg(status.message||"")
        default:return qsTr("Version %1. Not checked yet.").arg(status.current||Qt.application.version)
        }
    }
    function blockedText() {
        if(status.forced)return qsTr("Updates are turned off for this build or by the environment.")
        let kind=status.install?status.install.kind:""
        if(kind==="source_checkout")return qsTr("This copy runs from a source checkout. Update it with Git and a new build.")
        if(kind==="system_package")return qsTr("This copy belongs to a system package. Update it with your package manager.")
        if(kind==="unwritable")return qsTr("The installation cannot be written, so it cannot be updated here.")
        if(kind==="unmanaged")return qsTr("This copy was not installed by its installer, so it is left alone.")
        if(status.signing_key_configured===false)return qsTr("This build has no release signing key, so it cannot verify or install updates.")
        return ""
    }
    function stateText() {
        if(error)return error
        if(restartNeeded)return qsTr("Version %1 is installed. Restart to use it.").arg(status.current)
        if(installing)return qsTr("Downloading, verifying and installing the update.")
        if(status.interrupted)return qsTr("An update was interrupted. It is undone before the next installation.")
        if(status.last_failure)return qsTr("The last installation failed: %1").arg(status.last_failure.message||"")
        if(updateAvailable && status.busy_reason)return qsTr("Installing has to wait until jobs, device sessions and the virtual iOS device have ended.")
        return checkText()
    }

    // Whether the model checks by itself; the tests turn it off to set the status themselves.
    property bool background: true
    Timer {interval:5000;running:model.background;onTriggered:model.scheduledCheck()}
    // tomaccoctl decides whether a day has passed; asking costs no request before that.
    Timer {interval:3600000;running:model.background;repeat:true;onTriggered:model.scheduledCheck()}
    Timer {interval:2000;running:model.installing;repeat:true;onTriggered:model.refresh()}
    Connections {
        target:Agent
        function onApiFinished(id,op,response) {
            let request=model.requests[id];if(!request)return;delete model.requests[id]
            if(!response.ok) {
                if(op==="update.check")model.checking=false
                if(op==="update.apply" || op==="update.rollback")model.working=false
                if(!request.quiet)model.error=response.error.message
                if(op==="update.apply" || op==="update.rollback")model.refresh()
                return
            }
            if(op==="update.status"){model.status=response.result;return}
            if(op==="update.check") {
                model.checking=false
                model.refresh()
                let found=response.result
                if(request.quiet && found.mode==="auto" && found.update_available===true && found.can_apply===true && !model.installing && !model.restartNeeded)
                    model.ask("update.apply",{auto:true},true)
                return
            }
            model.working=false
            model.refresh()
            if(request.then==="check")model.scheduledCheck()
        }
    }
    Component.onCompleted:Qt.callLater(refresh)
}
