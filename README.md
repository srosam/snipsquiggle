# SnipSquiggle

A Windows Snipping-Tool clone where your annotations are **squiggly and animated**,
and **Ctrl+C copies a looping GIF** to your clipboard.

## What it does

1. **Launch** → the screen dims. Drag a rectangle to snip (Esc cancels).
2. **Editor** opens with your snip. Draw with **Pen**, **Arrow**, or **Box**.
   Every stroke is drawn hand-drawn-squiggly and wobbles with a "boiling line"
   animation (3 jittered variants cycled ~8 fps).
3. **Ctrl+C** → a looping animated GIF is placed on the clipboard.
   - Pastes as an **animated file** into Slack, Discord, Teams, Explorer, etc.
     (via the `CF_HDROP` file-drop format — a real .gif in your temp folder).
   - Pastes as a **static image** into apps that only accept bitmaps.

## Run

```powershell
pip install -r requirements.txt
python snipsquiggle.py
```

## Shortcuts (in the editor)

| Key        | Action              |
|------------|---------------------|
| `Ctrl+C`   | Copy animated GIF   |
| `Ctrl+S`   | Save GIF to disk    |
| `Ctrl+Z`   | Undo last stroke    |
| `Ctrl+N`   | New snip            |
| `Esc`      | Quit                |

## Build a standalone .exe (optional)

```powershell
pip install pyinstaller
pyinstaller --onefile --windowed --name SnipSquiggle snipsquiggle.py
# result: dist\SnipSquiggle.exe
```

## Notes / knobs

Tuning constants live at the top of `snipsquiggle.py`:

- `N_FRAMES` – how many wobble variants (more = smoother boil, bigger GIF)
- `FRAME_MS` – animation speed / GIF frame delay
- `JITTER_BASE` – how squiggly the lines are
- `RESAMPLE_SPACING` – wobble granularity

The GIF is 256-color (GIF format limit), so photo-heavy snips will dither a
little. The animated part is your drawing; the background stays put.

### Copying an animated GIF on Windows — why the file trick?

The Windows clipboard has no native "animated image" format. `Clipboard.SetImage`
only ever holds one static frame. The reliable way to get animation across is to
write a real `.gif` file and advertise it via `CF_HDROP`, so paste targets treat
it like a copied file and keep the animation. This app also sets `CF_DIB` (static
fallback) and a raw `GIF` format for apps that read it.
