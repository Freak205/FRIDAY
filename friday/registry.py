"""Skill registry.

A skill is a plain Python function wearing an `@skill` decorator. The decorator
records everything the rest of FRIDAY needs:

  * `name`        dotted id, e.g. `system.volume.up`
  * `tier`        permission tier — see friday.permissions
  * `examples`    phrasings used by the brain's embedding matcher
  * parameters    derived from the signature + type hints (slot filling)
  * `dry_run`     optional preview function for L2/L3 confirmation
  * `undo`        optional inverse operation for the undo journal

Skills never check permissions themselves. The executor does that, so a skill
can be tested in isolation and can't accidentally bypass the guard.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import importlib
import inspect
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, get_args, get_origin, get_type_hints

from friday.log import get, log_exception

log = get(__name__)

Tier = Literal["L0", "L1", "L2", "L3"]


class Detach:
    """Phase 53: lets the caller of a worker-thread skill learn what became of the thread after the caller stopped waiting.

    A plain-function skill runs in a thread Python cannot stop. If the await is cancelled (a step timeout, a cancelled run) the
    thread carries on and its side effect may still happen. The Executor puts one of these in `DETACH` before calling the skill;
    `Skill.__call__` sets `started` when the thread really begins the work, and -- if the await is cancelled after that -- hands
    `on_done` the finished future, whenever it finishes, so the real outcome can still be recorded. A thread that never
    started (cancelled while queued) did nothing and leaves `started` False."""

    def __init__(self, on_done: Callable[[concurrent.futures.Future], None]) -> None:
        self.on_done = on_done
        self.started = False
        # Phase 96: the cancel reached the future while it was still queued, so the worker never took it and nothing ran.
        self.never_ran = False


DETACH: contextvars.ContextVar[Detach | None] = contextvars.ContextVar("DETACH", default=None)


@dataclass(slots=True)
class SkillResult:
    """What a skill hands back. `speech` is what FRIDAY says out loud."""

    speech: str
    data: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    # Phase 32.0: the inverse of THIS call, when it depends on what actually happened (which
    # snapshot of the overwritten file was kept). Same shape as the journal's inverse:
    # {"skill": ..., "args": {...}} or {"steps": [that, ...]}. The Executor records it only for a
    # call that succeeded; it takes precedence over the static `undo=` of the skill.
    undo: dict[str, Any] | None = None

    @staticmethod
    def coerce(value: Any) -> "SkillResult":
        if isinstance(value, SkillResult):
            return value
        if value is None:
            return SkillResult(speech="Done.")
        if isinstance(value, str):
            return SkillResult(speech=value)
        if isinstance(value, dict):
            return SkillResult(speech=value.get("speech", "Done."), data=value)
        return SkillResult(speech=str(value))


@dataclass(slots=True)
class Param:
    name: str
    type: type
    required: bool
    default: Any = None
    description: str = ""


@dataclass(slots=True)
class Skill:
    name: str
    tier: Tier
    description: str
    examples: list[str]
    fn: Callable[..., Any]
    params: list[Param]
    is_async: bool
    dry_run: Callable[..., str] | None = None
    undo: Callable[..., Any] | None = None
    # Optional per-call risk classifier: given the actual call args, returns
    # True when this specific invocation looks consequential enough to need
    # confirmation even though the skill's own tier is more permissive (e.g.
    # browser.click is L1 in general, but a click targeting "Send" isn't).
    # See friday.permissions.evaluate, which escalates the effective tier to
    # L3 for the policy decision only — the skill's declared tier is unchanged.
    risk: Callable[..., bool] | None = None
    # Phase 20.0: what KIND of thing this skill does, as an
    # `friday.intent.ActionClass` value ("read", "open", "modify", ...) or an
    # `friday.intent.ActionRule` for a tool whose class depends on its arguments
    # (a click on "Back" vs "Delete"). Distinct from `tier`, which is about RISK:
    # the tier decides whether a human must confirm, the action class decides
    # whether the call fits what the user asked for at all. None on an L0 skill
    # means read/observe (see friday.intent._default_class).
    action: Any = None
    # Phase 22.0: names of arguments that only change HOW MUCH of the same thing a
    # read returns (a truncation cap like `max_chars`), never WHAT it reads. The
    # repeat guard ignores them — but only for a tool that also declares a page
    # cursor (`offset`/`page`), so ignoring the cap can never hide content the
    # planner has no other way to reach. Empty for every state-changing tool.
    presentational: tuple[str, ...] = ()
    # Phase 32.0: a skill that exists only to be run by the undo journal (files.restore). It is
    # still an ordinary registered skill -- the Executor runs it under the same policy -- but it
    # is not offered to the planner, the matcher, or the skill listing as something to ask for.
    internal: bool = False

    def unknown_args(self, kwargs: dict[str, Any]) -> list[str]:
        """The argument names this skill does not take. A call with any is refused by `__call__` before the skill runs; the
        Executor asks the same question to know that such a refusal never started anything (Phase 96)."""
        known = {p.name for p in self.params}
        return sorted(set(kwargs) - known)

    async def __call__(self, **kwargs: Any) -> SkillResult:
        unknown = self.unknown_args(kwargs)
        if unknown:
            valid = ", ".join(p.name for p in self.params) or "none"
            return SkillResult(
                speech=(
                    f"'{self.name}' doesn't take argument(s) {', '.join(unknown)}. "
                    f"Valid arguments: {valid}."
                ),
                ok=False,
            )
        if self.is_async:
            value = await self.fn(**kwargs)
        else:
            # Skills are mostly blocking Win32 calls; keep the loop responsive.
            value = await self._in_thread(kwargs)
        return SkillResult.coerce(value)

    async def _in_thread(self, kwargs: dict[str, Any]) -> Any:
        """`asyncio.to_thread`, except that a cancelled await can still be told how the thread ended (see `Detach`)."""
        loop = asyncio.get_running_loop()
        cf: concurrent.futures.Future = concurrent.futures.Future()
        ctx = contextvars.copy_context()
        detach = DETACH.get()

        def work() -> None:
            if not cf.set_running_or_notify_cancel():  # cancelled while queued: nothing ran
                return
            if detach is not None:
                detach.started = True
            try:
                cf.set_result(ctx.run(lambda: self.fn(**kwargs)))
            except BaseException as exc:  # noqa: BLE001 -- handed to the awaiting side, as to_thread does
                cf.set_exception(exc)

        loop.run_in_executor(None, work)
        try:
            return await asyncio.wrap_future(cf, loop=loop)
        except asyncio.CancelledError:
            if detach is not None:
                # Phase 96: whether the worker took the job is read from the future itself, not from the flag the worker sets a
                # moment after taking it -- a cancel landing in that gap left `started` False for a thread that then ran, and its
                # outcome was never recorded. A future the cancel really cancelled was never run; any other one is running or done.
                if cf.cancelled():
                    detach.never_ran = True
                else:
                    detach.started = True
                    cf.add_done_callback(detach.on_done)  # runs at once if it has already finished
            raise

    def preview(self, **kwargs: Any) -> str:
        """Human-readable description of what calling this would do."""
        if self.dry_run:
            return self.dry_run(**kwargs)
        args = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
        return f"{self.name}({args})"


class Registry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def add(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise ValueError(f"duplicate skill name: {skill.name}")
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def __len__(self) -> int:
        return len(self._skills)

    def discover(self, package: str = "friday.skills") -> int:
        """Import every module under `package` so its decorators run."""
        before = len(self._skills)
        pkg = importlib.import_module(package)
        for mod in pkgutil.iter_modules(pkg.__path__):
            if mod.name.startswith("_"):
                continue
            try:
                importlib.import_module(f"{package}.{mod.name}")
            except Exception:
                log_exception(log, "failed to load skill module %s", mod.name)
        added = len(self._skills) - before
        log.info("registry: %d skills loaded (%d modules)", len(self._skills), added)
        return added


REGISTRY = Registry()


def _describe(annotation: Any) -> tuple[type, str]:
    """Unwrap Annotated[int, "docs"] into (int, "docs")."""
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        base = args[0]
        note = next((a for a in args[1:] if isinstance(a, str)), "")
        return base, note
    return annotation, ""


def _params_of(fn: Callable[..., Any]) -> list[Param]:
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        hints = {}

    params: list[Param] = []
    for pname, p in sig.parameters.items():
        if pname in ("self", "cls") or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        ptype, note = _describe(hints.get(pname, str))
        params.append(
            Param(
                name=pname,
                type=ptype,
                required=p.default is inspect.Parameter.empty,
                default=None if p.default is inspect.Parameter.empty else p.default,
                description=note,
            )
        )
    return params


def skill(
    *,
    name: str,
    tier: Tier,
    description: str,
    examples: list[str] | None = None,
    dry_run: Callable[..., str] | None = None,
    undo: Callable[..., Any] | None = None,
    risk: Callable[..., bool] | None = None,
    action: Any = None,
    presentational: tuple[str, ...] = (),
    internal: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a function as a skill. Returns the function unchanged."""

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        REGISTRY.add(
            Skill(
                name=name,
                tier=tier,
                description=description,
                examples=examples or [],
                fn=fn,
                params=_params_of(fn),
                is_async=inspect.iscoroutinefunction(fn),
                dry_run=dry_run,
                undo=undo,
                risk=risk,
                action=action,
                presentational=tuple(presentational),
                internal=internal,
            )
        )
        return fn

    return wrap
