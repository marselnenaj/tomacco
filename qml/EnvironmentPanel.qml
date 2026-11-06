import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "."
import "Numbers.js" as Numbers

// Named QA environments: the same environment.* operations agents call.
// The target device is chosen here on purpose and never taken from the
// device selected elsewhere, and a preset reaches a device only through a
// plan the person has seen.
ColumnLayout {
    id: view
    objectName: "environmentPanel"
    required property var workspace
    spacing: Theme.gapXs
    property var presets: []
    property var problems: []
    property string presetId: ""
    property int revision: 0
    property var draft: ({})
    property string device: ""
    property var plan: null
    property var applied: null
    property var restored: null
    property var inspected: null
    property string notice: ""
    property bool failed: false
    property int busy: 0
    property var requests: ({})
    // Route playback (gpx.*): the chosen file, what gpx.inspect said, the playback job and its last fix.
    property string routePath: ""
    property var route: null
    property var routeJob: null
    property var routeProgress: null
    property string routeNotice: ""
    readonly property bool routePlaying: !!routeJob && ["queued","running"].indexOf(routeJob.state)>=0
    readonly property bool routePaused: routePlaying && !!routeProgress && routeProgress.state==="paused"
    readonly property bool routeTargetIsEmulator: hasTarget && device.indexOf("emulator-")===0
    readonly property var targets: (workspace.devices || []).filter(d=>d.platform==="android" && d.state==="device")
    readonly property bool hasTarget: targets.some(d=>d.id===device)
    readonly property bool planIsCurrent: !!plan && plan.device===device && plan.preset.id===presetId && plan.preset.revision===revision
    readonly property var fields: [
        {key:"dark_mode",label:qsTr("Dark mode"),choices:["on","off","auto"]},
        {key:"font_scale",label:qsTr("Font size"),number:true},
        {key:"animations",label:qsTr("Animations"),flag:true},
        {key:"airplane_mode",label:qsTr("Airplane mode"),flag:true},
        {key:"app_locale",label:qsTr("App language"),text:true},
        {key:"location",label:qsTr("GPS location"),location:true},
        {key:"talkback",label:qsTr("TalkBack"),flag:true}
    ]
    function label(key) { const f=fields.find(x=>x.key===key);return f?f.label:key }
    function choiceText(value) { return ({on:qsTr("On"),off:qsTr("Off"),auto:qsTr("Automatic")})[value] || value }
    function show(value) {
        if(value===undefined || value===null)return "—"
        if(value===true)return qsTr("On")
        if(value===false)return qsTr("Off")
        if(typeof value==="object")return Number(value.latitude).toFixed(4)+", "+Number(value.longitude).toFixed(4)
        if(value==="")return qsTr("Device language")
        return choiceText(String(value))
    }
    function statusText(status) {
        return ({change:qsTr("Will change"),unchanged:qsTr("Already set"),unsupported:qsTr("Not supported here"),applied:qsTr("Applied and read back"),
                 applied_unverified:qsTr("Applied, not read back"),not_applied:qsTr("Android did not apply it"),restored:qsTr("Restored"),
                 not_restorable:qsTr("Cannot be restored"),not_restored:qsTr("Not restored")})[status] || status
    }
    function call(op,args,context) { const id=workspace.request(op,args,true);if(!requests[id])busy++;requests[id]=context || {};return id }
    function message(text,error) { notice=text;failed=!!error }
    function refresh() { call("android.environment.preset.list",{}) }
    function choose(id) {
        const p=presets.find(x=>x.id===id)
        presetId=p?p.id:"";revision=p?p.revision:0;draft=p?JSON.parse(JSON.stringify(p.settings)):({})
        idInput.text=p?p.id:"";nameInput.text=p?p.name:"";plan=null;applied=null;restored=null
    }
    function setSetting(key,value) { let next=JSON.parse(JSON.stringify(draft));if(value===undefined)delete next[key];else next[key]=value;draft=next }
    function save() {
        let args={id:idInput.text.trim(),name:nameInput.text.trim(),settings:draft}
        if(presetId===args.id && revision>0)args.if_revision=revision
        call("android.environment.preset.save",args)
    }
    function remove() { if(presetId)call("android.environment.preset.delete",{id:presetId,if_revision:revision}) }
    function exportTo(path) { if(presetId)call("android.environment.preset.export",{id:presetId,path:path}) }
    function importFrom(path) { call("android.environment.preset.import",{path:path}) }
    function inspect() { if(hasTarget){inspected=null;call("android.environment.inspect",{device:device,bundle_id:bundleInput.text.trim()})} }
    function makePlan() {
        if(!hasTarget || !presetId)return
        plan=null;applied=null;restored=null
        let args={preset:presetId,device:device}
        if(bundleInput.text.trim())args.bundle_id=bundleInput.text.trim()
        call("android.environment.plan",args)
    }
    function apply() { if(planIsCurrent)call("android.environment.apply",{plan_id:plan.plan_id,device:plan.device,allow_partial:partial.checked}) }
    function restore() { if(applied && applied.restore_id)call("android.environment.restore",{restore_id:applied.restore_id,device:applied.device}) }
    function inspectRoute(path) { routePath=path;route=null;routeJob=null;routeProgress=null;routeNotice="";if(path)call("gpx.inspect",hasTarget?{path:path,device:device}:{path:path}) }
    function playRoute() {
        if(!routePath || !routeTargetIsEmulator)return
        routeJob=null;routeProgress=null
        call("gpx.play",{device:device,path:routePath,speed:Number(routeSpeed.text)>0?Number(routeSpeed.text):1,loop:routeLoop.checked})
    }
    function routeSend(command) { if(routeJob)call("job.send",{job_id:routeJob.id,command:command}) }
    function pollRoute() { if(routeJob){call("gpx.status",{job_id:routeJob.id});call("job.get",{job_id:routeJob.id})} }
    function routeStateText() {
        if(!routeJob)return ""
        if(routeJob.state==="queued")return qsTr("Starting playback…")
        if(routeJob.state==="running") {
            if(!routeProgress || routeProgress.index===null)return qsTr("Playing…")
            const where=qsTr("point %1 of %2").arg(Number(routeProgress.index)+1).arg(routeProgress.total)
            return routeProgress.state==="paused"?qsTr("Paused at %1").arg(where):qsTr("Playing %1").arg(where)
        }
        if(routeJob.state==="succeeded")return routeProgress && routeProgress.state==="stopped"?qsTr("Stopped. The last position stays on the emulator."):qsTr("Finished. The last position stays on the emulator.")
        return qsTr("Playback ended: %1").arg(routeJob.state)
    }
    Timer {interval:1000;repeat:true;running:view.routePlaying;onTriggered:view.pollRoute()}
    Connections {
        target: Agent
        function onApiFinished(id,op,response) {
            if(!view.requests[id])return
            delete view.requests[id];view.busy=Math.max(0,view.busy-1)
            if(!response.ok) {
                const details=response.error.details || {}
                if(op==="android.environment.apply" && details.restore_id)view.applied=Object.assign({device:view.plan?view.plan.device:view.device},details)
                view.message(response.error.message,true);return
            }
            const r=response.result
            if(op==="android.environment.preset.list") {view.presets=r.presets;view.problems=r.problems;if(view.presetId && !r.presets.some(p=>p.id===view.presetId))view.choose("");return}
            if(op==="android.environment.preset.save" || op==="android.environment.preset.import") {const p=r.preset;view.presetId=p.id;view.revision=p.revision;view.draft=p.settings;idInput.text=p.id;nameInput.text=p.name;view.plan=null;view.message(r.created?qsTr("Preset saved."):qsTr("Preset updated to revision %1.").arg(p.revision),false);view.refresh()}
            if(op==="android.environment.preset.delete") {view.message(qsTr("Preset deleted."),false);view.choose("");view.refresh()}
            if(op==="android.environment.preset.export") view.message(qsTr("Preset exported to %1.").arg(r.path),false)
            if(op==="android.environment.inspect") view.inspected=r
            if(op==="android.environment.plan") {view.plan=r;view.message(r.unsupported.length?qsTr("Plan ready. This device does not support: %1.").arg(r.unsupported.map(view.label).join(", ")):qsTr("Plan ready. Nothing has changed on the device yet."),false)}
            if(op==="android.environment.apply") {view.applied=r;view.plan=null;view.message(qsTr("Applied and read back. You can restore the previous settings."),false)}
            if(op==="android.environment.restore") {view.restored=r;view.applied=null;view.message(qsTr("Previous settings restored."),false)}
            if(op==="gpx.inspect") {view.route=r;view.routeNotice=r.timing.timed?qsTr("%1 points, %2 km, %3 min recorded").arg(r.points).arg((r.distance_m/1000).toFixed(2)).arg((r.duration_s/60).toFixed(1)):qsTr("%1 points, %2 km, no times: one point per second").arg(r.points).arg((r.distance_m/1000).toFixed(2))}
            if(op==="gpx.play") {view.routeJob=r.job;view.routeProgress=null;view.message(qsTr("Playing the route on %1. The previous position is not known and will not be restored.").arg(r.device),false)}
            if(op==="gpx.status") view.routeProgress=r.progress
            if(op==="job.get" && view.routeJob && r.id===view.routeJob.id) view.routeJob=r
        }
    }
    Component.onCompleted: refresh()
    component Caption: Text {color:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap;Layout.fillWidth:true;Layout.minimumWidth:0}
    component Row3: RowLayout {
        property string name: ""
        property string first: ""
        property string second: ""
        property bool warn: false
        Layout.fillWidth:true;spacing:Theme.gapSm
        Text {text:parent.name;color:Theme.text;font.pixelSize:Theme.fontBody;Layout.preferredWidth:Theme.px(130);elide:Text.ElideRight}
        Text {text:parent.first;color:Theme.muted;font.pixelSize:Theme.fontSmall;Layout.fillWidth:true;Layout.minimumWidth:0;wrapMode:Text.WordWrap}
        Text {text:parent.second;color:parent.warn?Theme.danger:Theme.text;font.pixelSize:Theme.fontSmall;Layout.fillWidth:true;Layout.minimumWidth:0;wrapMode:Text.WordWrap}
    }
    Rectangle {Layout.fillWidth:true;color:Theme.inset;implicitHeight:noticeText.implicitHeight+Theme.gapMd*2;visible:!!view.notice
        Text {id:noticeText;objectName:"environmentNotice";anchors {left:parent.left;right:parent.right;top:parent.top;margins:Theme.gapMd} text:view.notice;color:view.failed?Theme.danger:Theme.text;font.pixelSize:Theme.fontBody;wrapMode:Text.WordWrap}
    }
    StudioPanel {
        Layout.fillWidth:true;title:qsTr("QA environments")
        lead:qsTr("Save a combination of theme, font size, animations, airplane mode, app language, GPS and TalkBack under a name, then apply it to one device you choose.")
        GridLayout {Layout.fillWidth:true;columns:view.width>=Theme.px(560)?2:1;columnSpacing:Theme.gapSm;rowSpacing:Theme.gapSm
            StudioComboBox {objectName:"environmentPreset";Layout.fillWidth:true;Layout.minimumWidth:0;model:view.presets;textRole:"name";valueRole:"id";currentIndex:view.presets.findIndex(p=>p.id===view.presetId);displayText:currentIndex>=0?currentText:qsTr("New preset");onActivated:view.choose(currentValue)}
            StudioButton {text:qsTr("New preset");Layout.maximumWidth:parent.width;onClicked:view.choose("")}
            StudioTextField {id:idInput;objectName:"environmentId";Layout.fillWidth:true;Layout.minimumWidth:0;placeholderText:qsTr("ID · e.g. dark-large-text");enabled:!view.presetId}
            StudioTextField {id:nameInput;objectName:"environmentName";Layout.fillWidth:true;Layout.minimumWidth:0;placeholderText:qsTr("Name")}
        }
        Repeater {
            model: view.fields
            delegate: GridLayout {
                required property var modelData
                readonly property var current: view.draft[modelData.key]
                Layout.fillWidth:true;columns:view.width>=Theme.px(640)?3:1;columnSpacing:Theme.gapSm;rowSpacing:Theme.gapXs
                Text {text:modelData.label;color:Theme.text;font.pixelSize:Theme.fontBody;Layout.preferredWidth:Theme.px(150)}
                StudioComboBox {
                    objectName:"environmentField_"+modelData.key
                    Layout.fillWidth:true;Layout.minimumWidth:0
                    model:[qsTr("Leave as it is")].concat(modelData.choices?modelData.choices.map(view.choiceText):modelData.flag?[qsTr("On"),qsTr("Off")]:[qsTr("Set")])
                    currentIndex:current===undefined?0:modelData.choices?1+modelData.choices.indexOf(current):modelData.flag?(current?1:2):1
                    onActivated:index=>{
                        if(index===0)view.setSetting(modelData.key,undefined)
                        else if(modelData.choices)view.setSetting(modelData.key,modelData.choices[index-1])
                        else if(modelData.flag)view.setSetting(modelData.key,index===1)
                        else if(modelData.number)view.setSetting(modelData.key,1.3)
                        else if(modelData.location)view.setSetting(modelData.key,{latitude:48.2082,longitude:16.3738})
                        else view.setSetting(modelData.key,"en-US")
                    }
                }
                StudioTextField {
                    visible:current!==undefined && !!(modelData.number || modelData.text || modelData.location)
                    Layout.fillWidth:true;Layout.minimumWidth:0
                    text:current===undefined?"":modelData.location?current.latitude+", "+current.longitude:String(current)
                    placeholderText:modelData.location?qsTr("Latitude, longitude"):modelData.number?qsTr("0.5 to 2"):qsTr("Language tags · e.g. de-DE")
                    onEditingFinished:{
                        if(modelData.number){const n=Numbers.parse(text);if(n>=.5 && n<=2)view.setSetting(modelData.key,n)}
                        else if(modelData.location){const p=text.split(/[,;]/).length===2?text.split(/[,;]/).map(x=>Numbers.parse(x)):text.split(/\s+/).map(x=>Numbers.parse(x));if(p.length===2 && p.every(x=>isFinite(x)))view.setSetting(modelData.key,{latitude:p[0],longitude:p[1]})}
                        else view.setSetting(modelData.key,text.trim())
                    }
                }
            }
        }
        Caption {text:qsTr("GPS works on emulators only and cannot be read back or restored. App language needs Android 13; airplane mode needs Android 11.")}
        Flow {Layout.fillWidth:true;spacing:Theme.gapSm
            StudioButton {objectName:"environmentSave";text:qsTr("Save preset");accent:true;width:Math.min(parent.width,implicitWidth);enabled:!!idInput.text.trim() && !!nameInput.text.trim() && Object.keys(view.draft).length>0;onClicked:view.save()}
            StudioButton {text:qsTr("Import…");width:Math.min(parent.width,implicitWidth);onClicked:importDialog.open()}
            StudioButton {text:qsTr("Export…");width:Math.min(parent.width,implicitWidth);enabled:!!view.presetId;onClicked:exportDialog.open()}
            StudioButton {objectName:"environmentDelete";text:qsTr("Delete preset");danger:true;width:Math.min(parent.width,implicitWidth);enabled:!!view.presetId;onClicked:deleteDialog.open()}
        }
        Caption {visible:view.problems.length>0;text:qsTr("%n preset file(s) could not be read and are not listed.","",view.problems.length)}
    }
    StudioPanel {
        Layout.fillWidth:true;title:qsTr("Apply to a device")
        lead:qsTr("Choose the device here. Tomacco reads it first, shows what would change, and applies only that plan to that device.")
        GridLayout {Layout.fillWidth:true;columns:view.width>=Theme.px(560)?2:1;columnSpacing:Theme.gapSm;rowSpacing:Theme.gapSm
            StudioComboBox {objectName:"environmentDevice";Layout.fillWidth:true;Layout.minimumWidth:0;model:view.targets;textRole:"name";valueRole:"id";currentIndex:view.targets.findIndex(d=>d.id===view.device);displayText:currentIndex>=0?currentText+" · "+currentValue:qsTr("Choose the target device");onActivated:{view.device=currentValue;view.plan=null;view.inspected=null}}
            StudioTextField {id:bundleInput;objectName:"environmentBundle";Layout.fillWidth:true;Layout.minimumWidth:0;placeholderText:qsTr("App ID for the app language · optional")}
        }
        Flow {Layout.fillWidth:true;spacing:Theme.gapSm
            StudioButton {objectName:"environmentInspect";text:qsTr("Read device");width:Math.min(parent.width,implicitWidth);enabled:view.hasTarget;onClicked:view.inspect()}
            StudioButton {objectName:"environmentPlan";text:qsTr("Plan");width:Math.min(parent.width,implicitWidth);enabled:view.hasTarget && !!view.presetId;onClicked:view.makePlan()}
            StudioButton {objectName:"environmentApply";text:qsTr("Apply plan");accent:true;width:Math.min(parent.width,implicitWidth);enabled:view.planIsCurrent && view.busy===0 && (partial.checked || !view.plan.unsupported.length);onClicked:view.apply()}
            StudioButton {objectName:"environmentRestore";text:qsTr("Restore previous settings");width:Math.min(parent.width,implicitWidth);enabled:!!view.applied && !!view.applied.restore_id && view.busy===0;onClicked:view.restore()}
        }
        StudioCheckBox {id:partial;objectName:"environmentPartial";Layout.fillWidth:true;Layout.minimumWidth:0;visible:!!view.plan && view.plan.unsupported.length>0;text:qsTr("Skip the settings this device does not support")}
        Caption {visible:!!view.plan;text:view.plan?qsTr("Plan for %1 (%2) · preset revision %3").arg(view.plan.identity.model).arg(view.plan.device).arg(view.plan.preset.revision):""}
        Repeater {model:view.plan?view.plan.entries:[]
            delegate: Row3 {required property var modelData;name:view.label(modelData.field);first:view.show(modelData.current.value)+" → "+view.show(modelData.target);second:view.statusText(modelData.action)+(modelData.action==="change" && !modelData.current.restorable?" · "+view.statusText("not_restorable"):"")+(modelData.action==="unsupported"?" · "+modelData.current.reason:"");warn:modelData.action==="unsupported"}}
        Repeater {model:view.applied && view.applied.results?view.applied.results:[]
            delegate: Row3 {required property var modelData;name:view.label(modelData.field);first:view.show(modelData.value);second:view.statusText(modelData.status);warn:modelData.status==="not_applied" || modelData.status==="unsupported"}}
        Repeater {model:view.restored?view.restored.results:[]
            delegate: Row3 {required property var modelData;name:view.label(modelData.field);first:view.show(modelData.value);second:view.statusText(modelData.status);warn:modelData.status!=="restored"}}
        Repeater {model:view.inspected?view.fields:[]
            delegate: Row3 {required property var modelData;readonly property var read:view.inspected.fields[modelData.key];name:modelData.label;first:read.supported?view.show(read.value):"";second:read.supported?(read.restorable?"":view.statusText("not_restorable")):view.statusText("unsupported")+" · "+read.reason;warn:!read.supported}}
        Caption {objectName:"environmentTalkBackNote";text:qsTr("TalkBack here switches the screen reader on the device and keeps your other accessibility services. It does not show that your app works with a screen reader; the accessibility audit only checks the screen's structure.")}
    }
    StudioPanel {
        Layout.fillWidth:true;title:qsTr("Route playback")
        lead:qsTr("Play a GPX route on the chosen emulator's location. Timed points keep their timing; untimed points play one per second.")
        GridLayout {Layout.fillWidth:true;columns:view.width>=Theme.px(560)?2:1;columnSpacing:Theme.gapSm;rowSpacing:Theme.gapSm
            StudioButton {objectName:"routeChoose";text:view.routePath?qsTr("Change route…"):qsTr("Choose a GPX file…");Layout.maximumWidth:parent.width;onClicked:routeDialog.open()}
            Text {objectName:"routeSummary";Layout.fillWidth:true;Layout.minimumWidth:0;text:view.route?(view.route.name?view.route.name+" · ":"")+view.routeNotice:view.routePath?qsTr("Reading the route…"):qsTr("No route chosen");color:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            StudioTextField {id:routeSpeed;objectName:"routeSpeed";Layout.fillWidth:true;Layout.minimumWidth:0;text:"1";placeholderText:qsTr("Speed · 1 is real time")}
            StudioCheckBox {id:routeLoop;objectName:"routeLoop";Layout.fillWidth:true;Layout.minimumWidth:0;text:qsTr("Repeat the route")}
        }
        Caption {visible:view.hasTarget && !view.routeTargetIsEmulator;text:qsTr("Route playback reaches emulators only. A phone takes no mock location from here.")}
        Flow {Layout.fillWidth:true;spacing:Theme.gapSm
            StudioButton {objectName:"routePlay";text:qsTr("Play route");accent:true;width:Math.min(parent.width,implicitWidth);enabled:!!view.route && view.routeTargetIsEmulator && !view.routePlaying && view.busy===0;onClicked:view.playRoute()}
            StudioButton {objectName:"routePause";text:view.routePaused?qsTr("Resume"):qsTr("Pause");width:Math.min(parent.width,implicitWidth);enabled:view.routePlaying && !!view.routeProgress;onClicked:view.routeSend(view.routePaused?"resume":"pause")}
            StudioButton {objectName:"routeStop";text:qsTr("Stop");danger:true;width:Math.min(parent.width,implicitWidth);enabled:view.routePlaying;onClicked:view.routeSend("stop")}
        }
        Caption {objectName:"routeState";visible:!!view.routeJob;text:view.routeStateText()}
        Caption {text:qsTr("The emulator does not report its position, so the position before playback is unknown and is not restored. Set one in the preset's GPS field afterwards if you need it.")}
    }
    FileDialog {id:routeDialog;nameFilters:[qsTr("GPX route (*.gpx)")];onAccepted:view.inspectRoute(decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,"")))}
    FileDialog {id:importDialog;nameFilters:[qsTr("QA environment (*.json)")];onAccepted:view.importFrom(decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,"")))}
    FileDialog {id:exportDialog;fileMode:FileDialog.SaveFile;defaultSuffix:"json";nameFilters:[qsTr("QA environment (*.json)")];onAccepted:view.exportTo(decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,"")))}
    StudioDialog {id:deleteDialog;objectName:"environmentDeleteDialog";title:qsTr("Delete this preset?");modal:true;parent:Overlay.overlay;anchors.centerIn:parent;width:Math.min((parent?parent.width:view.width)-Theme.gapLg,Theme.px(480));contentItem:ColumnLayout {
        Caption {text:qsTr("The preset is removed from this computer. Devices keep their settings, and restore points stay.")}
        Flow {Layout.fillWidth:true;spacing:Theme.gapSm
            StudioButton {text:qsTr("Keep preset");width:Math.min(parent.width,implicitWidth);onClicked:deleteDialog.close()}
            StudioButton {text:qsTr("Delete preset");width:Math.min(parent.width,implicitWidth);danger:true;onClicked:{deleteDialog.close();view.remove()}}
        }
    }}
}
