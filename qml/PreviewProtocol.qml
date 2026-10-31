import QtQuick

// Media and network emulation of the layout lab's page through the Chrome
// DevTools protocol. Qt WebEngine has no API for prefers-color-scheme,
// prefers-reduced-motion or throttling, but it attaches a DevTools frontend to
// a page: the web inspector's view. From the first emulation on, that view
// stays attached, also with its window hidden, and protocol commands go
// through the frontend's own channel to the page. Turned off, the overrides
// are reset; the frontend stays, because a frontend attached again after one
// was let go lost its overrides to the old session's late reset. While the
// inspector's window is hidden, pauses (a debugger statement) are skipped, so
// that an unseen debugger cannot stop the page.
QtObject {
    id: protocol
    // The web inspector's WebEngineView; its inspectedView follows attached.
    required property var inspector
    // The inspector's window is open: the person debugs, pauses apply.
    property bool inspectorShown: false
    onInspectorShownChanged: if(attached && frontendReady)send()
    property string colorScheme: "system"
    property string reducedMotion: "system"
    property var network: ({offline:false,latency_ms:0,download_kbps:0,upload_kbps:0})
    // Chromium DevTools' presets: Fast 3G and Slow 3G.
    readonly property var presets: ({
        "none":{offline:false,latency_ms:0,download_kbps:0,upload_kbps:0},
        "fast-3g":{offline:false,latency_ms:562.5,download_kbps:1440,upload_kbps:675},
        "slow-3g":{offline:false,latency_ms:2000,download_kbps:400,upload_kbps:400},
        "offline":{offline:true,latency_ms:0,download_kbps:0,upload_kbps:0}
    })
    readonly property bool throttled: network.offline || network.latency_ms>0 || network.download_kbps>0 || network.upload_kbps>0
    readonly property bool active: colorScheme!=="system" || reducedMotion!=="system" || throttled
    property bool attached: false
    // The frontend has loaded in the inspector's view: commands can go.
    property bool frontendReady: false
    property string error: ""
    property var pending: []
    function preset() {
        for(const name in presets) {
            const p=presets[name]
            if(p.offline===network.offline && p.latency_ms===network.latency_ms && p.download_kbps===network.download_kbps && p.upload_kbps===network.upload_kbps)return name
        }
        return "custom"
    }
    function state() { return {color_scheme:colorScheme,reduced_motion:reducedMotion,network:Object.assign({preset:preset()},network),emulation:{attached:attached,error:error}} }
    // Validates a configure request's media and network arguments; throws on a bad one.
    function parse(args) {
        const result={}
        if(args.color_scheme!==undefined) {
            if(["system","light","dark"].indexOf(args.color_scheme)<0)throw new Error("color_scheme is system, light or dark")
            result.colorScheme=args.color_scheme
        }
        if(args.reduced_motion!==undefined) {
            if(["system","reduce","no-preference"].indexOf(args.reduced_motion)<0)throw new Error("reduced_motion is system, reduce or no-preference")
            result.reducedMotion=args.reduced_motion
        }
        if(args.network!==undefined) {
            const given=args.network || {}
            if(given.preset!==undefined && !presets[given.preset])throw new Error("Unknown network preset: "+given.preset)
            const next=Object.assign({},given.preset!==undefined?presets[given.preset]:network)
            for(const key of ["latency_ms","download_kbps","upload_kbps"])if(given[key]!==undefined) {
                if(typeof given[key]!=="number" || !isFinite(given[key]) || given[key]<0)throw new Error(key+" must be a number of at least 0")
                next[key]=given[key]
            }
            if(given.offline!==undefined)next.offline=given.offline===true
            result.network=next
        }
        return result
    }
    // Applies the values parse() returned; done(error) runs once the page has them.
    function update(values,done) {
        if(values.colorScheme!==undefined)colorScheme=values.colorScheme
        if(values.reducedMotion!==undefined)reducedMotion=values.reducedMotion
        if(values.network!==undefined)network=values.network
        if(done)pending=pending.concat([done])
        if(active)attached=true
        if(!attached) {finish("");return}
        if(frontendReady)send()
    }
    function commands() {
        const rate=kbps=>kbps>0?kbps*1000/8:-1
        return [
            {method:"Emulation.setEmulatedMedia",params:{media:"",features:[
                {name:"prefers-color-scheme",value:colorScheme==="system"?"":colorScheme},
                {name:"prefers-reduced-motion",value:reducedMotion==="system"?"":reducedMotion}]}},
            {method:"Network.emulateNetworkConditions",params:{offline:network.offline===true,latency:network.latency_ms,downloadThroughput:rate(network.download_kbps),uploadThroughput:rate(network.upload_kbps)}},
            {method:"Debugger.enable",params:{}},
            {method:"Debugger.setSkipAllPauses",params:{skip:!inspectorShown}}
        ]
    }
    // Runs in the frontend. Replies to these commands are kept from the frontend, which
    // would report unknown ids; when the frontend sets the same domains itself (its own
    // emulation at start-up), the lab's values are sent again after it.
    function script() {
        return "(()=>{const w=window;if(!w.__tomaccoHook){w.__tomaccoHook=true;w.__tomaccoReplies={};w.__tomaccoNext=2000000000;"
            +"const api=w.DevToolsAPI,deliver=api.dispatchMessage.bind(api);"
            +"api.dispatchMessage=function(m){try{const j=typeof m==='string'?JSON.parse(m):m;if(j&&j.id>=2000000000){w.__tomaccoReplies[j.id]=j.error?String(j.error.message):'';return}}catch(e){}return deliver(m)};"
            +"const host=w.InspectorFrontendHost,raw=host.sendMessageToBackend.bind(host);"
            +"host.sendMessageToBackend=function(m){const r=raw(m);try{const j=JSON.parse(m);if(w.__tomaccoActive&&j.id<2000000000&&/^(Emulation\\.setEmulatedMedia|Network\\.emulateNetworkConditions|Network\\.overrideNetworkState|Debugger\\.setSkipAllPauses)/.test(j.method))setTimeout(w.__tomaccoSend,0)}catch(e){}return r};"
            +"w.__tomaccoSend=()=>(w.__tomaccoCommands||[]).map(c=>{const id=w.__tomaccoNext++;raw(JSON.stringify({id,method:c.method,params:c.params}));return id})}"
            +"w.__tomaccoCommands="+JSON.stringify(commands())+";w.__tomaccoActive="+(attached?"true":"false")+";return w.__tomaccoSend()})()"
    }
    function send() {
        inspector.runJavaScript(script(),function(ids) {
            if(!ids || !ids.length) {finish("The web inspector could not take the emulation");return}
            poll(ids,0)
        })
    }
    function poll(ids,tries) {
        inspector.runJavaScript("JSON.stringify("+JSON.stringify(ids)+".map(id=>window.__tomaccoReplies?window.__tomaccoReplies[id]:undefined))",function(text) {
            const replies=JSON.parse(text || "[]").map(r=>r===null?undefined:r)
            if(replies.some(r=>r===undefined)) {
                if(tries>=100) {finish("The page did not confirm the emulation");return}
                timer.createObject(protocol,{callback:()=>poll(ids,tries+1)})
                return
            }
            finish(replies.filter(r=>r.length).join("; "))
        })
    }
    property Component timer: Component { Timer { property var callback; interval: 50; running: true; onTriggered: { callback(); destroy() } } }
    function finish(failure) {
        error=failure
        const callbacks=pending;pending=[]
        for(const done of callbacks)done(failure)
    }
    property Connections frontend: Connections {
        target: protocol.inspector
        function onLoadingChanged(request) {
            if(request.status===2 /* LoadSucceededStatus */ && request.url.toString().startsWith("devtools:")) {
                protocol.frontendReady=true
                if(protocol.attached)protocol.send()
            } else if(request.status===0 /* LoadStartedStatus */)protocol.frontendReady=false
        }
        // A frontend attached anew loads again; commands wait for it.
        function onInspectedViewChanged() { protocol.frontendReady=false }
    }
}
