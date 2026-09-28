"""Deterministic, offscreen tests for the `CoreWidget` rendering-performance
optimization (paint-resource caching + the 50 FPS -> 30 FPS active timer).

Companion to `scripts/smoke_gui.py` (which covers the wider GUI wiring) and
`scripts/benchmark_core_widget.py` (which measures actual paint cost) — this
script only asserts the specific behaviors that optimization must not break:

  - the active/inactive QTimer intervals and how state transitions pick
    between them
  - that the cached paint resources (_StateColors, the reusable QGradient
    objects) are genuinely reused rather than rebuilt every frame
  - that a resize updates those cached gradients' geometry rather than
    leaving them stale
  - that the animation is elapsed-time-based, so a lower frame rate changes
    how often motion is sampled, never how fast it moves

Forces `QT_QPA_PLATFORM=offscreen` before any Qt import, same convention as
scripts/smoke_gui.py.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from friday.gui import core_widget as cw  # noqa: E402
from friday.gui.core_widget import CoreWidget, _ACTIVE_STATES  # noqa: E402
from friday.gui.state_hub import CoreState  # noqa: E402


def main() -> bool:
    overall = True

    def check(label: str, cond: bool) -> None:
        nonlocal overall
        print(f"  {'OK  ' if cond else 'MISS'} {label}")
        overall &= cond

    QApplication.instance() or QApplication(sys.argv)

    # -- timer intervals: named constants, not magic numbers -----------------

    print("\n--- active/inactive frame-interval constants ---\n")
    check("active interval is ~30 FPS (33ms)", cw.ACTIVE_FRAME_INTERVAL_MS == 33)
    check("inactive interval unchanged (60ms)", cw.INACTIVE_FRAME_INTERVAL_MS == 60)
    check(
        "every CoreState is classified as active or inactive",
        set(CoreState) - _ACTIVE_STATES == {CoreState.BOOTING, CoreState.IDLE},
    )

    # -- state transitions select the correct timer interval -----------------

    print("\n--- state transitions select the correct timer ---\n")
    widget = CoreWidget()
    check(
        "constructed widget starts on the inactive interval (BOOTING)",
        widget._timer.interval() == cw.INACTIVE_FRAME_INTERVAL_MS,
    )

    for state in CoreState:
        widget.set_core_state(state.value)
        expected = cw.ACTIVE_FRAME_INTERVAL_MS if state in _ACTIVE_STATES else cw.INACTIVE_FRAME_INTERVAL_MS
        check(f"{state.value:<16} -> timer interval {expected}ms", widget._timer.interval() == expected)

    print()
    widget.set_core_state(CoreState.LISTENING.value)
    check("IDLE -> LISTENING switches to the active interval", widget._timer.interval() == cw.ACTIVE_FRAME_INTERVAL_MS)
    widget.set_core_state(CoreState.IDLE.value)
    check("LISTENING -> IDLE switches back to the inactive interval", widget._timer.interval() == cw.INACTIVE_FRAME_INTERVAL_MS)

    # -- cached resources are reused, not reconstructed -----------------------

    print("\n--- cached paint resources are reused across frames/states ---\n")
    widget2 = CoreWidget()
    widget2.resize(400, 400)

    colors_a = widget2._colors_for(CoreState.LISTENING)
    colors_b = widget2._colors_for(CoreState.LISTENING)
    check("_colors_for returns the SAME object for the same state (cached)", colors_a is colors_b)

    colors_idle = widget2._colors_for(CoreState.IDLE)
    check("_colors_for returns a DIFFERENT object for a different state", colors_a is not colors_idle)

    check(
        "cached transparent color is genuinely transparent",
        colors_a.transparent.alpha() == 0,
    )
    check(
        "cached inner_edge/core_edge_pen carry the fixed alphas the original code used",
        colors_a.inner_edge.alpha() == 230 and colors_a.core_edge_pen.alpha() == 160,
    )

    gradient_id_before = id(widget2._ambient_gradient)
    conic_id_before = id(widget2._outer_conic_gradient)
    bloom_id_before = id(widget2._core_bloom_gradient)

    image = QImage(400, 400, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    for _ in range(5):
        widget2.render(image)

    check(
        "ambient-field QRadialGradient object identity unchanged across repeated paints",
        id(widget2._ambient_gradient) == gradient_id_before,
    )
    check(
        "outer-ring QConicalGradient object identity unchanged across repeated paints",
        id(widget2._outer_conic_gradient) == conic_id_before,
    )
    check(
        "core-bloom QRadialGradient object identity unchanged across repeated paints",
        id(widget2._core_bloom_gradient) == bloom_id_before,
    )

    # -- resize updates (not leaves stale) the cached gradients' geometry ----

    print("\n--- resize keeps cached gradient geometry in sync ---\n")
    widget3 = CoreWidget()
    widget3.set_core_state(CoreState.LISTENING.value)
    widget3.resize(300, 300)
    image3 = QImage(600, 600, QImage.Format.Format_ARGB32_Premultiplied)
    image3.fill(0)
    widget3.render(image3)
    center_before = widget3._ambient_gradient.center()
    radius_before = widget3._ambient_gradient.radius()

    widget3.resize(520, 520)
    widget3.render(image3)
    center_after = widget3._ambient_gradient.center()
    radius_after = widget3._ambient_gradient.radius()

    check(
        "ambient gradient center moved to match the new widget size after resize",
        (center_before.x(), center_before.y()) != (center_after.x(), center_after.y())
        and (center_after.x(), center_after.y()) == (260.0, 260.0),
    )
    check(
        "ambient gradient radius grew to match the new (larger) widget after resize",
        radius_after > radius_before,
    )

    conic_center_after = widget3._outer_conic_gradient.center()
    check(
        "outer-ring conic gradient center also matches the new geometry after resize",
        (conic_center_after.x(), conic_center_after.y()) == (260.0, 260.0),
    )

    # A reused QRadialGradient's focalPoint does NOT automatically follow
    # setCenter() (unlike the 3-arg constructor, which sets both) — a real
    # bug this optimization introduced and then fixed (skewed/off-center
    # glow after the first resize). Guard against regressing that fix.
    for grad_name in ("_ambient_gradient", "_core_bloom_gradient", "_core_mid_gradient", "_core_inner_gradient"):
        gradient = getattr(widget3, grad_name)
        center = gradient.center()
        focal = gradient.focalPoint()
        check(
            f"{grad_name}.focalPoint() matches its center() after resize (no off-center glow)",
            (round(center.x(), 3), round(center.y(), 3)) == (round(focal.x(), 3), round(focal.y(), 3)),
        )

    # -- animation is elapsed-time-based, not frame-count-based --------------

    print("\n--- animation advances by elapsed time, not tick count ---\n")
    import friday.gui.core_widget as core_widget_module

    real_monotonic = core_widget_module.time.monotonic

    def run_ticks(n_ticks: int, total_elapsed_s: float, state: CoreState) -> float:
        """Drive `n_ticks` calls to `_tick()` spread evenly across
        `total_elapsed_s` of *simulated* wall-clock time, and return how far
        `_phase` advanced. If `_tick` is time-based this should come out
        (almost) the same regardless of how many ticks were used to cover
        that same total elapsed time — exactly what changing the timer
        interval from 20ms to 33ms relies on for correctness."""
        w = CoreWidget()
        w.set_core_state(state.value)
        w._phase = 0.0
        fake_now = [1000.0]
        core_widget_module.time.monotonic = lambda: fake_now[0]
        w._last_tick = fake_now[0]
        try:
            dt = total_elapsed_s / n_ticks
            for _ in range(n_ticks):
                fake_now[0] += dt
                w._tick()
        finally:
            core_widget_module.time.monotonic = real_monotonic
        return w._phase

    phase_50fps = run_ticks(n_ticks=50, total_elapsed_s=1.0, state=CoreState.LISTENING)  # ~20ms ticks
    phase_30fps = run_ticks(n_ticks=30, total_elapsed_s=1.0, state=CoreState.LISTENING)  # ~33ms ticks
    phase_10fps = run_ticks(n_ticks=10, total_elapsed_s=1.0, state=CoreState.LISTENING)  # coarse, still 1.0s total

    rel_diff_30 = abs(phase_30fps - phase_50fps) / phase_50fps
    rel_diff_10 = abs(phase_10fps - phase_50fps) / phase_50fps
    print(f"  phase after 1.0s simulated:  50 ticks/s={phase_50fps:.4f}  "
          f"30 ticks/s={phase_30fps:.4f}  10 ticks/s={phase_10fps:.4f}")
    check(
        "phase advance over the SAME elapsed time barely differs between 50 and 30 ticks/s (<1%)",
        rel_diff_30 < 0.01,
    )
    check(
        "even a much coarser 10 ticks/s still lands close to the same total phase advance (<5%)",
        rel_diff_10 < 0.05,
    )

    # A direct dt*speed check: phase advance is proportional to elapsed time,
    # not to how many _tick() calls happened.
    _, _, sweep_speed, _ = cw._STATE_PROFILE[CoreState.LISTENING]
    check(
        "phase advance over 1.0s matches dt*sweep_speed (time-based), not tick count",
        abs(phase_50fps - sweep_speed) / sweep_speed < 0.01,
    )

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    return overall


if __name__ == "__main__":
    _ok = main()
    sys.stdout.flush()
    os._exit(0 if _ok else 1)
