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
    property var report: ({})
    property string comparison: ""
    property var requests: ({})
    function call(op,args) { let id=workspace.request(op,args);requests[id]=true }
    Connections { target: Agent; function onApiFinished(id,op,response) { if(!view.requests[id]) return;delete view.requests[id];if(!response.ok) return;if(op==="profile.analyze") view.report=response.result;if(op==="profile.compare") view.comparison=JSON.stringify(response.result,null,2) } }
    ColumnLayout {
    width: Math.min(parent.width-Theme.gapLg,1240); spacing: Theme.gapXs
    StudioPanel {
        Layout.fillWidth: true
        title: qsTr("Record a trace")
        lead: qsTr("Use your app while it records. You get its CPU time, memory and frame times.")
        Label { text: workspace.selectedDevice || qsTr("Pick a phone under Devices first."); color: Theme.muted; font.family: workspace.selectedDevice?Theme.mono:Theme.sans }
        StudioTextField { id: application; Layout.fillWidth: true; placeholderText: qsTr("Application ID · e.g. com.example.app") }
        RowLayout { Layout.fillWidth: true; spacing: Theme.gapXs
            StudioTextField { id: trace; Layout.fillWidth: true; placeholderText: qsTr("Path to the Perfetto trace (.pftrace)") }
            StudioButton { text: qsTr("Choose file"); onClicked: traceDialog.open() }
        }
        StudioTextField { id: reportFile; Layout.fillWidth: true; placeholderText: qsTr("Save JSON report as…") }
        Flow {
            Layout.fillWidth: true; spacing: Theme.gapSm
            Label { text: qsTr("Duration (seconds)"); color: Theme.muted; height: Theme.controlHeight; verticalAlignment: Text.AlignVCenter }
            StudioSpinBox { id: seconds; from: 1; to: 600; value: 10; editable: true; Accessible.name: qsTr("Duration (seconds)") }
            StudioButton { text: qsTr("Record"); iconName: "play"; accent: true; enabled: workspace.selectedDevice.length>0 && application.text.length>0 && trace.text.length>0; onClicked: view.call("profile.capture",{device:workspace.selectedDevice,bundle_id:application.text,path:trace.text,seconds:seconds.value}) }
            StudioButton { text: qsTr("Analyze"); enabled: application.text.length>0 && trace.text.length>0 && reportFile.text.length>0; onClicked: view.call("profile.analyze",{bundle_id:application.text,path:trace.text,report:reportFile.text}) }
        }
        GridLayout {
            columns: 2; Layout.fillWidth: true; visible: !!view.report.metrics
            Repeater { model: view.report.metrics?Object.keys(view.report.metrics):[]
                RowLayout {
                    required property string modelData
                    Layout.fillWidth: true
                    Label { text: modelData; color: Theme.muted; font.family: Theme.mono; font.pixelSize: Theme.fontSmall; Layout.fillWidth: true }
                    Label { text: view.report.metrics[modelData]===null ? qsTr("not available") : Number(view.report.metrics[modelData]).toLocaleString(Qt.locale(Studio.uiLanguage),"f",2); color: Theme.text }
                }
            }
        }
    }
    StudioPanel {
        Layout.fillWidth: true
        title: qsTr("Catch regressions")
        StudioTextField { id: baseline; Layout.fillWidth: true; placeholderText: qsTr("Previous JSON report") }
        Flow {
            Layout.fillWidth: true; spacing: Theme.gapSm
            StudioCheckBox { id: cpu; text: "CPU"; checked: true }
            StudioCheckBox { id: memory; text: qsTr("Memory") }
            StudioCheckBox { id: frames; text: qsTr("Frame time") }
            StudioSpinBox { id: budget; from: 0; to: 1000; value: 10; editable: true; Accessible.name: qsTr("% maximum increase") }
            Label { text: qsTr("% maximum increase"); color: Theme.muted; height: Theme.controlHeight; verticalAlignment: Text.AlignVCenter }
            StudioButton { text: qsTr("Compare"); enabled: baseline.text.length>0 && reportFile.text.length>0 && (cpu.checked||memory.checked||frames.checked); onClicked: { let b={};if(cpu.checked)b.cpu_ms={increase_percent:budget.value};if(memory.checked)b.rss_peak_bytes={increase_percent:budget.value};if(frames.checked)b.frame_avg_ms={increase_percent:budget.value};view.call("profile.compare",{baseline:baseline.text,current:reportFile.text,budgets:b}) } }
        }
    }
    StudioCodeView { Layout.fillWidth: true; Layout.preferredHeight: 180; visible: !!text; text: view.comparison || view.report.threads_csv || "" }
    }
    FileDialog { id: traceDialog; nameFilters: ["Perfetto (*.pftrace *.perfetto-trace)"]; onAccepted: trace.text=decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,"")) }
}
