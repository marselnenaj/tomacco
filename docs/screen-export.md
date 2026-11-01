# Screen export format

`screen.export` writes one screen of a device as a bundle: the screenshot at full resolution and a JSON file with the
device, the display, the system bars, the theme, the foreground app and every element on the screen with its bounds. It
exists so that a design tool, a coding agent or a visual comparison can take a real device screen, its geometry and its
element tree as input, without scraping Tomacco's other results. Nothing in the format belongs to one consumer: it is a
plain directory of a PNG and JSON.

The format is named `tomacco.screen` and has a version, now 1. [Versioning](#versioning) says what a change may do to it.
The JSON Schema is [`assets/schemas/screen-export-v1.schema.json`](../assets/schemas/screen-export-v1.schema.json).

## Making an export

```sh
tomaccoctl call screen.export '{"device":"emulator-5554","directory":"/absolute/exports/settings","marked":true}'
```

`directory` must not exist or must be empty. It gets:

| File | Content |
| --- | --- |
| `screen.png` | The screenshot, unscaled, as the device took it. Every pixel coordinate in `screen.json` refers to it. |
| `screen.json` | The document described below. |
| `marked.png` | Only with `marked:true`: the screenshot with the numbered boxes `screen.marks` draws. |

The call answers a summary (paths, size, device, display, theme, locale, app, how many elements and marks, warnings); the
elements are in `screen.json`. Arguments: `source` (`auto`, `hierarchy` or `ocr`, as in `screen.marks`), `max_marks`
(default 200), `min_confidence` and `language` for OCR, and `platform:"ios"` for a USB iPhone. After an export,
`screen.tap` takes the mark numbers of that screen, as after `screen.marks`.

Several devices at once:

```sh
tomaccoctl call screen.export_batch '{"devices":["emulator-5554","emulator-5556"],"directory":"/absolute/exports/all"}'
```

writes `01-emulator-5554/`, `02-emulator-5556/` (a bundle each) and `index.json`. A device that fails is listed in the
index with its `error` and the others still run; the call's `ok` is then false. It takes the current screen of each
device. It does not reproduce `android.store_screenshots`' matrix of languages and themes, which launches the app and
drives it through scenarios for each combination; to export those screens, call `screen.export` once each is on the screen.

`screen.diff` also takes two bundles (the directory or its `screen.json`) as `before` and `after`: it lists the metadata
that changed, the elements added, removed and changed, and the pixel regions.

## screen.json

```json
{
  "format": "tomacco.screen",
  "version": 1,
  "tomacco_version": "1.3.0",
  "captured_at": "2026-11-01T21:20:07Z",
  "device": {
    "platform": "android",
    "id": "emulator-5554",
    "serial": "emulator-5554",
    "udid": null,
    "virtual": true,
    "model": "sdk_gphone64_x86_64",
    "manufacturer": "Google",
    "os_name": "Android",
    "os_version": "16",
    "sdk": 36,
    "form_factor": "phone"
  },
  "display": {
    "width_px": 1080,
    "height_px": 2424,
    "density_dpi": 420,
    "scale": 2.625,
    "width_dp": 411.43,
    "height_dp": 923.43,
    "unit": "dp",
    "orientation": "portrait",
    "rotation": 0
  },
  "insets": {
    "source": "dumpsys window",
    "estimated": false,
    "status_bar": {
      "left": 0,
      "top": 0,
      "right": 1080,
      "bottom": 63
    },
    "navigation_bar": {
      "left": 0,
      "top": 2361,
      "right": 1080,
      "bottom": 2424
    },
    "cutout": [],
    "safe_area_px": {
      "left": 0,
      "top": 63,
      "right": 0,
      "bottom": 63
    },
    "safe_area_dp": {
      "left": 0.0,
      "top": 24.0,
      "right": 0.0,
      "bottom": 24.0
    }
  },
  "theme": {
    "mode": "light",
    "source": "cmd uimode night"
  },
  "locale": {
    "tag": "en-US",
    "source": "getprop ro.product.locale"
  },
  "app": {
    "package": "com.android.settings",
    "activity": "com.android.settings.homepage.SettingsHomepageActivity",
    "source": "dumpsys window"
  },
  "screenshot": {
    "file": "screen.png",
    "width": 1080,
    "height": 2424,
    "bytes": 195037,
    "sha256": "869abf246f70147b656ab768e6398ca16bac0ad5a8205e9b298a6e0fbbf4c64d",
    "marked_file": null
  },
  "hierarchy": "available",
  "elements_source": "hierarchy",
  "marks": {
    "count": 11,
    "limit": 200,
    "truncated": false
  }
}
```

The `elements` list follows (see below), and `warnings` is a list of strings.

| Field | Meaning |
| --- | --- |
| `format`, `version` | `"tomacco.screen"` and `1`. Check both before reading the rest. |
| `tomacco_version` | The `tomaccoctl` that wrote the file. |
| `captured_at` | UTC time of the capture, RFC 3339. |
| `device` | `platform` (`android` or `ios`), `id` (what Tomacco's operations take), `serial` (Android) or `udid` (USB iPhone), `virtual`, `model`, `manufacturer`, `os_name`, `os_version`, `sdk` (Android API level) and `form_factor` (`phone`, `tablet`, `tv`, `watch`). |
| `display` | `width_px`, `height_px` (the size of `screen.png`), `density_dpi`, `scale` (pixels per dp or point), `width_dp`, `height_dp`, `unit` (`dp` or `pt`), `orientation` (`portrait` or `landscape`, from the pixel size) and `rotation` (degrees from the natural orientation: 0, 90, 180, 270). |
| `insets` | Where the system bars and cutouts are, in pixels of `screen.png`: `status_bar` and `navigation_bar` frames (`left`, `top`, `right`, `bottom`), `cutout` (a list of frames), `safe_area_px` and `safe_area_dp` (the distance from each edge that content should keep clear), `source` (where it was read) and `estimated`. |
| `theme` | `mode` (`dark`, `light`) and `source`. |
| `locale` | `tag` (such as `en-US`) and `source`. |
| `app` | `package`, `activity` and `source`: the app that has the focus. |
| `screenshot` | `file` (always `screen.png`), `width`, `height`, `bytes`, `sha256` and `marked_file` (`marked.png` or null). |
| `hierarchy` | `available`, `unavailable` (the platform reports no UI hierarchy) or `skipped` (OCR was asked for although a hierarchy exists). |
| `elements_source` | `hierarchy`, `ocr`, or `none` (no hierarchy and no OCR). |
| `marks` | `count` of numbered elements, `limit` (`max_marks`) and `truncated`. |
| `elements` | The element list. |
| `warnings` | What could not be read, in words. |

A value the platform does not report is `null`. Nothing is guessed to fill it.

### Elements

`elements` is a flat list in document order: a parent comes before its children, and `parent` is its id (or null). An
`id` is unique in the file. For a hierarchy, `e<N>` is the Nth node of the dump, so two exports of an unchanged screen
have the same ids; ids do not follow an element across different screens, and `screen.diff` matches elements by class,
resource id and text instead. For OCR lines, `o<N>`.

```json
{
  "id": "e7",
  "parent": "e6",
  "depth": 7,
  "source": "hierarchy",
  "role": "control",
  "class": "android.widget.LinearLayout",
  "package": "com.android.settings",
  "resource_id": "com.android.settings:id/search_action_bar",
  "text": "",
  "description": "",
  "label": "Search Settings",
  "bounds": {
    "px": {
      "left": 42,
      "top": 84,
      "right": 1038,
      "bottom": 273,
      "width": 996,
      "height": 189
    },
    "dp": {
      "left": 16.0,
      "top": 32.0,
      "right": 395.43,
      "bottom": 104.0,
      "width": 379.43,
      "height": 72.0
    }
  },
  "center": {
    "x": 540,
    "y": 178
  },
  "enabled": true,
  "clickable": true,
  "long_clickable": false,
  "focusable": true,
  "focused": false,
  "checkable": false,
  "checked": false,
  "selected": false,
  "scrollable": false,
  "editable": false,
  "password": false,
  "confidence": null,
  "mark": 1
}
```

| Field | Meaning |
| --- | --- |
| `id`, `parent`, `depth` | Identity and nesting. |
| `source` | `hierarchy` or `ocr`. |
| `role` | What a person would call it: `text_field`, `checkbox`, `radio`, `switch`, `button`, `image`, `progress`, `dropdown`, `web_view`, `list`, `scroll`, `text`, `control` (something pressable with no better name), `container`, `view`. It is derived from the class name and the flags; `class` keeps the original. OCR lines are `text`. |
| `class`, `package`, `resource_id` | As the platform reports them (empty for OCR). |
| `text`, `description` | The element's own text and content description. |
| `label` | What a person reads on or inside the element: its text, else its description, else the text of what is inside it. |
| `bounds` | `px`: `left`, `top`, `right`, `bottom`, `width`, `height` in pixels of `screen.png`. `dp`: the same in dp or points, rounded to 0.01, or null where the scale is unknown. |
| `center` | The tap point in pixels, inside the part on the screen. |
| `enabled`, `clickable`, `long_clickable`, `focusable`, `focused`, `checkable`, `checked`, `selected`, `scrollable`, `editable`, `password` | The state the hierarchy reports. `null` for OCR lines, which cannot tell. |
| `confidence` | Tesseract's confidence (0 to 100) for OCR lines, else null. |
| `mark` | The number `screen.marks` gives this element in the same hierarchy (enabled and interactive elements in document order, the outer twin of an element with the same area left out), or null. Numbers are 1 to `marks.count`; `max_marks` cuts the list but never renumbers it. For OCR lines it is the line's number in reading order. |

A password field's text is whatever the platform shows for it (bullets on Android).

## What each platform fills

| | Android (device or emulator) | Virtual iPhone | USB iPhone (`platform:"ios"`) |
| --- | --- | --- | --- |
| Screenshot | `screencap` | `ios.virtual.screenshot` | `idevicescreenshot` or go-ios |
| `device` | `getprop`: model, manufacturer, OS version, API level, `form_factor` from the build characteristics and the smallest width (600 dp or more is a tablet) | Model from `ios.virtual.status` and the device catalogue; `os_version` null | `ideviceinfo` (`ProductVersion`); model from the catalogue by screen size, else the product type |
| `display` | `wm density` and the screenshot; dp from the density; `rotation` from `dumpsys window` | Scale from the catalogue (`pt`); `rotation` 0 | Scale from the catalogue by screen size, else null |
| `insets` | `dumpsys window`: status bar, navigation bar, cutout | The catalogue's safe area, `estimated:true` | Same as the virtual iPhone |
| `theme` | `cmd uimode night`; with `auto`, the mode in effect from the display configuration | null | null |
| `locale` | `persist.sys.locale`, else `ro.product.locale` | null | null |
| `app` | The focused window in `dumpsys window` | null | null |
| Elements | The UI hierarchy: every node, `hierarchy:"available"` | OCR lines, `hierarchy:"unavailable"`, state flags null | OCR lines, same |

Content an app draws itself (a canvas, a game, much of a web view) is not in an Android hierarchy; with `source:"ocr"`
the lines of text are read from the pixels instead (`hierarchy:"skipped"`). OCR needs `tesseract`; without it and
without a hierarchy the export still succeeds, with `elements_source:"none"` and a warning. OCR reads pixels, not
controls: small, low-contrast or decorative text is misread or missed and an icon without text is absent.

The cutout frames come from the display's cutout bounds, and the safe area merges the status bar, navigation bar and
cutout. On a phone without a cutout `cutout` is an empty list.

## index.json

`screen.export_batch` writes `index.json`, format `tomacco.screen-index`, version 1, with `captured_at`, `count`,
`failed` and `screens`. Each entry has `device`, `ok` and, when it worked, `directory`, `screen_json`, `screenshot`
(paths relative to the index), `platform`, `model`, `width_px`, `height_px`, `orientation`, `theme`, `locale`, `app`,
`elements`, `elements_source`, `hierarchy` and `warnings`; when it failed, `error`. Its schema is
[`assets/schemas/screen-export-index-v1.schema.json`](../assets/schemas/screen-export-index-v1.schema.json).

## Versioning

- The version is an integer in `version`. A reader accepts the versions it knows and refuses a higher one;
  `screen.diff` answers `unsupported_export_version`.
- A change that only adds (a new optional field, a new value of `role`, a new file in the bundle) keeps the version. A
  reader ignores fields and files it does not know. The schema file for the version grows with them.
- Removing or renaming a field, changing what a field means or its unit, or making an optional field required raises the
  version. Tomacco then writes the new version, and the schema for it is a new file next to the old one.
- A test checks that every field of the schema is described on this page, and that recorded bundles (an Android
  hierarchy, an OCR screen and a live emulator) and the index validate against the schemas.

## What ran

Unit tests build bundles from the recorded hierarchies `tests/fixtures/screen_login.xml` and `screen_login_filled.xml`
and a real `dumpsys window` excerpt, with fake screens and fake Tesseract output, and compare them with the files in
`tests/fixtures/screen_export`; `tests/python/test_screen_export_schema.py` validates those and a live export against
the schemas. Live, on an API 36 x86_64 emulator started with hardware acceleration, `screen.export` wrote the Settings
screen (76 elements, 11 marks) and the 11 numbers and their positions matched those of `screen.marks` taken right after;
a landscape export said `rotation:90` with a 2424 by 1080 screen; `screen.export_batch` with the emulator and a serial
that does not exist wrote one bundle and listed the other as failed; and `screen.diff` of two bundles taken in the light
and the dark theme listed `theme.mode` as the only metadata change, no element change and the pixels as changed. The
virtual iPhone and a USB iPhone were not run: their paths are covered by unit tests with fake screens only, and the
catalogue's safe areas are estimates.
