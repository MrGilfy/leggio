<div align="center">
  <img src="data/icons/hicolor/scalable/apps/io.github.MrGilfy.Leggio.svg" alt="" width="112">
  <h1>Leggio</h1>
  <p>Read sheet music PDFs from a screen on your music stand.</p>
</div>

![The Leggio library](data/screenshots/library.png)

Leggio keeps a library of your sheet music PDFs, with search, tags and
setlists. Scores open fullscreen with the white margins cut off, and pages
turn instantly by tap, swipe, keyboard or Bluetooth pedal. You can draw on
a score, use half-page turns and night mode, and your PDF files are never
changed.

## Install

**Arch Linux.** From the AUR, or `leggio-git` for the newest code:

```sh
paru -S leggio
```

**Flatpak.** Download `leggio-<version>.flatpak` from the
[latest release](https://github.com/MrGilfy/leggio/releases/latest), then:

```sh
flatpak install --user leggio-*.flatpak
```

Leggio's settings and library live in `~/.var/app/io.github.MrGilfy.Leggio/`.
It can read your home folder but not write to it. To add a folder outside
home, run:

```sh
flatpak override --user --filesystem=/path:ro io.github.MrGilfy.Leggio
```

**From source.** Install Python 3.11+, PyGObject, pycairo, GTK 4,
libadwaita and poppler-glib, then:

```sh
# Fedora: sudo dnf install python3-gobject python3-cairo gtk4 libadwaita poppler-glib
# Debian, Ubuntu: sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-poppler-0.18
git clone https://github.com/MrGilfy/leggio.git && cd leggio
sudo make install PREFIX=/usr
```

## Controls

| Action | Touch | Keyboard |
|---|---|---|
| Next page | Tap right third, swipe left | `→` `Space` `PageDown` |
| Previous page | Tap left third, swipe right | `←` `Backspace` `PageUp` |
| Show or hide controls | Tap the centre | `M` |
| Zoom | Pinch | `+` `-` `0` |
| Draw, crop, rotate | Buttons in the top bar | `A` `C` `R` |
| Half-page turns, night mode | Buttons in the top bar | `H` `N` |
| Back to the library | Back button | `Esc` |

Maintainers: see [docs/MAINTAINING.md](docs/MAINTAINING.md).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
