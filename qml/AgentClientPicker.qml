import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// Connect a coding agent: the clients `tomaccoctl setup agents` knows, each
// with its own state, the command or the snippet for its own settings file, and
// setup or removal of Tomacco's own entry when the person asks for it. Status
// only reads; setup keeps a backup and never touches an entry it does not own;
// a disabled entry, a conflict or an unclear version stays as it is and the
// person gets the text to add by hand. No generic mcpServers JSON stands in for
// a client whose file looks different.
ColumnLayout {
    id: picker
    objectName: "agentClientPicker"
    spacing: Theme.gapMd
    // The report of the last status run: {program, clients:[...]}.
    property var report: ({})
    property var clients: report && report.clients ? report.clients : []
    property string selected: ""
    property bool busy: false
    property string busyAction: ""
    property string error: ""
    // The outcome of the last setup or removal, as a sentence.
    property string outcome: ""
    property bool copied: false
    // Below this width the list and the details stack (phone and narrow windows).
    readonly property bool stacked: width < Theme.px(640)
    readonly property var current: clients.find(c => c.id === selected) || null
    readonly property bool available: typeof Agent !== "undefined" && Agent && typeof Agent.agentClients === "function"

    // keep: after a setup or removal, its outcome or error stays on screen.
    function refresh(keep) {
        if (!available) { error = qsTr("This version of tomaccoctl cannot list coding agents."); return }
        if (!keep) error = ""
        busy = true; busyAction = "status"
        if (!Agent.agentClients("status", "")) { busy = false; error = qsTr("The agent list could not be requested.") }
    }
    function run(action) {
        if (!current || busy) return
        error = ""; outcome = ""; busy = true; busyAction = action
        if (!Agent.agentClients(action, current.id)) { busy = false; error = qsTr("The agent list could not be requested.") }
    }
    // Plain words for each state the setup report uses.
    function stateLabel(c) {
        if (!c) return ""
        switch (c.state) {
        case "connected": case "added": case "updated": return qsTr("Connected", "agent client")
        case "not_connected": return qsTr("Not connected", "agent client")
        case "absent": return c.found ? qsTr("Not connected", "agent client") : qsTr("Not found on this computer", "agent client")
        case "disabled": return qsTr("Turned off in its settings", "agent client")
        case "outdated": return qsTr("Needs an update", "agent client")
        case "conflict": return qsTr("Another entry is in the way", "agent client")
        case "manual": return qsTr("Add it by hand", "agent client")
        case "unknown": return qsTr("Version unclear", "agent client")
        case "removed": return qsTr("Removed", "agent client")
        case "declined": return qsTr("Left unchanged", "agent client")
        case "failed": return qsTr("Setup failed", "agent client")
        }
        return c.state
    }
    function tone(c) {
        if (!c) return "idle"
        if (["connected", "added", "updated"].indexOf(c.state) >= 0) return "ok"
        if (["outdated", "conflict", "unknown", "manual"].indexOf(c.state) >= 0) return "busy"
        if (c.state === "failed") return "failed"
        return "idle"
    }
    function dotColor(c) {
        const t = tone(c)
        return t === "ok" ? Theme.success : t === "busy" ? Theme.warning : t === "failed" ? Theme.danger : Theme.faint
    }
    // What the person can do about the state, in one sentence.
    function explanation(c) {
        if (!c) return ""
        switch (c.state) {
        case "connected": return qsTr("%1 starts Tomacco when it needs it. Nothing to do.").arg(c.name)
        case "disabled": return qsTr("Tomacco's entry is switched off in %1. Setup leaves your choice alone; switch it on there.").arg(c.name)
        case "outdated": return qsTr("Tomacco's entry points to another program or older settings. Update rewrites only that entry and keeps a backup.")
        case "conflict": return qsTr("Something else in %1's settings uses the name. Setup leaves it alone; add Tomacco by hand under another name.").arg(c.name)
        case "manual": return qsTr("This file is not one Tomacco rewrites. Add the text below yourself.")
        case "unknown": return qsTr("Tomacco could not tell which settings layout %1 reads. Add the text below yourself, or run setup in a terminal with the version.").arg(c.name)
        case "absent": return c.found ? qsTr("Setup adds Tomacco to %1's settings and keeps a backup of the file.").arg(c.name)
                                      : qsTr("%1 is not installed here. Install it first, or copy the text for another computer.").arg(c.name)
        case "not_connected": return qsTr("Setup adds Tomacco to %1's settings and keeps a backup of the file.").arg(c.name)
        }
        return ""
    }
    readonly property bool canSetUp: !!current && current.found && ["absent", "not_connected", "outdated"].indexOf(current.state) >= 0
    readonly property bool canRemove: !!current && current.state === "connected"
    readonly property string copyText: current ? (current.command || current.snippet || "") : ""

    Component.onCompleted: refresh()
    Connections {
        target: picker.available ? Agent : null
        ignoreUnknownSignals: true
        function onAgentClientsFinished(action, client, report, error) {
            picker.busy = false
            if (error) { picker.error = error; return }
            if (action === "status") {
                picker.report = report
                if (!picker.selected || !picker.clients.some(c => c.id === picker.selected)) {
                    const first = picker.clients.find(c => c.state === "connected") || picker.clients.find(c => c.found) || picker.clients[0]
                    picker.selected = first ? first.id : ""
                }
                return
            }
            const entry = (report.clients || []).find(c => c.id === client)
            if (!entry) picker.outcome = qsTr("Setup did not report on this agent.")
            else if (entry.state === "failed") picker.error = entry.detail || qsTr("Setup failed.")
            else if (entry.state === "added" || entry.state === "updated")
                picker.outcome = entry.backup ? qsTr("Connected. The previous file is saved as %1.").arg(entry.backup) : qsTr("Connected.")
            else if (entry.state === "removed") picker.outcome = qsTr("Tomacco's entry was removed; the rest of the file is unchanged.")
            else picker.outcome = picker.explanation(entry) || picker.stateLabel(entry)
            picker.refresh(true)
        }
    }

    StudioStatus { objectName: "agentClientsLoading"; visible: picker.busy && picker.busyAction === "status" && picker.clients.length === 0; tone: "busy"; text: qsTr("Looking for coding agents…") }
    Text {
        objectName: "agentClientsError"; visible: picker.error.length > 0; Layout.fillWidth: true
        text: picker.error; color: Theme.danger; font.pixelSize: Theme.fontSmall; wrapMode: Text.Wrap; textFormat: Text.PlainText
    }
    GridLayout {
        Layout.fillWidth: true
        visible: picker.clients.length > 0
        columns: picker.stacked ? 1 : 2
        columnSpacing: Theme.gapLg; rowSpacing: Theme.gapMd
        // The clients, each a row with its state.
        ColumnLayout {
            objectName: "agentClientList"
            Layout.fillWidth: picker.stacked
            Layout.preferredWidth: picker.stacked ? -1 : Math.min(Theme.px(320), picker.width * 0.42)
            Layout.alignment: Qt.AlignTop
            spacing: 0
            Repeater {
                model: picker.clients
                MobileRow {
                    required property var modelData
                    objectName: "agentClient-" + modelData.id
                    Layout.fillWidth: true
                    implicitWidth: 0
                    title: modelData.name
                    detail: picker.stateLabel(modelData)
                    dot: picker.dotColor(modelData)
                    chevron: picker.stacked
                    highlighted: picker.selected === modelData.id
                    onClicked: { picker.selected = modelData.id; picker.copied = false; picker.outcome = "" }
                }
            }
        }
        // The chosen client: why it is in this state and what to do.
        ColumnLayout {
            objectName: "agentClientDetail"
            visible: !!picker.current
            Layout.fillWidth: true; Layout.minimumWidth: 0; Layout.alignment: Qt.AlignTop
            spacing: Theme.gapSm
            StudioHeading { Layout.fillWidth: true; text: picker.current ? picker.current.name : ""; size: Theme.fontTitle }
            StudioStatus { objectName: "agentClientState"; tone: picker.tone(picker.current); text: picker.stateLabel(picker.current) }
            Text {
                objectName: "agentClientExplanation"; Layout.fillWidth: true
                text: picker.explanation(picker.current); color: Theme.text; font.pixelSize: Theme.fontSmall; wrapMode: Text.Wrap; lineHeight: 1.3
                visible: text.length > 0
            }
            Text {
                objectName: "agentClientDetailText"; Layout.fillWidth: true
                visible: !!picker.current && !!picker.current.detail
                text: picker.current && picker.current.detail ? picker.current.detail : ""
                color: Theme.muted; font.pixelSize: Theme.fontSmall; wrapMode: Text.Wrap; textFormat: Text.PlainText
            }
            Text {
                Layout.fillWidth: true
                visible: !!picker.current && !picker.current.command && !!picker.current.file
                text: picker.current && picker.current.file ? qsTr("Add this to %1:").arg(picker.current.file) : ""
                color: Theme.muted; font.pixelSize: Theme.fontSmall; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
            }
            Text {
                Layout.fillWidth: true
                visible: !!picker.current && !!picker.current.command
                text: qsTr("Or run this in a terminal:")
                color: Theme.muted; font.pixelSize: Theme.fontSmall; wrapMode: Text.Wrap
            }
            Rectangle {
                Layout.fillWidth: true
                visible: picker.copyText.length > 0
                implicitHeight: copyLabel.implicitHeight + Theme.gapMd * 2
                color: Theme.inset; border.color: Theme.subtle; border.width: 1
                Text {
                    id: copyLabel; objectName: "agentClientCopyText"
                    anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.gapMd }
                    text: picker.copyText; color: Theme.text; font.family: Theme.mono; font.pixelSize: Theme.fontSmall
                    wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
                }
            }
            Repeater {
                model: picker.current && picker.current.notes ? picker.current.notes : []
                Text {
                    required property var modelData
                    Layout.fillWidth: true
                    text: "· " + modelData; color: Theme.faint; font.pixelSize: Theme.fontSmall; wrapMode: Text.Wrap; textFormat: Text.PlainText
                }
            }
            Flow {
                Layout.fillWidth: true; spacing: Theme.gapSm
                StudioButton {
                    objectName: "agentClientSetUp"; visible: picker.canSetUp; accent: true; enabled: !picker.busy
                    text: picker.busy && picker.busyAction === "setup" ? qsTr("Connecting…")
                        : picker.current && picker.current.state === "outdated" ? qsTr("Update entry") : qsTr("Connect")
                    iconName: "plus"
                    onClicked: picker.run("setup")
                }
                StudioButton {
                    objectName: "agentClientCopy"; visible: picker.copyText.length > 0
                    text: picker.copied ? qsTr("Copied") : picker.current && picker.current.command ? qsTr("Copy command") : qsTr("Copy text")
                    iconName: picker.copied ? "check" : "copy"; quiet: picker.canSetUp
                    onClicked: { Studio.copy(picker.copyText); picker.copied = true }
                }
                StudioButton {
                    objectName: "agentClientRemove"; visible: picker.canRemove; quiet: true; danger: true; enabled: !picker.busy
                    text: qsTr("Remove Tomacco"); tip: qsTr("Removes only the entry setup added; the rest of the file stays.")
                    onClicked: picker.run("remove")
                }
                StudioButton {
                    objectName: "agentClientRefresh"; quiet: true; enabled: !picker.busy
                    text: qsTr("Check again"); iconName: "refresh"
                    onClicked: picker.refresh()
                }
            }
            Text {
                objectName: "agentClientOutcome"; visible: picker.outcome.length > 0; Layout.fillWidth: true
                text: picker.outcome; color: Theme.muted; font.pixelSize: Theme.fontSmall; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
            }
        }
    }
}
