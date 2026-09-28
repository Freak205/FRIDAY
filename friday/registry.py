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
import importlib
import inspect
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, get_args, get_origin, get_type_hints

from friday.log import get

log = get(__name__)

Tier = Literal["L0", "L1", "L2", "L3"]


@dataclass(slots=True)
class SkillResult:
    """What a skill hands back. `speech` is what FRIDAY says out loud."""

    speech: str
    data: dict[str, Any] = field(default_factory=dict)
    ok: bool = True

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

    async def __call__(self, **kwargs: Any) -> SkillResult:
        known = {p.name for p in self.params}
        unknown = sorted(set(kwargs) - known)
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
            value = await asyncio.to_thread(lambda: self.fn(**kwargs))
        return SkillResult.coerce(value)

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
                log.exception("failed to load skill module %s", mod.name)
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
            )
        )
        return fn

    return wrap
