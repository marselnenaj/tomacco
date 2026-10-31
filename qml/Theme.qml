pragma Singleton
import QtQuick
import "Theme.js" as Tokens

// The tokens of Theme.js as live properties. Bindings to them follow the
// appearance (Studio.dark) and the reduced-motion preference without a restart.
QtObject {
    readonly property bool isDark: Studio.dark
    readonly property var colors: isDark ? Tokens.dark : Tokens.light

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

    readonly property int gapXxs: Tokens.space.xxs
    readonly property int gapXs: Tokens.space.xs
    readonly property int gapSm: Tokens.space.sm
    readonly property int gapMd: Tokens.space.md
    readonly property int gapLg: Tokens.space.lg
    readonly property int gapXl: Tokens.space.xl
    readonly property int gapXxl: Tokens.space.xxl
    readonly property int gapXxxl: Tokens.space.xxxl

    readonly property int fontMicro: Tokens.type.micro
    readonly property int fontSmall: Tokens.type.small
    readonly property int fontBody: Tokens.type.body
    readonly property int fontLead: Tokens.type.lead
    readonly property int fontTitle: Tokens.type.title
    readonly property int fontDisplay: Tokens.type.display
    readonly property int fontHero: Tokens.type.hero
    readonly property int fontGiant: Tokens.type.giant
    readonly property int fontMega: Tokens.type.mega
    readonly property int fontLarge: Tokens.type.large
    readonly property real trackingGiant: Tokens.tracking.giant
    readonly property real trackingHero: Tokens.tracking.hero
    readonly property real trackingDisplay: Tokens.tracking.display
    readonly property real trackingTitle: Tokens.tracking.title

    readonly property int radiusSmall: Tokens.radii.small
    readonly property int radius: Tokens.radii.medium
    readonly property int radiusLarge: Tokens.radii.large

    readonly property int controlHeight: Tokens.sizes.control
    readonly property int controlSmall: Tokens.sizes.controlSmall
    readonly property int rowHeight: Tokens.sizes.row
    readonly property int barHeight: Tokens.sizes.bar
    readonly property int statusHeight: Tokens.sizes.status
    readonly property int railWidth: Tokens.sizes.rail
    readonly property int railWideWidth: Tokens.sizes.railWide
    readonly property int panelWidth: Tokens.sizes.panel
    readonly property int iconSize: Tokens.sizes.icon

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
