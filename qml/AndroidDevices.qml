import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
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
    // A phone-wide workspace: one row per emulator and phone; a tap opens its page (Workspace.phoneDevice).
    readonly property bool phone: workspace.phone===true
    function stateLabel(avd) {const connected=deviceFor(avd.name);return connected?(connected.state==="device"?qsTr("Connected"):connected.state):pendingBoots[avd.name]?qsTr("Starting…"):qsTr("Off")}
    function showOnPhone(id,device) {if(device && device.state==="device"){workspace.selectedDevice=device.id;workspace.selectedPlatform="android"}workspace.phoneDevice=id}
    // Pinned emulators first, then the others as android.avds lists them.
    // All screens: the running devices side by side instead of the list.
    property bool gridMode: false
    readonly property var runningPhones: phones.filter(d=>d.state==="device" && d.booted!==false)
    readonly property var shownAvds: avds.filter(a=>a.pinned).concat(avds.filter(a=>!a.pinned))
    // The device being renamed: {device, name, title}.
    property var renaming: ({})
    function rename(device,current,title) {renaming={device:device,title:title};nicknameField.text=current || "";renameDialog.open();nicknameField.forceActiveFocus()}
    // The AVD being managed (duplicate, real rename, wipe) and android.avd_usage by AVD name.
    property var managing: ({})
    property var usage: ({})
    property string manageAction: ""
    readonly property var managedUsage: usage[managing.name] || ({})
    readonly property bool managedRunning: !!deviceFor(managing.name) || !!pendingBoots[managing.name] || !!managedUsage.running || (managedUsage.locks || []).length>0
    function manage(avd) {managing=avd;manageAction="";manageName.text="";manageConfirm.text="";failed=false;manageDialog.open();request("android.avd_usage",{},true)}
    function manageRequest(action) {
        let args={action:action,name:managing.name}
        if(action!=="wipe")args.new_name=manageName.text.trim()
        if(action!=="duplicate")args.confirm=manageConfirm.text.trim()
        manageAction=action;failed=false;request("android.avd_manage",args)
    }
    // The plan android.avd_plan made for the create dialog's current name, model and image.
    property var plan: ({})
    property bool planning: false
    readonly property bool planNeedsLicense: !!plan.steps && plan.steps.length>0 && !!plan.steps[0].needs_license
    function planCreate() {
        if(!createDialog.visible || !nameField.acceptableInput || !nameField.text || !imageField.text.trim() || !selectedProfile.id){plan={};return}
        planning=true;request("android.avd_plan",{name:nameField.text,profile_id:selectedProfile.id,system_image:imageField.text.trim()},true)
    }
    function formatBytes(n) {
        if(n===undefined || n===null)return "?"
        if(n>=1073741824)return (n/1073741824).toFixed(1)+" GB"
        if(n>=1048576)return Math.round(n/1048576)+" MB"
        return Math.max(1,Math.round(n/1024))+" kB"
    }
    function planSummary() {
        if(plan.error)return plan.error
        if(planning && !plan.steps)return qsTr("Checking…")
        if(!plan.steps)return ""
        let lines=[]
        lines.push(plan.image_installed?qsTr("System image installed · no download"):qsTr("Download about %1 (estimate) · needs Google's SDK licence: %2").arg(formatBytes(plan.estimate.download_bytes)).arg(plan.licenses_accepted?qsTr("already accepted"):qsTr("not yet accepted")))
        lines.push(qsTr("Disk: about %1 for the emulator · %2 free").arg(formatBytes(plan.estimate.avd_bytes)).arg(formatBytes(plan.free_bytes)))
        if(plan.blockers.length)lines.push(qsTr("Cannot start: %1").arg(plan.blockers.map(b=>b==="avd_exists"?qsTr("the name is taken"):b==="form_factor_unsupported"?qsTr("TV, Wear, Automotive and XR images are not supported yet"):b==="avd_space"?qsTr("not enough free space"):b.replace("_missing"," "+qsTr("is missing")).replace("sdk ",qsTr("the Android SDK")+" ")).join(" · ")))
        else lines.push(qsTr("Steps: %1").arg(plan.steps.filter(s=>s.needed).map(s=>s.step==="install_image"?qsTr("install image"):s.step==="create"?qsTr("create"):s.step==="profile"?qsTr("apply model"):qsTr("start")).join(" → ")))
        return lines.join("\n")
    }
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
            if(op==="android.avd_plan"){library.planning=false;library.plan=response.ok?response.result:{error:response.error.message};return}
            if(!response.ok){library.notice=response.error.message;library.failed=true;if(op==="android.avd_configure")library.savingPerformance=false;if(op==="android.avd_create" || op==="android.avd_setup")library.submitting=false;if(op==="android.avd_manage")library.manageAction="";if(op==="android.avd_start"){let next=Object.assign({},library.pendingBoots);delete next[context.args.name];library.pendingBoots=next}return}
            let value=response.result
            // Only a changed answer replaces the lists: an equal one would rebuild every row and drop a tap in progress.
            if(op==="android.avds"){if(JSON.stringify(value.avds)!==JSON.stringify(library.avds))library.avds=value.avds;const images=value.system_images || [];if(JSON.stringify(images)!==JSON.stringify(library.images))library.images=images}
            if(op==="catalog.import"){Studio.reloadModels();library.notice=qsTr("Model imported");library.failed=false;Qt.callLater(()=>{let index=library.profiles.findIndex(p=>value.imported.indexOf(p.id)>=0);if(index>=0){modelPicker.currentIndex=index;nameField.text="Tomacco-"+library.selectedProfile.id}else library.notice=qsTr("Imported · contains no Android model")})}
            if(op==="device.list"){
                const devices=workspace.devices.filter(d=>d.platform!=="android").concat(value.devices)
                if(JSON.stringify(devices)!==JSON.stringify(workspace.devices))workspace.devices=devices
                let next=Object.assign({},library.pendingBoots)
                for(let name in next){let device=value.devices.find(d=>d.avd===name && d.state==="device" && d.booted!==false);if(device){delete next[name];library.openDevice(device);library.notice=qsTr("Emulator ready")}}
                library.pendingBoots=next
            }
            if(op==="android.avd_create" || op==="android.avd_setup"){library.submitting=false;library.createJob=value.id;createDialog.close();library.notice=qsTr("Setting up device…")}
            if(op==="android.avd_usage"){let next={};for(let a of value.avds)next[a.name]=a;library.usage=next}
            if(op==="android.avd_manage"){
                library.manageAction=""
                if(context.args.action==="duplicate"){library.createJob=value.id;manageDialog.close();library.notice=qsTr("Copying device…")}
                else{manageDialog.close();library.notice=context.args.action==="rename"?qsTr("Renamed to %1").arg(value.to):qsTr("App data removed · %1 freed · the next start boots like a new device").arg(library.formatBytes(value.freed_bytes));library.refresh()}
            }
            if(op==="android.avd_configure"){library.savingPerformance=false;performanceDialog.close();library.notice=qsTr("Performance settings saved · used on the next start");library.refresh()}
            if(op==="android.emulator")library.refresh()
            if(op==="device.label"){if(context.args.nickname!==undefined)renameDialog.close();library.refresh()}
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
    RowLayout {Layout.fillWidth:true;spacing:Theme.gapMd;visible:!library.phone
        Text {Layout.fillWidth:true;text:qsTr("Each emulator keeps its own model and app data.");color:Theme.muted;font.pixelSize:Theme.fontBody;wrapMode:Text.WordWrap}
        StudioButton {objectName:"deviceGridToggle";text:library.gridMode?qsTr("List"):qsTr("All screens");displayText:library.width<Theme.px(760)?"":text;iconName:library.gridMode?"devices":"grid";quiet:true;selected:library.gridMode;tip:library.gridMode?qsTr("Show the device list"):qsTr("Show every running device's screen side by side");onClicked:library.gridMode=!library.gridMode}
        StudioButton {objectName:"createAndroidEmulator";text:library.createJob?qsTr("Creating…"):qsTr("Add device");iconName:"plus";accent:true;enabled:!library.createJob && !library.submitting;onClicked:library.newEmulator()}
    }
    DeviceGrid {
        objectName:"androidDeviceGrid";Layout.fillWidth:true;Layout.fillHeight:true;Layout.minimumHeight:Theme.px(320);visible:library.gridMode && !library.phone
        devices:library.runningPhones;selected:workspace.selectedDevice
        onChosen:device=>{workspace.selectedDevice=device.id;library.openDevice(device)}
    }
    // The phone's list: the action that adds a device first, then a row per device with its state on the title line.
    StudioButton {objectName:"createAndroidEmulatorPhone";visible:library.phone;Layout.fillWidth:true;Layout.leftMargin:Theme.mobileGutter;Layout.rightMargin:Theme.mobileGutter;touch:true;accent:true;iconName:"plus";text:library.createJob?qsTr("Creating…"):qsTr("Add device");enabled:!library.createJob && !library.submitting;onClicked:library.newEmulator()}
    Column {
        objectName:"androidPhoneList";visible:library.phone;Layout.fillWidth:true
        Repeater {model:library.phone?library.shownAvds:[]
            MobileRow {
                required property var modelData
                readonly property var connected:library.deviceFor(modelData.name)
                objectName:"avdRow-"+modelData.name;width:parent.width
                title:modelData.nickname || modelData.profile_name || modelData.name
                detail:modelData.name+" · "+(modelData.display && modelData.display.width?modelData.display.width+" × "+modelData.display.height:qsTr("External profile"))+(modelData.pinned?" · "+qsTr("Pinned"):"")
                meta:library.stateLabel(modelData)
                dot:connected?Theme.success:library.pendingBoots[modelData.name]?Theme.warning:Theme.faint
                highlighted:workspace.phoneDevice==="avd:"+modelData.name
                onClicked:library.showOnPhone("avd:"+modelData.name,connected)
            }
        }
        Repeater {model:library.phone?library.phones.filter(d=>!library.avds.some(a=>a.name===d.avd)):[]
            MobileRow {
                required property var modelData
                objectName:"phoneRow-"+modelData.id;width:parent.width
                title:modelData.name;detail:modelData.id;monoDetail:true
                meta:modelData.state==="device"?qsTr("Connected"):modelData.state
                dot:modelData.state==="device"?Theme.success:modelData.state==="unauthorized"?Theme.warning:Theme.faint
                highlighted:workspace.phoneDevice===modelData.id
                onClicked:library.showOnPhone(modelData.id,modelData)
            }
        }
        StudioEmptyState {visible:!library.avds.length && !library.phones.length;width:parent.width;topPadding:Theme.gapXl;showMark:false;title:qsTr("No phone yet.");body:qsTr("Add an emulator, or plug in a phone with USB debugging on.")}
    }
    StudioScrollView {
        Layout.fillWidth:true;Layout.fillHeight:true;clip:true;contentWidth:availableWidth;visible:!library.gridMode && !library.phone
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
                        StudioButton {objectName:"renameAvd-"+modelData.name;iconName:"edit";displayText:"";quiet:true;text:qsTr("Nickname");tip:qsTr("Give this device a nickname");onClicked:library.rename("avd:"+modelData.name,modelData.nickname,modelData.profile_name || modelData.name)}
                        StudioButton {objectName:"manageAvd-"+modelData.name;iconName:"copy";displayText:"";quiet:true;text:qsTr("Manage");tip:qsTr("Duplicate, rename or wipe this emulator");onClicked:library.manage(modelData)}
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
            StudioEmptyState {visible:!library.avds.length && !library.phones.length;width:Math.min(parent.width,Theme.px(520));topPadding:Theme.gapXxl;title:qsTr("No phone yet.");body:qsTr("Add an emulator, or plug in a phone with USB debugging on.")}
        }
    }
    RowLayout {Layout.fillWidth:true;visible:!!library.notice;Layout.leftMargin:library.phone?Theme.mobileGutter:0;Layout.rightMargin:Layout.leftMargin
        Text {Layout.fillWidth:true;text:library.notice;color:library.failed?Theme.danger:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
        StudioButton {visible:library.failed && !!workspace.selectedJob;text:qsTr("Open job");quiet:true;onClicked:workspace.showTab("jobs")}
    }
    StudioDialog {
        id:createDialog;objectName:"createAndroidDialog";parent:Overlay.overlay;anchors.centerIn:parent;width:Math.min(parent.width-Theme.gapXxxl,Theme.px(620));height:Math.min(parent.height-Theme.gapLg,implicitHeight);modal:true;title:qsTr("Add Android device")
        onOpened:{if(!imageField.text && library.images.length)imageField.text=library.images[0];library.plan={};library.planCreate()}
        Timer {id:planTimer;interval:350;onTriggered:library.planCreate()}
        contentItem:StudioScrollView {clip:true;contentWidth:availableWidth;ColumnLayout {width:parent.width-Theme.gapMd;spacing:Theme.gapMd
            Text {text:qsTr("Model");color:Theme.muted;font.pixelSize:Theme.fontSmall}
            StudioComboBox {id:modelPicker;objectName:"androidModelPicker";Layout.fillWidth:true;model:library.profiles;textRole:"name";onActivated:{nameField.text="Tomacco-"+library.selectedProfile.id;planTimer.restart()}}
            RowLayout {Layout.fillWidth:true;spacing:Theme.gapLg
                Item {Layout.preferredWidth:Theme.px(72);Layout.preferredHeight:Theme.px(120)
                    NativeDeviceFrame {id:modelPreview;profile:library.selectedProfile;backgroundColor:Theme.surface;scale:Math.min(72/width,120/height);anchors.centerIn:parent
                        Rectangle {parent:modelPreview.screen;anchors.fill:parent;color:Theme.inset}
                    }
                }
                Text {Layout.fillWidth:true;text:qsTr("%1 pixels\n%2 dpi\n\nThe model determines the display and frame. Android comes from the chosen system image; vendor interfaces such as One UI are not included.").arg(library.selectedProfile.pixels || "").arg(Math.round((library.selectedProfile.dpr || 1)*160));color:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap;lineHeight:1.3}
            }
            StudioTextField {id:nameField;objectName:"androidAvdName";Layout.fillWidth:true;placeholderText:qsTr("Device name");validator:RegularExpressionValidator {regularExpression:/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/} onTextChanged:planTimer.restart()}
            Text {text:qsTr("Android system image");color:Theme.muted;font.pixelSize:Theme.fontSmall}
            StudioComboBox {id:imagePicker;objectName:"androidImagePicker";Layout.fillWidth:true;font.family:Theme.mono;model:library.images;displayText:currentText || qsTr("No system image installed yet");onActivated:imageField.text=currentText}
            StudioTextField {id:imageField;objectName:"androidImageField";Layout.fillWidth:true;font.family:Theme.mono;placeholderText:"system-images;android-36;google_apis;x86_64";onTextChanged:planTimer.restart()}
            Text {Layout.fillWidth:true;text:qsTr("Pick an installed image, or type another package: it is downloaded as the first step. Tomacco creates phone, foldable and tablet emulators.");color:Theme.faint;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            Text {objectName:"androidPlanText";Layout.fillWidth:true;visible:text.length>0;text:library.planSummary();color:library.plan.error || (library.plan.blockers && library.plan.blockers.length)?Theme.danger:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap;lineHeight:1.3}
            StudioCheckBox {id:acceptLicense;objectName:"androidAcceptLicense";Layout.fillWidth:true;Layout.minimumWidth:0;visible:library.planNeedsLicense;text:qsTr("Accept Google's Android SDK licence for this download")}
            StudioCheckBox {id:startAfter;objectName:"androidStartAfter";Layout.fillWidth:true;Layout.minimumWidth:0;checked:true;text:qsTr("Start the emulator when it is ready")}
            Text {Layout.fillWidth:true;visible:library.failed;text:library.notice;color:Theme.danger;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            Flow {Layout.fillWidth:true;Layout.topMargin:Theme.gapSm;spacing:Theme.gapXs;layoutDirection:Qt.RightToLeft
                StudioButton {objectName:"confirmAndroidCreate";text:library.submitting?qsTr("Creating…"):library.planNeedsLicense?qsTr("Download and create"):qsTr("Create");accent:true;enabled:!library.submitting && !library.planning && !!library.plan.ready && (!library.planNeedsLicense || acceptLicense.checked);onClicked:{library.submitting=true;library.failed=false;let args={name:nameField.text,profile_id:library.selectedProfile.id,system_image:imageField.text.trim()};if(library.planNeedsLicense)args.accept_licenses=true;if(startAfter.checked)args.start={headless:true};library.request("android.avd_setup",args)}}
                StudioButton {text:qsTr("Cancel");onClicked:createDialog.close()}
                StudioButton {text:qsTr("Import custom model");quiet:true;onClicked:modelFile.open()}
            }
        }}
    }
    StudioDialog {
        id:performanceDialog;objectName:"androidPerformanceDialog";parent:Overlay.overlay;anchors.centerIn:parent;width:Math.min(parent.width-Theme.gapXxxl,Theme.px(520));height:Math.min(parent.height-Theme.gapLg,implicitHeight);modal:true;title:qsTr("Emulator performance")
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
        id:manageDialog;objectName:"manageAvdDialog";parent:Overlay.overlay;anchors.centerIn:parent;width:Math.min(parent.width-Theme.gapXxxl,Theme.px(560));height:Math.min(parent.height-Theme.gapLg,implicitHeight);modal:true;title:qsTr("Manage emulator")
        contentItem:StudioScrollView {clip:true;contentWidth:availableWidth;ColumnLayout {width:parent.width-Theme.gapMd;spacing:Theme.gapSm
            Text {Layout.fillWidth:true;text:library.managing.name || "";color:Theme.text;font.family:Theme.mono;font.pixelSize:Theme.fontBody;elide:Text.ElideMiddle}
            Text {objectName:"manageAvdUsage";Layout.fillWidth:true;wrapMode:Text.WordWrap;color:Theme.muted;font.pixelSize:Theme.fontSmall
                text:library.managedUsage.bytes===undefined?qsTr("Reading disk usage…"):qsTr("On disk: %1 · a wipe frees %2").arg(library.formatBytes(library.managedUsage.bytes)).arg(library.formatBytes(library.managedUsage.wipe_bytes))}
            StudioStatus {objectName:"manageAvdRunning";visible:library.managedRunning;tone:"warn";text:qsTr("The emulator is on or its lock files exist. Turn it off first; nothing here changes a running emulator.")}
            Text {Layout.fillWidth:true;text:qsTr("New name");color:Theme.muted;font.pixelSize:Theme.fontSmall}
            StudioTextField {id:manageName;objectName:"manageAvdNewName";Layout.fillWidth:true;font.family:Theme.mono;placeholderText:qsTr("Letters, digits, - and _");validator:RegularExpressionValidator {regularExpression:/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/}}
            Text {Layout.fillWidth:true;text:qsTr("Duplicate copies the emulator with its apps and data under the new name, as a job you can cancel. Rename is a real rename of the emulator through the Android SDK; the nickname moves along. A nickname alone is set with the pencil button.");color:Theme.faint;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            Flow {Layout.fillWidth:true;spacing:Theme.gapXs
                StudioButton {objectName:"duplicateAvd";text:library.manageAction==="duplicate"?qsTr("Copying…"):qsTr("Duplicate");iconName:"copy";enabled:!library.manageAction && !library.managedRunning && manageName.acceptableInput && !!manageName.text;onClicked:library.manageRequest("duplicate")}
                StudioButton {objectName:"renameAvdReal";text:library.manageAction==="rename"?qsTr("Renaming…"):qsTr("Rename");iconName:"edit";enabled:!library.manageAction && !library.managedRunning && manageName.acceptableInput && !!manageName.text && manageConfirm.text.trim()===library.managing.name;onClicked:library.manageRequest("rename")}
            }
            Text {Layout.fillWidth:true;Layout.topMargin:Theme.gapSm;text:qsTr("Rename and wipe need the emulator's name typed here");color:Theme.muted;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            StudioTextField {id:manageConfirm;objectName:"manageAvdConfirm";Layout.fillWidth:true;font.family:Theme.mono;placeholderText:library.managing.name || ""}
            Text {Layout.fillWidth:true;text:qsTr("Wipe removes the apps, their data and the snapshots; the next start boots like a new device. The emulator's model and settings stay.");color:Theme.faint;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            Text {Layout.fillWidth:true;visible:library.failed && manageDialog.visible;text:library.notice;color:Theme.danger;font.pixelSize:Theme.fontSmall;wrapMode:Text.WordWrap}
            Flow {Layout.fillWidth:true;Layout.topMargin:Theme.gapSm;spacing:Theme.gapXs;layoutDirection:Qt.RightToLeft
                StudioButton {text:qsTr("Close");enabled:!library.manageAction;onClicked:manageDialog.close()}
                StudioButton {objectName:"wipeAvd";text:library.manageAction==="wipe"?qsTr("Wiping…"):qsTr("Wipe app data");danger:true;enabled:!library.manageAction && !library.managedRunning && manageConfirm.text.trim()===library.managing.name && !!library.managing.name;onClicked:library.manageRequest("wipe")}
            }
        }}
    }
    StudioDialog {
        id:renameDialog;objectName:"renameDeviceDialog";parent:Overlay.overlay;anchors.centerIn:parent;width:Math.min(parent.width-Theme.gapXxxl,Theme.px(440));modal:true;title:qsTr("Device nickname")
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
        id:pairDialog;objectName:"pairWifiDialog";parent:Overlay.overlay;anchors.centerIn:parent;width:Math.min(parent.width-Theme.gapXxxl,Theme.px(480));height:Math.min(parent.height-Theme.gapLg,implicitHeight);modal:true;title:qsTr("Pair over Wi-Fi")
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
                objectName:"pairQrCode";visible:library.pairMode==="qr";Layout.alignment:Qt.AlignHCenter;Layout.preferredWidth:Theme.px(232);Layout.preferredHeight:Theme.px(232);color:"white";radius:Theme.radiusSmall
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
    FileChooserDialog {id:modelFile;objectName:"androidModelFileDialog";mode:"open";title:qsTr("Import Android model");nameFilters:[qsTr("Device profile (*.json)")];onAccepted:library.request("catalog.import",{path:selectedPath})}
}
