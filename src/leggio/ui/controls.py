# SPDX-License-Identifier: GPL-3.0-or-later
"""Translucent overlay bars for the viewer.

Buttons are oversized on purpose: they are used at arm's length.
"""

from __future__ import annotations

import math
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango  # noqa: E402

from .symbols import CATEGORIES, LABELS, draw_preview, draw_tool_icon


def _button(icon: str, tooltip: str, callback: Callable) -> Gtk.Button:
    button = Gtk.Button.new_from_icon_name(icon)
    button.set_tooltip_text(tooltip)
    button.add_css_class("viewer-button")
    button.connect("clicked", lambda _b: callback())
    return button


def _toggle(icon: str, tooltip: str, callback: Callable) -> Gtk.ToggleButton:
    button = Gtk.ToggleButton()
    button.set_icon_name(icon)
    button.set_tooltip_text(tooltip)
    button.add_css_class("viewer-button")
    button.connect("toggled", lambda b: callback(b.get_active()))
    return button


class ControlBar(Gtk.Box):
    """back · title · page · fit · half · spread · night · rotate · crop · zoom"""

    __gtype_name__ = "LeggioControlBar"

    def __init__(self, viewer):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.add_css_class("viewer-overlay")
        self.viewer = viewer
        self._updating = False

        self.append(_button("leggio-back-symbolic", "Back to library (Esc)",
                            viewer.close_viewer))

        self.title = Gtk.Label(xalign=0.0)
        self.title.add_css_class("viewer-title")
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.title.set_hexpand(True)
        self.append(self.title)

        self.page_label = Gtk.Label()
        self.page_label.add_css_class("viewer-page")
        self.append(self.page_label)

        self.fit_button = _toggle(
            "leggio-fit-width-symbolic",
            "Fill the width instead of fitting the whole page",
            viewer.set_fit_width,
        )
        self.append(self.fit_button)

        self.half_button = _toggle(
            "leggio-half-page-symbolic",
            "Half-page turns — show the next system before the turn",
            viewer.set_half_page,
        )
        self.append(self.half_button)

        self.spread_button = _toggle(
            "leggio-spread-symbolic", "Two-page spread", viewer.set_spread
        )
        self.append(self.spread_button)

        self.night_button = _toggle(
            "leggio-night-symbolic",
            "Night mode — invert page colours (N)",
            viewer.set_night_mode,
        )
        self.append(self.night_button)

        self.append(_button("leggio-rotate-symbolic",
                            "Rotate 90° (R)", viewer.rotate))
        self.append(_button("leggio-annotate-symbolic",
                            "Annotate (A)", viewer.enter_annotate_mode))
        self.append(_button("leggio-crop-symbolic",
                            "Adjust margin crop (C)", viewer.enter_crop_mode))
        self.append(_button("leggio-zoom-out-symbolic", "Zoom out", viewer.zoom_out))
        self.append(_button("leggio-zoom-reset-symbolic", "Reset zoom",
                            viewer.zoom_reset))
        self.append(_button("leggio-zoom-in-symbolic", "Zoom in", viewer.zoom_in))

    def sync(self, title: str, page_text: str, fit_width: bool,
             half: bool, night: bool, spread: bool) -> None:
        # Guard against the toggled handlers firing back into the viewer
        # while we are only reflecting its state.
        self._updating = True
        self.title.set_text(title)
        self.page_label.set_text(page_text)
        self.fit_button.set_active(fit_width)
        self.half_button.set_active(half)
        self.night_button.set_active(night)
        self.spread_button.set_active(spread)
        self.half_button.set_sensitive(not spread)
        self._updating = False

    @property
    def updating(self) -> bool:
        return self._updating


# The first colour is the default. Near-black rather than pure black, so
# night mode's inversion turns it near-white like the printed notation.
PEN_COLORS = [
    ("#2b2b2b", "Black"),
    ("#d92626", "Red"),
    ("#1c71d8", "Blue"),
    ("#26a269", "Green"),
]
HIGHLIGHT_COLOR = "#f5c21199"   # translucent, so notation shows through
PEN_WIDTHS = [(0.0018, "Thin"), (0.0032, "Medium"), (0.0055, "Thick")]


def _dot_area(size: int, radius: float, color: str) -> Gtk.DrawingArea:
    """A filled circle, used for colour swatches and stroke-width buttons."""
    area = Gtk.DrawingArea()
    area.set_content_width(size)
    area.set_content_height(size)

    def draw(_a, ctx, width, height):
        r = int(color[1:3], 16) / 255
        g = int(color[3:5], 16) / 255
        b = int(color[5:7], 16) / 255
        a = int(color[7:9], 16) / 255 if len(color) >= 9 else 1.0
        ctx.set_source_rgba(r, g, b, a)
        ctx.arc(width / 2, height / 2, radius, 0, 2 * math.pi)
        ctx.fill()

    area.set_draw_func(draw)
    return area


class AnnotateBar(Gtk.Box):
    """Pen, highlighter, symbols, eraser — plus undo, which matters more
    than any of them the first time you put a stroke in the wrong place."""

    __gtype_name__ = "LeggioAnnotateBar"

    def __init__(self, viewer):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.add_css_class("viewer-overlay")
        self.viewer = viewer
        self._updating = False

        self.append(_button("leggio-prev-page-symbolic", "Previous page",
                            lambda: viewer.turn(-1)))
        self.append(_button("leggio-next-page-symbolic", "Next page",
                            lambda: viewer.turn(1)))
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # --- tools, mutually exclusive ---
        self.tool_buttons: dict[str, Gtk.ToggleButton] = {}
        first: Gtk.ToggleButton | None = None
        for tool, icon, tip in (
            ("pen", "leggio-annotate-symbolic", "Pen"),
            ("highlighter", None, "Highlighter"),
            ("eraser", None, "Eraser — removes a whole stroke"),
            ("link", None, "Jump link — tap the source, then the target (L)"),
        ):
            button = Gtk.ToggleButton()
            if icon is not None:
                button.set_icon_name(icon)
            else:
                # Drawn to match the symbol palette; no stock icon fits.
                area = Gtk.DrawingArea()
                area.set_content_width(22)
                area.set_content_height(22)
                area.set_draw_func(self._draw_tool_icon, tool)
                button.set_child(area)
            button.set_tooltip_text(tip)
            button.add_css_class("viewer-button")
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.connect("toggled", self._on_tool_toggled, tool)
            self.tool_buttons[tool] = button
            self.append(button)

        # --- symbol picker: a toggle for the tool, a popover for the glyph ---
        self.symbol_button = Gtk.ToggleButton()
        self.symbol_button.set_group(first)
        self.symbol_button.set_tooltip_text("Stamp a musical symbol")
        self.symbol_button.add_css_class("viewer-button")
        self.symbol_preview = Gtk.DrawingArea()
        self.symbol_preview.set_content_width(30)
        self.symbol_preview.set_content_height(24)
        self.symbol_preview.set_draw_func(self._draw_symbol_preview)
        self.symbol_button.set_child(self.symbol_preview)
        self.symbol_button.connect("toggled", self._on_tool_toggled, "symbol")
        self.tool_buttons["symbol"] = self.symbol_button
        self.append(self.symbol_button)

        picker = Gtk.MenuButton()
        picker.set_icon_name("leggio-expand-symbolic")
        picker.set_tooltip_text("Choose symbol")
        picker.add_css_class("viewer-button")
        picker.set_popover(self._build_symbol_popover())
        self.append(picker)

        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # --- colours ---
        self.color_buttons: dict[str, Gtk.ToggleButton] = {}
        color_first: Gtk.ToggleButton | None = None
        for color, label in PEN_COLORS:
            button = Gtk.ToggleButton()
            button.set_tooltip_text(label)
            button.add_css_class("viewer-button")
            button.set_child(_dot_area(22, 9, color))
            if color_first is None:
                color_first = button
            else:
                button.set_group(color_first)
            button.connect("toggled", self._on_color_toggled, color)
            self.color_buttons[color] = button
            self.append(button)

        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # --- stroke widths ---
        self.width_buttons: dict[float, Gtk.ToggleButton] = {}
        width_first: Gtk.ToggleButton | None = None
        for value, label in PEN_WIDTHS:
            button = Gtk.ToggleButton()
            button.set_tooltip_text(label)
            button.add_css_class("viewer-button")
            button.set_child(_dot_area(22, 2 + value * 900, "#ffffff"))
            if width_first is None:
                width_first = button
            else:
                button.set_group(width_first)
            button.connect("toggled", self._on_width_toggled, value)
            self.width_buttons[value] = button
            self.append(button)

        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self.undo_button = _button("leggio-undo-symbolic", "Undo (Ctrl+Z)",
                                   viewer.annotation_undo)
        self.append(self.undo_button)
        self.append(_button("leggio-clear-page-symbolic", "Clear the pages on screen",
                            viewer.annotation_clear_page))

        done = Gtk.Button(label="Done")
        done.add_css_class("viewer-button")
        done.add_css_class("suggested-action")
        done.connect("clicked", lambda _b: viewer.exit_annotate_mode())
        self.append(done)

    def _build_symbol_popover(self) -> Gtk.Popover:
        """Grouped, and scrollable — 57 glyphs in one flat grid would be a
        wall. Navigation comes first: those are the marks that derail a
        whole performance when misread, not just one note."""
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for margin in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{margin}")(6)

        columns = 5
        for category, items in CATEGORIES:
            heading = Gtk.Label(label=category.upper(), xalign=0.0)
            heading.add_css_class("palette-heading")
            box.append(heading)
            grid = Gtk.Grid(row_spacing=2, column_spacing=2)
            for index, (glyph, label) in enumerate(items):
                button = Gtk.Button()
                button.set_tooltip_text(label)
                button.add_css_class("flat")
                area = Gtk.DrawingArea()
                area.set_content_width(58)
                area.set_content_height(40)
                area.set_draw_func(self._draw_palette_symbol, glyph)
                button.set_child(area)
                button.connect("clicked", self._on_symbol_chosen, glyph, popover)
                grid.attach(button, index % columns, index // columns, 1, 1)
            box.append(grid)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_max_content_height(460)
        scroller.set_propagate_natural_height(True)
        scroller.set_propagate_natural_width(True)
        scroller.set_child(box)
        popover.set_child(scroller)
        return popover

    def _draw_palette_symbol(self, _area, ctx, width, height, glyph) -> None:
        ctx.set_source_rgb(0.92, 0.92, 0.94)
        draw_preview(ctx, glyph, width, height)

    def _draw_tool_icon(self, _area, ctx, width, height, tool) -> None:
        ctx.set_source_rgb(1, 1, 1)
        draw_tool_icon(ctx, tool, width / 2, height / 2, min(width, height) * 0.9)

    def _draw_symbol_preview(self, _area, ctx, width, height) -> None:
        ctx.set_source_rgb(1, 1, 1)
        draw_preview(ctx, self.viewer.symbol_glyph, width, height)

    def _on_symbol_chosen(self, _button, glyph: str, popover: Gtk.Popover) -> None:
        popover.popdown()
        self.viewer.symbol_glyph = glyph
        self.symbol_button.set_tooltip_text(LABELS.get(glyph, "Symbol"))
        self.symbol_preview.queue_draw()
        self.symbol_button.set_active(True)

    def _on_tool_toggled(self, button: Gtk.ToggleButton, tool: str) -> None:
        if self._updating or not button.get_active():
            return
        self.viewer.set_tool(tool)

    def _on_color_toggled(self, button: Gtk.ToggleButton, color: str) -> None:
        if self._updating or not button.get_active():
            return
        self.viewer.set_pen_color(color)

    def _on_width_toggled(self, button: Gtk.ToggleButton, value: float) -> None:
        if self._updating or not button.get_active():
            return
        self.viewer.set_pen_width(value)

    def sync(self, tool: str, color: str, width: float, can_undo: bool) -> None:
        self._updating = True
        if tool in self.tool_buttons:
            self.tool_buttons[tool].set_active(True)
        if color in self.color_buttons:
            self.color_buttons[color].set_active(True)
        if width in self.width_buttons:
            self.width_buttons[width].set_active(True)
        # Colour and width are meaningless for the eraser and for links.
        drawing = tool not in ("eraser", "link")
        for button in self.color_buttons.values():
            button.set_sensitive(drawing)
        for button in self.width_buttons.values():
            button.set_sensitive(tool in ("pen", "highlighter"))
        self.undo_button.set_sensitive(can_undo)
        self.symbol_preview.queue_draw()
        self._updating = False


class CropBar(Gtk.Box):
    """Shown in place of the control bar while editing the crop rectangle."""

    __gtype_name__ = "LeggioCropBar"

    def __init__(self, viewer):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.add_css_class("viewer-overlay")
        self.viewer = viewer

        label = Gtk.Label(label="Drag the edges to set the crop")
        label.add_css_class("viewer-title")
        label.set_hexpand(True)
        label.set_xalign(0.0)
        self.append(label)

        self.append(_button("leggio-autodetect-symbolic", "Detect automatically",
                            viewer.crop_autodetect))
        self.append(_button("leggio-whole-page-symbolic", "Use the whole page",
                            viewer.crop_reset))

        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("viewer-button")
        cancel.connect("clicked", lambda _b: viewer.exit_crop_mode(save=False))
        self.append(cancel)

        save = Gtk.Button(label="Save")
        save.add_css_class("viewer-button")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda _b: viewer.exit_crop_mode(save=True))
        self.append(save)
