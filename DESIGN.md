---
name: WuLab Console
description: A warm, bright bench console where one blue shows what you can press and what is on now.
colors:
  ground: "#F2EEE9"
  card: "#FFFFFF"
  surface: "#F7F4F0"
  fill: "#F2EEE9"
  fill-hover: "#E9E3DC"
  line: "#EDE6DE"
  edge: "#E0D8CF"
  knob-off: "#D6CDC3"
  text: "#2A2622"
  text-2: "#6F625A"
  count: "#8A7F74"
  action: "#2E5BFF"
  action-hover: "#2149E0"
  action-soft: "#E8EEFF"
  on-action: "#FFFFFF"
  stop: "#B4232C"
  stop-soft: "#FDE8E8"
  stop-hover: "#F9D6D6"
  stop-press: "#9A1C24"
  on-stop: "#FFFFFF"
  ok: "#27774C"
  warn: "#8F6200"
  warn-soft: "#FFF3D6"
  well: "#171310"
  well-text: "#CFC6BC"
  well-chip: "rgba(255, 255, 255, 0.88)"
  well-scrim: "rgba(23, 19, 16, 0.72)"
typography:
  headline:
    fontFamily: "IBM Plex Sans, WuLab Sans SC, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "18px"
    fontWeight: 700
  title:
    fontFamily: "IBM Plex Sans, WuLab Sans SC, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "14px"
    fontWeight: 700
  body:
    fontFamily: "IBM Plex Sans, WuLab Sans SC, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.45
  control:
    fontFamily: "IBM Plex Sans, WuLab Sans SC, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "14px"
    fontWeight: 600
  body-small:
    fontFamily: "IBM Plex Sans, WuLab Sans SC, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "13px"
    fontWeight: 400
  label:
    fontFamily: "IBM Plex Sans, WuLab Sans SC, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "12px"
    fontWeight: 600
  badge:
    fontFamily: "IBM Plex Sans, WuLab Sans SC, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "12px"
    fontWeight: 700
    letterSpacing: "0.4px"
  readout:
    fontFamily: "IBM Plex Sans, WuLab Sans SC, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "20px"
    fontWeight: 700
    fontFeature: "tnum"
  readout-small:
    fontFamily: "IBM Plex Sans, WuLab Sans SC, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "14px"
    fontWeight: 600
    fontFeature: "tnum"
  brand:
    fontFamily: "IBM Plex Sans, WuLab Sans SC, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "16px"
    fontWeight: 700
  key-letter:
    fontFamily: "IBM Plex Sans, WuLab Sans SC, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "11px"
    fontWeight: 600
    lineHeight: 1
rounded:
  xs: "6px"
  sm: "8px"
  md: "10px"
  lg: "12px"
  xl: "16px"
  xxl: "20px"
  pill: "999px"
spacing:
  xxs: "4px"
  xs: "6px"
  sm: "8px"
  sm-plus: "10px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  xxl: "24px"
components:
  button:
    backgroundColor: "{colors.fill}"
    textColor: "{colors.text}"
    typography: "{typography.control}"
    rounded: "{rounded.lg}"
    padding: "0 14px"
    height: "40px"
  button-hover:
    backgroundColor: "{colors.fill-hover}"
  button-primary:
    backgroundColor: "{colors.action}"
    textColor: "{colors.on-action}"
    typography: "{typography.control}"
    rounded: "{rounded.lg}"
    padding: "0 14px"
    height: "40px"
  button-primary-hover:
    backgroundColor: "{colors.action-hover}"
  button-primary-disabled:
    backgroundColor: "{colors.fill}"
    textColor: "{colors.text-2}"
  button-on:
    backgroundColor: "{colors.action-soft}"
    textColor: "{colors.action}"
  button-device-stop:
    backgroundColor: "{colors.stop-soft}"
    textColor: "{colors.stop}"
    rounded: "{rounded.lg}"
    padding: "0 16px"
    height: "40px"
  button-device-stop-hover:
    backgroundColor: "{colors.stop-hover}"
  button-stop-all:
    backgroundColor: "{colors.stop}"
    textColor: "{colors.on-stop}"
    padding: "0 20px"
    height: "48px"
  button-stop-all-header:
    backgroundColor: "{colors.stop}"
    textColor: "{colors.on-stop}"
    typography: "{typography.brand}"
    rounded: "{rounded.lg}"
    padding: "0 28px"
    height: "84px"
    width: "256px"
  button-stop-all-hover:
    backgroundColor: "{colors.stop-press}"
  page-tabs:
    backgroundColor: "{colors.fill}"
    rounded: "{rounded.lg}"
    padding: "3px"
  page-tab:
    backgroundColor: "transparent"
    textColor: "{colors.text}"
    typography: "{typography.control}"
    rounded: "{rounded.sm}"
    padding: "0 14px"
    height: "36px"
  page-tab-on:
    backgroundColor: "{colors.action}"
    textColor: "{colors.on-action}"
  control-indicator:
    backgroundColor: "transparent"
    textColor: "{colors.text-2}"
    typography: "{typography.control}"
    rounded: "{rounded.lg}"
    padding: "0 12px"
    height: "40px"
  control-indicator-on:
    backgroundColor: "{colors.action-soft}"
    textColor: "{colors.action}"
  view-tab:
    backgroundColor: "{colors.well-chip}"
    textColor: "{colors.text}"
    typography: "{typography.body-small}"
    rounded: "{rounded.md}"
    padding: "0 12px"
    height: "32px"
  view-tab-on:
    backgroundColor: "{colors.action}"
    textColor: "{colors.on-action}"
  segment:
    backgroundColor: "{colors.fill}"
    rounded: "{rounded.lg}"
    padding: "3px"
  segment-option:
    backgroundColor: "transparent"
    textColor: "{colors.text}"
    typography: "{typography.body-small}"
    rounded: "{rounded.sm}"
    padding: "0 10px"
    height: "32px"
  segment-option-on:
    backgroundColor: "{colors.action}"
    textColor: "{colors.on-action}"
  segment-option-on-disabled:
    backgroundColor: "{colors.card}"
    textColor: "{colors.text}"
  hold:
    backgroundColor: "{colors.fill}"
    textColor: "{colors.text}"
    typography: "{typography.control}"
    rounded: "{rounded.lg}"
    padding: "0 12px"
    height: "44px"
  hold-on:
    backgroundColor: "{colors.action}"
    textColor: "{colors.on-action}"
  jog-key:
    backgroundColor: "{colors.fill}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
    size: "48px"
  key-cap:
    backgroundColor: "{colors.fill}"
    textColor: "{colors.text-2}"
    typography: "{typography.key-letter}"
    rounded: "{rounded.xs}"
    size: "20px"
  key-cap-on:
    backgroundColor: "{colors.action}"
    textColor: "{colors.on-action}"
  key-hint:
    backgroundColor: "{colors.fill}"
    textColor: "{colors.text-2}"
    typography: "{typography.label}"
    rounded: "{rounded.xs}"
    size: "24px"
  station:
    backgroundColor: "{colors.card}"
    textColor: "{colors.text}"
    rounded: "{rounded.xxl}"
    padding: "12px 12px 14px"
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.xl}"
    padding: "12px"
  media-well:
    backgroundColor: "{colors.well}"
    textColor: "{colors.well-text}"
    rounded: "{rounded.xl}"
  media-meta:
    backgroundColor: "{colors.well-scrim}"
    textColor: "{colors.ground}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "4px 10px"
  input:
    backgroundColor: "{colors.card}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "0 10px"
    height: "40px"
  badge-demo:
    backgroundColor: "{colors.action-soft}"
    textColor: "{colors.action-hover}"
    typography: "{typography.badge}"
    rounded: "{rounded.pill}"
    padding: "2px 8px"
  flag-warn:
    backgroundColor: "{colors.warn-soft}"
    textColor: "{colors.warn}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "2px 10px"
  alarm-note:
    backgroundColor: "{colors.stop-soft}"
    textColor: "{colors.text}"
    rounded: "{rounded.xl}"
    padding: "12px 14px"
---

# Design System: WuLab Console

## Overview

**Creative North Star: "The Bright Bench"**

The WuLab Console is a warm, bright console for driving the lab devices from a laptop at the bench. White cards with large soft corners sit on a warm tinted ground. Text is warm graphite. Labels are plain, sentence-case words, and the layout does the explaining. Each device sits under the view that shows its effect. Controls are weighted by real use: frequent controls are large and near the view, and rare tools fold away.

Color is scarce, so it carries meaning. One saturated blue, Action Blue, appears only on what you can press as the main action or on what is on right now: the current view tab, a held key, a held axis, the selected step mode, a switch that is on. Red is only for stops. Live video sits in dark Media Wells, the only dark areas on the page. Motion is short and functional: a pressed button shrinks for 120ms, and a held key lights at once.

This is the console-wide direction, and all Lab Console pages follow it. The page shows English and 中文 in the same layout. It loads no remote assets: 中文 uses WuLab Sans SC, which the console serves itself.

**Key Characteristics:**
- A warm tinted ground, white cards on 16px and 20px corners, and a faint warm shadow.
- Soft neutral fills for controls. No outlined buttons.
- Action Blue only for "press this" or "on now".
- A filled red only on the two stop-everything buttons. Device stops are coral fills with red text.
- Dark Media Wells for every live view, with light chips on top.
- Device numbers in tabular numerals in fixed cells.

### Known limitation
The top band and the Motion, Cameras and Furnace + microscope workspaces use this system now (`static/console/shell.css` with tokens on `#lc-top`, and `motion.css`, `cameras.css` and `process.css` with tokens on `#ws-motion`, `#ws-cameras` and `#ws-process`). Overview and the chip passport (including its header) still use the previous dark styles in `shell.css` and `passport.css`. The shared camera tile rules in `cameras.css` (outside `#ws-cameras`) still carry the previous colours; each workspace restyles the tile inside its own scope. Move each page to the tokens in this file when that page changes.

## Colors

A warm, low-chroma paper palette with one saturated blue, one stop red and a small set of status tones.

### Primary
- **Action Blue** (#2E5BFF): the fill of the primary action, the current view tab, a held key, a held Hold button, the selected step mode and step size, and an on switch. It also colors the speed slider, focus rings, fold titles and the busy or moving state text.
- **Deep Action** (#2149E0): primary hover, and the DEMO badge text.
- **Blue Wash** (#E8EEFF): a soft "on now" fill with Action Blue text, for a control that is on but is not the main action (Release while you hold control, the current named position). Also the DEMO badge and text selection.
- **White on Blue** (#FFFFFF): text on an Action Blue fill.

### Secondary
- **Stop Red** (#B4232C): the fill of STOP ALL MOTION and Stop heat and gas, with white text. The text of every device stop, the alarm title, and the bad tone (fault, locked, error events).
- **Coral Wash** (#FDE8E8) and **Coral Hover** (#F9D6D6): the fill of device stops, the danger Confirm button, the alarm note, and a bad state in the status strip.
- **Stop Press** (#9A1C24): hover on the header STOP ALL MOTION.

### Status tones
- **Ready Green** (#27774C): ready, connected, live and idle axes, and a stop result whose phase is Done.
- **Caution Ochre** (#8F6200) on **Ochre Wash** (#FFF3D6): stale feedback, required homing, warnings and the connection note.
- **Count Grey** (#8A7F74): the off tone dot and stale numbers.

### Neutral
- **Warm Ground** (#F2EEE9): the page background. The resting **Fill** of buttons, jog keys, segments, key caps and key hints uses the same value.
- **Card White** (#FFFFFF): stations, the activity card and inputs.
- **Warm Surface** (#F7F4F0): panels inside a card (the keyboard map, confirmations, reason notes).
- **Fill Hover** (#E9E3DC): hover on neutral fills.
- **Hairline** (#EDE6DE): row dividers and the fold divider.
- **Edge** (#E0D8CF): input borders and the outline of a selected option that you cannot change.
- **Knob Off** (#D6CDC3): the track of an off switch.
- **Warm Graphite** (#2A2622): primary text.
- **Umber** (#6F625A): secondary text, units, field labels and plain state text.
- **Media Well** (#171310): the background of every live view and every empty view.
- **Well Text** (#CFC6BC), **Well Chip** (rgba(255, 255, 255, 0.88)) and **Well Scrim** (rgba(23, 19, 16, 0.72)): text, light chips and the dark meta chip on media.

### Named Rules
**The Press-This-or-On-Now Rule.** Action Blue marks only what you can press as the main action or what is on right now. A held key and a held axis light in it in real time, for as long as the input lasts. A disabled primary turns neutral (Fill with Umber text). A selected option that you cannot change keeps a white fill with an Edge outline and loses the blue. A blue control that you cannot press and that is not on is a defect.

**The One Red Rule.** Only STOP ALL MOTION and Stop heat and gas have a filled Stop Red background. They are in the shell header and in the Overview Safety card. Device stops (Stop arm, Stop stage, the microscope Stop, Stop all gas) have a Coral Wash fill with Stop Red text in weight 700. A stop is never disabled, never dims and never asks for a confirmation.

**The Tone Plus Word Rule.** A tone is never the only signal. It always comes with a word: Moving, Idle, Home required, LIVE, No fresh feedback. A dot alone is not a state.

**The Green Means Done Rule.** A stop result turns Ready Green only when its phase is Done. Before that, it uses the info tone. Old readings dim to Count Grey and do not keep their tone.

## Typography

**Body Font:** the stack IBM Plex Sans, WuLab Sans SC, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif.

**Chinese Font:** WuLab Sans SC is IBM Plex Sans SC (SIL Open Font License), cut to the Chinese characters in the console sources and renamed as the license requires. The console serves it in 3 weights (400, 600 and 700, about 55 KiB each, `static/console/wulab-sans-sc-*.woff2`). Its `unicode-range` covers only CJK characters, CJK punctuation and full-width forms, so Latin text and numbers keep the platform face.

**Character:** One plain sans for all text, numbers included. IBM Plex Sans shows only when it is installed on the viewing machine. Otherwise the system UI face shows for Latin text, and the layout must work with it. 中文 always uses WuLab Sans SC, the Chinese member of the Plex family. A Chinese character outside the subset falls back to PingFang SC, Microsoft YaHei or another system face. Hierarchy comes from weight (600 and 700) more than from size. UI text stays between 12px and 20px.

### Hierarchy
- **Brand** (700, 16px): the console name, and the STOP ALL MOTION label in the header.
- **Headline** (700, 18px): station titles (Robot arm, Stage). The activity title is 17px.
- **Title** (700, 14px): group labels (Keyboard jog, Stepping, Gripper, Named positions).
- **Control** (600, 14px): buttons, holds and state text. Tabs and segment options use 13px.
- **Body** (400, 14px, 1.45 line height): running text.
- **Body small** (400, 13px): messages, units, the speed label and reading labels.
- **Label** (600, 12px): flags, media meta and key hints. Field labels and the key map labels use 400.
- **Badge** (700, 12px, 0.4px tracking): the DEMO badge.
- **Readout** (700, 20px, tabular numerals): pose values, axis pulses and the gripper position.
- **Readout small** (600, 14px, tabular numerals): joint angles and speed values.
- **Key letter** (600, 11px, line height 1): the letter on a key cap in the keyboard map.

### Named Rules
**The No Remote Assets Rule.** The host can be offline. Never load a font or any other asset from another host. When you add Chinese strings, run `uv run scripts/console_font.py <package dir>` to cut the font again (see the script). `tests/test_console.py` fails when a Chinese character in the console sources is not in the subset.

**The Fixed Cell Rule.** A number from a device uses `font-variant-numeric: tabular-nums` in a cell with a fixed or minimum width, aligned against its label or unit. A live value never changes the layout.

## Layout

- **Frame:** the shell header (with the status strip in it), the stop result band (after a stop, until Dismiss) and the notice bar form one top band.
- **Header:** a grid on Card White with a Hairline below. Row 1 has the brand (mark, name and commit), the page tabs, then the control indicator and the language switch on the right. Row 2 is the status strip. STOP ALL MOTION is a tall block at the right edge that spans both rows. At 1240px and below, the page tabs move to their own row and the block spans 3 rows. At 720px and below, the block is full width and 48px tall, directly under the brand. Above 720px, the band stays at the top while the page scrolls. Its height is published as `--lc-top`. Sticky elements and `scroll-padding-top` add it to their offset. The workspace is centered and at most 1440px wide.
- **Stations:** Motion has 2 stations side by side in a 711 : 649 ratio, 20px apart. Each station is a subgrid with 2 rows, the view row and the body row, so both views have the same height. The arm view is 16:9. The stage view fills the row and is at least 240px tall.
- **Station body:** a header row (title, state and badge, then the actions and the device stop on the right), then the main controls, then folds for rare tools below a Hairline. In the arm body, the keyboard panel (switch, speed and key map, 62 parts) is beside the pose and gripper readout (38 parts, at least 190px).
- **Rows:** a stage axis is 1 grid row at least 56px tall: name and state, value, Hold −, Hold +, speed and key hints.
- **Rhythm:** 20px between cards. 12px between blocks in a card. 8px inside a group. 4–6px between a label and its value.
- **Responsive:** when a station is narrower than 610px, its header buttons get tighter so that the header stays on 1 row. At 1100px and below, the stations stack and the stage view becomes 16:9. At 720px and below, the arm body stacks, axis rows hide speed and key hints, and pose fields use 2 columns.

### Named Rules
**The Weighted By Use Rule.** Frequent controls are large and near the view that they affect. Rare tools (single steps, exact poses, setup, zero) go in folds. Do not give every control the same weight.

**The Bilingual Fit Rule.** Check every layout with English and 中文 strings. Rows and headers wrap. Labels are never truncated.

## Elevation & Depth

The system is soft and low. Cards lift a little from the ground with a faint warm shadow. Everything inside a card is flat and separates by fill steps (Warm Surface, Fill) and Hairlines. Media Wells are the darkest layer and read as windows, not as raised surfaces. The top band is opaque Card White with a Hairline below and has no shadow. The stop result band and the notice bar use Warm Surface.

### Shadow Vocabulary
- **Card lift** (`box-shadow: 0 1px 2px rgba(60, 40, 20, 0.06), 0 4px 14px rgba(60, 40, 20, 0.05)`): stations and the activity card only.

### Named Rules
**The One Lift Rule.** Only cards get the card lift. Inside a card, use the next fill step or a Hairline, never a second shadow.

## Shapes

Soft rounded rectangles without borders. The radius grows with the size of the element:

- **6px:** key caps and key hints.
- **8px:** segment options.
- **10px:** view tabs, inputs, media chips and reference images.
- **12px:** buttons, holds, jog keys and the segment track.
- **16px:** media views, the keyboard panel, the alarm note, confirmations and notes.
- **20px:** stations and the activity card.
- **Pill (999px):** badges, flags and the switch track.
- **Circle:** the switch knob and status dots.

Controls have no border; the fill does the work. Inputs keep a 1px Edge border, so an empty field still reads as a field. Views clip their media to the 16px corner.

## Components

### Buttons
Soft and tactile: a neutral fill by default, blue for the primary action, coral for stops.

- **Shape:** 12px corners, at least 40px tall, 14px side padding, Control type.
- **Default:** Fill with Warm Graphite text. Fill Hover on hover.
- **Primary:** Action Blue with white text. Deep Action on hover. A disabled primary turns neutral.
- **On now:** Blue Wash with Action Blue text, for a control whose state is on.
- **Device stop:** Coral Wash with Stop Red text in weight 700 and 16px side padding. Coral Hover on hover. Never disabled.
- **STOP ALL MOTION and Stop heat and gas:** a Stop Red fill with white text and a square stop shape, 48px tall. In the header, STOP ALL MOTION is the heaviest control: a block at least 256px wide and 84px tall with a 16px label, so the layout does not move when the language changes. In the Overview Safety card they are 64px tall and full width.
- **Press:** `scale(0.97)`, 120ms ease-out.
- **Focus:** a 2px Action Blue outline with a 2px offset on every button, input, select, summary and focusable element.
- **Disabled:** 0.45 opacity and the not-allowed cursor. Stops and a held Hold stay at full strength.

### Segmented control
A Fill track with 3px padding and 12px corners. Options are transparent and 32px tall. On hover an option shows Card White. The selected option is Action Blue with white text. It is used for the step mode and the jog step size.

### Switch
A pill track, 40 by 22px (34 by 20px for the small switch), in Knob Off with a white 16px knob. When on, the track turns Action Blue and the knob slides in 150ms. The label is beside the track in weight 700.

### Cards / Containers
- **Station:** Card White, 20px corners, 12px 12px 14px padding and the card lift.
- **Panel:** Warm Surface, 16px corners, 12px padding, flat.
- **Notes:** a reason note is Warm Surface with 12px corners. The connection note is Ochre Wash with 16px corners.
- **Inline confirmation:** Warm Surface, 16px corners, an inset 1px Edge line and 12px 14px padding. It holds the question, a danger Confirm (Coral Wash) and a Cancel. It takes the focus when it opens. Esc or Cancel closes it and gives the focus back.

### Inputs / Fields
- **Style:** Card White, a 1px Edge border, 10px corners, 40px tall and tabular numerals. Speed fields in an axis row have no border and use Fill.
- **Labels:** 12px Umber text above the field, 4px away.
- **Focus:** the Action Blue outline.
- **Disabled:** 0.55 opacity.

### Navigation
- **View tabs:** in the top left corner of the media frame. Well Chip fills with Warm Graphite 13px text, 32px tall, with 10px corners. The current tab is Action Blue with white text.
- **Page tabs:** a segmented track in the header: Fill, 3px padding, 12px corners. A tab is 36px tall, 14px weight 600, with 8px corners and Card White on hover. The current page is Action Blue with white text. Chip passport opens another page, so it is a plain Umber link after the track.
- **Status strip:** 1 item for each device: a tone dot, the name in weight 600 and the state word in its tone (Umber when off). A warn or bad state also gets a pill wash (Ochre Wash or Coral Wash) and a weight 600 word, so it stands out in a row of Ready.
- **Control indicator:** "You control: none" in Umber with a Count Grey dot. When this page controls a device, it turns Blue Wash with Action Blue text (on now).
- **Folds:** a summary in Action Blue, weight 600, with a small triangle that turns 90 degrees in 150ms.

### Media view (signature)
A Media Well with 16px corners that holds the live frame. The view tabs are at the top left. The state badge (a dot, LIVE or another state word, and the frame age) is on a Well Chip at the top right, in its tone. The frame size and rate are on a Well Scrim chip at the bottom left. Snapshot and Expand are Well Chip buttons at the bottom right. A camera that is off shows its state word on a centered translucent chip.

### Keyboard map (signature)
A quiet legend under the Keyboard jog switch and speed slider: the switch and slider are the main control, and the map stays in the background. The keys are in pairs, in one wrapping line: W S (out and in), A D (J1), R F (Z), Q E (J4) and Z X (gripper). Each pair is 2 key caps and a 12px Umber label in weight 400, for example "J1 + / −". The first key of a pair moves out, +, up or opens. A key cap is a 20px Fill square with 6px corners and an 11px Umber letter, with no base line and no shadow. Only a held key stands out: its cap turns Action Blue with white text at once (80ms). Each pair label is 1 string, so a pair can get a new label without a layout change. Key hints beside an axis are 24px Fill squares and light the same way.

### Axis row and Hold
Each stage axis row has the name and state, the value with its unit, Hold − and Hold + (44px, Fill), a speed field and the key hints. While you press it, a Hold is Action Blue. It stays at full strength while its axis is busy.

### Alarm note
A Coral Wash block with 16px corners at the top of the arm station. The title is Stop Red in weight 700. Each alarm line gives the ID, the description and the solution. The button that clears the state is inside the note.

### Badges and flags
- **DEMO badge:** a pill in Blue Wash with Deep Action text and Badge type.
- **Flag:** a pill in Ochre Wash with Caution Ochre text, below the station header, for a state that needs attention (for example No fresh feedback).

## Do's and Don'ts

### Do:
- **Do** scope each workspace under `#ws-<id>` and take every color from the tokens in this file.
- **Do** keep STOP ALL MOTION in the header on every page. It and Stop heat and gas are the only filled red controls.
- **Do** give device stops the Coral Wash fill with Stop Red text, and keep every stop enabled.
- **Do** light a held key and a held axis in Action Blue for as long as the input lasts.
- **Do** make a disabled primary, or a selected option that you cannot change, neutral.
- **Do** put every live view on a Media Well, with Well Chips or the Well Scrim for anything on top.
- **Do** show device numbers in tabular numerals in fixed cells.
- **Do** give every tone a word.
- **Do** remove transitions when `prefers-reduced-motion` is set.

### Don't:
- **Don't** use Action Blue for decoration, for headings, or for a control that you cannot press and that is not on.
- **Don't** fill any control with red except STOP ALL MOTION and Stop heat and gas.
- **Don't** add a shadow inside a card or a border to a button.
- **Don't** add dark surfaces other than Media Wells.
- **Don't** load fonts or other assets from another host.
- **Don't** add secondary labels or descriptions that the layout already makes clear.
- **Don't** show a state with color alone.
