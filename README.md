# SnipSquiggle

A cross-platform Snipping-Tool clone where your annotations are **squiggly and
animated**, and **Copy (Ctrl/Cmd+C) puts a looping GIF** on your clipboard.

Runs on **Windows** (fully tested), **macOS**, and **Linux**.

## What it does

1. **Launch** → select a screen region to snip.
   - **Windows / Linux:** the screen dims — drag a rectangle (Esc cancels).
   - **macOS:** the native `screencapture` crosshair.
2. **Editor** opens with your snip. Draw with **Pen**, **Arrow**, or **Box**,
   and pick an **animation style** per stroke:

   | Style   | Look                                            |
   |---------|-------------------------------------------------|
   | 〰 Boil  | hand-drawn squiggle that gently wobbles (default) |
   | ┅ Ants  | marching-ants moving dashes                     |
   | •• Dots | dots flowing along the stroke                   |
   | 😀 Emoji | emojis marching + bobbing along the stroke (🔥 ❤️ ⭐ ✅ 👍 …) |

   ![animation styles](docs/styles.png)

3. **Copy (Ctrl/Cmd+C)** → a looping animated GIF is placed on the clipboard.
   Pastes as an **animated file** into Slack / Discord / Teams / Finder /
   Explorer, and as a **static image** into apps that only accept bitmaps.

## Run

```sh
pip install -r requirements.txt
python snipsquiggle.py
```

`requirements.txt` installs the right OS extras automatically
(`pywin32` on Windows, `pyobjc-framework-Cocoa` on macOS; nothing extra on Linux).

## Shortcuts (in the editor)

Use **Ctrl** on Windows/Linux, **Cmd** on macOS.

| Key         | Action              |
|-------------|---------------------|
| `⌃/⌘ + C`   | Copy animated GIF   |
| `⌃/⌘ + S`   | Save GIF to disk    |
| `⌃/⌘ + Z`   | Undo last stroke    |
| `⌃/⌘ + N`   | New snip            |
| `Esc`       | Quit                |

## Platform notes

- **macOS**
  - **Needs Tk 8.6+.** Apple's built-in `python3` (Command Line Tools) uses the
    old **Tk 8.5**, which crashes GUI apps. Use a Python with modern Tk:
    ```sh
    brew install python-tk
    "$(brew --prefix)/bin/python3" snipsquiggle.py
    ```
    (or install Python from [python.org](https://www.python.org), which bundles
    Tk 8.6). Check with `python3 -c "import tkinter; print(tkinter.TkVersion)"`.
  - First run needs **Screen Recording** permission (System Settings →
    Privacy & Security → Screen Recording) for the capture to contain window
    contents; grant it to your terminal / the built app, then relaunch.
  - On Retina displays the snip is captured at 2× pixels, so the editor window
    can be large — that's expected; the GIF is crisp.
  - Emoji use **Apple Color Emoji**.
- **Linux**
  - Copying needs `xclip` (X11) or `wl-clipboard` (Wayland) installed; without
    them the GIF is still saved via **Save**.
  - Screenshot uses Pillow's `ImageGrab` (needs `scrot`/`gnome-screenshot` on
    some distros). Emoji use **Noto Color Emoji** if installed.
- **Consistent emoji across machines:** drop a color-emoji font at
  `assets/NotoColorEmoji.ttf` (or `assets/emoji.ttf`) and it's used on every
  platform, so GIFs look identical everywhere.

## Build a standalone app (optional)

Windows `.exe`:

```powershell
pip install pyinstaller
pyinstaller --onefile --windowed --name SnipSquiggle snipsquiggle.py
# result: dist\SnipSquiggle.exe
```

macOS `.app`:

```sh
pip install pyinstaller
pyinstaller --windowed --name SnipSquiggle snipsquiggle.py
# result: dist/SnipSquiggle.app
```

> `create_shortcut.py` and `make_icon.py` are Windows-only helpers (Start Menu
> shortcut + `.ico`). On macOS, drag the built `.app` to your Applications folder.

## Notes / knobs

Tuning constants live at the top of `snipsquiggle.py`:

- `N_FRAMES` – frames in the loop (more = smoother marching, bigger GIF)
- `FRAME_MS` – animation speed / GIF frame delay
- `JITTER_BASE` – how squiggly the lines are
- `RESAMPLE_SPACING` – wobble granularity
- `EMOJIS` – the emoji picker set

Each stroke precomputes `N_FRAMES` of backend-agnostic draw *ops* (lines, dots,
emoji stamps) that both the live canvas preview and the GIF exporter consume, so
what you see is exactly what gets copied. Add a new style by extending
`build_ops()`.

The GIF is 256-color (GIF format limit), so photo-heavy snips will dither a
little. The animated part is your drawing; the background stays put.

### Copying an animated GIF — why it's per-OS

No desktop clipboard has a single "animated image" format, so each OS needs its
own trick. SnipSquiggle writes a real `.gif` to a temp file and advertises it in
the way each platform's paste targets understand:

- **Windows** — `CF_HDROP` (the file), plus `CF_DIB` (static fallback) and a raw
  `GIF` format.
- **macOS** — `NSPasteboard` with `public.gif` + `public.png` data and a file URL.
- **Linux** — `xclip` / `wl-copy` with the `image/gif` target.
