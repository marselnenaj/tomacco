.pragma library
// The design tokens: every colour, spacing step, type size, radius and control
// size of the interface. Theme.qml serves them to QML and follows the chosen
// appearance; components read Theme.<token> and write no values of their own.
//
// Ink or paper, tomato red and calyx green from the logo, flat full-bleed blocks
// where they earn it and 1 px lines where work happens. Physical device colours are not tokens:
// a phone's body looks the same in either theme.

// The one accent. Change this and the hover, the text on it and the
// thin-line variants follow in both themes.
var brand = "#f2361f"
// The logo's other colours: the calyx, and the ink and paper it sits on.
var green = "#2fa84f"
var ink = "#0e0e0c"
var paper = "#f4f2ec"

// The product's mark: a resource path to an SVG (for example
// "qrc:/assets/logo.svg", listed in CMakeLists.txt). Empty draws the block.
var logo = "qrc:/assets/logo.svg"

function channels(hex) {
    return [1, 3, 5].map(function (i) { return parseInt(hex.substr(i, 2), 16) })
}
function mix(hex, other, amount) {
    var a = channels(hex), b = channels(other)
    return "#" + a.map(function (v, i) {
        var c = Math.round(v + (b[i] - v) * amount).toString(16)
        return c.length < 2 ? "0" + c : c
    }).join("")
}
function luminance(hex) {
    var c = channels(hex).map(function (v) {
        v /= 255
        return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)
    })
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
}
// Near-black on the accent wherever that reads, as in the brand; white otherwise.
function onAccent(hex) { return (luminance(hex) + 0.05) / 0.0545 >= 4.5 ? ink : "#ffffff" }
var onBrand = onAccent(brand)
// The WCAG contrast ratio of two colours, 1 to 21.
function contrast(a, b) {
    var x = luminance(a), y = luminance(b)
    return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05)
}
function isColour(text) { return /^#[0-9a-fA-F]{6}$/.test(String(text)) }
// Another accent, such as the Omarchy theme's, where it reads clearly: at
// least 3:1 against this theme's background and against the background of
// the theme it comes from (WCAG's measure for large text and controls).
// Otherwise an empty string, and the brand stays.
function readableAccent(accent, ground, theirGround) {
    if (!isColour(accent) || !isColour(ground)) return ""
    if (contrast(accent, ground) < 3) return ""
    if (isColour(theirGround) && contrast(accent, theirGround) < 3) return ""
    return accent.toLowerCase()
}
// A theme's colours with another accent; its hover, the text on it and the
// thin-line variant follow it as they follow the brand.
function withAccent(colours, accent, isDark) {
    if (!accent) return colours
    var next = {}
    for (var key in colours) next[key] = colours[key]
    next.accent = accent
    next.accentHover = isDark ? mix(accent, "#ffffff", 0.14) : mix(accent, "#000000", 0.1)
    next.accentText = onAccent(accent)
    next.accentInk = isDark ? mix(accent, "#ffffff", 0.2) : mix(accent, "#000000", 0.26)
    return next
}

var dark = {
    background: ink,   // The window's ground, the stage around a device.
    surface: "#151513",      // Bars, panels, dialogs.
    elevated: "#1d1d1b",     // Raised controls, popups.
    hover: "#272724",
    inset: "#0a0a09",        // Fields, logs, code.
    border: "#34342f",       // Lines that delimit a control.
    subtle: "#242421",       // Lines that divide a surface.
    text: paper,
    muted: "#adaba3",
    faint: "#85837b",
    accent: brand,
    accentHover: mix(brand, "#ffffff", 0.14),
    accentText: onBrand,
    accentInk: mix(brand, "#ffffff", 0.2),  // The accent as text or a thin line on a surface.
    selected: "#272724",
    success: "#4cc26c",
    danger: "#ff6b6b",
    warning: "#e5b758",
    dangerSurface: "#2c1616",
    warningSurface: "#2a2212",
    scrim: "#b3060605"
}

var light = {
    background: paper,
    surface: "#fbfaf6",
    elevated: "#ffffff",
    hover: "#e8e7e0",
    inset: "#eeede7",
    border: "#c9c7bd",
    subtle: "#dfddd5",
    text: ink,
    muted: "#55534c",
    faint: "#6f6d65",
    accent: brand,
    accentHover: mix(brand, "#000000", 0.1),
    accentText: onBrand,
    accentInk: mix(brand, "#000000", 0.26),
    selected: "#e8e7e0",
    success: "#1d7a4d",
    danger: "#c62f2f",
    warning: "#8a6100",
    dangerSurface: "#f9e4e0",
    warningSurface: "#f6edd6",
    scrim: "#66141412"
}

// A device's startup screen is dark in either theme, as a phone's would be.
var deviceScreen = {background: "#060605", text: paper, muted: "#8f8d85"}

// Spacing, in pixels. Everything sits on this scale of a 4 px grid.
var space = {xxs: 2, xs: 4, sm: 8, md: 12, lg: 16, xl: 24, xxl: 32, xxxl: 48}

// Type sizes, in pixels. Large type is set tight; see tracking below.
var type = {micro: 11, small: 12, body: 13, lead: 15, title: 20, display: 28, hero: 40, giant: 68, large: 18, mega: 96}
var tracking = {giant: -2.6, hero: -1.4, display: -0.7, title: -0.3}

// Sharp: a hint of softening on controls, none on the layout.
var radii = {none: 0, small: 2, medium: 3, large: 4}

// Heights of the shell and its controls, in pixels.
var sizes = {control: 32, controlSmall: 26, row: 32, bar: 44, status: 24, rail: 48, railWide: 184, panel: 296, icon: 16}

// Durations in milliseconds; Theme.qml makes them 0 when reduced motion is asked for.
var motion = {fast: 90, slow: 220}

// The first installed family of each list is used.
var sansFamilies = ["Archivo", "Adwaita Sans", "Noto Sans", "DejaVu Sans"]
var monoFamilies = ["JetBrains Mono", "JetBrainsMono Nerd Font", "Adwaita Mono", "Noto Sans Mono", "DejaVu Sans Mono"]

function firstInstalled(families, installed) {
    for (var i = 0; i < families.length; i++)
        if (installed.indexOf(families[i]) >= 0)
            return families[i]
    return families[0]
}
