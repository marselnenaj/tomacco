import QtQuick

Item {
    id: automation
    required property var app
    required property var device
    function state() {
        let result={session:Agent.session,device:app.currentModel.id,platform:app.currentModel.platform,workspace:app.workspaceState(),native:app.nativeState(),mode:app.nativeMode?"runtime":"preview",
            device_category:app.deviceCategory,visible_devices:app.visibleDevices.map(d=>d.id),
            cutout:{type:device.cutout,notch:device.notchVisible,island:device.islandVisible,width:device.model.cutoutWidth || 0,height:device.model.cutoutHeight || 0},
            orientation:app.rotated?"landscape":"portrait",unfolded:app.foldedOpen,safe_area:app.safeMode,guides:app.guidesOn,
            zoom:app.requestedZoom,url:device.browser.url.toString(),loading:device.browser.loading,error:device.loadError,
            viewport:{width:device.viewportWidth,height:device.viewportHeight},screen:{width:device.screenWidth,height:device.screenHeight},
            status_style:device.statusStyle,status_edge:device.barEdge,status_bar:device.showChrome,chrome_theme:device.chromeTheme,effective_chrome_theme:device.effectiveChromeTheme,density:device.model.dpr,engine:app.nativeMode?"Native device video / Qt":"Qt WebEngine / Chromium",nativeRuntime:app.nativeMode && app.nativeState().status==="live"}
        if(app.nativeMode) {
            const native=result.native
            result.device=native.profile_id || native.device;result.platform=native.platform;result.device_category=native.platform
            result.visible_devices=Studio.models.filter(p=>p.platform===native.platform).map(p=>p.id)
            result.orientation=native.orientation;result.unfolded=native.unfolded
            result.screen={width:native.width,height:native.height,units:"device-pixels"};result.viewport=result.screen
            result.density=native.reference_density;result.frame=native.frame_visible
            result.status_style="system";result.status_edge="system";result.status_bar=true
            result.cutout=null;result.safe_area=null;result.guides=false;result.zoom=null;result.url=""
            result.chrome_theme="system";result.effective_chrome_theme="system";result.error=native.error;result.loading=native.status==="connecting"
        }
        return result
    }
    function evaluate(id,script) {
        let wrapped="(()=>{try{return {value:(0,eval)("+JSON.stringify(script)+")}}catch(e){return {failure:String(e)}}})()"
        device.browser.runJavaScript(wrapped,function(value) {
            if(!value) Agent.complete(id,null,"JavaScript returned no result")
            else if(value.failure) Agent.complete(id,null,value.failure)
            else Agent.complete(id,value.value===undefined?null:value.value)
        })
    }
    Connections {
        target: Agent
        function onPreviewRequest(id,method,args) {
            try {
                if(method==="state") { Agent.complete(id,automation.state()); return }
                if(method==="show") { app.showWorkspace(args.tab || "project", args.path || "", args.file || "", args.line || 1); Agent.complete(id,{shown:true,session:Agent.session,tab:args.tab || "project"}); return }
                if(method==="console") { Agent.complete(id,device.consoleMessages); return }
                if(method==="navigate") {
                    let url=Studio.normalizeUrl(args.url||"")
                    if(!url.toString().length) throw new Error("Invalid URL")
                    app.openUrl(args.url); Qt.callLater(()=>Agent.complete(id,automation.state())); return
                }
                if(method==="configure") {
                    if(args.native!==undefined) app.configureNative(args.native)
                    if(args.ios_guest!==undefined) app.configureIosGuest(args.ios_guest)
                    let index=app.selectedIndex
                    if(args.device!==undefined) {
                        index=app.devices.findIndex(x=>x.id===args.device)
                        if(index<0) throw new Error("Unknown device profile: "+args.device)
                    }
                    if(args.orientation!==undefined && ["portrait","landscape"].indexOf(args.orientation)<0) throw new Error("Invalid orientation")
                    if(args.unfolded===true && !app.devices[index].openWidth) throw new Error("This model is not foldable")
                    if(args.zoom!==undefined && (!isFinite(args.zoom) || args.zoom<0 || args.zoom>3)) throw new Error("Zoom outside 0–3")
                    if(index!==app.selectedIndex) app.selectDevice(index)
                    if(args.orientation!==undefined) app.rotated=args.orientation==="landscape"
                    if(args.unfolded!==undefined) app.foldedOpen=args.unfolded
                    if(args.safe_area!==undefined) app.safeMode=args.safe_area
                    if(args.guides!==undefined) app.guidesOn=args.guides
                    if(args.zoom!==undefined) app.requestedZoom=args.zoom
                    if(args.status_bar!==undefined) device.showChrome=args.status_bar
                    if(args.chrome_theme!==undefined) device.chromeTheme=args.chrome_theme
                    Qt.callLater(()=>Agent.complete(id,automation.state())); return
                }
                if(method==="reload") { device.browser.reload(); Agent.complete(id,{reloading:true}); return }
                if(method==="catalog_reload") { app.selectDevice(0); Studio.reloadModels(); Agent.complete(id,{count:Studio.models.length}); return }
                if(method==="evaluate") { automation.evaluate(id,args.script); return }
                if(method==="inspect") {
                    automation.evaluate(id,"({title:document.title,url:location.href,viewport:{width:innerWidth,height:innerHeight,dpr:devicePixelRatio},text:document.body.innerText.slice(0,20000),elements:Array.from(document.querySelectorAll('a,button,input,select,textarea,[role=button]')).slice(0,300).map(e=>({tag:e.tagName,id:e.id,role:e.getAttribute('role'),name:e.getAttribute('aria-label')||e.innerText||e.getAttribute('placeholder'),type:e.getAttribute('type'),value:e.value,href:e.href,rect:{x:e.getBoundingClientRect().x,y:e.getBoundingClientRect().y,width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height}}))})"); return
                }
                if(method==="click" || method==="fill") {
                    let script="(()=>{const e=document.querySelector("+JSON.stringify(args.selector)+");if(!e)throw new Error('Element not found');"
                    if(method==="click") script+="e.click();return {clicked:true};})()"
                    else script+="const proto=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;const setter=Object.getOwnPropertyDescriptor(proto,'value').set;setter.call(e,"+JSON.stringify(args.text)+");e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return {value:e.value};})()"
                    automation.evaluate(id,script); return
                }
                if(method==="capture") {
                    if(!args.path || args.path[0]!=="/" || !args.path.endsWith(".png")) throw new Error("Give an absolute PNG path")
                    let scale=args.scale===undefined?2:args.scale
                    if(args.window===true) {
                        let captured=Agent.captureWindow(app,args.path,scale)
                        if(captured.error)throw new Error(captured.error)
                        Agent.complete(id,captured);return
                    }
                    if(app.nativeMode && app.nativeState().status!=="live" && !(args.frame!==false && app.nativeState().pending))throw new Error("No native display connected")
                    let target=app.nativeMode?app.nativeCaptureItem(args.frame!==false):(args.frame===false?device.displayItem:device)
                    if(!isFinite(scale) || scale<1 || scale>3 || target.width*target.height*scale*scale>25000000) throw new Error("Screenshot too large")
                    let started=target.grabToImage(function(result) {
                        if(!result.saveToFile(args.path)) Agent.complete(id,null,"Could not save the PNG")
                        else Agent.complete(id,{path:args.path,width:Math.round(target.width*scale),height:Math.round(target.height*scale)})
                    },Qt.size(Math.round(target.width*scale),Math.round(target.height*scale)))
                    if(!started) throw new Error("Image capture could not start")
                    return
                }
                throw new Error("Unknown preview operation: "+method)
            } catch(error) { Agent.complete(id,null,String(error)) }
        }
    }
}
