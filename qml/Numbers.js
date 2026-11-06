.pragma library

// Numbers people type and read in the interface. The control plane's JSON is
// canonical (a dot, ASCII digits); what a person types may use the language's
// decimal mark and digits. parse() turns such text into a number for the
// machine side, show() writes a canonical number the way the interface
// language does, without grouping, so it parses back.

// The digit zero of each script Omarchy's registry writes numbers in, and the others Qt knows.
var zeros = ["0", "٠", "۰", "০", "०", "௦", "๐", "෦"]

function asciiDigits(text) {
    var out = ""
    for (var i = 0; i < text.length; i++) {
        var c = text.charCodeAt(i), done = false
        for (var z = 1; z < zeros.length && !done; z++) {
            var zero = zeros[z].charCodeAt(0)
            if (c >= zero && c <= zero + 9) { out += String.fromCharCode(48 + c - zero); done = true }
        }
        if (!done) out += text[i]
    }
    return out
}

// The number in text, or NaN. Accepts ".", "," and the Arabic decimal mark as
// the decimal separator; a single separator is always the decimal one. With
// both present, the last one is the decimal separator and the other groups
// thousands. Spaces and narrow spaces group thousands.
function parse(text) {
    var s = asciiDigits(String(text === undefined || text === null ? "" : text)).replace(/[\s   ']/g, "")
    s = s.replace(/٫/g, ",").replace(/٬/g, ".").replace(/−/g, "-")
    if (s === "") return NaN
    var dot = s.lastIndexOf("."), comma = s.lastIndexOf(",")
    if (dot >= 0 && comma >= 0) {
        if (dot > comma) s = s.replace(/,/g, "")
        else s = s.replace(/\./g, "").replace(",", ".")
    } else if (comma >= 0) {
        if (s.indexOf(",") !== comma) return NaN
        s = s.replace(",", ".")
    } else if (dot >= 0 && s.indexOf(".") !== dot) return NaN
    if (!/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(s)) return NaN
    return Number(s)
}

// A canonical number for the interface language: its decimal mark, no grouping.
function show(value, decimalPoint) {
    if (value === undefined || value === null || value === "") return ""
    var text = String(value)
    return decimalPoint && decimalPoint !== "." ? text.replace(".", decimalPoint) : text
}
