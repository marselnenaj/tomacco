import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Shapes
import QtCore
import "."
import "NativeProfiles.js" as NativeProfiles

ColumnLayout {
    id: view
    objectName: "iosGuestView"
    focus: true
    property string jobId: ""
    property bool running: false
    property bool observing: false
    property int pending: 0
    property var requests: ({})
    property var guest: ({})
    property string frameSource: ""
    property string frameHash: ""
    property string notice: ""
    property string inputNotice: ""
    property int inputEpoch: 0
    property string capturedAt: ""
    property int frameWidth: 0
    property int frameHeight: 0
    // Inputs wait in a short queue while a frame is being fetched, so a tap
    // is never lost to the preview.
    property var inputQueue: []
    // Uncompressed frames from the user's runtime directory; falls back to
    // inline PNG frames when that transport is unavailable.
    property bool fileFrames: true
    // New pictures per second: what the guest actually drew, not how often
    // this view asked.
    property int framesThisSecond: 0
    property int framesPerSecond: 0
    // Consecutive identical frames; the preview slows down on a still screen.
    property int stillFrames: 0
    readonly property bool canInput: running && !!guest.state && guest.state.running
    // The device model the running guest is described as (a built-in profile),
    // or empty for the profile's own description: a phone with a Home button.
    // iOS lays itself out for that device, so the window shows that device.
    property var deviceProfile: ({})
    readonly property bool modern: !!deviceProfile.id
    // iOS draws this model's island into the picture (see NativeProfiles).
    readonly property bool pictureDrawsCutout: NativeProfiles.pictureDrawsCutout(deviceProfile,deviceProfile.cutout)
    // Panel pixels per point. A model's profile says it (three for every
    // built-in iPhone; the picture confirms it once it is there). The classic
    // phone has only the panel the guest was started with: its density tells
    // (460 ppi: three, 326 ppi: two), and three is assumed until it is known.
    readonly property real frameScale: {
        if(modern && deviceProfile.width>0)return frameWidth>0?frameWidth/deviceProfile.width:(deviceProfile.dpr || 3)
        let panel=guest.requested_display_profile
        if(panel && panel.width>0 && panel.width_mm>0)return Math.max(1,Math.round(panel.width/(panel.width_mm/25.4)/153))
        return 3
    }
    // The zoom the user chose for the phone window, kept between sessions;
    // zero until they choose one. Until then the window opens as sharp as
    // the monitor allows, each panel pixel on one screen pixel where that
    // fits, else as large as fits. That automatic zoom, and the clamp when
    // the window lands on a smaller monitor, are never kept as a choice, so
    // a later open on a monitor that fits the panel is pixel-exact again.
    property real detachedZoom: 0
    Settings {category:"IosGuestDisplay";property alias detachedZoom:view.detachedZoom}
    // Height the docked picture may take; the dialog offers what it has.
    property int dockHeight: 0
    // The guest display can leave the dialog as its own phone-shaped window.
    property bool detached: false
    // Solo: this window is the whole app (see DeviceWindow).
    property bool solo: false
    signal closeRequested()
    signal dockRequested()
    // App bundles dragged onto the picture: the kinds the owner takes (an
    // .app for the virtual iOS device), why others do not fit, and what
    // became of the last one.
    property var appKinds: []
    property string appRefusal: ""
    property string appNotice: ""
    signal appDropped(string path)
    readonly property bool windowVisible: guestWindow.visible
    readonly property bool watching: (observing && visible) || (detached && running)
    onRunningChanged: {
        if(!running){touchUp();inputEpoch+=1;detached=false;inputQueue=[];pending=0;inputRetry.stop();nextFrame.stop();wheelBurst.stop();wheelX=0;wheelY=0;framesThisSecond=0;framesPerSecond=0}
        Qt.callLater(pump)
    }
    spacing: 10

    function queuedAction(action) {return action!=="frame" && action!=="status"}
    function dispatch(action, args, retries) {
        let stored=Object.assign({},args || {})
        if(Array.isArray(stored.keys))stored.keys=stored.keys.slice()
        let id=Agent.request("ios.emulator.control",Object.assign({},stored,{job_id:jobId,action:action}))
        requests[id]={job:jobId,epoch:inputEpoch,action:action,args:stored,retries:retries || 0,file:action==="frame" && stored.transport==="file"}
        pending=id
    }
    function send(action, args) {
        if(!running || !jobId)return false
        let active=requests[pending]
        let held=active && queuedAction(active.action)?1:0
        if(inputQueue.length+held>=16) {
            inputNotice=qsTr("Input queue full · this input was not sent")
            return false
        }
        stillFrames=0
        inputNotice=""
        let stored=Object.assign({},args || {})
        if(Array.isArray(stored.keys))stored.keys=stored.keys.slice()
        inputQueue=inputQueue.concat([{action:action,args:stored,retries:0}])
        pump()
        return true
    }
    // Translate Qt's key values to QEMU's physical key names. Modifiers are
    // pressed with the key in one send-key request, then released by QEMU.
    function sendHardwareKey(key, modifiers) {
        let code=""
        if(key>=Qt.Key_A && key<=Qt.Key_Z)code=String.fromCharCode(key).toLowerCase()
        else if(key>=Qt.Key_0 && key<=Qt.Key_9)code=String.fromCharCode(key)
        else if(key>=Qt.Key_F1 && key<=Qt.Key_F12)code="f"+(key-Qt.Key_F1+1)
        else {
            const special={}
            special[Qt.Key_Return]="ret";special[Qt.Key_Enter]="kp_enter"
            special[Qt.Key_Escape]="esc";special[Qt.Key_Backspace]="backspace"
            special[Qt.Key_Tab]="tab";special[Qt.Key_Backtab]="tab"
            special[Qt.Key_Space]="spc";special[Qt.Key_Minus]="minus"
            special[Qt.Key_Equal]="equal";special[Qt.Key_BracketLeft]="bracket_left"
            special[Qt.Key_BracketRight]="bracket_right";special[Qt.Key_Semicolon]="semicolon"
            special[Qt.Key_Apostrophe]="apostrophe";special[Qt.Key_QuoteLeft]="grave_accent"
            special[Qt.Key_Backslash]="backslash";special[Qt.Key_Comma]="comma"
            special[Qt.Key_Period]="dot";special[Qt.Key_Slash]="slash"
            special[Qt.Key_Left]="left";special[Qt.Key_Right]="right"
            special[Qt.Key_Up]="up";special[Qt.Key_Down]="down"
            special[Qt.Key_Home]="home";special[Qt.Key_End]="end"
            special[Qt.Key_PageUp]="pgup";special[Qt.Key_PageDown]="pgdn"
            special[Qt.Key_Insert]="insert";special[Qt.Key_Delete]="delete"
            special[Qt.Key_Shift]="shift";special[Qt.Key_Control]="ctrl"
            special[Qt.Key_Alt]="alt";special[Qt.Key_Meta]="meta_l"
            code=special[key] || ""
        }
        if(!code || !canInput)return false
        let keys=[]
        if((modifiers & Qt.ControlModifier) && code!=="ctrl")keys.push("ctrl")
        if((modifiers & Qt.AltModifier) && code!=="alt")keys.push("alt")
        if((modifiers & Qt.ShiftModifier) && code!=="shift")keys.push("shift")
        if((modifiers & Qt.MetaModifier) && code!=="meta_l")keys.push("meta_l")
        keys.push(code)
        send("key",{keys:keys,hold_ms:100})
        return true
    }
    Keys.onPressed: event=>{if(sendHardwareKey(event.key,event.modifiers))event.accepted=true}
    // Decides what to do once nothing is in flight: queued input first, then
    // the next frame while someone is watching.
    // The zoom a window opens at when the user has chosen none: one panel
    // pixel per screen pixel where that fits, else the largest clean ratio of
    // screen pixels to iOS points (2, 1.5 or 1) that fits. Between them (the
    // 1.7 or 2.27 that merely fill the screen) glyph strokes land between
    // screen pixels and text looks soft; only below one pixel per point is
    // the picture shown as large as fits.
    function sharpZoom(fit) {
        // A zoom is in the monitor's logical pixels; a scaled monitor (Hyprland's
        // 1.25, 1.5, 2) has more physical ones to each.
        const exact=frameScale/guestWindow.pixelRatio
        for(const zoom of [exact,2,1.5,1])if(zoom<=exact && zoom<=fit)return zoom
        return Math.min(exact,fit)
    }
    function pump() {
        if(!running || !jobId || pending || inputRetry.running)return
        if(inputQueue.length) {
            let next=inputQueue[0];inputQueue=inputQueue.slice(1)
            dispatch(next.action,next.args,next.retries);return
        }
        if(!watching)return
        if(!guest.state)dispatch("status")
        else dispatch("frame",fileFrames?{transport:"file"}:{})
    }
    function refresh() {pump()}
    // Going home. A device without a Home button is left with a swipe up from
    // the bottom edge. Unlock depends on the native lock state; the Home usage is sent first
    // because it is what brings an app in front back to the Home screen.
    function home() {
        send("home")
        if(modern)send("drag",{x:.5,y:.99,to_x:.5,to_y:.25,duration_ms:1500})
    }
    // The system gestures of a device without a Home button, as one-shot
    // drags that start exactly at the screen's edge, where iOS looks for them.
    // The app switcher is the swipe up that pauses before the finger lifts;
    // the others lift while still moving, so iOS completes them.
    readonly property var systemGestures: ({
        appSwitcher:{x:.5,y:1,to_x:.5,to_y:.6,duration_ms:600,hold_ms:800},
        notificationCenter:{x:.25,y:0,to_x:.25,to_y:.6,duration_ms:400},
        controlCenter:{x:.85,y:0,to_x:.85,to_y:.6,duration_ms:400},
        back:{x:0,y:.5,to_x:.75,to_y:.5,duration_ms:350}
    })
    function systemGesture(name) {
        let drag=systemGestures[name]
        if(!drag || !canInput)return false
        // A finger still down from the mouse would cross the gesture.
        touchUp()
        return send("drag",drag)
    }
    // Shortcuts as in Xcode's Simulator, with Ctrl for Command. They are the
    // application's while this device has the keyboard: docked in the active
    // main window, or in its own window when that one is active.
    readonly property bool shortcutsActive: canInput && (detached?guestWindow.active:visible && Window.active)
    readonly property bool canHome: canInput && guest.input_transport==="usb-hid"
    Shortcut {sequence:"Ctrl+Shift+H";context:Qt.ApplicationShortcut;enabled:view.shortcutsActive && view.canHome;onActivated:view.home()}
    Shortcut {sequence:"Ctrl+Shift+L";context:Qt.ApplicationShortcut;enabled:view.shortcutsActive && view.guest.power_button_supported===true;onActivated:view.send("power_button")}
    Shortcut {sequence:"Ctrl+Shift+A";context:Qt.ApplicationShortcut;enabled:view.shortcutsActive && view.modern;onActivated:view.systemGesture("appSwitcher")}
    Shortcut {sequence:"Ctrl+Shift+N";context:Qt.ApplicationShortcut;enabled:view.shortcutsActive && view.modern;onActivated:view.systemGesture("notificationCenter")}
    Shortcut {sequence:"Ctrl+Shift+C";context:Qt.ApplicationShortcut;enabled:view.shortcutsActive && view.modern;onActivated:view.systemGesture("controlCenter")}
    Shortcut {sequence:"Ctrl+Shift+B";context:Qt.ApplicationShortcut;enabled:view.shortcutsActive && view.modern;onActivated:view.systemGesture("back")}
    onJobIdChanged: {
        // A contact on the guest before keeps its job and is lifted there.
        touchUp();liveTouch=true
        inputEpoch+=1;inputRetry.stop();nextFrame.stop();wheelBurst.stop()
        frameSource="";frameHash="";capturedAt="";guest=({});notice="";inputNotice="";pending=0;inputQueue=[]
        frameWidth=0;frameHeight=0;framesThisSecond=0;framesPerSecond=0
        // Each guest gets the fast transport again; only its own refusal turns it off.
        fileFrames=true;stillFrames=0;wheelX=0;wheelY=0
        Qt.callLater(pump)
    }
    // Live touch: press, moves and release reach the guest as they happen,
    // through the persistent lane that also fetches frames (Agent keeps one
    // tomaccoctl mcp child). One touch request is in flight at a time; moves
    // wait coalesced to the latest point, at most about 120 a second. Off
    // where the control plane refuses the lane: a gesture is then played
    // back after release, as one click or drag.
    property bool liveTouch: true
    // The contact being made: {serial, job, x, y, failed, ended, gesture, played}.
    property var touch: null
    property int touchSerial: 0
    property var touchQueue: []
    property int touchPending: 0
    property var touchRequests: ({})
    function queueTouch(action, x, y) {
        stillFrames=0
        let event={action:action,serial:touch.serial,job:touch.job,x:x,y:y}
        let queue=touchQueue.slice(),last=queue[queue.length-1]
        if(action==="touch_move" && last && last.action==="touch_move" && last.serial===event.serial)queue[queue.length-1]=event
        else queue.push(event)
        touchQueue=queue
        pumpTouch()
    }
    function pumpTouch() {
        if(touchPending || touchPace.running || !touchQueue.length)return
        let next=touchQueue[0];touchQueue=touchQueue.slice(1)
        let id=Agent.request("ios.emulator.control",{job_id:next.job,action:next.action,x:next.x,y:next.y})
        touchRequests[id]=next;touchPending=id
        touchPace.restart()
    }
    function touchDown(x, y) {
        touchUp()
        touchSerial+=1
        touch={serial:touchSerial,job:jobId,x:x,y:y,failed:false,ended:false,gesture:null,played:false}
        queueTouch("touch_down",x,y)
        touchKeep.restart()
    }
    function touchMove(x, y) {
        if(!touch || touch.failed || touch.ended)return
        touch.x=x;touch.y=y
        queueTouch("touch_move",x,y)
    }
    // Every contact ends with a release, whatever became of its press.
    function touchUp() {
        if(!touch || touch.ended)return
        touch.ended=true;touchKeep.stop()
        queueTouch("touch_up",touch.x,touch.y)
    }
    // Ends the contact; a gesture the lane could not carry is played back.
    function touchEnd(gesture) {
        if(!touch)return
        touch.gesture=gesture
        touchUp()
        if(touch.failed)playBack(touch)
    }
    function playBack(contact) {
        if(contact.played || !contact.gesture || contact.job!==jobId)return
        contact.played=true
        let g=contact.gesture
        if(g.tap)send("click",{x:g.fromX,y:g.fromY,duration_ms:g.duration})
        // Played back at the speed of the hand: iOS turns a page or
        // flings a list from the release velocity, and a slowed-down
        // swipe of less than half a page springs back.
        else send("drag",{x:g.fromX,y:g.fromY,to_x:g.x,to_y:g.y,duration_ms:Math.max(60,g.duration)})
    }
    function touchAnswered(event, response) {
        let contact=touch && touch.serial===event.serial?touch:null
        if(!response.ok && contact && event.action!=="touch_up" && !contact.failed) {
            let code=(response.error || {}).code
            contact.failed=true;touchKeep.stop()
            // A lifted contact takes no more moves; its release still goes.
            touchQueue=touchQueue.filter(next=>next.serial!==event.serial || next.action==="touch_up")
            // An older control plane without the lane, or a one-shot one.
            if(code==="invalid_argument" || code==="ios_touch_lane_required")liveTouch=false
            // A press that never took is the whole gesture, played back once it is known.
            if(event.action==="touch_down" && contact.ended)playBack(contact)
        }
        pumpTouch()
    }
    // Pacing between touch requests, and the keep-alive of a contact held
    // still (the control plane lifts one without any event for 5 s).
    Timer {id:touchPace;objectName:"iosGuestTouchPace";interval:8;onTriggered:view.pumpTouch()}
    Timer {id:touchKeep;interval:1000;repeat:true;onTriggered:if(view.touch && !view.touch.ended && !view.touch.failed)view.queueTouch("touch_move",view.touch.x,view.touch.y)}
    Component.onDestruction: {
        // Nothing will send the queue any more.
        let open=touch && !touch.ended?touch:touchQueue.find(next=>next.action==="touch_up")
        if(open)Agent.request("ios.emulator.control",{job_id:open.job,action:"touch_up"})
    }
    // Wheel movement becomes one touch drag per burst: a touchscreen has no
    // wheel, and a drag per notch would queue up behind the slow guest.
    property real wheelX: 0
    property real wheelY: 0
    property real wheelAtX: .5
    property real wheelAtY: .5
    function scroll(atX, atY, dx, dy) {
        wheelAtX=atX;wheelAtY=atY;wheelX+=dx;wheelY+=dy
        wheelBurst.restart()
    }
    Timer {
        id:wheelBurst;interval:140
        onTriggered: {
            // One notch moves the finger an eighth of the screen, at most half of it.
            let far=value=>Math.max(-.5,Math.min(.5,value/120*.125))
            let dx=far(view.wheelX),dy=far(view.wheelY)
            view.wheelX=0;view.wheelY=0
            if(Math.abs(dx)<.01 && Math.abs(dy)<.01)return
            // Start away from the edges, where iOS has gestures of its own.
            let inside=value=>Math.max(.12,Math.min(.88,value))
            let x=inside(view.wheelAtX),y=inside(view.wheelAtY)
            // Rest at the end so iOS does not read the burst as a fling and
            // scroll on past where the wheel stopped.
            view.send("drag",{x:x,y:y,to_x:Math.max(.02,Math.min(.98,x+dx)),to_y:Math.max(.02,Math.min(.98,y+dy)),duration_ms:360,hold_ms:150})
        }
    }
    onObservingChanged: Qt.callLater(pump)
    onWatchingChanged: Qt.callLater(pump)
    // Frames follow each other with a short pause; inline PNG frames are far
    // more expensive and keep a slower pace.
    Timer {id:nextFrame;interval:!view.fileFrames?500:view.stillFrames>8?220:60;onTriggered:view.pump()}
    // Only the lane owner can certify that no action was submitted. Keep the
    // refused head in order, at most two retries, and pause newer input/frames.
    Timer {id:inputRetry;interval:250;onTriggered:view.pump()}
    // Restarts the loop after a refused request and keeps the rate display honest.
    Timer {
        interval:1000;repeat:true;running:view.watching && view.running
        onTriggered:{view.framesPerSecond=view.framesThisSecond;view.framesThisSecond=0;view.pump()}
    }
    DeviceWindow {
        id:guestWindow
        objectName:"iosGuestWindow"
        visible:view.detached && view.running
        deviceWidth:view.modern?modernFrame.width:(view.frameWidth>0?view.frameWidth/view.frameScale:390)+28
        deviceHeight:view.modern?modernFrame.height:(view.frameHeight>0?view.frameHeight/view.frameScale:844)+146
        caption:qsTr("Virtual iOS device")
        controlsEnabled:view.canInput
        showHome:view.guest.input_transport==="usb-hid"
        showPower:view.guest.power_button_supported===true
        // A device without a Home button goes back with the left-edge swipe.
        showBack:view.modern
        showFlip:view.modern && !!view.deviceProfile.back
        homeShortcut:"Ctrl+Shift+H";powerShortcut:"Ctrl+Shift+L";backShortcut:"Ctrl+Shift+B"
        onBackRequested:view.systemGesture("back")
        forwardHardwareKeys:true
        // Up to one panel pixel per screen pixel: the picture is never
        // enlarged beyond what the guest drew.
        maxZoom:Math.max(1.5,view.frameScale/guestWindow.pixelRatio)
        onVisibleChanged:if(visible)setZoom(view.detachedZoom>0?view.detachedZoom:view.sharpZoom(fitZoom))
        // Only a zoom the user chose is kept (see detachedZoom).
        onZoomChosen:value=>view.detachedZoom=value
        solo:view.solo
        // Docking returns to the dialog; the app restores its main window.
        onDockRequested:{view.detached=false;view.dockRequested()}
        // Hidden first, so this window does not veto the app's own shutdown.
        onCloseRequested:{view.detached=false;view.closeRequested()}
        onHomeRequested:view.home()
        onPowerRequested:view.send("power_button")
        onHardwareKeyRequested:(key,modifiers)=>view.sendHardwareKey(key,modifiers)
        onScreenshotRequested:view.send("screenshot")
    }
    // The body of a current iPhone: the same frame the web preview and the
    // Android devices use, drawn from the model's profile. The picture is cut
    // to the screen's corners. A notch or punch hole is the frame's; an island
    // is iOS's, in the picture.
    NativeDeviceFrame {
        id:modernFrame
        objectName:"iosGuestModernFrame"
        parent:guestWindow.host
        // Scaled from its top left corner, centred in the window on whole pixels.
        transformOrigin:Item.TopLeft
        x:NativeProfiles.pixelOrigin(parent.x,(parent.width-width*scale)/2,screenPixels/scale)
        y:NativeProfiles.pixelOrigin(parent.y,(parent.height-height*scale)/2,screenPixels/scale)
        pixelAligned:true
        pictureHasCutout:!!view.frameSource && view.pictureDrawsCutout
        visible:view.detached && view.modern
        profile:view.deviceProfile
        sourceSize:Qt.size(view.frameWidth,view.frameHeight)
        framed:true;roundedScreen:true;backgroundColor:"transparent"
        scale:guestWindow.zoom
        // The bezel around the screen is the screen's edge (see edgeBand);
        // the frame hands its presses there over. The body beyond it and the
        // window's margin still move the window.
        screenReach:contact.edgeReach
        onScreenEdgePressed:at=>{const p=screen.mapToItem(contact,at.x,at.y);contact.begin(p.x,p.y)}
        onScreenEdgeMoved:at=>{const p=screen.mapToItem(contact,at.x,at.y);contact.follow(p.x,p.y)}
        onScreenEdgeReleased:at=>{const p=screen.mapToItem(contact,at.x,at.y);contact.lift(p.x,p.y)}
        onScreenEdgeCanceled:contact.cancel()
        onBezelPressed:guestWindow.move()
        rearView:guestWindow.flipped && guestWindow.showFlip
        // The side button on the body is the guest's power button.
        onHardwareKey:key=>{if(key==="power" && view.guest.power_button_supported===true && view.canInput)view.send("power_button")}
    }
    Connections {
        target:Agent
        function onApiFinished(id,operation,response) {
            let touchEvent=view.touchRequests[id]
            if(touchEvent) {
                delete view.touchRequests[id]
                if(view.touchPending===id)view.touchPending=0
                view.touchAnswered(touchEvent,response);return
            }
            let context=view.requests[id]
            if(!context)return
            delete view.requests[id]
            if(context.job!==view.jobId || context.epoch!==view.inputEpoch)return
            if(view.pending===id)view.pending=0
            if(!response.ok) {
                let error=response.error || ({})
                // A session without a private runtime directory, or an older
                // control plane that does not know the transport: keep the
                // picture alive with inline frames. Any other failure says
                // nothing about the transport.
                if(context.file && (error.code==="ios_frame_file_unavailable" || error.code==="invalid_argument")) {
                    view.fileFrames=false;nextFrame.restart();return
                }
                if(error.code==="ios_control_busy") {
                    if(view.queuedAction(context.action)) {
                        let proof=error.details || ({})
                        if(proof.automatic_retry_safe===true && proof.request_may_have_executed===false && context.retries<2 && view.running) {
                            view.inputQueue=[{action:context.action,args:context.args,retries:context.retries+1}].concat(view.inputQueue)
                            inputRetry.restart();return
                        }
                        view.inputNotice=qsTr("Control still busy · input not delivered")
                    }
                    nextFrame.restart();return
                }
                if(view.queuedAction(context.action)) {
                    view.inputNotice=error.message || qsTr("Input failed")
                    if((error.details || {}).request_may_have_executed===true || error.code==="control_plane_error")
                        view.inputNotice=qsTr("%1 · outcome unclear, not retried automatically").arg(view.inputNotice)
                }
                view.notice=error.message || qsTr("Display output unreachable")
                if(error.code==="ios_guest_paused")view.guest=({})
                // Queued input still goes out; the slow timer retries the picture.
                if(view.inputQueue.length)nextFrame.restart()
                return
            }
            let result=response.result.result
            if(context.action==="status") {view.guest=result;view.pump()}
            else if(context.action==="frame") {
                if(context.file) {
                    view.stillFrames=result.changed===false?view.stillFrames+1:0
                    if(result.changed!==false)view.framesThisSecond+=1
                    view.frameSource="file://"+result.path;view.frameHash="frame-"+result.sequence
                } else if(result.sha256!==view.frameHash) {
                    view.frameSource=result.data_url;view.frameHash=result.sha256;view.framesThisSecond+=1
                }
                view.frameWidth=result.width;view.frameHeight=result.height
                view.capturedAt=new Date().toLocaleTimeString(Qt.locale(),"HH:mm:ss")
                view.notice=""
                if(view.inputQueue.length)view.pump();else nextFrame.restart()
            } else if(context.action==="screenshot") {view.notice=(Studio.copyImage(result.path)?qsTr("Screenshot saved and copied: %1"):qsTr("Capture saved: %1")).arg(result.path);view.pump()}
            else view.pump()
        }
    }
    Item {
        id:dockSlot
        objectName:"iosGuestDock"
        Layout.fillWidth:true;Layout.preferredHeight:view.detached?72:Math.max(460,view.dockHeight)
        Label {
            anchors.centerIn:parent;visible:view.detached;color:Theme.muted
            text:qsTr("The iOS device is running in its own window")
        }
    }
    AppDropArea {
        objectName:"iosGuestAppDrop"
        parent:view.detached?guestWindow.host:dockSlot
        anchors.fill:parent;z:3
        kinds:view.appKinds;targetName:qsTr("Virtual iOS device");refusal:view.appRefusal
        onAppsDropped:paths=>view.appDropped(paths[0])
        onRefused:reason=>view.appNotice=reason
    }
    Rectangle {
        id:screenBox
        parent:view.detached?guestWindow.host:dockSlot
        anchors.fill:parent
        visible:!(view.detached && view.modern)
        color:view.detached?"#0b0c10":"#08090c";radius:view.detached?46*guestWindow.zoom:10
        border.color:view.detached?"#3a3f4b":Theme.border;border.width:view.detached?2:1
        // The bezel moves the detached window; the picture keeps its own input.
        MouseArea {anchors.fill:parent;enabled:view.detached;onPressed:guestWindow.move()}
        // The guest draws a rectangular, home-button display (its status bar
        // reaches into the corners), so the detached body is a classic iPhone:
        // square screen, forehead with earpiece, chin with a working Home button.
        Rectangle {visible:view.detached;anchors.horizontalCenter:parent.horizontalCenter;y:28*guestWindow.zoom;width:52*guestWindow.zoom;height:5*guestWindow.zoom;radius:height/2;color:"#23262e"}
        Rectangle {visible:view.detached;x:parent.width/2-52*guestWindow.zoom;y:25*guestWindow.zoom;width:10*guestWindow.zoom;height:width;radius:width/2;color:"#161a22";border.color:"#262b36"}
        Rectangle {
            objectName:"iosGuestHomeButton"
            visible:view.detached;anchors.horizontalCenter:parent.horizontalCenter;anchors.bottom:parent.bottom;anchors.bottomMargin:19*guestWindow.zoom
            width:48*guestWindow.zoom;height:width;radius:width/2
            color:homeArea.pressed?"#2a2f3a":"transparent";border.width:2;border.color:homeArea.containsMouse && homeArea.enabled?Theme.accent:"#3a3f4b"
            opacity:homeArea.enabled?1:.45
            MouseArea {id:homeArea;anchors.fill:parent;hoverEnabled:true;enabled:view.canInput && view.guest.input_transport==="usb-hid";cursorShape:Qt.PointingHandCursor;onClicked:view.home()}
        }
        Image {
            id:frame;objectName:"iosGuestFrame"
            // Inside the modern frame the picture fills the screen exactly.
            readonly property bool inFrame:view.detached && view.modern
            parent:inFrame?modernFrame.screen:screenBox
            anchors.fill:parent
            anchors.leftMargin:inFrame?0:view.detached?14*guestWindow.zoom:8;anchors.rightMargin:anchors.leftMargin
            anchors.topMargin:inFrame?0:view.detached?60*guestWindow.zoom:8;anchors.bottomMargin:inFrame?0:view.detached?86*guestWindow.zoom:8
            source:view.frameSource
            // The opening has the picture's aspect by construction; decoded at a rounded
            // size it would otherwise be fitted a pixel short of it.
            fillMode:inFrame?Image.Stretch:Image.PreserveAspectFit
            // Each frame is a new file: keep showing the last one until the
            // next has been decoded, and never cache them.
            cache:false;asynchronous:true;retainWhileLoading:true
            // The panel has more pixels than fit on a 2160-pixel screen, so the
            // picture is usually shown at about half size. Scaled on the GPU
            // (bilinear, a 2x2 footprint) that drops every second row of text
            // pixels and the picture looks soft and frayed; decoded at the size
            // it is shown, the reader averages every panel pixel and the GPU
            // then draws it one to one. Never larger than the frame itself.
            readonly property real screenScale: {
                width;height;guestWindow.zoom;inFrame;view.detached
                const origin=mapToItem(null,0,0),corner=mapToItem(null,1000,0)
                return Math.max(.01,(corner.x-origin.x)/1000)*Screen.devicePixelRatio
            }
            sourceSize.height:view.frameWidth>0 && view.frameHeight>0?
                Math.min(view.frameHeight,Math.ceil(Math.min(width/view.frameWidth,height/view.frameHeight)*view.frameHeight*screenScale)):0
            // The frame just around the picture: a press there is a touch
            // on the screen's nearest edge, so dragging in from the bezel is
            // an edge swipe as on the device. Beneath the picture, which
            // keeps every press on itself.
            MouseArea {
                id:edgeBand;objectName:"iosGuestEdgeBand"
                x:contact.x-contact.edgeReach;y:contact.y-contact.edgeReach
                width:contact.width+contact.edgeReach*2;height:contact.height+contact.edgeReach*2
                enabled:contact.enabled;cursorShape:Qt.PointingHandCursor;preventStealing:true
                function local(mouse){return mapToItem(contact,mouse.x,mouse.y)}
                onPressed: mouse=>{const p=local(mouse);contact.begin(p.x,p.y)}
                onPositionChanged: mouse=>{if(pressed){const p=local(mouse);contact.follow(p.x,p.y)}}
                onReleased: mouse=>{const p=local(mouse);contact.lift(p.x,p.y)}
                onCanceled: contact.cancel()
            }
            MouseArea {
                id:contact;objectName:"iosGuestContact"
                anchors.centerIn:parent;width:frame.paintedWidth;height:frame.paintedHeight
                enabled:view.canInput && view.frameWidth>0
                cursorShape:Qt.PointingHandCursor
                // A swipe on the phone is the phone's, not the dialog's scroll view's.
                preventStealing:true
                // Screen pixels per point of the model's display, as painted here.
                readonly property real pointScale:view.modern && view.deviceProfile.width>0?width/view.deviceProfile.width:0
                // iOS does not round the screen's corners, and draws an island
                // but no notch or punch hole. Outside the model's frame (docked,
                // or in the classic body) the picture gets what it lacks here,
                // at its own scale.
                Shape {
                    id:guestCorners;objectName:"iosGuestCorners"
                    visible:!frame.inFrame && contact.pointScale>0 && (view.deviceProfile.radius || 0)>0
                    anchors.fill:parent;preferredRendererType:Shape.CurveRenderer
                    // The display's own corners: continuous curves, per corner
                    // where they differ (the Duo's hinge side).
                    readonly property var radii:(view.deviceProfile.cornerRadii || [0,0,0,0].map(()=>view.deviceProfile.radius || 0)).map(r=>Math.min(r,view.deviceProfile.width/2)*contact.pointScale)
                    ShapePath {strokeWidth:0;fillColor:screenBox.color;fillRule:ShapePath.OddEvenFill
                        PathSvg {path:NativeProfiles.outline(-1,-1,contact.width+2,contact.height+2,0,false)+" "+NativeProfiles.outline(0,0,contact.width,contact.height,guestCorners.radii,true)}
                    }
                }
                Rectangle {
                    id:dockedCutout;objectName:"iosGuestIsland"
                    readonly property string cutout:view.deviceProfile.cutout || "none"
                    readonly property bool punch:cutout==="punch" || cutout==="punch-right"
                    visible:!frame.inFrame && contact.pointScale>0 && cutout!=="none" && !view.pictureDrawsCutout
                    readonly property bool notch:cutout==="notch"
                    width:(view.deviceProfile.cutoutWidth || (punch?13:notch?164:119))*contact.pointScale
                    height:(view.deviceProfile.cutoutHeight || (punch?13:notch?34:35))*contact.pointScale
                    x:view.deviceProfile.cutoutX!==undefined?view.deviceProfile.cutoutX*contact.pointScale:cutout==="punch-right"?parent.width-width-16*contact.pointScale:(parent.width-width)/2
                    y:(view.deviceProfile.cutoutOffset===undefined?(notch?0:11):view.deviceProfile.cutoutOffset)*contact.pointScale
                    // A notch hangs from the top edge with rounded lower corners.
                    radius:Math.min(width,height)/2
                    topLeftRadius:notch?0:radius;topRightRadius:topLeftRadius
                    bottomLeftRadius:notch?Math.min(height,(view.deviceProfile.cutoutRadius || 12)*contact.pointScale):radius;bottomRightRadius:bottomLeftRadius
                    color:"#020205"
                }
                property real fromX:0
                property real fromY:0
                property real pressedAt:0
                // Where the pointer went down, in this item's units.
                property real pressX:0
                property real pressY:0
                // Left the few pixels around the press: from then on the
                // contact follows the pointer; until then a release is a tap.
                property bool moved:false
                // The press was taken to the screen's edge (see edgeAt).
                property bool fromEdge:false
                // The live contact of this press, or 0 where it is played back.
                property int contactSerial:0
                // Screen pixels per unit of this item: the model's frame is
                // scaled as a whole, the docked picture is not.
                readonly property real shown:Math.max(.01,frame.screenScale/Screen.devicePixelRatio)
                // iOS opens its system gestures (Home and the app switcher from
                // the bottom, the two centres from the top corners, back from
                // the left) only for a touch that starts at the very edge, which
                // a pointer rarely hits on a picture with round corners. A press
                // this close to an edge starts there: about 4 % of the screen's
                // width, 6 to 12 screen pixels, in this item's units.
                readonly property real edgeMargin:Math.max(6,Math.min(12,Math.min(width,height)*shown*.04))/shown
                // How far the frame around the picture counts as its edge: the
                // bezel of the model's frame, the dark margin around the docked
                // picture, a strip of the classic body that stops short of its
                // Home button. Never more than 18 screen pixels.
                readonly property real edgeReach:Math.min(18/shown,frame.inFrame?modernFrame.bezel:8*(view.detached?guestWindow.zoom:1)/shown)
                function at(px,py){return Qt.point(Math.max(0,Math.min(1,px/width)),Math.max(0,Math.min(1,py/height)))}
                // Where a press at (px,py) puts the finger: on the edge it is
                // near or beyond, else where it is.
                function edgeAt(px,py) {
                    const m=edgeMargin,point=at(px,py)
                    const edgeX=px<=m?0:px>=width-m?1:-1,edgeY=py<=m?0:py>=height-m?1:-1
                    return {x:edgeX>=0?edgeX:point.x,y:edgeY>=0?edgeY:point.y,edge:edgeX>=0 || edgeY>=0}
                }
                function begin(px,py) {
                    if(!view.detached)view.forceActiveFocus()
                    const start=edgeAt(px,py)
                    fromX=start.x;fromY=start.y;fromEdge=start.edge;pressX=px;pressY=py
                    pressedAt=Date.now();moved=false;touchMark.show(px,py)
                    contactSerial=0
                    if(view.liveTouch){view.touchDown(fromX,fromY);contactSerial=view.touchSerial}
                }
                function follow(px,py) {
                    touchMark.show(px,py)
                    // An edge swipe follows at once: iOS tracks it from the first
                    // movement and the few pixels away from the edge are its start.
                    if(!moved && (fromEdge?px!==pressX || py!==pressY:Math.hypot(px-pressX,py-pressY)>=6))moved=true
                    if(moved){let point=at(px,py);view.touchMove(point.x,point.y)}
                }
                function lift(px,py) {
                    touchMark.release()
                    let point=at(px,py)
                    let gesture={fromX:fromX,fromY:fromY,x:point.x,y:point.y,duration:Math.max(1,Math.min(2000,Date.now()-pressedAt)),
                        tap:fromEdge?!moved:Math.hypot((point.x-fromX)*width,(point.y-fromY)*height)<6}
                    // An edge press that stayed put is a tap at the edge.
                    if(gesture.tap && fromEdge){gesture.x=fromX;gesture.y=fromY}
                    if(!contactSerial){view.playBack({job:view.jobId,gesture:gesture});return}
                    // Live, the finger lifts where it is: at the press point
                    // after a tap, so iOS sees the press and release of a click.
                    // Right after the last move and without a rest, so a flick
                    // keeps its speed. A contact already lifted (focus lost) is
                    // not played back.
                    if(moved)view.touchMove(point.x,point.y)
                    if(view.touch && view.touch.serial===contactSerial && !view.touch.ended)view.touchEnd(gesture)
                }
                function cancel() {touchMark.release();view.touchUp()}
                onPressed: mouse=>begin(mouse.x,mouse.y)
                onPositionChanged: mouse=>{if(pressed)follow(mouse.x,mouse.y)}
                onReleased: mouse=>lift(mouse.x,mouse.y)
                // A lost grab or window lifts the finger; nothing is played back.
                onCanceled: cancel()
                readonly property bool windowActive:Window.active
                onWindowActiveChanged: if(!windowActive)cancel()
                onWheel: wheel=>{
                    // A MouseArea with a wheel handler takes every wheel event. Ctrl+wheel resizes the
                    // device window (DeviceWindow), so it passes on to the items behind this one.
                    if(wheel.modifiers & Qt.ControlModifier){wheel.accepted=false;return}
                    let pixels=wheel.pixelDelta.x!==0 || wheel.pixelDelta.y!==0
                    let dx=pixels?wheel.pixelDelta.x*2:wheel.angleDelta.x,dy=pixels?wheel.pixelDelta.y*2:wheel.angleDelta.y
                    // Shift turns a plain wheel sideways, as everywhere on the desktop.
                    if(wheel.modifiers & Qt.ShiftModifier && dx===0){dx=dy;dy=0}
                    view.scroll(wheel.x/width,wheel.y/height,dx,dy)
                }
                // The guest answers a touch late. The mark shows at once where
                // the finger is, so a touch is never mistaken for a missed click.
                Rectangle {
                    id:touchMark;objectName:"iosGuestTouchMark"
                    width:34;height:34;radius:17;visible:opacity>0;opacity:0
                    color:"#40ffffff";border.color:"#b0ffffff";border.width:1.5
                    function show(px,py){fade.stop();x=px-width/2;y=py-height/2;opacity=1}
                    function release(){fade.restart()}
                    NumberAnimation {id:fade;target:touchMark;property:"opacity";to:0;duration:420}
                }
            }
        }
        Label {
            parent:frame.parent
            anchors.centerIn:parent;width:parent.width-48;horizontalAlignment:Text.AlignHCenter
            visible:!view.frameSource;color:Theme.muted;wrapMode:Text.Wrap
            text:view.running?qsTr("Waiting for the display output of iOS…"):qsTr("No running interactive iOS session")
        }
    }
    RowLayout {
        Layout.fillWidth:true
        Label {
            objectName:"iosGuestFrameStatus";Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;font.pixelSize:12
            text:view.capturedAt?(view.running?qsTr("Live frame · %1").arg(view.framesPerSecond>0?qsTr("%n new frame(s)/s","",view.framesPerSecond):qsTr("Still frame")):qsTr("Last frame"))+" · "+view.frameWidth+" × "+view.frameHeight+" · "+view.capturedAt:qsTr("Real output of the virtual display")
        }
        StudioButton {objectName:"iosGuestPower";text:qsTr("Side button");visible:view.guest.power_button_supported===true;enabled:view.canInput;tip:qsTr("Wake or lock the display")+" · Ctrl+Shift+L";onClicked:view.send("power_button")}
        StudioButton {objectName:"iosGuestHome";text:"Home";enabled:view.canHome;tip:(view.modern?qsTr("Home button and swipe up from the bottom edge"):qsTr("Home button"))+" · Ctrl+Shift+H";onClicked:view.home()}
        StudioButton {text:"Screenshot";enabled:view.running;onClicked:view.send("screenshot")}
        StudioButton {objectName:"iosGuestDetach";text:view.detached?qsTr("Dock"):qsTr("Own window");iconName:"phone";selected:view.detached;enabled:view.running;tip:qsTr("Detach the virtual iOS device into its own window");onClicked:view.detached=!view.detached}
    }
    // The system gestures of a device without a Home button, one click or
    // shortcut each. By hand they start at the screen's edge or on the frame
    // around it.
    RowLayout {
        objectName:"iosGuestGestures"
        Layout.fillWidth:true;visible:view.modern
        Label {Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;font.pixelSize:12;text:qsTr("Edge swipes start on the frame or at the screen's edge")}
        Repeater {
            model:[
                {name:"appSwitcher",text:qsTr("App switcher"),tip:qsTr("Swipe up from the bottom edge and pause"),keys:"Ctrl+Shift+A"},
                {name:"notificationCenter",text:qsTr("Notification Center"),tip:qsTr("Swipe down from the top-left edge"),keys:"Ctrl+Shift+N"},
                {name:"controlCenter",text:qsTr("Control Center"),tip:qsTr("Swipe down from the top-right edge"),keys:"Ctrl+Shift+C"},
                {name:"back",text:qsTr("Back"),tip:qsTr("Swipe right from the left edge"),keys:"Ctrl+Shift+B"}
            ]
            StudioButton {
                required property var modelData
                objectName:"iosGuestGesture_"+modelData.name
                text:modelData.text;quiet:true;enabled:view.canInput
                tip:modelData.tip+" · "+modelData.keys
                onClicked:view.systemGesture(modelData.name)
            }
        }
    }
    Label {visible:!!view.inputNotice || !!view.appNotice || !!view.notice;Layout.fillWidth:true;wrapMode:Text.Wrap;color:Theme.muted;font.pixelSize:12;text:view.inputNotice || view.appNotice || view.notice}
}
