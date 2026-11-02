# Leggio

Leggio shows your sheet music PDFs on screen, so you can put a laptop or
tablet on a music stand and play from it instead of from paper.

![Leggio](data/screenshots/library.png)

It runs on Linux. It only reads your PDF files — it never changes them.

## What it does

- Keeps a library of your scores. Search by title, composer or tag, and
  group them into setlists.
- Cuts off the white margins automatically, so the notes fill the screen.
  You can adjust the crop by hand if it gets it wrong.
- Turns pages instantly, by tap, swipe, keyboard, or a Bluetooth
  page-turner pedal.
- Half-page turns: the bottom of the page you're on and the top of the
  next one are shown together, so you see what's coming.
- Shows two pages side by side on a wide screen, one page on a tall one.
- Lets you draw on a score with a pen, highlighter and eraser, and add
  musical symbols like dynamics, segno, coda and volta brackets. Your
  marks stay in the right place when you zoom or rotate.
- Night mode turns the page white-on-black for dark stages. Leggio
  remembers it for each score.
- A setlist reads as one long document, so you page from the end of one
  score straight into the next.
- Remembers where you stopped in each score, and keeps the screen from
  going to sleep while you play.

## Install

### Arch Linux

```sh
paru -S leggio        # or: yay -S leggio
```

There is also `leggio-git`, which builds the newest code.

### From source

First install the packages Leggio needs:

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

To remove it again: `sudo make uninstall PREFIX=/usr`.

To try it without installing:

```sh
PYTHONPATH=src python3 -m leggio
```

## Opening a PDF directly

Open a PDF with Leggio from your file manager, or from a terminal:

```sh
leggio score.pdf
```

It opens fullscreen straight away. Files outside your library folders work
too, and Leggio remembers where you stopped in them.

## Controls

| | |
|---|---|
| Tap the left third / right third | Previous / next page |
| Tap the centre | Show or hide the controls |
| Swipe left or right | Previous / next page |
| `←` `PageUp` `Backspace` | Previous page |
| `→` `PageDown` `Space` | Next page |
| `Home` / `End` | First / last page |
| Pinch, drag | Zoom and move around |

| Key | |
|---|---|
| `A` | Draw on the page |
| `C` | Crop the margins |
| `R` | Rotate 90° |
| `F` | Whole page / fit to width |
| `H` | Half-page turns |
| `N` | Night mode |
| `M` | Show or hide the controls |
| `+` `-` `0` | Zoom in, out, reset |
| `Esc` | Go back |

## What it doesn't do

Leggio only displays PDFs. There is no MIDI, no MusicXML, no sound, no
music scanning and no cloud sync.

## Where it keeps things

| | |
|---|---|
| `~/.config/leggio/config.toml` | Settings |
| `~/.local/share/leggio/library.db` | Your library |
| `~/.cache/leggio/thumbs/` | Thumbnails — safe to delete |

## Contributing

Bug reports and pull requests are welcome on
[GitHub](https://github.com/MrGilfy/leggio/issues).

The checks CI runs need ruff, pytest and pycairo, plus the GTK packages
above for the start-up test:

```sh
ruff check src tests
python3 -m pytest
xvfb-run -a python3 tests/smoke.py
```

For maintainers: [RELEASING.md](RELEASING.md) explains how to make a
release, and [SUBMITTING.md](SUBMITTING.md) how to update the AUR
packages.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
