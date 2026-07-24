# Leggio

A sheet music viewer for Linux, built for use at arm's length — on a stand,
hands busy, mid-performance.

![Leggio](data/screenshots/library.png)

GTK4 and libadwaita, PDF rendering through Poppler, Wayland native.

## Install

### Arch Linux

```sh
paru -S leggio        # or: yay -S leggio
```

For the development version, `leggio-git`.

### From source

Install the dependencies for your distribution:

```sh
# Arch, Manjaro
sudo pacman -S --needed python python-gobject python-cairo gtk4 libadwaita poppler-glib

# Fedora
sudo dnf install python3 python3-gobject python3-cairo gtk4 libadwaita poppler-glib

# Debian, Ubuntu
sudo apt install python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-poppler-0.18
```

Then:

```sh
git clone https://github.com/MrGilfy/leggio.git
cd leggio
sudo make install PREFIX=/usr
```

`sudo make uninstall PREFIX=/usr` removes it again. Without `PREFIX` it
installs to `/usr/local`.

To run without installing:

```sh
PYTHONPATH=src python3 -m leggio
```

## What it does

- **Library** of your PDF scores — search by title, composer or tag, sort,
  group into setlists. Titles and composers are editable, and never written
  back to the file.
- **Automatic margin cropping**, so the notation fills the screen instead of
  a page of white space. Adjustable by hand, saved per score.
- **Instant page turns.** The next page is already rendered before you ask
  for it.
- **Half-page turns** show the bottom of the current page above the top of
  the next, so the upcoming system is on screen before you commit.
- **Two-page spread** on wide screens, one page on tall ones, automatically.
- **Annotation** — pen, highlighter, eraser, and 57 musical symbols from
  dynamics and articulation to segno, coda and volta brackets. Stored as
  vectors against the page, so they stay on the notation through zoom,
  rotation and cropping. Your PDFs are never modified.
- **Setlists** page from the end of one score into the start of the next, so
  a whole set reads as one document.
- Remembers where you stopped, per score. Keeps the screen awake.

## Controls

Pages turn by tap, swipe, keyboard, or Bluetooth page-turner pedal — pedals
present themselves as keyboards, and Leggio reads the keys before any
on-screen control can swallow them.

| | |
|---|---|
| Tap left third / right third | Previous / next page |
| Tap the centre | Show or hide the controls |
| Swipe horizontally | Previous / next page |
| `←` `PageUp` `Backspace` | Previous page |
| `→` `PageDown` `Space` | Next page |
| `Home` / `End` | First / last page |
| Pinch, drag | Zoom and pan |

| Key | |
|---|---|
| `A` | Annotate |
| `C` | Crop margins |
| `R` | Rotate 90° |
| `F` | Whole page / fit width |
| `H` | Half-page turns |
| `+` `-` `0` | Zoom in, out, reset |
| `Esc` | Back |

## Not in scope

No MIDI, no MusicXML, no optical music recognition, no audio playback, no
cloud sync. Leggio displays PDFs of sheet music and gets out of the way.

## Files

| | |
|---|---|
| `~/.config/leggio/config.toml` | Configuration |
| `~/.local/share/leggio/library.db` | Library index |
| `~/.cache/leggio/thumbs/` | Thumbnails — safe to delete |

## Contributing

[RELEASING.md](RELEASING.md) covers cutting a release;
[SUBMITTING.md](SUBMITTING.md) covers the AUR packages.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
