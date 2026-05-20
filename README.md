# Forza Gamepad Skin — White Xbox One

A tiny custom skin for [gamepadviewer.com](https://gamepadviewer.com), tuned
for Forza on the white Xbox One controller (use as an OBS browser source).

Two changes from the stock white skin:

- **Sticks inverted** — white at rest, black when L3/R3 is clicked. Forza
  rewards subtle stick inputs, so resting sticks need to read clearly
  against road backgrounds.
- **Triggers as a white meter** — LT/RT fill bottom-up like a pressure
  gauge instead of fading in/out.

Everything else (A/B/X/Y, bumpers, d-pad, start/back) is unchanged from the
built-in `s=0` skin.

---

## Use it

Open this URL with your Xbox controller connected, replacing the
`editcss=` value with the raw GitHub URL of `forza-white.css` on this branch:

```
https://gamepadviewer.com/?s=0&smeter=1&editcss=https://raw.githubusercontent.com/hanneslundh/tw/claude/forza-gamepad-skin-3g6zr/forza-white.css
```

**Both query parameters matter:**

- `s=0` selects the built-in **White Xbox One Controller** skin as the base.
- `smeter=1` switches gamepadviewer.com's trigger rendering from
  opacity-fade to a clip-path meter. Without it, LT/RT will still fade
  in/out and the white silhouette won't read as a bar.
- `editcss=…` layers our overrides (sticks + trigger appearance) on top.

Add the same URL as a **Browser Source** in OBS for a stream overlay.

---

## Local preview

Open `preview.html` directly in a browser. Toggle the L3/R3 buttons to
flip the sticks; drag the LT/RT sliders to drive the meter clip-path. The
preview loads `forza-white.css` from this folder, so any edit you make to
the skin shows up after a reload.

---

## Files

- `forza-white.css` — the skin. Self-contained: SVG sprites are embedded
  as `data:` URIs, so no external asset hosting is needed.
- `assets/stick-white.svg` — design source for the resting stick.
- `assets/stick-black.svg` — design source for the L3/R3-pressed stick.
- `assets/trigger.svg` — design source for the trigger meter silhouette.
- `preview.html` — offline test harness.

The CSS embeds inline copies of the three SVGs; the `assets/` files are
the editable source-of-truth. If you change a sprite, update both.
