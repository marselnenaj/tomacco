pragma Singleton
import QtQuick
import "Theme.js" as Tokens

// The tokens of Theme.js as live properties. Bindings to them follow the
// appearance (Studio.dark) and the reduced-motion preference without a restart.
QtObject {
    // Only app chrome uses these lengths. Device geometry, preview zoom and
    // exported pixels keep their own units.
    readonly property real uiScale: Studio.uiScale/100
    function px(value) { return Math.round(value*uiScale) }
    readonly property bool isDark: Studio.dark
    readonly property var baseColors: isDark ? Tokens.dark : Tokens.light
    // The active Omarchy theme's accent, when that is chosen in the settings
    // and it reads clearly here; otherwise empty and the brand's red stays.
    readonly property var omarchy: Studio.omarchyTheme
    readonly property string omarchyAccent: Studio.followOmarchyAccent && omarchy.available===true
        ? Tokens.readableAccent(omarchy.accent,baseColors.background,omarchy.background) : ""
    // Chosen, but this theme's accent does not read clearly on this background.
    readonly property bool omarchyAccentRefused: Studio.followOmarchyAccent && omarchy.available===true && !omarchyAccent
    readonly property var colors: Tokens.withAccent(baseColors,omarchyAccent,isDark)

    readonly property color background: colors.background
    readonly property color surface: colors.surface
    readonly property color elevated: colors.elevated
    readonly property color hover: colors.hover
    readonly property color inset: colors.inset
    readonly property color border: colors.border
    readonly property color subtle: colors.subtle
    readonly property color text: colors.text
    readonly property color muted: colors.muted
    readonly property color faint: colors.faint
    readonly property color accent: colors.accent
    readonly property color accentHover: colors.accentHover
    readonly property color accentText: colors.accentText
    readonly property color accentInk: colors.accentInk
    readonly property color selected: colors.selected
    readonly property color success: colors.success
    readonly property color danger: colors.danger
    readonly property color warning: colors.warning
    readonly property color dangerSurface: colors.dangerSurface
    readonly property color warningSurface: colors.warningSurface
    readonly property color scrim: colors.scrim

    readonly property color green: Tokens.green
    readonly property color ink: Tokens.ink
    readonly property color paper: Tokens.paper

    readonly property color deviceScreen: Tokens.deviceScreen.background
    readonly property color deviceScreenText: Tokens.deviceScreen.text
    readonly property color deviceScreenMuted: Tokens.deviceScreen.muted

    readonly property int gapXxs: px(Tokens.space.xxs)
    readonly property int gapXs: px(Tokens.space.xs)
    readonly property int gapSm: px(Tokens.space.sm)
    readonly property int gapMd: px(Tokens.space.md)
    readonly property int gapLg: px(Tokens.space.lg)
    readonly property int gapXl: px(Tokens.space.xl)
    readonly property int gapXxl: px(Tokens.space.xxl)
    readonly property int gapXxxl: px(Tokens.space.xxxl)

    readonly property int fontMicro: px(Tokens.type.micro)
    readonly property int fontSmall: px(Tokens.type.small)
    readonly property int fontBody: px(Tokens.type.body)
    readonly property int fontLead: px(Tokens.type.lead)
    readonly property int fontTitle: px(Tokens.type.title)
    readonly property int fontDisplay: px(Tokens.type.display)
    readonly property int fontHero: px(Tokens.type.hero)
    readonly property int fontGiant: px(Tokens.type.giant)
    readonly property int fontMega: px(Tokens.type.mega)
    readonly property int fontLarge: px(Tokens.type.large)
    readonly property real trackingGiant: Tokens.tracking.giant*uiScale
    readonly property real trackingHero: Tokens.tracking.hero*uiScale
    readonly property real trackingDisplay: Tokens.tracking.display*uiScale
    readonly property real trackingTitle: Tokens.tracking.title*uiScale

    readonly property int radiusSmall: px(Tokens.radii.small)
    readonly property int radius: px(Tokens.radii.medium)
    readonly property int radiusLarge: px(Tokens.radii.large)

    readonly property int controlHeight: px(Tokens.sizes.control)
    readonly property int controlSmall: px(Tokens.sizes.controlSmall)
    readonly property int rowHeight: px(Tokens.sizes.row)
    readonly property int barHeight: px(Tokens.sizes.bar)
    readonly property int statusHeight: px(Tokens.sizes.status)
    readonly property int railWidth: px(Tokens.sizes.rail)
    readonly property int railWideWidth: px(Tokens.sizes.railWide)
    readonly property int panelWidth: px(Tokens.sizes.panel)
    readonly property int iconSize: px(Tokens.sizes.icon)

    // The hint exists from Qt 6.12; older versions have no preference to follow.
    readonly property bool reducedMotion: {
        const hints=Qt.styleHints.accessibility
        return !!hints && hints.motionPreference===Qt.ReducedMotion
    }
    readonly property int motionFast: reducedMotion ? 0 : Tokens.motion.fast
    readonly property int motionSlow: reducedMotion ? 0 : Tokens.motion.slow

    readonly property string logo: Tokens.logo
    readonly property string sans: Tokens.firstInstalled(Tokens.sansFamilies,Qt.fontFamilies())
    readonly property string mono: Tokens.firstInstalled(Tokens.monoFamilies,Qt.fontFamilies())
}
