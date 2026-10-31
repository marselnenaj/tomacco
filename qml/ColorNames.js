.pragma library
// Case color names of the models in assets/models.json, for the desktop UI.
// The catalog keeps the English names that tomaccoctl, its JSON output and MCP
// clients read; this list marks the same names for translation (context
// "Colors") so lupdate finds them. "Device" is the single color of a profile
// measured from a connected device (native/devices.rs). A name not listed,
// such as one from an imported model, is shown as written.
// tests/python/test_i18n.py keeps the list and the catalog in step.
var names = [
    QT_TRANSLATE_NOOP("Colors", "Black"),
    QT_TRANSLATE_NOOP("Colors", "Burgundy"),
    QT_TRANSLATE_NOOP("Colors", "Canyon"),
    QT_TRANSLATE_NOOP("Colors", "Cloud White"),
    QT_TRANSLATE_NOOP("Colors", "Cobalt Violet"),
    QT_TRANSLATE_NOOP("Colors", "Device"),
    QT_TRANSLATE_NOOP("Colors", "Fog"),
    QT_TRANSLATE_NOOP("Colors", "Frost"),
    QT_TRANSLATE_NOOP("Colors", "Glacier"),
    QT_TRANSLATE_NOOP("Colors", "Hibiscus"),
    QT_TRANSLATE_NOOP("Colors", "Lavender"),
    QT_TRANSLATE_NOOP("Colors", "Light Gold"),
    QT_TRANSLATE_NOOP("Colors", "Mist Blue"),
    QT_TRANSLATE_NOOP("Colors", "Night Sky"),
    QT_TRANSLATE_NOOP("Colors", "Obsidian"),
    QT_TRANSLATE_NOOP("Colors", "Olive"),
    QT_TRANSLATE_NOOP("Colors", "Pink"),
    QT_TRANSLATE_NOOP("Colors", "Pistachio"),
    QT_TRANSLATE_NOOP("Colors", "Sage"),
    QT_TRANSLATE_NOOP("Colors", "Silver"),
    QT_TRANSLATE_NOOP("Colors", "Sky Blue"),
    QT_TRANSLATE_NOOP("Colors", "Soft Pink"),
    QT_TRANSLATE_NOOP("Colors", "Space Black"),
    QT_TRANSLATE_NOOP("Colors", "Star White"),
    QT_TRANSLATE_NOOP("Colors", "Teal"),
    QT_TRANSLATE_NOOP("Colors", "Ultramarine"),
    QT_TRANSLATE_NOOP("Colors", "White")
]

function name(source) {
    return names.indexOf(source)>=0 ? qsTranslate("Colors", source) : (source || "")
}
