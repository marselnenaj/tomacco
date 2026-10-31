.pragma library

// Files dragged onto the window: their local paths and what kind of app they are.

// The local path of a file URL, without a trailing slash (an .app is a
// directory); an empty string for anything that is not a local file.
function localPath(url) {
    const text=String(url)
    if(!text.startsWith("file://"))return ""
    const path=decodeURIComponent(text.slice(7).replace(/^[^/]*/,""))
    return path.length>1?path.replace(/\/+$/,""):path
}

// The local paths of a drag's URLs; empty when any of them is not local.
function paths(urls) {
    const list=Array.from(urls || []).map(localPath)
    return list.length && list.every(p=>p.length>0)?list:[]
}

// "apk", "ipa", "app" or "" for anything else.
function kind(path) {
    const match=/\.(apk|ipa|app)$/i.exec(path || "")
    return match?match[1].toLowerCase():""
}

// Whether every path is an app at all, whatever device takes it.
function allApps(list) {
    return list.length>0 && list.every(p=>kind(p)!=="")
}

// Whether a device that installs these kinds takes the files: Android takes
// one or more APKs (split APKs install together), the other kinds one at a time.
function installable(list, kinds) {
    if(!list.length || !list.every(p=>kinds.indexOf(kind(p))>=0))return false
    return list.length===1 || list.every(p=>kind(p)==="apk")
}

// The last part of a path, for messages.
function name(path) {
    return String(path || "").split("/").pop()
}
