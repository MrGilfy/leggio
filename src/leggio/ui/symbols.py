# SPDX-License-Identifier: GPL-3.0-or-later
"""Musical symbols, drawn as cairo paths.

Deliberately not text glyphs. The Unicode musical blocks (U+1D1xx, which is
where segno and coda live) are missing from most system fonts, and cairo's
toy text API does no font fallback, so anything drawn as a character risks
coming out as an empty box on a machine without a music font. Paths always
render. The exceptions are the markings that genuinely *are* lettering in
engraved music - dynamics, tempo words, D.C./D.S. - which are plain ASCII
in a stock serif face.

Two shapes of symbol:

  POINT  centred on (x, y) at a given size - articulations, accidentals,
         dynamics, navigation marks.
  SPAN   drawn from x to x+span - slurs, ties, hairpins, voltas, 8va. These
         have no meaningful fixed size: a slur is however long the phrase
         is, so the viewer draws them by dragging.
"""

from __future__ import annotations

import math
from typing import Callable, Optional

import cairo

SPAN = "span"
POINT = "point"

# Glyphs whose whole point is their length.
SPAN_GLYPHS = {
    "cresc", "dim", "slur", "tie", "volta1", "volta2", "octave8va",
    "octave8vb",
}

# Glyphs that carry a piece of user text (the number, the chord name).
TEXT_GLYPHS = {"metronome", "chord"}


# --------------------------------------------------------------- primitives

def _serif(ctx, italic: bool = True, bold: bool = True) -> None:
    ctx.select_font_face(
        "serif",
        cairo.FontSlant.ITALIC if italic else cairo.FontSlant.NORMAL,
        cairo.FontWeight.BOLD if bold else cairo.FontWeight.NORMAL,
    )


def _centred_text(ctx, label: str, cx: float, cy: float, size: float,
                  italic: bool = True, bold: bool = True,
                  max_width: Optional[float] = None) -> None:
    _serif(ctx, italic, bold)
    ctx.set_font_size(size)
    extents = ctx.text_extents(label)
    if max_width and extents.width > max_width > 0:
        # "D.C. al Fine" is many times wider than "p" at the same font size.
        # Shrink to fit so a palette button never bleeds into its neighbour.
        ctx.set_font_size(size * max_width / extents.width)
        extents = ctx.text_extents(label)
    ctx.move_to(
        cx - extents.width / 2 - extents.x_bearing,
        cy - extents.height / 2 - extents.y_bearing,
    )
    ctx.show_text(label)
    ctx.new_path()


def _text(label: str, italic: bool = True, bold: bool = True,
          scale: float = 1.0) -> Callable:
    def draw(ctx, cx, cy, s, lw, max_width=None):
        _centred_text(ctx, label, cx, cy, s * scale, italic, bold, max_width)
    draw.is_text = True
    return draw


def _dot(ctx, cx: float, cy: float, r: float) -> None:
    ctx.arc(cx, cy, r, 0, 2 * math.pi)
    ctx.fill()


# ------------------------------------------------------------- articulation

def _staccato(ctx, cx, cy, s, lw):
    _dot(ctx, cx, cy, max(s * 0.13, lw))


def _tenuto(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw * 1.2)
    ctx.move_to(cx - s * 0.32, cy)
    ctx.line_to(cx + s * 0.32, cy)
    ctx.stroke()


def _accent(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw)
    ctx.move_to(cx - s * 0.32, cy - s * 0.24)
    ctx.line_to(cx + s * 0.32, cy)
    ctx.line_to(cx - s * 0.32, cy + s * 0.24)
    ctx.stroke()


def _marcato(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw)
    ctx.move_to(cx - s * 0.28, cy + s * 0.24)
    ctx.line_to(cx, cy - s * 0.28)
    ctx.line_to(cx + s * 0.28, cy + s * 0.24)
    ctx.stroke()


def _fermata(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw)
    ctx.arc(cx, cy + s * 0.14, s * 0.42, math.pi, 2 * math.pi)
    ctx.stroke()
    _dot(ctx, cx, cy + s * 0.08, lw * 0.95)


# --------------------------------------------------------------- accidentals

def _sharp(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw)
    for dx in (-0.14, 0.14):
        ctx.move_to(cx + s * dx, cy - s * 0.42)
        ctx.line_to(cx + s * dx, cy + s * 0.46)
    ctx.stroke()
    ctx.set_line_width(lw * 1.5)
    for dy in (-0.16, 0.14):
        ctx.move_to(cx - s * 0.3, cy + s * dy + s * 0.06)
        ctx.line_to(cx + s * 0.3, cy + s * dy - s * 0.06)
    ctx.stroke()


def _flat(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw)
    ctx.move_to(cx - s * 0.14, cy - s * 0.48)
    ctx.line_to(cx - s * 0.14, cy + s * 0.36)
    ctx.stroke()
    ctx.move_to(cx - s * 0.14, cy + s * 0.36)
    ctx.curve_to(cx + s * 0.34, cy + s * 0.06,
                 cx + s * 0.18, cy - s * 0.22,
                 cx - s * 0.14, cy + s * 0.02)
    ctx.stroke()


def _natural(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw)
    ctx.move_to(cx - s * 0.15, cy - s * 0.42)
    ctx.line_to(cx - s * 0.15, cy + s * 0.24)
    ctx.move_to(cx + s * 0.15, cy - s * 0.24)
    ctx.line_to(cx + s * 0.15, cy + s * 0.42)
    ctx.stroke()
    ctx.set_line_width(lw * 1.4)
    ctx.move_to(cx - s * 0.15, cy - s * 0.12)
    ctx.line_to(cx + s * 0.15, cy - s * 0.2)
    ctx.move_to(cx - s * 0.15, cy + s * 0.2)
    ctx.line_to(cx + s * 0.15, cy + s * 0.12)
    ctx.stroke()


def _double_sharp(ctx, cx, cy, s, lw):
    # An x of four wedges; a bold saltire reads correctly at score size.
    ctx.set_line_width(lw * 1.9)
    for sx in (-1, 1):
        ctx.move_to(cx - s * 0.26 * sx, cy - s * 0.26)
        ctx.line_to(cx + s * 0.26 * sx, cy + s * 0.26)
    ctx.stroke()


def _double_flat(ctx, cx, cy, s, lw):
    _flat(ctx, cx - s * 0.22, cy, s, lw)
    _flat(ctx, cx + s * 0.22, cy, s, lw)


def _aug_dot(ctx, cx, cy, s, lw):
    _dot(ctx, cx, cy, max(s * 0.15, lw))


# ---------------------------------------------------------------- ornaments

def _trill(ctx, cx, cy, s, lw):
    _centred_text(ctx, "tr", cx, cy, s)


def _mordent(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw)
    ctx.move_to(cx - s * 0.36, cy)
    ctx.line_to(cx - s * 0.18, cy - s * 0.18)
    ctx.line_to(cx, cy)
    ctx.line_to(cx + s * 0.18, cy - s * 0.18)
    ctx.line_to(cx + s * 0.36, cy)
    ctx.stroke()
    ctx.move_to(cx, cy - s * 0.3)
    ctx.line_to(cx, cy + s * 0.3)
    ctx.stroke()


def _turn(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw)
    ctx.move_to(cx - s * 0.38, cy + s * 0.06)
    ctx.curve_to(cx - s * 0.34, cy - s * 0.24, cx - s * 0.02, cy - s * 0.24,
                 cx, cy)
    ctx.curve_to(cx + s * 0.02, cy + s * 0.24, cx + s * 0.34, cy + s * 0.24,
                 cx + s * 0.38, cy - s * 0.06)
    ctx.stroke()


def _grace_note(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw * 0.9)
    ctx.save()
    ctx.translate(cx - s * 0.12, cy + s * 0.26)
    ctx.rotate(-0.35)
    ctx.scale(1.0, 0.68)
    ctx.arc(0, 0, s * 0.15, 0, 2 * math.pi)
    ctx.restore()
    ctx.fill()
    ctx.move_to(cx + s * 0.02, cy + s * 0.24)
    ctx.line_to(cx + s * 0.02, cy - s * 0.36)
    ctx.stroke()
    # the slash that makes it an acciaccatura
    ctx.move_to(cx - s * 0.18, cy - s * 0.04)
    ctx.line_to(cx + s * 0.22, cy - s * 0.3)
    ctx.stroke()


def _metronome(ctx, cx, cy, s, lw, label: str = "120",
               max_width: Optional[float] = None):
    """A quarter note plus '= N'. Drawn rather than typed: the note glyph
    is exactly the character most fonts lack."""
    text = f"= {label}"
    _serif(ctx, italic=False, bold=False)
    ctx.set_font_size(s * 0.9)
    extents = ctx.text_extents(text)
    note_w = s * 0.42
    total = note_w + extents.width
    if max_width and total > max_width > 0:
        s = s * max_width / total
        note_w = s * 0.42
        ctx.set_font_size(s * 0.9)
        extents = ctx.text_extents(text)
        total = note_w + extents.width
    left = cx - total / 2

    ctx.save()
    ctx.translate(left + s * 0.16, cy + s * 0.28)
    ctx.rotate(-0.3)
    ctx.scale(1.0, 0.7)
    ctx.arc(0, 0, s * 0.17, 0, 2 * math.pi)
    ctx.restore()
    ctx.fill()
    ctx.set_line_width(lw)
    ctx.move_to(left + s * 0.31, cy + s * 0.26)
    ctx.line_to(left + s * 0.31, cy - s * 0.42)
    ctx.stroke()

    ctx.move_to(left + note_w - extents.x_bearing,
                cy - extents.height / 2 - extents.y_bearing)
    ctx.show_text(text)
    ctx.new_path()


# --------------------------------------------------------------- navigation

def _segno(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw)
    ctx.move_to(cx - s * 0.34, cy + s * 0.36)
    ctx.line_to(cx + s * 0.34, cy - s * 0.36)
    ctx.stroke()
    ctx.move_to(cx + s * 0.30, cy - s * 0.20)
    ctx.curve_to(cx + s * 0.14, cy - s * 0.48, cx - s * 0.26, cy - s * 0.34,
                 cx - s * 0.10, cy - s * 0.04)
    ctx.curve_to(cx + s * 0.10, cy + s * 0.26, cx - s * 0.20, cy + s * 0.46,
                 cx - s * 0.32, cy + s * 0.20)
    ctx.stroke()
    _dot(ctx, cx + s * 0.20, cy + s * 0.16, lw * 0.9)
    _dot(ctx, cx - s * 0.20, cy - s * 0.16, lw * 0.9)


def _coda(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw)
    ctx.arc(cx, cy, s * 0.28, 0, 2 * math.pi)
    ctx.stroke()
    ctx.move_to(cx, cy - s * 0.46)
    ctx.line_to(cx, cy + s * 0.46)
    ctx.move_to(cx - s * 0.46, cy)
    ctx.line_to(cx + s * 0.46, cy)
    ctx.stroke()


def _repeat(ctx, cx, cy, s, lw, start: bool):
    top, bottom = cy - s * 0.5, cy + s * 0.5
    direction = 1 if start else -1
    thick_x = cx - direction * s * 0.26
    thin_x = cx - direction * s * 0.08
    ctx.rectangle(thick_x - s * 0.06, top, s * 0.12, s)
    ctx.fill()
    ctx.set_line_width(lw)
    ctx.move_to(thin_x, top)
    ctx.line_to(thin_x, bottom)
    ctx.stroke()
    for dy in (-0.17, 0.17):
        _dot(ctx, cx + direction * s * 0.22, cy + s * dy, max(lw * 0.9, s * 0.06))


def _repeat_start(ctx, cx, cy, s, lw):
    _repeat(ctx, cx, cy, s, lw, True)


def _repeat_end(ctx, cx, cy, s, lw):
    _repeat(ctx, cx, cy, s, lw, False)


def _simile(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw * 1.8)
    ctx.move_to(cx - s * 0.24, cy + s * 0.30)
    ctx.line_to(cx + s * 0.24, cy - s * 0.30)
    ctx.stroke()
    _dot(ctx, cx - s * 0.26, cy - s * 0.20, max(lw * 0.9, s * 0.06))
    _dot(ctx, cx + s * 0.26, cy + s * 0.20, max(lw * 0.9, s * 0.06))


def _pedal_release(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw)
    for i in range(6):
        angle = i * math.pi / 3
        ctx.move_to(cx, cy)
        ctx.line_to(cx + math.cos(angle) * s * 0.36,
                    cy + math.sin(angle) * s * 0.36)
    ctx.stroke()


def _downbow(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw)
    ctx.move_to(cx - s * 0.30, cy + s * 0.14)
    ctx.line_to(cx - s * 0.30, cy - s * 0.28)
    ctx.line_to(cx + s * 0.30, cy - s * 0.28)
    ctx.line_to(cx + s * 0.30, cy + s * 0.14)
    ctx.stroke()


def _upbow(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw)
    ctx.move_to(cx - s * 0.24, cy - s * 0.36)
    ctx.line_to(cx, cy + s * 0.36)
    ctx.line_to(cx + s * 0.24, cy - s * 0.36)
    ctx.stroke()


def _breath(ctx, cx, cy, s, lw):
    ctx.set_line_width(lw)
    ctx.move_to(cx + s * 0.12, cy - s * 0.32)
    ctx.curve_to(cx - s * 0.12, cy - s * 0.08, cx - s * 0.02, cy + s * 0.14,
                 cx - s * 0.18, cy + s * 0.36)
    ctx.stroke()


_POINT_DRAW: dict[str, Callable] = {
    # dynamics
    "pp": _text("pp"), "p": _text("p"), "mp": _text("mp"),
    "mf": _text("mf"), "f": _text("f"), "ff": _text("ff"),
    "sfz": _text("sfz"), "fp": _text("fp"),
    # articulation
    "staccato": _staccato, "tenuto": _tenuto, "accent": _accent,
    "marcato": _marcato, "fermata": _fermata,
    # tempo
    "largo": _text("Largo", bold=False, scale=0.9),
    "adagio": _text("Adagio", bold=False, scale=0.9),
    "andante": _text("Andante", bold=False, scale=0.9),
    "moderato": _text("Moderato", bold=False, scale=0.9),
    "allegro": _text("Allegro", bold=False, scale=0.9),
    "presto": _text("Presto", bold=False, scale=0.9),
    "rit": _text("rit.", bold=False, scale=0.9),
    "rall": _text("rall.", bold=False, scale=0.9),
    "accel": _text("accel.", bold=False, scale=0.9),
    "atempo": _text("a tempo", bold=False, scale=0.85),
    "rubato": _text("rubato", bold=False, scale=0.9),
    "metronome": _metronome,
    # navigation
    "segno": _segno, "coda": _coda,
    "repeat_start": _repeat_start, "repeat_end": _repeat_end,
    "dc_al_fine": _text("D.C. al Fine", bold=False, scale=0.72),
    "ds_al_coda": _text("D.S. al Coda", bold=False, scale=0.72),
    "fine": _text("Fine", bold=False, scale=0.9),
    "simile": _simile,
    # pitch and duration
    "sharp": _sharp, "flat": _flat, "natural": _natural,
    "double_sharp": _double_sharp, "double_flat": _double_flat,
    "aug_dot": _aug_dot, "trill": _trill, "mordent": _mordent,
    "turn": _turn, "grace_note": _grace_note,
    # instrument
    "pedal": _text("Ped.", italic=False, bold=False, scale=0.8),
    "pedal_release": _pedal_release,
    "downbow": _downbow, "upbow": _upbow, "breath": _breath,
    "chord": _text("C", italic=False, scale=0.95),
}


# -------------------------------------------------------------------- spans

def _span_hairpin(ctx, x0, y, span, s, lw, opening: bool):
    ctx.set_line_width(lw)
    half = s * 0.34
    if opening:
        ctx.move_to(x0, y)
        ctx.line_to(x0 + span, y - half)
        ctx.move_to(x0, y)
        ctx.line_to(x0 + span, y + half)
    else:
        ctx.move_to(x0, y - half)
        ctx.line_to(x0 + span, y)
        ctx.move_to(x0, y + half)
        ctx.line_to(x0 + span, y)
    ctx.stroke()


def _span_curve(ctx, x0, y, span, s, lw, depth_factor: float):
    ctx.set_line_width(lw)
    depth = min(span * depth_factor, s * 1.6)
    ctx.move_to(x0, y)
    ctx.curve_to(x0 + span * 0.25, y - depth,
                 x0 + span * 0.75, y - depth,
                 x0 + span, y)
    ctx.stroke()


def _span_volta(ctx, x0, y, span, s, lw, label: str):
    ctx.set_line_width(lw)
    height = s * 0.85
    ctx.move_to(x0, y + height)
    ctx.line_to(x0, y)
    ctx.line_to(x0 + span, y)
    ctx.stroke()
    _serif(ctx, italic=False, bold=False)
    ctx.set_font_size(s * 0.75)
    ctx.move_to(x0 + s * 0.22, y + height * 0.85)
    ctx.show_text(label)
    ctx.new_path()


def _span_octave(ctx, x0, y, span, s, lw, label: str):
    _serif(ctx, italic=True, bold=False)
    ctx.set_font_size(s * 0.9)
    extents = ctx.text_extents(label)
    ctx.move_to(x0, y + extents.height / 2)
    ctx.show_text(label)
    ctx.new_path()

    line_start = x0 + extents.width + s * 0.18
    if line_start < x0 + span:
        ctx.set_line_width(lw)
        ctx.save()
        ctx.set_dash([s * 0.28, s * 0.22])
        ctx.move_to(line_start, y)
        ctx.line_to(x0 + span, y)
        ctx.stroke()
        ctx.restore()
        ctx.move_to(x0 + span, y)
        ctx.line_to(x0 + span, y + s * 0.42)
        ctx.stroke()


_SPAN_DRAW: dict[str, Callable] = {
    "cresc": lambda ctx, x, y, sp, s, lw: _span_hairpin(ctx, x, y, sp, s, lw, True),
    "dim": lambda ctx, x, y, sp, s, lw: _span_hairpin(ctx, x, y, sp, s, lw, False),
    "slur": lambda ctx, x, y, sp, s, lw: _span_curve(ctx, x, y, sp, s, lw, 0.22),
    "tie": lambda ctx, x, y, sp, s, lw: _span_curve(ctx, x, y, sp, s, lw, 0.10),
    "volta1": lambda ctx, x, y, sp, s, lw: _span_volta(ctx, x, y, sp, s, lw, "1."),
    "volta2": lambda ctx, x, y, sp, s, lw: _span_volta(ctx, x, y, sp, s, lw, "2."),
    "octave8va": lambda ctx, x, y, sp, s, lw: _span_octave(ctx, x, y, sp, s, lw, "8va"),
    "octave8vb": lambda ctx, x, y, sp, s, lw: _span_octave(ctx, x, y, sp, s, lw, "8vb"),
}


# The palette, grouped the way a musician would look for them. Navigation
# sits high up because those are the marks that derail a whole performance
# when misread, not just one note.
CATEGORIES: list[tuple[str, list[tuple[str, str]]]] = [
    ("Navigation", [
        ("segno", "Segno"), ("coda", "Coda"),
        ("ds_al_coda", "D.S. al Coda"), ("dc_al_fine", "D.C. al Fine"),
        ("fine", "Fine"), ("simile", "Simile"),
        ("repeat_start", "Repeat start"), ("repeat_end", "Repeat end"),
        ("volta1", "1st ending"), ("volta2", "2nd ending"),
    ]),
    ("Dynamics", [
        ("pp", "pianissimo"), ("p", "piano"), ("mp", "mezzo-piano"),
        ("mf", "mezzo-forte"), ("f", "forte"), ("ff", "fortissimo"),
        ("sfz", "sforzando"), ("fp", "forte-piano"),
        ("cresc", "Crescendo"), ("dim", "Diminuendo"),
    ]),
    ("Articulation", [
        ("staccato", "Staccato"), ("tenuto", "Tenuto"), ("accent", "Accent"),
        ("marcato", "Marcato"), ("fermata", "Fermata"),
        ("slur", "Slur"), ("tie", "Tie"),
    ]),
    ("Tempo", [
        ("largo", "Largo"), ("adagio", "Adagio"), ("andante", "Andante"),
        ("moderato", "Moderato"), ("allegro", "Allegro"), ("presto", "Presto"),
        ("rit", "ritardando"), ("rall", "rallentando"),
        ("accel", "accelerando"), ("atempo", "a tempo"), ("rubato", "rubato"),
        ("metronome", "Metronome mark"),
    ]),
    ("Pitch & duration", [
        ("sharp", "Sharp"), ("flat", "Flat"), ("natural", "Natural"),
        ("double_sharp", "Double sharp"), ("double_flat", "Double flat"),
        ("aug_dot", "Augmentation dot"), ("trill", "Trill"),
        ("mordent", "Mordent"), ("turn", "Turn"), ("grace_note", "Grace note"),
        ("octave8va", "8va"), ("octave8vb", "8vb"),
    ]),
    ("Instrument", [
        ("pedal", "Pedal down"), ("pedal_release", "Pedal up"),
        ("downbow", "Down bow"), ("upbow", "Up bow"),
        ("breath", "Breath mark"), ("chord", "Chord symbol"),
    ]),
]

LABELS: dict[str, str] = {
    glyph: label for _cat, items in CATEGORIES for glyph, label in items
}


def is_span(glyph: str) -> bool:
    return glyph in SPAN_GLYPHS


def needs_text(glyph: str) -> bool:
    return glyph in TEXT_GLYPHS


def draw_symbol(ctx, glyph: str, cx: float, cy: float, size: float,
                label: Optional[str] = None,
                max_width: Optional[float] = None) -> None:
    """Draw a point symbol centred on (cx, cy), in the current colour."""
    handler = _POINT_DRAW.get(glyph)
    if handler is None:
        return
    ctx.save()
    # Start from a clean path. cairo save/restore does not cover the path,
    # so a caller that left a current point behind (show_text does) would
    # otherwise get a stray line drawn from it into our first arc.
    ctx.new_path()
    ctx.set_line_cap(cairo.LineCap.ROUND)
    ctx.set_line_join(cairo.LineJoin.ROUND)
    line_width = max(size * 0.075, 1.0)
    if glyph == "metronome":
        _metronome(ctx, cx, cy, size, line_width, label or "120", max_width)
    elif glyph == "chord":
        _centred_text(ctx, label or "C", cx, cy, size * 0.95, italic=False,
                      max_width=max_width)
    elif getattr(handler, "is_text", False):
        handler(ctx, cx, cy, size, line_width, max_width)
    else:
        handler(ctx, cx, cy, size, line_width)
    ctx.new_path()
    ctx.restore()


def draw_span_symbol(ctx, glyph: str, x0: float, y: float, span: float,
                     size: float) -> None:
    """Draw a spanning symbol from x0 to x0+span at height y."""
    handler = _SPAN_DRAW.get(glyph)
    if handler is None:
        return
    ctx.save()
    ctx.new_path()
    ctx.set_line_cap(cairo.LineCap.ROUND)
    ctx.set_line_join(cairo.LineJoin.ROUND)
    handler(ctx, x0, y, max(span, size * 0.5), size, max(size * 0.075, 1.0))
    ctx.new_path()
    ctx.restore()


def draw_preview(ctx, glyph: str, width: float, height: float) -> None:
    """Render a glyph to fill a palette button of the given size."""
    if is_span(glyph):
        size = height * 0.42
        draw_span_symbol(ctx, glyph, width * 0.12, height * 0.55,
                         width * 0.76, size)
    else:
        draw_symbol(ctx, glyph, width / 2, height / 2,
                    min(width, height) * 0.66, max_width=width * 0.92)


# ------------------------------------------------------------- tool icons

def draw_eraser(ctx, cx: float, cy: float, size: float) -> None:
    """Adwaita has no eraser icon, and the nearest stand-in (edit-clear)
    reads as "clear the field", not "rub this out"."""
    ctx.save()
    ctx.translate(cx, cy)
    ctx.rotate(-math.pi / 5)
    w, h = size * 0.86, size * 0.44
    ctx.set_line_width(max(size * 0.085, 1.0))
    ctx.set_line_join(cairo.LineJoin.ROUND)
    ctx.rectangle(-w / 2, -h / 2, w, h)
    ctx.stroke()
    ctx.rectangle(-w / 2, -h / 2, w * 0.38, h)
    ctx.fill()
    ctx.restore()


def draw_highlighter(ctx, cx: float, cy: float, size: float) -> None:
    ctx.save()
    ctx.translate(cx, cy)
    ctx.set_line_width(max(size * 0.085, 1.0))
    ctx.set_line_join(cairo.LineJoin.ROUND)
    body_w, body_h = size * 0.42, size * 0.4
    ctx.rectangle(-body_w / 2, -size * 0.44, body_w, body_h)
    ctx.stroke()
    ctx.move_to(-body_w / 2, -size * 0.04)
    ctx.line_to(body_w / 2, -size * 0.04)
    ctx.line_to(body_w * 0.34, size * 0.18)
    ctx.line_to(-body_w * 0.34, size * 0.18)
    ctx.close_path()
    ctx.fill()
    ctx.set_line_width(max(size * 0.14, 1.5))
    ctx.move_to(-size * 0.42, size * 0.38)
    ctx.line_to(size * 0.42, size * 0.38)
    ctx.stroke()
    ctx.restore()


TOOL_ICONS = {"eraser": draw_eraser, "highlighter": draw_highlighter}


def draw_tool_icon(ctx, tool: str, cx: float, cy: float, size: float) -> None:
    handler = TOOL_ICONS.get(tool)
    if handler is not None:
        ctx.save()
        ctx.new_path()
        ctx.set_line_cap(cairo.LineCap.ROUND)
        handler(ctx, cx, cy, size)
        ctx.new_path()
        ctx.restore()
