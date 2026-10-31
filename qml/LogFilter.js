.pragma library

// The lines of a job's log that a reader asked for: those containing the text
// (case-insensitive) at the chosen level or above. "warning" keeps warnings
// and errors, "error" only errors. Levels are guessed from common words and
// from Android's logcat letters (E/Tag, " E Tag:").
const errorWords=/\b(error|fatal|failed|failure|exception|panic|panicked)\b/i
const warningWords=/\b(warn|warning|warnings|deprecated)\b/i
const errorTag=/^\s*[EF]\/|\s[EF]\s+[^\s:]+\s*:/
const warningTag=/^\s*W\/|\sW\s+[^\s:]+\s*:/

function level(line) {
    if(errorWords.test(line) || errorTag.test(line)) return "error"
    if(warningWords.test(line) || warningTag.test(line)) return "warning"
    return "info"
}

function filter(text, query, wanted) {
    const needle=String(query || "").toLowerCase()
    const rows=String(text || "").split("\n")
    if(rows.length && rows[rows.length-1]==="") rows.pop()
    return rows.filter(line=>{
        if(needle && line.toLowerCase().indexOf(needle)<0) return false
        if(wanted==="error") return level(line)==="error"
        if(wanted==="warning") return level(line)!=="info"
        return true
    })
}
