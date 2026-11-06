---
name: WuLab Console
description: A calm, dark instrument panel for operating and monitoring the lab devices.
colors:
  bg: "#0F1418"
  header: "#11181D"
  card: "#161D23"
  inset: "#1B232A"
  tab: "#1F2A33"
  row: "#222C35"
  line: "#26313A"
  line2: "#2E3B46"
  media-well: "#0A0E11"
  scrim: "rgba(10, 14, 17, 0.85)"
  text: "#E6EDF2"
  soft: "#B8C6D1"
  muted: "#9FB0BD"
  "off": "#7C8C99"
  accent: "#4DA3FF"
  on-accent: "#0B1A2A"
  info: "#7DBBFF"
  info-hover: "#B3D7FF"
  ok: "#3CCB7F"
  warn: "#F2B84B"
  warn-tint: "#2A2213"
  bad: "#FF7A7A"
  bad-text: "#FF9A9A"
  stop: "#C9252B"
  on-stop: "#FFFFFF"
typography:
  brand:
    fontFamily: "IBM Plex Sans, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "16px"
    fontWeight: 700
    letterSpacing: "0.2px"
  headline:
    fontFamily: "IBM Plex Sans, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "17px"
    fontWeight: 600
  title:
    fontFamily: "IBM Plex Sans, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "14px"
    fontWeight: 600
  body:
    fontFamily: "IBM Plex Sans, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.45
  body-small:
    fontFamily: "IBM Plex Sans, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "13px"
    fontWeight: 400
  label:
    fontFamily: "IBM Plex Sans, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "12px"
    fontWeight: 500
  label-strong:
    fontFamily: "IBM Plex Sans, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "12px"
    fontWeight: 700
    letterSpacing: "0.3px"
  stop-label:
    fontFamily: "IBM Plex Sans, system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "15px"
    fontWeight: 700
    letterSpacing: "0.4px"
  readout:
    fontFamily: "IBM Plex Mono, ui-monospace, monospace"
    fontSize: "14px"
    fontWeight: 500
  readout-small:
    fontFamily: "IBM Plex Mono, ui-monospace, monospace"
    fontSize: "13px"
    fontWeight: 400
  readout-large:
    fontFamily: "IBM Plex Mono, ui-monospace, monospace"
    fontSize: "22px"
    fontWeight: 500
rounded:
  xs: "6px"
  sm: "8px"
  md: "10px"
  lg: "12px"
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
    backgroundColor: "transparent"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: "0 14px"
    height: "44px"
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    rounded: "{rounded.sm}"
    padding: "0 14px"
    height: "44px"
  button-danger:
    backgroundColor: "transparent"
    textColor: "{colors.bad-text}"
    rounded: "{rounded.md}"
    padding: "0 16px"
    height: "48px"
  button-stop:
    backgroundColor: "{colors.stop}"
    textColor: "{colors.on-stop}"
    typography: "{typography.stop-label}"
    rounded: "{rounded.md}"
    padding: "0 20px"
    height: "48px"
  button-stop-large:
    backgroundColor: "{colors.stop}"
    textColor: "{colors.on-stop}"
    typography: "{typography.stop-label}"
    rounded: "{rounded.md}"
    height: "64px"
    width: "100%"
  tab:
    backgroundColor: "transparent"
    textColor: "{colors.soft}"
    rounded: "{rounded.sm}"
    padding: "0 16px"
    height: "44px"
  tab-current:
    backgroundColor: "{colors.tab}"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: "0 16px"
    height: "44px"
  chip-filter:
    backgroundColor: "transparent"
    textColor: "{colors.soft}"
    typography: "{typography.body-small}"
    rounded: "{rounded.pill}"
    padding: "0 12px"
    height: "44px"
  chip-filter-on:
    backgroundColor: "{colors.tab}"
    textColor: "{colors.text}"
    typography: "{typography.body-small}"
    rounded: "{rounded.pill}"
    padding: "0 12px"
    height: "44px"
  control-pill:
    backgroundColor: "transparent"
    textColor: "{colors.soft}"
    typography: "{typography.body-small}"
    rounded: "{rounded.pill}"
    padding: "6px 12px"
  card:
    backgroundColor: "{colors.card}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
    padding: "20px"
  readout-tile:
    backgroundColor: "{colors.inset}"
    textColor: "{colors.text}"
    typography: "{typography.readout}"
    rounded: "{rounded.sm}"
    padding: "8px"
  readout-tile-large:
    backgroundColor: "{colors.inset}"
    textColor: "{colors.text}"
    typography: "{typography.readout-large}"
    rounded: "{rounded.md}"
    padding: "12px"
  input:
    backgroundColor: "{colors.bg}"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: "0 12px"
    height: "44px"
  input-numeric:
    backgroundColor: "{colors.bg}"
    textColor: "{colors.text}"
    typography: "{typography.readout-small}"
    rounded: "{rounded.sm}"
    padding: "0 10px"
    height: "44px"
  camera-tile:
    backgroundColor: "{colors.card}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
  media-badge:
    backgroundColor: "{colors.scrim}"
    textColor: "{colors.soft}"
    typography: "{typography.label-strong}"
    rounded: "{rounded.xs}"
    padding: "3px 8px"
  hardware-badge:
    backgroundColor: "{colors.tab}"
    textColor: "{colors.info}"
    typography: "{typography.label-strong}"
    rounded: "{rounded.xs}"
    padding: "4px 10px"
  hardware-badge-real:
    backgroundColor: "{colors.warn-tint}"
    textColor: "{colors.warn}"
    typography: "{typography.label-strong}"
    rounded: "{rounded.xs}"
    padding: "4px 10px"
  jog-key:
    backgroundColor: "{colors.inset}"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    size: "48px"
---

# Design System: WuLab Console

## Overview

**Creative North Star: "The Instrument Panel"**

The WuLab Console is a calm, dark panel for operating the lab devices from a laptop. The surface stays quiet, so the few colors with meaning read at a glance: the status tones, Signal Blue for "this one", and E-Stop Red for the stop. The mood is calm, dense and literal. A screen holds many devices at once. Labels are plain words, and the layout does the explaining.

The panel is flat. Depth comes from small steps in the Night Slate surfaces and from 1px lines, not from shadows. Nothing moves: the system has no transitions or animations, and a state change shows immediately. The only images are live device media (camera, thermal and microscope frames), which sit in dark Media Wells.

This system covers the Lab Console: `src/orchestrator/web/templates/console.html` and `src/orchestrator/web/static/console/*.css`. The variables in `shell.css` are the source of truth. The chip passport page (`templates/passport.html`) loads `shell.css` and uses the same tokens; `static/passport.css` styles only its records.

**Key Characteristics:**
- Dark Night Slate surfaces in close steps, with 1px lines.
- A fixed map from state to color, and each color comes with a word.
- Filled color only for the primary action (Signal Blue) and the two stop-everything buttons (E-Stop Red).
- Device values in monospace, with tabular numerals for live readings.
- Controls at least 44px tall. Stop controls 48px or 64px.
- No web fonts, no shadows, no motion.

## Colors

A cool, near-black slate base with one blue for selection and a fixed set of status tones.

### Primary
- **Signal Blue** (#4DA3FF): the "this one" color. The current tab underline, focus rings, a pressed filter chip, the current recipe step, the selected camera, the fill of the primary action, the focus-bar fill, the speed slider and the chip detection boxes on camera 4.
- **Ink Navy** (#0B1A2A): text on a Signal Blue fill.

### Secondary
- **Readout Blue** (#7DBBFF): links, info notices, the busy, moving and running tone, the furnace SV line and the thermal minimum marker. It shows information. It does not show selection.
- **Pale Signal** (#B3D7FF): link hover.

### Tertiary
- **E-Stop Red** (#C9252B): the fill of the two stop-everything buttons: STOP ALL MOTION (in the header and in the Overview Safety card) and Stop heat and gas (in the Overview Safety card and on the Furnace card). White text (#FFFFFF) on top.

### Status tones
- **Ready Green** (#3CCB7F): ready, live, configured, homed or zeroed, idle, and "you control" in the control pill.
- **Caution Amber** (#F2B84B): stale state, a reference that is required, warnings, the furnace PV line, gas state text when the flow or the valve is not confirmed, and the region that is being drawn. It is also the REAL ROBOT and REAL STAGE badge, on **Amber Shadow** (#2A2213).
- **Fault Coral** (#FF7A7A): unavailable, fault, locked, error events and the thermal maximum marker. It is the border color of danger buttons, inline confirmations, device notes and the arm alarm note.
- **Soft Coral** (#FF9A9A): text on danger outlines.
- **Idle Grey** (#7C8C99): the off tone. Unconfigured, loading, disconnected and no run, and the control pill when this page controls nothing.

### Neutral
- **Night Slate** (#0F1418): the page background. It is also the background of inputs and charts, which therefore look recessed into a card.
- **Header Slate** (#11181D): the header band.
- **Card Slate** (#161D23): cards and camera tiles.
- **Inset Slate** (#1B232A): readout tiles, jog keys, stage axis panels, gas rows and table headers inside a card.
- **Selected Slate** (#1F2A33): the background of the current tab, a pressed chip, the current table row and the DEMO badge.
- **Row Rule** (#222C35): the top border between rows in lists, events and tables.
- **Panel Line** (#26313A): borders of cards, tiles and views, and the header and strip dividers.
- **Control Edge** (#2E3B46): borders of buttons, inputs, chips, pills and key caps.
- **Media Well** (#0A0E11): the background behind camera, thermal and microscope frames, empty views and the focus bar.
- **Media Scrim** (rgba(10, 14, 17, 0.85)): badges and labels drawn on top of media.
- **Frost** (#E6EDF2): primary text.
- **Mist** (#B8C6D1): secondary text: tabs, pills, chips, camera roles and modes.
- **Haze** (#9FB0BD): labels, details, the commit hash, axis labels and placeholders.

### Named Rules
**The One Red Rule.** Only the stop-everything buttons have a filled E-Stop Red background: STOP ALL MOTION and Stop heat and gas. Device stops (Stop arm, Stop stage, the microscope Stop, Stop all gas), Acknowledge fault…, Set focus zero… and Confirm use the Fault Coral outline. A stop button is never disabled and never asks for a confirmation.

**The Tone Map Rule.** A state always gets the same tone: ok green, warn amber, bad coral, info blue or off grey. A tone is never the only signal. It comes with a word: a dot and a text in the status strip, or LIVE, STALE or OFFLINE on a camera tile.

**The Blue Means "This One" Rule.** Signal Blue marks selection, focus, the current item and the primary action. Never use it for status, and never use a status tone for selection.

## Typography

**Body Font:** IBM Plex Sans (with system-ui, -apple-system, Segoe UI, Noto Sans, PingFang SC, Microsoft YaHei, sans-serif)
**Label/Mono Font:** IBM Plex Mono (with ui-monospace, monospace)

**Character:** One plain sans for all words and one monospace for device numbers. The stack includes PingFang SC and Microsoft YaHei so that 中文 strings use a matching system face. Hierarchy comes from weight (500 to 700) more than from size. UI text stays between 12px and 22px. Only chart axis text and thermal colour-bar labels use 11px.

### Hierarchy
- **Brand** (700, 16px, 0.2px tracking): the "WuLab Console" name in the header.
- **Headline** (600, 17px): card titles.
- **Title** (600, 14px): section titles inside a card (Jog, Gripper, Keyboard and the furnace and microscope blocks).
- **Body** (400, 14px, 1.45 line height): all running text and control labels. Buttons use 600.
- **Body small** (400, 13px): status chips, events, pills, filter chips, muted details and gas state.
- **Label** (500, 12px): field labels, tile labels, legends and frame metadata.
- **Label strong** (700, 12px, 0.3px tracking; the DEMO and REAL badges use 0.6px): camera state badges (LIVE, STALE, OFFLINE, FROZEN) and the hardware badge.
- **Stop label** (700, 15px, 0.4px tracking): STOP ALL MOTION. The copy is written in capitals. CSS does not transform it.
- **Readout** (Mono 500, 14px): the X, Y, Z and R pose values.
- **Readout small** (Mono 400, 13px): stage readouts, key caps, numeric inputs and the commit hash (12px).
- **Readout large** (Mono 500, 22px): furnace PV, SV and the crucible maximum.

### Named Rules
**The No Web Fonts Rule.** The host can be offline. Never load a font from the network. IBM Plex is used only when it is installed on the viewing machine. Otherwise the system UI face shows, and the layout must work with it.

**The Green Means Done Rule.** A stop result turns Ready Green only when its phase is Done. Before that, a confirmed target reads Confirmed (provisional) in Readout Blue. Old readings dim to Idle Grey instead of keeping their tone.

**The Mono for Measurements Rule.** A number that comes from a device (pose, pulses, mm, PV, SV, frame age, the commit) uses the monospace face or `font-variant-numeric: tabular-nums`, so that a live value does not change width.

## Layout

- **Frame:** the header band, then the status strip, then the stop result band (after a stop, until Dismiss), then the notice bar (only when there is a notice), then the workspace. In a window wider than 720px, the header, strip, stop result band and notice form one top band that stays at the top while the page scrolls. Its height is published as `--lc-top`; other sticky elements and `scroll-padding-top` add it to their offset. The workspace is centered, with a maximum width of 1440px and 24px padding (16px at 720px and below).
- **Header:** brand and commit, workspace tabs, then the actions: the control pill, the language switch and STOP ALL MOTION. It wraps on narrow screens. At 720px and below, the actions take the full width and STOP ALL MOTION stretches.
- **Workspace columns:** a main column (flex basis 640px) and a side column (flex basis 360px), 24px apart. Motion has 3 columns: robot arm (340px basis), cameras (520px basis, grows first) and stage (340px basis).
- **Camera grids:** 3 columns, then 2 at 1100px and below, then 1 at 720px and below. The Focus strip has 5 columns, then 3, then 2. Furnace readout tiles fill with a minimum of 140px.
- **Rhythm:** 24px between cards and columns; 20px inside a card; 12px between blocks in a card; 8–10px inside a group; 4–6px between a label and its value. Rows have 12px of vertical padding and a Row Rule above them.
- **Density:** the base size is 14px. Lists scroll inside their card (events stop at 420px height), so the page keeps its shape.

### Named Rules
**The Wrap, Don't Break Rule.** Columns reflow through `flex-wrap` and flex bases, not breakpoints. Media queries (720px and 1100px) only change grid column counts and padding.

**The 44px Floor Rule.** Every interactive control is at least 44px tall: buttons, tabs, inputs, selects, chips, filters and sliders. STOP ALL MOTION, most danger buttons and the jog keys are 48px. Stop stage and the Furnace + microscope danger buttons are 44px. The large stop in the Safety card is 64px.

## Elevation & Depth

The system is flat. There are no drop shadows. Depth comes from tonal steps: Media Well under Night Slate (page and inputs), under Header Slate, under Card Slate, under Inset Slate and Selected Slate. Each container also has a 1px line. Two things are sticky: the top band (z-index 20, opaque backgrounds, no shadow) and the Cameras side panel (16px below the top band).

### Shadow Vocabulary
- **Current marker** (`box-shadow: inset 0 -2px 0 var(--accent)`): a 2px Signal Blue underline inside the current workspace tab and the selected microscope objective. It marks place, not height.

### Named Rules
**The Flat Panel Rule.** Do not add a shadow to show height. To make an area stand out, use the next surface step or a Panel Line border.

## Shapes

Rounded rectangles with 1px borders. The radius grows with the size of the element:

- **6px:** badges, key caps and small bars.
- **8px:** buttons, tabs, inputs, readout tiles, recipe steps and inline confirmations.
- **10px:** stop and danger buttons, stage axis panels, furnace tiles, the chart and the table frame.
- **12px:** cards, camera tiles and media views.
- **Pill (999px):** the control pill, filter chips, the Motion step chips and the keyboard switch.
- **Circle:** only the 8px status dot.

Camera tiles clip their media to the 12px corner. Media uses `object-fit: contain` on a Media Well, so a frame is never cropped.

## Components

### Buttons
Quiet until it matters: outlined by default, filled only for the primary action and the two stop-everything buttons.

- **Shape:** gently rounded (8px). Stop and danger buttons use 10px.
- **Default:** transparent, with a Control Edge border, Frost text in weight 600, 44px tall and 14px side padding.
- **Primary:** a Signal Blue fill with Ink Navy text in weight 700 and no border. Each workspace defines its own copy (`mo-primary`, `pr-primary`).
- **STOP ALL MOTION:** an E-Stop Red fill, white Stop-label text and a square stop glyph, 48px tall with 20px side padding. In the Overview Safety card it is a full-width 64px block, with a second line in 12px that lists the targets.
- **Stop heat and gas:** the same E-Stop Red fill, in the Overview Safety card and on the Furnace card. Both send the same route on 1 press: it holds the run, then stops heat and gas.
- **Danger:** transparent, with a Fault Coral border and Soft Coral text in weight 600, 48px tall (44px for Stop stage and in Furnace + microscope).
- **Hover / Focus:** buttons have no hover change. Links change to Pale Signal. Focus is a 2px Signal Blue outline with a 2px offset on all buttons, links, inputs and selects.
- **Disabled:** 0.55 opacity and the not-allowed cursor.

### Inline confirmation
A hazardous action asks for confirmation in the page, not in a browser dialog. The block has a 1px Fault Coral border, an 8px radius and 12px padding. It contains the question, a Confirm button and an outlined Cancel button. Confirm is a Danger button in every workspace. The block takes the keyboard focus when it opens. Esc or Cancel closes it and gives the focus back to the button that opened it.

### Alarm note
An arm alarm or an unconfirmed stop shows at the top of the arm card: a 1px Fault Coral border on an 8% Fault Coral tint, an 8px radius and 12px padding. The title is Fault Coral in weight 700 (Alarm, or Stop not confirmed). Each alarm line gives the ID, the description and the solution in Frost. The button that clears the state (Clear alarm or Recover) is inside the note.

### Navigation
- **Workspace tabs:** links 44px tall with 16px side padding and an 8px radius, in Mist text with weight 500. The current tab gets a Selected Slate background, Frost text, weight 600 and the Signal Blue current marker. The tabs wrap on narrow screens.

### Status strip and control pill
- **Status strip:** a band under the header with 1 chip for each service. A chip has no container: an 8px tone dot, the service name in weight 600 and a detail in Haze.
- **Console offline:** when the console does not answer for 3 seconds, the strip shows 1 Fault Coral chip, Console offline, with the age of the last state, instead of 8 stale devices.
- **Stop result band:** a band under the strip, 13px text on Night Slate. The head row has the tone dot, the button and the press time in weight 700, the phases joined by → (past in Haze, current in Frost 600, future in Idle Grey) and a Dismiss button. The second row lists each target in weight 600 with its result in tone text. A note line in Caution Amber (Fault Coral when the result is bad) says Sending…, Not sent, or that the run hold is not confirmed.
- **Control pill:** a pill with a Control Edge border and 6px 12px padding, in Mist 13px text. Its dot is Idle Grey, and Ready Green when this page controls a device.

### Chips
- **Style:** filter chips, step chips and the keyboard switch are pills, 44px tall, with a Control Edge border and Mist 13px text.
- **State:** a pressed chip (`aria-pressed="true"` or `mo-on`) gets a Signal Blue border, a Selected Slate background and Frost text.

### Cards / Containers
- **Corner Style:** 12px.
- **Background:** Card Slate.
- **Shadow Strategy:** none. See Elevation & Depth.
- **Border:** 1px Panel Line.
- **Internal Padding:** 20px (16px for Furnace + microscope cards at 720px and below), with a 12px gap between blocks.
- **Head:** the Headline title on the left and the badge or actions on the right. It wraps.
- **Rows:** 12px vertical padding with a Row Rule above each row. The name column is at least 96px.

### Readout tiles
An Inset Slate tile with a Label above a monospace value. Pose tiles are 8px radius and 8px padding, 4 in a row (2 at 720px and below). Furnace tiles are 10px radius and 12px padding with a 22px value; PV is Caution Amber and SV is Readout Blue. The pose tiles show `—` while the arm moves.

### Inputs / Fields
- **Style:** a Night Slate background (recessed), a Control Edge border, an 8px radius and 44px height. Numeric inputs use the monospace face at 13–14px and fixed widths (76px to 110px). Text and select fields fill their column.
- **Focus:** the 2px Signal Blue outline.
- **Labels:** a 12px Haze label above the field, 4–6px away.
- **Disabled:** 0.55 opacity.

### Camera tile (signature)
A `figure` in Card Slate with a 12px radius that clips its media. Every workspace uses it.

- **Head:** the view name in weight 600, then "· role" in Mist. On the right is the state badge (LIVE in Ready Green, STALE in Caution Amber, OFFLINE in Haze, failures in Fault Coral) with the frame age in tabular numerals.
- **Stage:** a 4:3 Media Well, or 16:10 for a large tile. A click or Enter selects the view. The focus outline is inside the frame.
- **Overlays:** chip detection boxes on camera 4 have a 2px Signal Blue line and a label on Media Scrim. Thermal markers are Fault Coral (maximum), Readout Blue (minimum) and Frost (center), with a colour bar on Media Scrim. The thermal palettes (ironbow, rainbow, grey) are data colors and are not part of the UI palette.
- **Foot:** the mode and a note in 12px, and Snapshot and Expand buttons.
- **Selected:** a 2px Signal Blue border.

### Badges
- **Hardware badge:** DEMO in Readout Blue on Selected Slate. REAL ROBOT and REAL STAGE in Caution Amber on Amber Shadow. Label-strong text, 6px radius.
- **Media badge:** Media Scrim with a 6px radius, 12px text in weight 600, placed 10px from the top left of a view.

### Jog pad and key caps
- **Jog pad:** a 3 by 3 grid of 48px Inset Slate keys with a 6px gap. Z and R keys are 56px wide.
- **Key caps:** 32px wide, monospace, with a Control Edge border and a 6px radius. A held key gets the pressed chip state.

### Furnace chart and focus bar
- **Chart:** 160px tall on Night Slate with a 10px radius. Grid lines are Panel Line. Axis text is 11px monospace in Haze. PV is a 2.5px solid Caution Amber line. SV is a 2px Readout Blue line with a 6 5 dash.
- **Focus bar:** 12px tall on a Media Well with a Control Edge border and a Signal Blue fill. The limit labels are in 12px Haze below it.

## Do's and Don'ts

### Do:
- **Do** take every color from the `shell.css` variables (`--bg`, `--card`, `--accent`, `--ok` and the others). Put workspace CSS under `#ws-<id>` and reuse the shell classes (`lc-card`, `lc-button`, `lc-danger`, `lc-row`, `lc-muted`).
- **Do** give every state a word next to its tone: a dot and a text, or a badge text.
- **Do** keep STOP ALL MOTION in the header on every workspace, in E-Stop Red.
- **Do** use the Fault Coral outline for device stops and for every hazardous or irreversible action, and ask for confirmation in the page.
- **Do** show device numbers in monospace or tabular numerals.
- **Do** keep every control at least 44px tall.
- **Do** put device media on a Media Well, and put any text on media on the Media Scrim.
- **Do** check every layout with both English and 中文 strings. Let rows and headers wrap; do not truncate labels.

### Don't:
- **Don't** fill any control with red except STOP ALL MOTION and Stop heat and gas.
- **Don't** use Signal Blue for status, or a status tone for selection.
- **Don't** add drop shadows. Use the next surface step or a Panel Line.
- **Don't** load web fonts or other remote assets.
- **Don't** add secondary labels or descriptions that the layout already makes clear.
- **Don't** show a state with color alone.
