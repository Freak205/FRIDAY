"""Offscreen paint-cost benchmark for `friday.gui.core_widget.CoreWidget`.

Measures raw QPainter cost per `CoreState` by rendering the real widget
straight onto a 520x520 `QImage`, bypassing the Qt event loop / window
system entirely (`QT_QPA_PLATFORM=offscreen`, no `app.exec()`). This is a
paint-cost measurement, not a wall-clock FPS measurement — it answers "how
expensive is one call to paintEvent," independent of the QTimer interval
that decides how often that call happens.

Reports, per `CoreState`:
  - mean/median ms per full `paintEvent` (`widget.render()`)
  - estimated CPU-core percentage that state would cost continuously at its
    *live* QTimer interval (paint ms / interval ms)

And, for one representative "busy" state (LISTENING, which exercises every
layer including the activity bars), a per-layer breakdown by calling each
`_paint_*` method directly with the same args `paintEvent` would use.

Usage: `python scripts/benchmark_core_widget.py`
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from friday.gui import core_widget as cw  # noqa: E402
from friday.gui.core_widget import CoreWidget  # noqa: E402
from friday.gui.state_hub import CoreState  # noqa: E402

SIZE = 520
WARMUP = 20
ITERS = 200
LAYER_ITERS = 300


def _prep(widget: CoreWidget, state: CoreState) -> None:
    widget.resize(SIZE, SIZE)
    widget.set_core_state(state.value)
    # Push phase/energy off their t=0 rest values so gradients/arcs aren't
    # measuring a degenerate all-zero case.
    widget._phase = 1.7
    widget._energy = widget._target_energy


def bench_state(widget: CoreWidget, image: QImage, state: CoreState) -> tuple[float, float]:
    _prep(widget, state)

    for _ in range(WARMUP):
        widget.render(image)

    times: list[float] = []
    for _ in range(ITERS):
        t0 = time.perf_counter()
        widget.render(image)
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.mean(times), statistics.median(times)


def _layer_calls(widget: CoreWidget, state: CoreState) -> dict:
    """Build the same (name -> zero-arg callable) map paintEvent's own
    call sites would produce for `state`, so layer timings reflect real
    per-frame arguments rather than synthetic ones. Only depends on public
    attributes / the same private helpers paintEvent itself uses, so it
    keeps working whether or not CoreWidget caches paint resources."""
    _prep(widget, state)
    w = h = SIZE
    cx = cy = SIZE / 2.0
    radius = min(w, h) * 0.32
    safe_radius = min(w, h) / 2.0 * 0.94
    _, _, _, bar_activity = cw._STATE_PROFILE[widget._state]
    energy = widget._energy
    breath = 0.5

    if hasattr(widget, "_colors_for"):
        state_colors = widget._colors_for(widget._state)
        color = state_colors.base
        extra = (state_colors,)
    else:
        from PySide6.QtGui import QColor

        _, color_hex, _, _ = cw._STATE_PROFILE[widget._state]
        color = QColor(color_hex)
        extra = ()

    return {
        "_paint_ambient_field": lambda p: widget._paint_ambient_field(p, cx, cy, safe_radius, color, *extra, energy),
        "_paint_particles": lambda p: widget._paint_particles(p, cx, cy, radius, color, energy, safe_radius),
        "_paint_outer_ticks": lambda p: widget._paint_outer_ticks(p, cx, cy, radius, color),
        "_paint_rings": lambda p: widget._paint_rings(p, cx, cy, radius, color, *extra, energy),
        "_paint_activity_bars": lambda p: widget._paint_activity_bars(p, cx, cy, radius, color, bar_activity),
        "_paint_core": lambda p: widget._paint_core(p, cx, cy, radius, color, *extra, energy, breath),
        "_paint_orbitals": lambda p: widget._paint_orbitals(p, cx, cy, radius, color, *extra, energy),
    }


def bench_layers(widget: CoreWidget, image: QImage, state: CoreState) -> dict[str, float]:
    calls = _layer_calls(widget, state)
    results: dict[str, float] = {}
    for name, fn in calls.items():
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for _ in range(20):
            fn(painter)
        painter.end()

        times: list[float] = []
        for _ in range(LAYER_ITERS):
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            t0 = time.perf_counter()
            fn(painter)
            times.append((time.perf_counter() - t0) * 1000)
            painter.end()
        results[name] = statistics.mean(times)
    return results


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    widget = CoreWidget()
    image = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)

    # Fall back to the pre-optimization hardcoded values (20ms/60ms) so this
    # same script can be run against either the baseline or optimized code.
    active_ms = getattr(cw, "ACTIVE_FRAME_INTERVAL_MS", 20)
    inactive_ms = getattr(cw, "INACTIVE_FRAME_INTERVAL_MS", 60)

    print(f"\nCoreWidget paint benchmark — offscreen {SIZE}x{SIZE} QImage, {ITERS} iters/state\n")
    print(f"{'state':<16} {'mean ms/frame':>14} {'median ms/frame':>16} "
          f"{'timer ms':>10} {'est. CPU %':>11}")

    per_state = {}
    for state in CoreState:
        mean_ms, median_ms = bench_state(widget, image, state)
        interval_ms = active_ms if state in cw._ACTIVE_STATES else inactive_ms
        cpu_pct = 100.0 * mean_ms / interval_ms
        per_state[state] = (mean_ms, median_ms, interval_ms, cpu_pct)
        print(f"{state.value:<16} {mean_ms:>14.3f} {median_ms:>16.3f} {interval_ms:>10d} {cpu_pct:>10.1f}%")

    print(f"\nactive timer interval:   {active_ms} ms (~{1000 / active_ms:.1f} FPS)")
    print(f"inactive timer interval: {inactive_ms} ms (~{1000 / inactive_ms:.1f} FPS)")

    print(f"\n--- layer breakdown: LISTENING ({LAYER_ITERS} iters/layer) ---\n")
    layers = bench_layers(widget, image, CoreState.LISTENING)
    total = sum(layers.values())
    for name, ms in sorted(layers.items(), key=lambda kv: -kv[1]):
        pct = 100.0 * ms / total if total else 0.0
        print(f"  {name:<22} {ms:>8.3f} ms  ({pct:5.1f}%)")
    print(f"  {'sum of layers':<22} {total:>8.3f} ms")
    print()


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)
