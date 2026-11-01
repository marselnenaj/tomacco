.pragma library

// Fuzzy matching for the command palette. A query matches a text when its
// characters appear in it in order; matches at the start of words and runs of
// consecutive characters rank higher, a whole substring higher still.

const separators = " ._-/:·()"

function wordStart(text, index) {
    return index === 0 || separators.indexOf(text[index - 1]) >= 0
}

// The positions of the query's characters in the text, or null.
function positions(query, text) {
    const q = query.toLowerCase(), t = text.toLowerCase()
    if (!q) return []
    const at = t.indexOf(q)
    if (at >= 0) {
        let out = []
        for (let i = 0; i < q.length; ++i) out.push(at + i)
        return out
    }
    return walk(q, t, true) || walk(q, t, false)
}

// Each character in order: right after the previous one when it is there,
// else (when preferStarts) at the next word start that has it, else the next one.
function walk(q, t, preferStarts) {
    let out = [], from = 0
    for (const ch of q) {
        const previous = out.length ? out[out.length - 1] : -1
        let found = previous >= 0 && t[previous + 1] === ch ? previous + 1 : -1
        if (found < 0 && preferStarts)
            for (let i = from; i < t.length; ++i) if (t[i] === ch && wordStart(t, i)) { found = i; break }
        if (found < 0) found = t.indexOf(ch, from)
        if (found < 0) return null
        out.push(found)
        from = found + 1
    }
    return out
}

// How well one word of a query matches a text: -1 for not at all.
function scoreToken(query, text) {
    if (!query) return 0
    if (!text) return -1
    const q = query.toLowerCase(), t = text.toLowerCase()
    const at = t.indexOf(q)
    if (at >= 0) return 100 + (at === 0 ? 50 : 0) + (wordStart(t, at) ? 30 : 0) + (q.length === t.length ? 40 : 0) - t.length * 0.1
    const found = positions(q, t)
    if (!found) return -1
    let score = 0, gaps = 0
    for (let i = 0; i < found.length; ++i) {
        if (wordStart(t, found[i])) score += 10
        else if (i > 0 && found[i] === found[i - 1] + 1) score += 8
        else score += 2
        if (i > 0) gaps += found[i] - found[i - 1] - 1
    }
    score -= Math.min(gaps, 30) + t.length * 0.05
    // Scattered letters are no match.
    return score >= q.length * 3 ? score : -1
}

// The score of a query, word by word, against fields given as [text, weight]:
// every word has to match one of them. -1 for no match, 0 for an empty query.
function score(query, fields) {
    const words = query.trim().split(/\s+/).filter(w => w.length)
    let total = 0
    for (const word of words) {
        let best = -1
        for (const field of fields) {
            const s = scoreToken(word, field[0] || "")
            if (s >= 0) best = Math.max(best, s * field[1])
        }
        if (best < 0) return -1
        total += best
    }
    return total
}

function escape(text) {
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
}

// The text as rich text with the characters the query matched in the colour given.
function highlight(text, query, colour) {
    const words = query.trim().split(/\s+/).filter(w => w.length)
    let marked = {}
    for (const word of words) {
        const found = positions(word, text)
        if (found) for (const i of found) marked[i] = true
    }
    let out = ""
    for (let i = 0; i < text.length; ++i)
        out += marked[i] ? "<font color=\"" + colour + "\"><b>" + escape(text[i]) + "</b></font>" : escape(text[i])
    return out
}
