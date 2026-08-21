---
name: zenboard-display
description: Designing and changing anything that renders on the ZenBoard e-ink panel - plugin layouts, CSS, artwork. Covers the panel's hard constraints, the render-and-look loop, and the rule that nothing reaches the physical display without permission.
---

# ZenBoard display work

The panel is a Waveshare 7.5" BWR e-ink at 800x480, driven by InkyPi on a Pi
Zero 2W. Plugins render HTML/CSS through headless Chromium to a PNG, which is
then pushed to the panel.

## Never push to the display without asking

Ask, in the chat, every time, before anything reaches the glass. This includes
triggering a refresh, calling `/update_now`, restarting `inkypi` in a way that
causes a paint, or running a script that drives the panel. Approval for one
push is not approval for the next one.

Rendering offscreen is always fine and is how design work is done. "Show me"
means render to a PNG and display the image - not paint the panel.

A refresh is slow (~41s full) and physically wears the panel, so an unwanted
push costs real time and real cycles.

## Where the work happens

There is a full copy of the InkyPi tree on the Mac, kept in sync with the Pi.
Design there, not on the Pi:

1. Change the Mac copy.
2. Render offscreen and **look at the PNG** (see below).
3. Show the user. Iterate until they approve that plugin's look.
4. Only then sync to the Pi.
5. Ask before pushing to the display.

Do not skip to step 4 because a change "looks obviously right". It usually is
not - see the pushpin below.

## The panel's hard constraints

**Three colours, nothing else.** `#000000`, `#ffffff`, `#d40000`. There is no
grey. Any mid-tone dithers into visible noise, so `#555`, `opacity`, and
`rgba()` are all traps. Hierarchy must come from size, weight, case, spacing,
rule thickness and inversion - never from tone.

**`stem_darken()` snaps every pixel below 200 to black.** This protects thin
dark strokes on light grounds, which is what it was written for. On a *dark*
ground it does the opposite: it eats the anti-aliased edges of light glyphs and
small white text disappears. Light-on-dark therefore needs more weight **and**
more size than the same text dark-on-light, and 1px light rules need to be 2px.

**Colour emoji are red.** A glyph like U+1F4CC renders through the colour emoji
font as a red pushpin and dithers badly. Draw shapes in CSS instead.

## Calibrated geometry

Read from `src/config/device.json` -> `display_margins`. Do not hardcode:
`base_plugin._safe_area_css()` injects these as CSS variables and `.container`
applies them.

| orientation | canvas  | margins      | usable  | origin |
|-------------|---------|--------------|---------|--------|
| horizontal  | 800x480 | T16 B8 L6 R6 | 788x456 | 6,16   |
| vertical    | 480x800 | T6 B6 L8 R16 | 456x788 | 8,6    |

Portrait margins are **rotated, not copied** - R16, not T16 - because of the
180-degree mount.

Design to the *usable* box. The plugin must still return the full canvas, and
must swap width/height for portrait, or the verifier reports `WRONG CANVAS`.
Every plugin has to work in both orientations.

## Render and look. Every time.

```
python3 tools/verify_plugins.py <plugin_id>     # renders both orientations
```

Then **open the PNG and actually look at it.** This is not optional and it is
not satisfied by re-reading the CSS.

The verifier reports `bleed` (ink under the mount), `edge` (ink hard against
the safe area, meaning content was clipped) and `ink` coverage. Treat a
pre-existing bad number as pre-existing only after checking it against the
version before your change.

Known verifier bug: it overwrites the `displayMode` setting with the
orientation, so plugins that use `displayMode` for light/dark render in the
wrong theme. Render those manually with the real saved settings.

Why this matters more than knowing design rules: a CSS pushpin took three
passes - it first read as a balloon, then as a map marker - and each fix came
from looking at the render, not from thinking harder. An inverted chip that was
correct in landscape stretched into a full-width black bar in portrait because
it was a block element. Neither was visible in the code.

## Design direction

ZenBoard's current house style is hairline rules, zero border-radius, dense
rows, and small letterspaced uppercase eyebrow labels. That is *exactly* one of
the three looks the `frontend-design` skill names as a generic AI default
("broadsheet-style layout with hairline rules, zero border-radius, dense
newspaper-like columns"). It is not wrong, but it arrived by default rather
than by choice, and it is why every plugin looks like the same plugin.

The user's words: it "lacks modern design, too boxy". Take that as the brief.

So, when reshaping a plugin:

- Follow `frontend-design`'s two-pass process: plan a token system (type,
  layout, signature) and critique it *before* writing CSS.
- Give each plugin its own identity. A weather board and a book reader should
  not share a skeleton.
- Reach for something other than another ruled table. Scale contrast, an
  asymmetric grid, a dominant single figure, generous negative space, inverted
  blocks used as structure rather than as highlight.
- Spend boldness in one place per plugin and keep the rest quiet.
- Negative space is affordable here: the panel is not a dashboard that must be
  dense. Empty area reads as calm on e-ink, not as waste.

Ignore the parts of `frontend-design` that assume a browser: motion and
animation, hover states, responsive breakpoints, keyboard focus, and its
palette advice. There is no interaction and no viewport - the panel is a still
image at one of two fixed sizes.

## Operational notes

- Plugin Python is imported once at process start, so `sudo systemctl restart
  inkypi` is required after changing a `.py`. Jinja templates and CSS are
  re-read per render, so those need no restart - but Flask may serve a cached
  settings page.
- `/usr/local/inkypi/src` is a symlink to `/home/zenith/InkyPi/src`. One tree.
- `src/config/device.json` is gitignored and holds API keys. Never commit it,
  never print it.
- The repo is public. No keys, tokens, or `.bak` files in commits.
