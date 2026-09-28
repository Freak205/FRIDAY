"""FRIDAY's cinematic desktop interface — a PySide6 presentation layer on
top of the existing brain/voice/permission/orchestrator backend.

Nothing under `friday.gui` owns any decision-making state: it subscribes to
`friday.bus.BUS`, polls read-only snapshots (`friday.intelligence.self_state.
SELF_STATE`, `friday.intelligence.state.INTEL`, `friday.desktop_observer.
observe`), and calls into `friday.session.SESSION` exactly the way
`friday/desktop.py` (Phase 0-9's Tkinter client, superseded by this package)
always did.
"""
