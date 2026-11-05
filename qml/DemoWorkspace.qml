import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "."

StudioScrollView {
    id: view
    objectName: "demoWorkspace"
    required property var workspace
    clip: true
    rightPadding: Theme.gapMd
    contentWidth: availableWidth
    property var capabilities: ({})
    property var media: ({})
    property var savedScene: ({})
    property var draft: ({})
    property string scenePath: ""
    property int revision: 0
    property int generation: 0
    property int editVersion: 0
    property int selectedPose: 0
    property var requests: ({})
    property var invalidFields: ({})
    property string notice: ""
    property bool failed: false
    property bool loading: false
    property bool saving: false
    property string jobId: ""
    property string jobKind: ""
    property string jobState: ""
    property string jobOutput: ""
    property string previewPath: ""
    property string completedOutput: ""
    property string completedScene: ""
    property int completedRevision: 0
    property bool jobPollPending: false
    readonly property bool loaded: revision>0 && !!draft.output
    readonly property bool dirty: loaded && (Object.keys(invalidFields).length>0 || JSON.stringify(draft)!==JSON.stringify(savedScene))
    readonly property bool rendering: jobState==="queued" || jobState==="running"
    readonly property bool canRender: loaded && !dirty && !saving && !loading && !rendering && capabilities.ready===true
    readonly property var poses: draft.keyframes || []
    readonly property var pose: poses[selectedPose] || ({})
    readonly property var presetIds: capabilities.presets && capabilities.presets.length ? capabilities.presets.map(p=>typeof p==="string"?p:p.id) : ["portrait-orbit","landscape-orbit","square-float","portrait-still"]
    function clone(value) { return JSON.parse(JSON.stringify(value)) }
    function fileUrl(path) { return "file://"+path.split("/").map(encodeURIComponent).join("/") }
    function localPath(url) { return decodeURIComponent(url.toString().replace(/^file:\/\//,"")) }
    function call(op,args,context) { const id=workspace.request(op,args,true);requests[id]=Object.assign({generation:generation},context || {});return id }
    function message(text,error) { notice=text;failed=!!error }
    function inspectSource() { if(sourceFile.text.trim())call("demo.media.inspect",{path:sourceFile.text.trim()},{source:sourceFile.text.trim()}) }
    function createScene() {
        if(loading || saving || rendering)return
        if(dirty){message(qsTr("Save your changes before opening another scene."),true);return}
        if(!sourceFile.text.trim() || !sceneFile.text.trim())return
        loading=true;generation++
        call("demo.scene.create",{path:sceneFile.text.trim(),source:sourceFile.text.trim(),preset:presetIds[preset.currentIndex],title:titleInput.text.trim() || qsTr("App demo")})
    }
    function openScene(path) {
        if(loading || saving || rendering)return
        if(dirty){message(qsTr("Save your changes before opening another scene."),true);return}
        const next=path || sceneFile.text.trim();if(!next)return
        loading=true;generation++;call("demo.scene.get",{path:next},{editVersion:editVersion})
    }
    function acceptScene(result,replaceDraft) {
        scenePath=result.path;revision=result.revision || result.scene.revision;savedScene=clone(result.scene)
        if(replaceDraft){invalidFields=({});draft=clone(result.scene);selectedPose=Math.min(selectedPose,Math.max(0,poses.length-1));editVersion++}
        else {const newer=clone(draft);newer.revision=savedScene.revision;draft=newer}
        sceneFile.text=scenePath;sourceFile.text=result.scene.source.path
    }
    function invalidNumber(key,invalid) {const next=Object.assign({},invalidFields);if(invalid)next[key]=true;else delete next[key];invalidFields=next}
    function setField(group,key,value) {
        if(!loaded)return
        invalidNumber(group+"."+key,false)
        let next=clone(draft)
        if(group)next[group][key]=value;else next[key]=value
        draft=next;editVersion++;message("",false)
    }
    function numberField(group,key,text) {
        const value=Number(text)
        if(text.trim()==="" || !Number.isFinite(value)){invalidNumber(group+"."+key,true);message(qsTr("Enter a valid number."),true);return}
        setField(group,key,value)
    }
    function setPose(key,value) {
        if(!poses.length)return
        invalidNumber("pose."+selectedPose+"."+key,false)
        const next=clone(draft);next.keyframes[selectedPose][key]=value;draft=next;editVersion++;message("",false)
    }
    function poseNumber(key,text) {
        const value=Number(text)
        if(text.trim()==="" || !Number.isFinite(value)){invalidNumber("pose."+selectedPose+"."+key,true);message(qsTr("Enter a valid number."),true);return}
        setPose(key,value)
    }
    function addPose() {
        if(!loaded || Object.keys(invalidFields).length)return
        const next=clone(draft),entry=Object.assign({yaw:0,pitch:0,roll:0,scale:.72,x:.5,y:.5,easing:"smooth"},clone(pose))
        // Retain every existing pose and its extension fields. New poses start
        // halfway through the next interval, with a distinct time when possible.
        const sorted=next.keyframes.map(p=>p.time).sort((a,b)=>a-b),at=Number(entry.time || 0),later=sorted.find(t=>t>at)
        entry.time=later!==undefined?(at+later)/2:Math.min(next.output.duration,at+Math.max(.1,next.output.duration/4))
        if(sorted.indexOf(entry.time)>=0){message(qsTr("Choose a different time for the new pose."),true);return}
        next.keyframes.push(entry);next.keyframes.sort((a,b)=>a.time-b.time);selectedPose=next.keyframes.indexOf(entry);draft=next;editVersion++
    }
    function removePose() { if(poses.length<=1 || Object.keys(invalidFields).length)return;const next=clone(draft);next.keyframes.splice(selectedPose,1);selectedPose=Math.min(selectedPose,next.keyframes.length-1);draft=next;editVersion++ }
    function saveChanges() {
        if(!dirty || saving)return
        if(Object.keys(invalidFields).length){message(qsTr("Enter a valid number."),true);return}
        const changes={}
        for(const key of ["title","clip","output","device","background","shadow","camera","keyframes"])
            if(JSON.stringify(draft[key])!==JSON.stringify(savedScene[key]))changes[key]=clone(draft[key])
        saving=true;call("demo.scene.update",{path:scenePath,if_revision:revision,changes:changes},{editVersion:editVersion})
    }
    function reloadSaved() { if(dirty)reloadDialog.open();else if(scenePath)openScene(scenePath) }
    function discardAndReload() { invalidFields=({});draft=clone(savedScene);openScene(scenePath) }
    function render(kind,path) {
        if(!canRender){if(dirty)message(qsTr("Save changes before rendering."),true);return}
        if(!path)return
        jobKind=kind;jobOutput=path;jobState="queued";completedOutput="";jobId=""
        const args={path:scenePath,if_revision:revision,output:path}
        if(kind==="preview")args.time=Math.max(0,Math.min(Number(previewTime.text) || 0,draft.output.duration))
        call(kind==="preview"?"demo.preview":"demo.export",args,{kind:kind,path:scenePath,revision:revision,output:path})
    }
    function pollJob() { if(!jobId || !rendering || jobPollPending)return;jobPollPending=true;call("job.get",{job_id:jobId},{jobId:jobId}) }
    function finishJob(job) {
        jobState=job.state
        if(jobState==="succeeded") {
            // Files become displayable only after the backend confirms success.
            const artifacts=job.result && job.result.artifacts ? job.result.artifacts : []
            const paths=artifacts.map(a=>typeof a==="string"?a:a.path)
            if(jobKind==="preview")previewPath=paths.find(p=>p && p.toLowerCase().endsWith(".png")) || jobOutput
            else {completedOutput=jobOutput;previewPath=paths.find(p=>p && p.endsWith(".poster.png")) || jobOutput+".poster.png"}
            completedScene=scenePath;completedRevision=revision;message(qsTr("Render complete."),false)
        } else if(["failed","cancelled","timed_out"].indexOf(jobState)>=0)message(job.result && job.result.error || job.error || qsTr("Rendering did not complete. Your scene is unchanged."),true)
    }
    Connections {
        target: Agent
        function onApiFinished(id,op,response) {
            const context=view.requests[id];if(!context)return;delete view.requests[id]
            if(op!=="demo.capabilities" && context.generation!==view.generation)return
            if(op==="job.get")view.jobPollPending=false
            if(op==="demo.scene.create" || op==="demo.scene.get")view.loading=false
            if(op==="demo.scene.update")view.saving=false
            if(!response.ok){if(op==="demo.preview" || op==="demo.export")view.jobState="failed";view.message(response.error.message,true);return}
            const result=response.result
            if(op==="demo.capabilities")view.capabilities=result
            if(op==="demo.media.inspect" && context.source===sourceFile.text.trim())view.media=result
            if(op==="demo.scene.create" || op==="demo.scene.get") {
                if(view.dirty){view.message(qsTr("Your draft has changes. Reload explicitly to replace it."),true);return}
                view.acceptScene(result,true);view.message(qsTr("Scene opened."),false)
            }
            if(op==="demo.scene.update") {const unchanged=context.editVersion===view.editVersion;view.acceptScene(result,unchanged);view.message(unchanged?qsTr("Changes saved."):qsTr("Saved. Your newer edits are still unsaved."),false)}
            if(op==="demo.preview" || op==="demo.export") {view.jobId=result.id || (result.job && result.job.id) || "";view.jobState=result.state || "queued";if(view.jobId)view.pollJob();else {view.jobState="failed";view.message(qsTr("The render job could not be followed."),true)}}
            if(op==="job.get" && context.jobId===view.jobId)view.finishJob(result)
        }
    }
    Component.onCompleted: call("demo.capabilities",{})
    Timer {interval:700;repeat:true;running:view.visible && view.rendering && !!view.jobId;onTriggered:view.pollJob()}
    Shortcut {sequence:"Ctrl+S";enabled:view.visible && view.dirty;onActivated:view.saveChanges()}
    component Caption: Text {color:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap;Layout.fillWidth:true;Layout.minimumWidth:0}
    component Field: ColumnLayout {
        id: field
        property string label: ""
        property var value: ""
        signal edited(string value)
        spacing:Theme.gapXs
        Layout.fillWidth:true;Layout.minimumWidth:0
        Caption {text:field.label}
        StudioTextField {objectName:field.objectName+"Input";Layout.fillWidth:true;Layout.minimumWidth:0;text:String(field.value);placeholderText:field.label;onTextEdited:field.edited(text)}
    }
    ColumnLayout {
        width:Math.max(0,view.availableWidth);spacing:Theme.gapXl
        Rectangle {Layout.fillWidth:true;color:Theme.inset;implicitHeight:messageText.implicitHeight+Theme.gapMd*2;visible:!!view.notice
            Text {id:messageText;anchors {left:parent.left;right:parent.right;top:parent.top;margins:Theme.gapMd} text:view.notice;color:view.failed?Theme.danger:Theme.text;font.pixelSize:Theme.fontBody;wrapMode:Text.WrapAnywhere}
        }
        StudioPanel {
            Layout.fillWidth:true;title:qsTr("A recording, made into a demo")
            lead:qsTr("Choose a local video, place it in a device and set its motion. Your source stays unchanged.")
            GridLayout {Layout.fillWidth:true;columns:width>=Theme.px(520)?2:1;columnSpacing:Theme.gapSm;rowSpacing:Theme.gapSm
                StudioTextField {id:sourceFile;objectName:"demoSource";Layout.fillWidth:true;Layout.minimumWidth:0;placeholderText:qsTr("Source video")}
                StudioButton {Layout.maximumWidth:parent.width;text:qsTr("Choose video");onClicked:sourceDialog.open()}
            }
            Flow {Layout.fillWidth:true;spacing:Theme.gapSm
                StudioButton {text:qsTr("Inspect video");width:Math.min(parent.width,implicitWidth);enabled:!!sourceFile.text;onClicked:view.inspectSource()}
                Text {width:Math.min(parent.width,implicitWidth);visible:!!view.media.width;text:view.media.width+" × "+view.media.height+" · "+Number(view.media.duration || 0).toFixed(1)+" s";color:Theme.muted;font.family:Theme.mono;font.pixelSize:Theme.fontSmall;wrapMode:Text.WrapAnywhere}
            }
            GridLayout {Layout.fillWidth:true;columns:width>=Theme.px(520)?2:1;columnSpacing:Theme.gapSm;rowSpacing:Theme.gapSm
                StudioTextField {id:sceneFile;objectName:"demoScenePath";Layout.fillWidth:true;Layout.minimumWidth:0;placeholderText:qsTr("Scene file (.json)")}
                StudioButton {text:qsTr("Choose scene path");Layout.maximumWidth:parent.width;onClicked:sceneSaveDialog.open()}
                StudioTextField {id:titleInput;Layout.fillWidth:true;Layout.minimumWidth:0;placeholderText:qsTr("Scene title")}
                StudioComboBox {id:preset;objectName:"demoPreset";Layout.fillWidth:true;Layout.minimumWidth:0;model:view.presetIds}
            }
            Flow {Layout.fillWidth:true;spacing:Theme.gapSm
                StudioButton {objectName:"demoCreate";text:qsTr("Create scene");width:Math.min(parent.width,implicitWidth);accent:true;enabled:!view.dirty && !view.loading && !view.rendering && !!sourceFile.text && !!sceneFile.text;onClicked:view.createScene()}
                StudioButton {text:qsTr("Open scene");width:Math.min(parent.width,implicitWidth);enabled:!view.dirty && !view.loading && !view.rendering;onClicked:sceneOpenDialog.open()}
            }
            Caption {visible:view.capabilities.ready===false;text:qsTr("The video renderer is not ready. Check the rendering tools under Agents & tools.")}
        }
        StudioPanel {
            Layout.fillWidth:true;visible:view.loaded;title:qsTr("Scene")
            lead:qsTr("Keep the original recording or crop its time. Canvas size changes the exported video.")
            GridLayout {Layout.fillWidth:true;columns:width>=Theme.px(420)?2:1;columnSpacing:Theme.gapMd;rowSpacing:Theme.gapSm
                Field {label:qsTr("Title");value:view.draft.title || "";onEdited:value=>view.setField("","title",value)}
                Field {objectName:"demoDuration";label:qsTr("Duration (seconds)");value:view.draft.output?view.draft.output.duration:0;onEdited:value=>view.numberField("output","duration",value)}
                Field {label:qsTr("Canvas width");value:view.draft.output?view.draft.output.width:0;onEdited:value=>view.numberField("output","width",value)}
                Field {label:qsTr("Canvas height");value:view.draft.output?view.draft.output.height:0;onEdited:value=>view.numberField("output","height",value)}
                Field {label:qsTr("Frames per second");value:view.draft.output?view.draft.output.fps:30;onEdited:value=>view.numberField("output","fps",value)}
                Field {label:qsTr("Clip start (seconds)");value:view.draft.clip?view.draft.clip.start:0;onEdited:value=>view.numberField("clip","start",value)}
                Field {label:qsTr("Background color");value:view.draft.background?view.draft.background.color:"";onEdited:value=>view.setField("background","color",value)}
                Field {label:qsTr("Second background color");value:view.draft.background?view.draft.background.color2:"";onEdited:value=>view.setField("background","color2",value)}
                ColumnLayout {Layout.fillWidth:true;Layout.minimumWidth:0;Caption {text:qsTr("Background")} StudioComboBox {Layout.fillWidth:true;Layout.minimumWidth:0;model:["solid","gradient"];currentIndex:Math.max(0,model.indexOf(view.draft.background?view.draft.background.style:"solid"));onActivated:view.setField("background","style",currentText)}}
                Field {label:qsTr("Device finish");value:view.draft.device?view.draft.device.finish:"";onEdited:value=>view.setField("device","finish",value)}
                Field {label:qsTr("Device model");value:view.draft.device?view.draft.device.model:"phone";onEdited:value=>view.setField("device","model",value)}
                Field {label:qsTr("Shadow opacity");value:view.draft.shadow?view.draft.shadow.opacity:0;onEdited:value=>view.numberField("shadow","opacity",value)}
            }
            StudioCheckBox {text:qsTr("Shadow");checked:view.draft.shadow?view.draft.shadow.enabled:false;onToggled:view.setField("shadow","enabled",checked)}
        }
        StudioPanel {
            Layout.fillWidth:true;visible:view.loaded;title:qsTr("Motion")
            lead:qsTr("Each pose sets the device at a moment in the video. The motion runs between them.")
            GridLayout {Layout.fillWidth:true;columns:width>=Theme.px(420)?2:1;columnSpacing:Theme.gapSm;rowSpacing:Theme.gapSm
                StudioComboBox {objectName:"demoPosePicker";Layout.fillWidth:true;Layout.minimumWidth:0;model:view.poses.map((p,i)=>qsTr("Pose %1 · %2 s").arg(i+1).arg(Number(p.time).toFixed(2)));currentIndex:view.selectedPose;onActivated:view.selectedPose=currentIndex}
                Flow {Layout.fillWidth:true;spacing:Theme.gapXs
                    StudioButton {objectName:"demoAddPose";text:qsTr("Add pose");width:Math.min(parent.width,implicitWidth);onClicked:view.addPose()}
                    StudioButton {text:qsTr("Remove pose");width:Math.min(parent.width,implicitWidth);enabled:view.poses.length>1;onClicked:view.removePose()}
                }
            }
            GridLayout {Layout.fillWidth:true;columns:width>=Theme.px(420)?2:1;columnSpacing:Theme.gapMd;rowSpacing:Theme.gapSm
                Field {label:qsTr("Time (seconds)");value:view.pose.time || 0;onEdited:value=>view.poseNumber("time",value)}
                Field {objectName:"demoYaw";label:qsTr("Turn · yaw (degrees)");value:view.pose.yaw || 0;onEdited:value=>view.poseNumber("yaw",value)}
                Field {label:qsTr("Tilt · pitch (degrees)");value:view.pose.pitch || 0;onEdited:value=>view.poseNumber("pitch",value)}
                Field {label:qsTr("Spin · roll (degrees)");value:view.pose.roll || 0;onEdited:value=>view.poseNumber("roll",value)}
                Field {label:qsTr("Scale");value:view.pose.scale || 1;onEdited:value=>view.poseNumber("scale",value)}
                Field {label:qsTr("Horizontal position (0–1)");value:view.pose.x===undefined?.5:view.pose.x;onEdited:value=>view.poseNumber("x",value)}
                Field {label:qsTr("Vertical position (0–1)");value:view.pose.y===undefined?.5:view.pose.y;onEdited:value=>view.poseNumber("y",value)}
                ColumnLayout {Layout.fillWidth:true;Layout.minimumWidth:0;Caption {text:qsTr("Motion curve")} StudioComboBox {Layout.fillWidth:true;Layout.minimumWidth:0;model:view.capabilities.easings || ["smooth","linear","ease-in","ease-out"];currentIndex:Math.max(0,model.indexOf(view.pose.easing || "smooth"));onActivated:view.setPose("easing",currentText)}}
            }
            Flow {Layout.fillWidth:true;spacing:Theme.gapSm
                StudioButton {objectName:"demoSave";text:view.saving?qsTr("Saving…"):qsTr("Save changes");width:Math.min(parent.width,implicitWidth);accent:true;enabled:view.dirty && !Object.keys(view.invalidFields).length && !view.saving && !view.rendering;onClicked:view.saveChanges()}
                StudioButton {text:qsTr("Reload saved scene");width:Math.min(parent.width,implicitWidth);enabled:!view.saving && !view.rendering;onClicked:view.reloadSaved()}
            }
            Caption {visible:view.dirty;text:qsTr("Unsaved changes · save before rendering.")}
        }
        StudioPanel {
            Layout.fillWidth:true;visible:view.loaded;title:qsTr("Preview & export")
            lead:qsTr("Preview one frame, then export a silent MP4. A poster and a render record are saved beside it.")
            Rectangle {Layout.fillWidth:true;implicitHeight:Math.min(Theme.px(440),Math.max(Theme.px(180),width*(view.draft.output?view.draft.output.height/view.draft.output.width:1)));color:Theme.inset;border.color:Theme.border
                Image {objectName:"demoPreviewImage";anchors.fill:parent;anchors.margins:Theme.gapSm;fillMode:Image.PreserveAspectFit;source:view.previewPath?view.fileUrl(view.previewPath):"";cache:false;asynchronous:true}
                Text {anchors.centerIn:parent;width:parent.width-Theme.gapLg;visible:!view.previewPath;text:qsTr("Your rendered preview appears here.");color:Theme.muted;font.pixelSize:Theme.fontBody;horizontalAlignment:Text.AlignHCenter;wrapMode:Text.WordWrap}
            }
            Caption {visible:!!view.previewPath && (view.dirty || view.completedRevision!==view.revision || view.completedScene!==view.scenePath);text:qsTr("This image shows the last completed render.")}
            Field {id:previewTime;label:qsTr("Preview time (seconds)");property string text:"0";value:text;onEdited:value=>text=value}
            Flow {Layout.fillWidth:true;spacing:Theme.gapSm
                StudioButton {objectName:"demoPreview";text:qsTr("Render preview");width:Math.min(parent.width,implicitWidth);enabled:view.canRender;onClicked:previewDialog.open()}
                StudioButton {objectName:"demoExport";text:qsTr("Export MP4");width:Math.min(parent.width,implicitWidth);accent:true;enabled:view.canRender;onClicked:exportDialog.open()}
                StudioButton {text:qsTr("Show exported video");width:Math.min(parent.width,implicitWidth);visible:!!view.completedOutput;onClicked:Studio.revealFile(view.completedOutput)}
            }
            BusyIndicator {visible:view.rendering;running:visible;Layout.preferredHeight:Theme.controlHeight;Layout.preferredWidth:Theme.controlHeight}
            Caption {objectName:"demoRenderStatus";visible:!!view.jobState;text:({queued:qsTr("Waiting to render…"),running:qsTr("Rendering…"),succeeded:qsTr("Render complete."),failed:qsTr("Render failed."),cancelled:qsTr("Render cancelled."),timed_out:qsTr("Render timed out.")})[view.jobState] || view.jobState}
            Caption {visible:!!view.completedOutput;text:view.completedOutput;wrapMode:Text.WrapAnywhere}
        }
    }
    FileDialog {id:sourceDialog;nameFilters:[qsTr("Videos (*.mp4 *.mov *.mkv *.webm)")];onAccepted:{sourceFile.text=view.localPath(selectedFile);view.inspectSource()}}
    FileDialog {id:sceneSaveDialog;fileMode:FileDialog.SaveFile;defaultSuffix:"json";nameFilters:[qsTr("Scenes (*.json)")];onAccepted:sceneFile.text=view.localPath(selectedFile)}
    FileDialog {id:sceneOpenDialog;nameFilters:[qsTr("Scenes (*.json)")];onAccepted:view.openScene(view.localPath(selectedFile))}
    FileDialog {id:previewDialog;fileMode:FileDialog.SaveFile;defaultSuffix:"png";nameFilters:[qsTr("PNG image (*.png)")];onAccepted:view.render("preview",view.localPath(selectedFile))}
    FileDialog {id:exportDialog;fileMode:FileDialog.SaveFile;defaultSuffix:"mp4";nameFilters:[qsTr("MP4 video (*.mp4)")];onAccepted:view.render("export",view.localPath(selectedFile))}
    StudioDialog {id:reloadDialog;title:qsTr("Discard unsaved changes?");modal:true;parent:Overlay.overlay;anchors.centerIn:parent;width:Math.min((parent?parent.width:view.width)-Theme.gapLg,Theme.px(480));contentItem:ColumnLayout {
        Caption {text:qsTr("Reload the saved scene and replace your draft. The video source stays unchanged.")}
        Flow {Layout.fillWidth:true;spacing:Theme.gapSm
            StudioButton {text:qsTr("Keep editing");width:Math.min(parent.width,implicitWidth);onClicked:reloadDialog.close()}
            StudioButton {text:qsTr("Discard and reload");width:Math.min(parent.width,implicitWidth);danger:true;onClicked:{reloadDialog.close();view.discardAndReload()}}
        }
    }}
}
