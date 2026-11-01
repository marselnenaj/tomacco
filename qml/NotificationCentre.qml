import QtQuick

// The recent notifications the activity panel lists: jobs that ended, updates,
// devices whose session ended and what agents did, newest first, each with
// what it links back to (a job, a device or the update settings). The last
// 50 are kept across starts.
QtObject {
    id: centre
    readonly property int limit: 50
    property var entries: {
        try {
            const value=JSON.parse(String(Studio.setting("Notifications/recent","[]")))
            return Array.isArray(value)?value.filter(x=>x && typeof x.title==="string").slice(0,limit):[]
        } catch(e) { return [] }
    }
    property int serial: 0
    // kind: job, update, device or agent; tone: ok, failed, warn or idle;
    // link: {job} or {platform, device} or {update:true}.
    function add(kind,tone,title,body,link) {
        const extra=link || {}
        const entry={id:Date.now().toString(36)+"-"+(++serial),kind:kind,tone:tone,title:String(title || ""),body:String(body || ""),at:Date.now(),
                     job:extra.job || "",platform:extra.platform || "",device:extra.device || "",update:extra.update===true}
        entries=[entry].concat(entries).slice(0,limit)
        Studio.saveSetting("Notifications/recent",JSON.stringify(entries))
        return entry
    }
    function clear() {
        entries=[]
        Studio.saveSetting("Notifications/recent","[]")
    }
}
