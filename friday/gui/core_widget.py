"""The animated FRIDAY core — the central visual, state-driven, and the
hero of the whole interface (it is sized by `MainWindow` to dominate the
composition, not just "a widget among widgets").

Everything drawn here is either a direct function of `CoreState` (see
`friday.gui.state_hub`) or purely decorative motion (the orbital markers,
the scanning sweep, the particles, the activity bars) used the way a
spinner communicates "I'm working" — it is never presented as a
measurement. In particular the activity bars around the ring during
LISTENING/SPEAKING are a stylized "something is happening" indicator, not a
rendering of actual microphone amplitude — friday.voice doesn't expose a
live amplitude stream, and fabricating one to draw a fancier waveform would
violate this project's "no fake telemetry" rule. If a real amplitude
callback is added to `friday.voice.capture`/`VoiceSession` later, this
widget has one place (`_paint_activity_bars`) to switch from stylized to
real.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QConicalGradient, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

from . import theme
from .state_hub import CoreState

_ACTIVE_STATES = {
    CoreState.LISTENING, CoreState.THINKING, CoreState.EXECUTING,
    CoreState.SPEAKING, CoreState.AWAITING_CONFIRM, CoreState.ERROR,
}

# -- animation timing ---------------------------------------------------------
# The animation itself is elapsed-time-based (see `CoreWidget._tick`), so
# these only decide how often we *sample* that continuous motion, not how
# fast it moves. 30 FPS is smooth enough for this widget's motion (slow
# breathing/sweeping, no fast cuts) and costs noticeably less CPU than the
# previous ~50 FPS — see scripts/benchmark_core_widget.py.
ACTIVE_FRAME_INTERVAL_MS = 33    # ~30 FPS while LISTENING/THINKING/etc.
INACTIVE_FRAME_INTERVAL_MS = 60  # ~16.7 FPS at rest (BOOTING/IDLE) — unchanged


@dataclass(frozen=True)
class _StateColors:
    """QColor variants for one `CoreState` that only depend on that state's
    fixed hex color (see `_STATE_PROFILE`) — never on the per-frame `energy`
    or animation phase. Built once per state and reused for as long as that
    state stays active, instead of being reconstructed (including a hex
    string re-parse and, for the `.lighter()` variants, an HSV round-trip)
    on every single `paintEvent`."""

    base: QColor
    transparent: QColor     # alpha 0 — the "faded" end of every gradient/pen
    inner_edge: QColor      # .lighter(115), fixed alpha 230 — core inner-gradient edge stop
    core_edge_pen: QColor   # .lighter(140), fixed alpha 160 — core boundary pen
    orbital_bright: QColor  # .lighter(150) — alpha still set per frame from energy
    orbital_dim: QColor     # .lighter(130) — alpha still set per frame from energy


def _build_state_colors(color_hex: str) -> _StateColors:
    base = QColor(color_hex)
    transparent = QColor(base)
    transparent.setAlpha(0)
    inner_edge = QColor(base).lighter(115)
    inner_edge.setAlpha(230)
    core_edge_pen = QColor(base).lighter(140)
    core_edge_pen.setAlpha(160)
    return _StateColors(
        base=base,
        transparent=transparent,
        inner_edge=inner_edge,
        core_edge_pen=core_edge_pen,
        orbital_bright=QColor(base).lighter(150),
        orbital_dim=QColor(base).lighter(130),
    )


# (energy 0-1, base color, sweep speed rad/s, bar activity 0-1)
_STATE_PROFILE: dict[CoreState, tuple[float, str, float, float]] = {
    CoreState.BOOTING: (0.15, theme.TEXT_SECONDARY, 0.15, 0.0),
    CoreState.IDLE: (0.22, theme.ACCENT_SOFT, 0.08, 0.0),
    CoreState.LISTENING: (0.85, theme.ACCENT, 1.4, 0.9),
    CoreState.THINKING: (0.5, theme.ACCENT_SOFT, 0.9, 0.0),
    CoreState.EXECUTING: (0.65, theme.ACCENT, 1.8, 0.0),
    CoreState.AWAITING_CONFIRM: (0.55, theme.WARNING, 0.2, 0.0),
    CoreState.SPEAKING: (0.75, theme.ACCENT, 0.7, 0.6),
    CoreState.ERROR: (0.6, theme.ERROR, 0.3, 0.0),
}


class CoreWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(240, 240)
        self._state = CoreState.BOOTING
        self._phase = 0.0
        self._last_tick = time.monotonic()
        # Smoothed energy so a state change eases rather than snaps.
        self._energy = 0.15
        self._target_energy = 0.15

        # Paint resources that don't need to change every frame — see
        # `_StateColors` and `_colors_for()`, and the per-layer comments
        # below for the gradients.
        self._state_color_cache: dict[CoreState, _StateColors] = {}
        self._hot_base = QColor(theme.ACCENT_BRIGHT)
        self._ambient_gradient = QRadialGradient()
        self._outer_conic_gradient = QConicalGradient()
        self._core_bloom_gradient = QRadialGradient()
        self._core_mid_gradient = QRadialGradient()
        self._core_inner_gradient = QRadialGradient()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(INACTIVE_FRAME_INTERVAL_MS)

    # -- public API ---------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state.value

    def set_core_state(self, state: str) -> None:
        try:
            new_state = CoreState(state)
        except ValueError:
            return
        if new_state == self._state:
            return
        self._state = new_state
        energy, *_ = _STATE_PROFILE[new_state]
        self._target_energy = energy
        self._timer.setInterval(
            ACTIVE_FRAME_INTERVAL_MS if new_state in _ACTIVE_STATES else INACTIVE_FRAME_INTERVAL_MS
        )
        self.update()

    def _colors_for(self, state: CoreState) -> _StateColors:
        colors = self._state_color_cache.get(state)
        if colors is None:
            _, color_hex, _, _ = _STATE_PROFILE[state]
            colors = _build_state_colors(color_hex)
            self._state_color_cache[state] = colors
        return colors

    # -- animation ------------------------------------------------------------

    def _tick(self) -> None:
        now = time.monotonic()
        dt = min(0.25, now - self._last_tick)
        self._last_tick = now

        _, _, sweep_speed, _ = _STATE_PROFILE[self._state]
        self._phase += dt * sweep_speed
        # Ease energy toward its target rather than snapping — a state
        # transition should feel like a breath, not a light switch.
        self._energy += (self._target_energy - self._energy) * min(1.0, dt * 4.0)
        self.update()

    # -- painting ---------------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802 — Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        radius = min(w, h) * 0.32
        # Every layer must fade to fully transparent *inside* this radius —
        # QWidget hard-clips painting to its own rect, so anything that
        # hasn't reached alpha 0 by the edge shows up as a visible box
        # against the window's own background gradient instead of blending
        # into it. A circle inscribed in the widget's rect (not the wider
        # bounding square) is the largest guaranteed-unclipped extent.
        safe_radius = min(w, h) / 2.0 * 0.94

        _, _, _, bar_activity = _STATE_PROFILE[self._state]
        state_colors = self._colors_for(self._state)
        color = state_colors.base
        energy = self._energy
        # A slow, near-imperceptible breath even at rest — the difference
        # between "idle" and "off."
        breath = math.sin(self._phase * 0.6) * 0.5 + 0.5

        self._paint_ambient_field(painter, cx, cy, safe_radius, color, state_colors, energy)
        self._paint_particles(painter, cx, cy, radius, color, energy, safe_radius)
        self._paint_outer_ticks(painter, cx, cy, radius, color)
        self._paint_rings(painter, cx, cy, radius, color, state_colors, energy)
        if bar_activity > 0:
            self._paint_activity_bars(painter, cx, cy, radius, color, bar_activity)
        self._paint_core(painter, cx, cy, radius, color, state_colors, energy, breath)
        self._paint_orbitals(painter, cx, cy, radius, color, state_colors, energy)

        painter.end()

    # -- layers -----------------------------------------------------------------

    def _paint_ambient_field(self, painter: QPainter, cx: float, cy: float, field_radius: float, color: QColor, state_colors: _StateColors, energy: float) -> None:
        """A very large, very soft glow behind everything — depth, not a halo.
        `field_radius` is already clamped to fade out inside the widget's own
        clip rect (see the `safe_radius` comment in `paintEvent`).

        The gradient's center/radius only actually change on resize, but we
        set them unconditionally each frame from the current geometry (cheap
        setters) rather than tracking a separate "did geometry change" flag —
        that keeps this correct across resize/DPR changes by construction,
        with no invalidation path to get wrong. Reusing the cached
        `QRadialGradient` object instead of constructing a new one is what
        actually saves the allocation."""
        gradient = self._ambient_gradient
        gradient.setCenter(cx, cy)
        gradient.setFocalPoint(cx, cy)
        gradient.setRadius(field_radius)
        bright = QColor(color)
        bright.setAlpha(int(10 + 14 * energy))
        gradient.setColorAt(0.0, bright)
        gradient.setColorAt(1.0, state_colors.transparent)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(QRectF(cx - field_radius, cy - field_radius, field_radius * 2, field_radius * 2))

    def _paint_particles(self, painter: QPainter, cx: float, cy: float, radius: float, color: QColor, energy: float, safe_radius: float) -> None:
        painter.save()
        count = 7
        max_drift = min(radius * 1.7, safe_radius * 0.98)
        min_drift = min(radius * 1.15, max_drift)
        for i in range(count):
            seed = i * 2.399963  # irrational-ish spread, avoids visible periodicity
            drift_r = min_drift + (max_drift - min_drift) * ((math.sin(seed * 3.1) + 1) / 2)
            angle = seed * 6.0 + self._phase * (0.08 + 0.04 * (i % 3))
            x = cx + drift_r * math.cos(angle)
            y = cy + drift_r * math.sin(angle) * 0.85
            twinkle = (math.sin(self._phase * 1.3 + seed * 4) + 1) / 2
            dot = QColor(color)
            dot.setAlpha(int(18 + 45 * twinkle * (0.4 + 0.6 * energy)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(dot)
            size = 1.4 + twinkle * 1.2
            painter.drawEllipse(QRectF(x - size / 2, y - size / 2, size, size))
        painter.restore()

    def _paint_outer_ticks(self, painter: QPainter, cx: float, cy: float, radius: float, color: QColor) -> None:
        tick_radius = radius * 1.42
        painter.save()
        pen_color = QColor(color)
        for i in range(48):
            angle = math.radians(i * 7.5 + math.degrees(self._phase) * 0.15)
            major = i % 6 == 0
            alpha = 55 if major else 16
            pen_color.setAlpha(alpha)
            width = 1.6 if major else 1.0
            pen = QPen(pen_color)
            pen.setWidthF(width)
            painter.setPen(pen)
            x1 = cx + tick_radius * math.cos(angle)
            y1 = cy + tick_radius * math.sin(angle)
            length = 10 if major else 4
            x2 = cx + (tick_radius - length) * math.cos(angle)
            y2 = cy + (tick_radius - length) * math.sin(angle)
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))
        painter.restore()

    def _paint_rings(self, painter: QPainter, cx: float, cy: float, radius: float, color: QColor, state_colors: _StateColors, energy: float) -> None:
        painter.save()

        # Outer ring: a rotating conic gradient rather than a flat stroke —
        # gives the "sophisticated processing system" depth the brief asks
        # for without resorting to a gimmicky spinner. The rotation angle is
        # genuinely animated (from `self._phase`) so it's set fresh every
        # frame; only the gradient *object* is cached/reused.
        outer_radius = radius * 1.18
        conic = self._outer_conic_gradient
        conic.setCenter(cx, cy)
        conic.setAngle(math.degrees(self._phase) * 0.5)
        edge = state_colors.transparent
        bright = QColor(color)
        bright.setAlpha(int(70 + 90 * energy))
        conic.setColorAt(0.0, edge)
        conic.setColorAt(0.12, bright)
        conic.setColorAt(0.28, edge)
        conic.setColorAt(0.62, edge)
        conic.setColorAt(0.78, bright)
        conic.setColorAt(1.0, edge)
        pen = QPen(QColor(color))
        pen.setBrush(conic)
        pen.setWidthF(1.4)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(cx - outer_radius, cy - outer_radius, outer_radius * 2, outer_radius * 2))

        # Static reference ring — the "instrument bezel."
        bezel = QColor(color)
        bezel.setAlpha(int(28 + 14 * energy))
        painter.setPen(bezel)
        painter.drawEllipse(QRectF(cx - radius, cy - radius, radius * 2, radius * 2))

        mid_radius = radius * 0.74
        mid = QColor(color)
        mid.setAlpha(int(34 + 26 * energy))
        painter.setPen(mid)
        painter.drawEllipse(QRectF(cx - mid_radius, cy - mid_radius, mid_radius * 2, mid_radius * 2))

        inner_radius = radius * 0.52
        inner = QColor(color)
        inner.setAlpha(int(18 + 16 * energy))
        painter.setPen(inner)
        painter.drawEllipse(QRectF(cx - inner_radius, cy - inner_radius, inner_radius * 2, inner_radius * 2))

        # Scanning sweep arc — a bright segment traveling around the mid ring
        # while THINKING/EXECUTING/LISTENING; barely present otherwise.
        sweep = QColor(color)
        sweep.setAlpha(int(190 * energy))
        sweep_pen = QPen(sweep)
        sweep_pen.setWidthF(1.8)
        painter.setPen(sweep_pen)
        span_deg = 42
        start_deg = -math.degrees(self._phase) % 360
        rect = QRectF(cx - mid_radius, cy - mid_radius, mid_radius * 2, mid_radius * 2)
        painter.drawArc(rect, int(start_deg * 16), int(span_deg * 16))

        # A slower counter-sweep on the inner ring — only visible once energy
        # climbs, so it reads as "additional processing," not clutter.
        if energy > 0.4:
            counter = QColor(color)
            counter.setAlpha(int(140 * (energy - 0.4) / 0.6))
            counter_pen = QPen(counter)
            counter_pen.setWidthF(1.4)
            painter.setPen(counter_pen)
            counter_start = math.degrees(self._phase * 0.6) % 360
            inner_rect = QRectF(cx - inner_radius, cy - inner_radius, inner_radius * 2, inner_radius * 2)
            painter.drawArc(inner_rect, int(counter_start * 16), int(28 * 16))

        painter.restore()

    def _paint_activity_bars(self, painter: QPainter, cx: float, cy: float, radius: float, color: QColor, activity: float) -> None:
        painter.save()
        bar_radius = radius * 1.02
        count = 40
        for i in range(count):
            angle = math.radians(i * (360 / count))
            # Layered sine terms, time- and index-offset — a stylized "busy"
            # texture, not a rendering of any real signal (see module docstring).
            wobble = (
                math.sin(self._phase * 6 + i * 0.9) * 0.5
                + math.sin(self._phase * 11 + i * 1.7) * 0.3
                + 0.5
            )
            height = 2 + wobble * 8 * activity
            bar_color = QColor(color)
            bar_color.setAlpha(int(50 + 90 * wobble * activity))
            pen = QPen(bar_color)
            pen.setWidthF(1.3)
            painter.setPen(pen)
            x1 = cx + bar_radius * math.cos(angle)
            y1 = cy + bar_radius * math.sin(angle)
            x2 = cx + (bar_radius + height) * math.cos(angle)
            y2 = cy + (bar_radius + height) * math.sin(angle)
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))
        painter.restore()

    def _paint_core(self, painter: QPainter, cx: float, cy: float, radius: float, color: QColor, state_colors: _StateColors, energy: float, breath: float) -> None:
        pulse = 1.0 + 0.06 * math.sin(self._phase * 1.6) * (0.3 + energy) + 0.015 * breath
        core_radius = radius * 0.32 * pulse

        # All three radii below are animated (they move with `pulse` every
        # frame), so — unlike the ambient/outer-ring gradients — there's no
        # "only changes on resize" geometry to skip recomputing. What's
        # still cached is the `QRadialGradient` object itself (reused via
        # setCenter/setFocalPoint/setRadius) plus the always-transparent stop
        # color, which
        # avoids reallocating a gradient + its stop list on every frame.

        # Outer bloom — soft, wide falloff for depth.
        bloom_radius = core_radius * 2.6
        bloom = self._core_bloom_gradient
        bloom.setCenter(cx, cy)
        bloom.setFocalPoint(cx, cy)
        bloom.setRadius(bloom_radius)
        bloom_bright = QColor(color)
        bloom_bright.setAlpha(min(255, int(70 + 60 * energy)))
        bloom.setColorAt(0.0, bloom_bright)
        bloom.setColorAt(1.0, state_colors.transparent)
        painter.setBrush(bloom)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(cx - bloom_radius, cy - bloom_radius, bloom_radius * 2, bloom_radius * 2))

        # Mid gradient — the "energy field" proper.
        mid_gradient = self._core_mid_gradient
        mid_gradient.setCenter(cx, cy)
        mid_gradient.setFocalPoint(cx, cy)
        mid_gradient.setRadius(core_radius * 1.5)
        mid_bright = QColor(color)
        mid_bright.setAlpha(min(255, int(150 + 90 * energy)))
        mid_gradient.setColorAt(0.0, mid_bright)
        mid_gradient.setColorAt(1.0, state_colors.transparent)
        painter.setBrush(mid_gradient)
        painter.drawEllipse(QRectF(cx - core_radius * 1.5, cy - core_radius * 1.5, core_radius * 3, core_radius * 3))

        # Hot inner center, near-white at its heart for a sense of depth
        # rather than a flat colored disc.
        inner_gradient = self._core_inner_gradient
        inner_gradient.setCenter(cx, cy)
        inner_gradient.setFocalPoint(cx, cy)
        inner_gradient.setRadius(core_radius * 0.62)
        hot = QColor(self._hot_base)
        hot.setAlpha(min(255, int(210 + 45 * energy)))
        inner_gradient.setColorAt(0.0, hot)
        # `inner_edge`'s color and alpha are both fixed for this state — no
        # per-frame copy needed, unlike `hot`/`bloom_bright`/`mid_bright`
        # above whose alpha tracks `energy`.
        inner_gradient.setColorAt(1.0, state_colors.inner_edge)
        painter.setBrush(inner_gradient)
        painter.drawEllipse(QRectF(cx - core_radius * 0.62, cy - core_radius * 0.62, core_radius * 1.24, core_radius * 1.24))

        # A crisp thin edge right at the core boundary — an "instrument"
        # feel rather than a soft blob. Also fixed per state — reused as-is.
        painter.setPen(state_colors.core_edge_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(cx - core_radius * 0.62, cy - core_radius * 0.62, core_radius * 1.24, core_radius * 1.24))

    def _paint_orbitals(self, painter: QPainter, cx: float, cy: float, radius: float, color: QColor, state_colors: _StateColors, energy: float) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        for idx, (orbit_scale, speed, ecc, size, phase_off) in enumerate((
            (0.92, 1.3, 0.55, 3.4, 0.0),
            (1.08, -0.75, 0.62, 2.2, 2.1),
        )):
            orbit_radius = radius * orbit_scale
            angle = self._phase * speed + phase_off
            x = cx + orbit_radius * math.cos(angle)
            y = cy + orbit_radius * math.sin(angle) * ecc
            # `.lighter()` (an HSV round-trip) is precomputed per state in
            # `state_colors` — only the alpha still needs a per-frame copy.
            base = state_colors.orbital_bright if idx == 0 else state_colors.orbital_dim
            dot = QColor(base)
            dot.setAlpha(int(120 + 100 * energy))
            painter.setBrush(dot)
            painter.drawEllipse(QRectF(x - size / 2, y - size / 2, size, size))
