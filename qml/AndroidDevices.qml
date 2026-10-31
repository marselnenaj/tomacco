import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "."

ColumnLayout {
    id: library
    objectName:"androidDeviceLibrary"
    required property var workspace
    property var avds: []
    property var images: []
    property var requests: ({})
    property var pendingBoots: ({})
    property string createJob: ""
    property bool submitting: false
    property string notice: ""
    property bool failed: false
    property var performanceAvd: ({})
    property bool savingPerformance: false
    readonly property var gpuOptions: [
        {value:"auto",label:qsTr("Automatic")}, {value:"host",label:qsTr("Computer GPU")},
        {value:"software",label:qsTr("Software · compatible renderer")},
        {value:"swiftshader",label:"Software · SwiftShader"}, {value:"lavapipe",label:"Software · Lavapipe"}
    ]
    readonly property var profiles: Studio.models.filter(p=>p.platform==="android")
    readonly property var selectedProfile: profiles[modelPicker.currentIndex] || ({})
    readonly property var phones: workspace.devices.filter(d=>d.platform==="android")
    // Pinned emulators first, then the others as android.avds lists them.
    readonly property var shownAvds: avds.filter(a=>a.pinned).concat(avds.filter(a=>!a.pinned))
    // The device being renamed: {device, name, title}.
    property var renaming: ({})
    function rename(device,current,title) {renaming={device:device,title:title};nicknameField.text=current || "";renameDialog.open();nicknameField.forceActiveFocus()}
    function pin(device,pinned) {request("device.label",{device:device,pinned:pinned})}
    // Wireless debugging pairing: a QR code from android.pair, or the phone's pairing code.
    property string pairMode: "qr"
    property var pairing: ({})
    // "", creating, waiting, pairing, connecting, done or failed
    property string pairState: ""
    property string pairMessage: ""
    property string pairHost: ""
    function openPairing() {pairMode="qr";pairDialog.open();startPairing()}
    function startPairing() {pairing={};pairState="creating";pairMessage="";request("android.pair",{action:"qr"})}
    function waitForPhone() {if(pairDialog.visible && pairMode==="qr" && pairing.service)request("android.pair",{action:"wait",service:pairing.service,password:pairing.password,timeout:15},true)}
    function pairWithCode() {pairState="pairing";pairMessage="";pairHost=pairAddress.text.replace(/:[0-9]+$/,"");request("android.connect",{address:pairAddress.text.trim(),pairing_code:pairCode.text.trim()})}
    function connectTo(address) {pairState="connecting";request("android.connect",{address:address})}
    function pairFailed(message) {pairState="failed";pairMessage=message}
    function pairReply(op,args,response) {
        let value=response.result || {}
        if(op==="android.pair" && args.action==="qr") {
            if(!response.ok){pairFailed(response.error.message);return}
            pairing=value;pairState="waiting";waitForPhone();return
        }
        if(op==="android.pair" && args.action==="wait") {
            if(args.service!==pairing.service || pairMode!=="qr")return
            if(!response.ok){if(response.error.code==="pairing_timeout")waitForPhone();else pairFailed(response.error.message);return}
            pairState="done";pairMessage=value.connected?qsTr("Paired and connected · %1").arg(value.device):qsTr("Paired. Enter the IP address and port shown under Wireless debugging to connect.");refresh();return
        }
        if(op==="android.pair" && args.action==="services") {
            if(!response.ok){pairFailed(response.error.message);return}
            let services=value.services || []
            if(pairState==="connecting"){let found=services.find(s=>s.type.indexOf("_adb-tls-connect")===0 && s.address.replace(/:[0-9]+$/,"")===pairHost);if(found)connectTo(found.address);else{pairState="done";pairMessage=qsTr("Paired. Enter the IP address and port shown under Wireless debugging to connect.")}}
            else{let offered=services.filter(s=>s.type.indexOf("_adb-tls-pairing")===0);if(offered.length===1 && !pairAddress.text)pairAddress.text=offered[0].address}
            return
        }
        if(op==="android.connect") {
            if(!response.ok){pairFailed(response.error.message);return}
            let output=((value.stdout || "")+" "+(value.stderr || "")).trim()
            if(args.pairing_code!==undefined){if(output.indexOf("Successfully paired")<0){pairFailed(output || qsTr("adb could not pair"));return}pairState="connecting";request("android.pair",{action:"services"});return}
            if(output.toLowerCase().indexOf("connected to")<0){pairFailed(output || qsTr("adb could not connect"));return}
            pairState="done";pairMessage=qsTr("Paired and connected · %1").arg(args.address);refresh()
        }
    }
    spacing:Theme.gapMd
    function request(op,args,quiet) {
        if(quiet)for(let id in requests)if(requests[id].op===op && JSON.stringify(requests[id].args)===JSON.stringify(args || {}))return
        let id=Agent.request(op,args || {});requests[id]={op:op,args:args || {}}
    }
    function refresh() {request("android.avds",{},true);request("device.list",{platform:"android"},true)}
    function deviceFor(name) {return phones.find(d=>d.avd===name)}
    function openDevice(device) {workspace.runtimeRequested("android",device.id)}
    function newEmulator() {notice="";failed=false;refresh();nameField.text="Tomacco-"+(selectedProfile.id || "Android");createDialog.open()}
    function performance(avd) {
        performanceAvd=avd;notice="";failed=false
        rendererPicker.currentIndex=Math.max(0,gpuOptions.findIndex(x=>x.value===avd.gpu))
        vulkanPicker.currentIndex=Math.max(0,["auto","on","off"].indexOf(avd.vulkan))
        memoryField.text=String(avd.memory_mb || 4096);coresField.text=String(avd.cores || 4)
        performanceDialog.open()
    }
    function start(avd) {
        let existing=deviceFor(avd.name)
        if(existing && existing.state==="device" && existing.booted!==false){openDevice(existing);return}
        if(pendingBoots[avd.name])return
        pendingBoots=Object.assign({},pendingBoots,{[avd.name]:{}})
        request("android.avd_start",{name:avd.name,headless:true});notice=qsTr("Emulator starting…");failed=false
    }
    onVisibleChanged:if(visible)refresh()
    Component.onCompleted:if(visible)refresh()
    Timer {interval:2500;repeat:true;running:library.visible || Object.keys(library.pendingBoots).length>0 || !!library.createJob;onTriggered:{library.refresh();if(library.createJob)library.request("job.get",{job_id:library.createJob},true);else for(let name in library.pendingBoots)if(library.pendingBoots[name].id)library.request("job.get",{job_id:library.pendingBoots[name].id},true)}}
    Connections {
        target:Agent
        function onApiFinished(id,op,response) {
            let context=library.requests[id];if(!context)return;delete library.requests[id]
            if(op==="android.pair" || op==="android.connect"){library.pairReply(op,context.args,response);return}
            if(!response.ok){library.notice=response.error.message;library.failed=true;if(op==="android.avd_configure")library.savingPerformance=false;if(op==="android.avd_create")library.submitting=false;if(op==="android.avd_start"){let next=Object.assign({},library.pendingBoots);delete next[context.args.name];library.pendingBoots=next}return}
            let value=response.result
            if(op==="android.avds"){library.avds=value.avds;library.images=value.system_images || []}
            if(op==="catalog.import"){Studio.reloadModels();library.notice=qsTr("Model imported");library.failed=false;Qt.callLater(()=>{let index=library.profiles.findIndex(p=>value.imported.indexOf(p.id)>=0);if(index>=0){modelPicker.currentIndex=index;nameField.text="Tomacco-"+library.selectedProfile.id}else library.notice=qsTr("Imported · contains no Android model")})}
            if(op==="device.list"){
                workspace.devices=workspace.devices.filter(d=>d.platform!=="android").concat(value.devices)
                let next=Object.assign({},library.pendingBoots)
                for(let name in next){let device=value.devices.find(d=>d.avd===name && d.state==="device" && d.booted!==false);if(device){delete next[name];library.openDevice(device);library.notice=qsTr("Emulator ready")}}
                library.pendingBoots=next
            }
            if(op==="android.avd_create"){library.submitting=false;library.createJob=value.id;createDialog.close();library.notice=qsTr("Setting up device…")}
            if(op==="android.avd_configure"){library.savingPerformance=false;performanceDialog.close();library.notice=qsTr("Performance settings saved · used on the next start");library.refresh()}
            if(op==="android.emulator")library.refresh()
            if(op==="device.label"){renameDialog.close();library.refresh()}
            if(op==="android.avd_start"){
                if(value.reused && value.device)library.refresh()
                else library.pendingBoots=Object.assign({},library.pendingBoots,{[context.args.name]:value})
            }
            if(op==="job.get" && ["succeeded","failed","cancelled","timed_out"].indexOf(value.state)>=0){
                if(value.id===library.createJob){library.createJob="";library.notice=value.state==="succeeded"?qsTr("Device created · open it with Start"):qsTr("Setup failed · details in the job");library.failed=value.state!=="succeeded";library.refresh()}
                let next=Object.assign({},library.pendingBoots)
                for(let name in next)if(next[name].id===value.id){delete next[name];library.notice=qsTr("Emulator ended · %1").arg(workspace.jobStateLabel(value));library.failed=value.state==="failed"}
                library.pendingBoots=next
                if(library.failed){workspace.activeJob=value;workspace.selectedJob=value.id}
            }
        }
    }
    RowLayout {Layout.fillWidth:true;spacing:Theme.gapMd
        Text {Layout.fillWidth:true;text:qsTr("Each emulator keeps its own model and app data.");color:Theme.muted;font.pixelSize:Theme.fontBody;wrapMode:Text.WordWrap}
        StudioButton {objectName:"createAndroidEmulator";text:library.createJob?qsTr("Creating…"):qsTr("Add device");iconName:"plus";accent:true;enabled:!library.createJob && !library.submitting;onClicked:library.newEmulator()}
    }
    StudioScrollView {
        Layout.fillWidth:true;Layout.fillHeight:true;clip:true;contentWidth:availableWidth
        Column {width:parent.width-Theme.gapMd;spacing:Theme.gapXs
            Repeater {model:library.shownAvds
                Rectangle {
                    required property var modelData
                    readonly property var connected:library.deviceFor(modelData.name)
                    width:parent.width;height:deviceColumn.implicitHeight+Theme.gapMd*2;radius:Theme.radius;color:Theme.surface;border.color:connected && workspace.selectedDevice===connected.id?Theme.accentInk:Theme.subtle
                    RowLayout {anchors.fill:parent;anchors.margins:Theme.gapMd;spacing:Theme.gapMd
                        StudioIcon {name:"phone";color:connected?Theme.accentInk:Theme.faint}
                        ColumnLayout {id:deviceColumn;Layout.fillWidth:true;spacing:Theme.gapXxs
                            Text {objectName:"avdTitle-"+modelData.name;Layout.fillWidth:true;text:modelData.nickname || modelData.profile_name || modelData.name;color:Theme.text;font.pixelSize:Theme.fontBody;font.weight:Font.DemiBold;elide:Text.ElideRight}
                            Text {Layout.fillWidth:true;text:modelData.name+" · "+(modelData.display.width?modelData.display.width+" × "+modelData.display.height:qsTr("External profile"));color:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontMicro;elide:Text.ElideMiddle}
                            RowLayout {Layout.fillWidth:true;spacing:Theme.gapSm
                                StudioStatus {tone:connected?"ok":library.pendingBoots[modelData.name]?"busy":"idle";text:connected?(connected.state==="device"?qsTr("Connected"):connected.state):library.pendingBoots[modelData.name]?qsTr("Starting…"):qsTr("Off")}
                                Text {Layout.fillWidth:true;text:qsTr("Graphics: %1").arg(modelData.gpu || "auto")+(modelData.memory_mb?" · "+modelData.memory_mb+" MB":"");color:Theme.faint;font.pixelSize:Theme.fontSmall;elide:Text.ElideRight}
                            }
                        }
                        StudioButton {objectName:"pinAvd-"+modelData.name;iconName:"pin";displayText:"";quiet:!modelData.pinned;selected:!!modelData.pinned;text:modelData.pinned?qsTr("Unpin"):qsTr("Pin");tip:modelData.pinned?qsTr("Unpin this device"):qsTr("Pin this device to the top");onClicked:library.pin("avd:"+modelData.name,!modelData.pinned)}
                        StudioButton {objectName:"renameAvd-"+modelData.name;iconName:"edit";displayText:"";quiet:true;text:qsTr("Rename");tip:qsTr("Give this device a nickname");onClicked:library.rename("avd:"+modelData.name,modelData.nickname,modelData.profile_name || modelData.name)}
                        StudioButton {objectName:"performanceAvd-"+modelData.name;iconName:"settings";quiet:true;tip:qsTr("Set graphics, memory and CPU");onClicked:library.performance(modelData)}
                        StudioButton {objectName:"startAvd-"+modelData.name;text:connected && connected.state==="device"?qsTr("Open"):library.pendingBoots[modelData.name]?qsTr("Starting…"):qsTr("Start");iconName:connected && connected.state==="device"?"phone":"play";enabled:!library.pendingBoots[modelData.name];onClicked:library.start(modelData)}
                    }
                }
            }
            Repeater {model:library.phones.filter(d=>!library.avds.some(a=>a.name===d.avd))
                ItemDelegate {required property var modelData;width:parent.width;height:Theme.barHeight+Theme.gapMd
                    background:Rectangle {radius:Theme.radius;color:Theme.surface;border.color:workspace.selectedDevice===modelData.id?Theme.accentInk:Theme.subtle}
                    contentItem:RowLayout {spacing:Theme.gapMd
                        StudioIcon {name:"phone";color:Theme.accentInk}
                        ColumnLayout {Layout.fillWidth:true;spacing:Theme.gapXxs
                            Text {text:modelData.name;color:Theme.text;font.pixelSize:Theme.fontBody;font.weight:Font.DemiBold;Layout.fillWidth:true;elide:Text.ElideRight}
                            Text {text:modelData.id+" · "+modelData.state;color:Theme.faint;font.family:Theme.mono;font.pixelSize:Theme.fontMicro;Layout.fillWidth:true;elide:Text.ElideMiddle}
                        }
                        StudioButton {objectName:"pinDevice-"+modelData.id;iconName:"pin";displayText:"";quiet:!modelData.pinned;selected:!!modelData.pinned;text:modelData.pinned?qsTr("Unpin"):qsTr("Pin");tip:modelData.pinned?qsTr("Unpin this device"):qsTr("Pin this device to the top");onClicked:library.pin(modelData.id,!modelData.pinned)}
                        StudioButton {objectName:"renameDevice-"+modelData.id;iconName:"edit";displayText:"";quiet:true;text:qsTr("Rename");tip:qsTr("Give this device a nickname");onClicked:library.rename(modelData.id,modelData.nickname,modelData.original_name || modelData.name)}
                        StudioButton {text:qsTr("Open");enabled:modelData.state==="device";onClicked:library.openDevice(modelData)}
                    }
                    onClicked:workspace.selectedDevice=modelData.id
                }
            }
            StudioEmptyState {visible:!library.avds.length && !library.phones.length;width:Math.min(parent.width,520);topPadding:Theme.gapXxl;title:qsTr("No phone yet.");body:qsTr("Add an emulator, or plug in a phone with USB debugging on.")}
        }
    }
    RowLayout {Layout.fillWidth:true;visible:!!library.notice
        Text {Layout.fillWidth:true;text:library.notice;color:library.failed?Theme.danger:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
        StudioButton {visible:library.failed && !!workspace.selectedJob;text:qsTr("Open job");quiet:true;onClicked:workspace.showTab("jobs")}
    }
    StudioDialog {
        id:createDialog;objectName:"createAndroidDialog";parent:Overlay.overlay;anchors.centerIn:parent;width:Math.min(parent.width-Theme.gapXxxl,620);height:Math.min(parent.height-Theme.gapLg,implicitHeight);modal:true;title:qsTr("Add Android device")
        contentItem:StudioScrollView {clip:true;contentWidth:availableWidth;ColumnLayout {width:parent.width-Theme.gapMd;spacing:Theme.gapMd
            Text {text:qsTr("Model");color:Theme.muted;font.pixelSize:Theme.fontSmall}
            StudioComboBox {id:modelPicker;objectName:"androidModelPicker";Layout.fillWidth:true;model:library.profiles;textRole:"name";onActivated:nameField.text="Tomacco-"+library.selectedProfile.id}
            RowLayout {Layout.fillWidth:true;spacing:Theme.gapLg
                Item {Layout.preferredWidth:72;Layout.preferredHeight:120
                    NativeDeviceFrame {id:modelPreview;profile:library.selectedProfile;backgroundColor:Theme.surface;scale:Math.min(72/width,120/height);anchors.centerIn:parent
                        Rectangle {parent:modelPreview.screen;anchors.fill:parent;color:Theme.inset}
                    }
                }
                Text {Layout.fillWidth:true;text:qsTr("%1 pixels\n%2 dpi\n\nThe model determines the display and frame. Android comes from the chosen system image; vendor interfaces such as One UI are not included.").arg(library.selectedProfile.pixels || "").arg(Math.round((library.selectedProfile.dpr || 1)*160));color:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap;lineHeight:1.3}
            }
            StudioTextField {id:nameField;objectName:"androidAvdName";Layout.fillWidth:true;placeholderText:qsTr("Device name");validator:RegularExpressionValidator {regularExpression:/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/}}
            Text {text:qsTr("Installed Android system");color:Theme.muted;font.pixelSize:Theme.fontSmall}
            StudioComboBox {id:imagePicker;Layout.fillWidth:true;font.family:Theme.mono;model:library.images;displayText:currentText || qsTr("No system image installed yet")}
            StudioButton {text:qsTr("Install Android system");visible:!library.images.length;onClicked:{createDialog.close();workspace.showAction("android.sdk_install")}}
            Text {Layout.fillWidth:true;visible:library.failed;text:library.notice;color:Theme.danger;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            RowLayout {Layout.fillWidth:true;Layout.topMargin:Theme.gapSm;spacing:Theme.gapXs
                StudioButton {text:qsTr("Import custom model");quiet:true;onClicked:modelFile.open()}
                Item {Layout.fillWidth:true}
                StudioButton {text:qsTr("Cancel");onClicked:createDialog.close()}
                StudioButton {objectName:"confirmAndroidCreate";text:library.submitting?qsTr("Creating…"):qsTr("Create");accent:true;enabled:!library.submitting && nameField.acceptableInput && !!nameField.text && !!imagePicker.currentText && !!library.selectedProfile.id;onClicked:{library.submitting=true;library.failed=false;library.request("android.avd_create",{name:nameField.text,profile_id:library.selectedProfile.id,system_image:imagePicker.currentText})}}
            }
        }}
    }
    StudioDialog {
        id:performanceDialog;objectName:"androidPerformanceDialog";parent:Overlay.overlay;anchors.centerIn:parent;width:Math.min(parent.width-Theme.gapXxxl,520);height:Math.min(parent.height-Theme.gapLg,implicitHeight);modal:true;title:qsTr("Emulator performance")
        contentItem:StudioScrollView {clip:true;contentWidth:availableWidth;ColumnLayout {width:parent.width-Theme.gapMd;spacing:Theme.gapSm
            Text {Layout.fillWidth:true;text:library.performanceAvd.name || "";color:Theme.text;font.family:Theme.mono;font.pixelSize:Theme.fontBody}
            Text {Layout.fillWidth:true;text:qsTr("The computer GPU can speed up animations considerably. If graphics glitch, choose a software renderer. Changes require the emulator to be off; app data is kept.");wrapMode:Text.WordWrap;color:Theme.muted;font.pixelSize:Theme.fontSmall}
            Text {text:qsTr("Graphics");color:Theme.muted;font.pixelSize:Theme.fontSmall}
            StudioComboBox {id:rendererPicker;objectName:"androidGpuPicker";Layout.fillWidth:true;model:library.gpuOptions;textRole:"label"}
            Text {text:qsTr("Vulkan compatibility");color:Theme.muted;font.pixelSize:Theme.fontSmall}
            StudioComboBox {id:vulkanPicker;objectName:"androidVulkanPicker";Layout.fillWidth:true;model:[qsTr("Automatic"), qsTr("Enabled"), qsTr("Disabled · OpenGL ES only")]}
            RowLayout {Layout.fillWidth:true;spacing:Theme.gapSm
                ColumnLayout {Layout.fillWidth:true
                    Text {text:qsTr("Memory (MB)");color:Theme.muted;font.pixelSize:Theme.fontSmall}
                    StudioTextField {id:memoryField;objectName:"androidMemoryField";Layout.fillWidth:true;validator:IntValidator {bottom:512;top:32768} inputMethodHints:Qt.ImhDigitsOnly}
                }
                ColumnLayout {Layout.fillWidth:true
                    Text {text:qsTr("CPU cores");color:Theme.muted;font.pixelSize:Theme.fontSmall}
                    StudioTextField {id:coresField;objectName:"androidCoresField";Layout.fillWidth:true;validator:IntValidator {bottom:1;top:32} inputMethodHints:Qt.ImhDigitsOnly}
                }
            }
            Text {Layout.fillWidth:true;visible:library.failed;text:library.notice;color:Theme.danger;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            RowLayout {Layout.fillWidth:true
                StudioButton {objectName:"stopPerformanceAvd";text:qsTr("Turn off emulator");visible:!!library.deviceFor(library.performanceAvd.name);enabled:!library.savingPerformance;onClicked:{let d=library.deviceFor(library.performanceAvd.name);if(d)library.request("android.emulator",{device:d.id,action:"stop"})}}
                Item {Layout.fillWidth:true}
                StudioButton {text:qsTr("Close");enabled:!library.savingPerformance;onClicked:performanceDialog.close()}
                StudioButton {objectName:"saveAndroidPerformance";text:library.savingPerformance?qsTr("Saving…"):qsTr("Save");accent:true;enabled:!library.savingPerformance && !library.deviceFor(library.performanceAvd.name) && !library.pendingBoots[library.performanceAvd.name] && memoryField.acceptableInput && coresField.acceptableInput;onClicked:{library.savingPerformance=true;library.request("android.avd_configure",{name:library.performanceAvd.name,gpu:library.gpuOptions[rendererPicker.currentIndex].value,vulkan:["auto","on","off"][vulkanPicker.currentIndex],memory_mb:Number(memoryField.text),cores:Number(coresField.text)})}}
            }
        }}
    }
    StudioDialog {
        id:renameDialog;objectName:"renameDeviceDialog";parent:Overlay.overlay;anchors.centerIn:parent;width:Math.min(parent.width-Theme.gapXxxl,440);modal:true;title:qsTr("Device nickname")
        contentItem:ColumnLayout {spacing:Theme.gapSm
            Text {Layout.fillWidth:true;text:qsTr("Shown instead of %1 here, in device lists and in the Omarchy panel.").arg(library.renaming.title || "");color:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            StudioTextField {id:nicknameField;objectName:"deviceNicknameField";Layout.fillWidth:true;maximumLength:40;placeholderText:qsTr("Nickname");onAccepted:if(renameSave.enabled)renameSave.clicked()}
            RowLayout {Layout.fillWidth:true;spacing:Theme.gapXs
                StudioButton {objectName:"removeDeviceNickname";text:qsTr("Remove nickname");quiet:true;onClicked:library.request("device.label",{device:library.renaming.device,nickname:""})}
                Item {Layout.fillWidth:true}
                StudioButton {text:qsTr("Cancel");onClicked:renameDialog.close()}
                StudioButton {id:renameSave;objectName:"saveDeviceNickname";text:qsTr("Save");accent:true;enabled:nicknameField.text.trim().length>0;onClicked:library.request("device.label",{device:library.renaming.device,nickname:nicknameField.text.trim()})}
            }
        }
    }
    StudioDialog {
        id:pairDialog;objectName:"pairWifiDialog";parent:Overlay.overlay;anchors.centerIn:parent;width:Math.min(parent.width-Theme.gapXxxl,480);height:Math.min(parent.height-Theme.gapLg,implicitHeight);modal:true;title:qsTr("Pair over Wi-Fi")
        onClosed:{library.pairing={};library.pairState=""}
        contentItem:StudioScrollView {clip:true;contentWidth:availableWidth;ColumnLayout {width:parent.width-Theme.gapMd;spacing:Theme.gapSm
            StudioSegmented {
                current:library.pairMode
                model:[{id:"qr",label:qsTr("QR code"),objectName:"pairModeQr"},{id:"code",label:qsTr("Pairing code"),objectName:"pairModeCode"}]
                onChosen:id=>{library.pairMode=id;library.pairMessage="";if(id==="qr")library.startPairing();else{library.pairing={};library.pairState="";library.request("android.pair",{action:"services"})}}
            }
            Text {Layout.fillWidth:true;wrapMode:Text.WordWrap;color:Theme.muted;font.pixelSize:Theme.fontSmall
                text:library.pairMode==="qr"?qsTr("On the phone, open Developer options › Wireless debugging › Pair device with QR code and scan this code. The phone and this computer must be on the same network; Android 11 or newer."):qsTr("On the phone, open Developer options › Wireless debugging › Pair device with pairing code, then enter the address and code it shows.")}
            Rectangle {
                objectName:"pairQrCode";visible:library.pairMode==="qr";Layout.alignment:Qt.AlignHCenter;Layout.preferredWidth:232;Layout.preferredHeight:232;color:"white";radius:Theme.radiusSmall
                Canvas {
                    id:qrCanvas;objectName:"pairQrCanvas";anchors.fill:parent;anchors.margins:12
                    readonly property var rows:library.pairing.qr?library.pairing.qr.rows:[]
                    onRowsChanged:requestPaint()
                    onPaint:{let c=getContext("2d");c.reset();c.fillStyle="white";c.fillRect(0,0,width,height);if(!rows.length)return;let cell=Math.floor(Math.min(width,height)/rows.length),offset=Math.floor((width-cell*rows.length)/2);c.fillStyle="black";for(let y=0;y<rows.length;y++)for(let x=0;x<rows[y].length;x++)if(rows[y][x]==="1")c.fillRect(offset+x*cell,offset+y*cell,cell,cell)}
                    Accessible.role:Accessible.Graphic;Accessible.name:qsTr("QR code for wireless debugging")
                }
                Text {anchors.centerIn:parent;visible:library.pairState==="creating";text:qsTr("Creating a code…");color:"black";font.pixelSize:Theme.fontSmall}
            }
            GridLayout {visible:library.pairMode==="code";Layout.fillWidth:true;columns:2;columnSpacing:Theme.gapSm;rowSpacing:Theme.gapXs
                Text {text:qsTr("Pairing address");color:Theme.muted;font.pixelSize:Theme.fontSmall}
                StudioTextField {id:pairAddress;objectName:"pairAddressField";Layout.fillWidth:true;placeholderText:"192.168.1.20:41235";font.family:Theme.mono}
                Text {text:qsTr("Pairing code");color:Theme.muted;font.pixelSize:Theme.fontSmall}
                StudioTextField {id:pairCode;objectName:"pairCodeField";Layout.fillWidth:true;placeholderText:"123456";maximumLength:6;inputMethodHints:Qt.ImhDigitsOnly;font.family:Theme.mono;validator:RegularExpressionValidator {regularExpression:/[0-9]{6}/}}
            }
            StudioStatus {objectName:"pairStatus";visible:["creating","waiting","pairing","connecting"].indexOf(library.pairState)>=0;tone:"busy"
                text:library.pairState==="waiting"?qsTr("Waiting for the phone…"):library.pairState==="pairing"?qsTr("Pairing…"):library.pairState==="connecting"?qsTr("Connecting…"):qsTr("Creating a code…")}
            Text {objectName:"pairMessage";Layout.fillWidth:true;visible:["done","failed"].indexOf(library.pairState)>=0;text:library.pairMessage;color:library.pairState==="failed"?Theme.danger:Theme.text;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            RowLayout {Layout.fillWidth:true;Layout.topMargin:Theme.gapSm;spacing:Theme.gapXs
                StudioButton {objectName:"pairNewCode";visible:library.pairMode==="qr" && ["failed","done"].indexOf(library.pairState)>=0;text:qsTr("New code");quiet:true;onClicked:library.startPairing()}
                Item {Layout.fillWidth:true}
                StudioButton {text:qsTr("Close");onClicked:pairDialog.close()}
                StudioButton {objectName:"pairWithCode";visible:library.pairMode==="code";text:qsTr("Pair");accent:true;enabled:pairCode.acceptableInput && /^[A-Za-z0-9.\[\]:_-]+:[0-9]{1,5}$/.test(pairAddress.text.trim()) && ["pairing","connecting"].indexOf(library.pairState)<0;onClicked:library.pairWithCode()}
            }
        }}
    }
    FileDialog {id:modelFile;title:qsTr("Import Android model");nameFilters:[qsTr("Device profile (*.json)")];onAccepted:library.request("catalog.import",{path:decodeURIComponent(selectedFile.toString().replace(/^file:\/\//,""))})}
}
