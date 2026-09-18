"""Scenarios: the stress tests, loaded from JSON, specified by markdown.

`plans/scenarios/96h-isolation-acute-care.md` was written at Phase 0, before
anything executed it, precisely so that it would be a specification rather
than a description of whatever got built. This module loads the JSON beside
it, and `questions_from_markdown` re-reads the markdown's "The plan must
answer" bullets so a test can assert that the two still say the same thing in
the same order. Change the markdown and the JSON must follow; change the JSON
alone and the suite goes red.

A scenario is four things:

* `injects` — what the facility experiences, in order, with the hour each
  begins. Hazard-agnostic on purpose: a hurricane, a riverine flood, an ice
  storm and a wildfire produce the same first four.
* `constants` — the numbers the injects imply, named, so a rule can cite
  `96h-isolation-acute-care/isolation_hours` rather than typing 96 into prose.
* `questions` — what a plan must answer, each bound to the rule that answers
  it and the facility field paths that rule reads.
* `fail_closed_on` — the field paths without which the scenario cannot be run
  at all. Missing them is a `cannot_run` finding naming the document that
  would supply them, never a region-level substitute.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
from typing import Any, Mapping

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SCENARIOS_DIR = REPO_ROOT / "plans" / "scenarios"

#: The scenario every Phase 3 command defaults to.
DEFAULT_SCENARIO = "96h-isolation-acute-care"

#: The markdown heading that introduces the questions, verbatim.
QUESTIONS_HEADING = "**The plan must answer, with evidence:**"

_STATUSES = ("answered", "unanswered", "failed", "cannot_run")


class ScenarioError(ValueError):
    """A scenario file we will not load."""


@dataclasses.dataclass(frozen=True)
class Inject:
    """One thing that happens to the facility, at one hour of the scenario."""

    id: str
    hour: int
    kind: str
    params: dict = dataclasses.field(default_factory=dict)
    #: The inject as the markdown words it, for a reader of `scenarios list`.
    text: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "hour": self.hour, "kind": self.kind,
                "params": dict(self.params), "text": self.text}


@dataclasses.dataclass(frozen=True)
class Question:
    """One question a plan must answer, and the rule that answers it here."""

    id: str
    text: str
    rule: str
    #: Facility field paths the rule reads. Informational for a reader and
    #: checked by a test, so a rule cannot quietly grow a new input.
    requires: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "rule": self.rule,
                "requires": list(self.requires)}


@dataclasses.dataclass(frozen=True)
class Scenario:
    """A stress test: what happens, what it asks, and when it refuses to run."""

    id: str
    title: str
    injects: tuple[Inject, ...]
    questions: tuple[Question, ...]
    fail_closed_on: tuple[str, ...]
    source_doc: str
    constants: dict[str, float] = dataclasses.field(default_factory=dict)

    # -- lookups -----------------------------------------------------------

    def question(self, question_id: str) -> Question:
        for question in self.questions:
            if question.id == question_id:
                return question
        raise ScenarioError(f"scenario {self.id}: no question {question_id!r}")

    def question_for_rule(self, rule: str) -> Question:
        for question in self.questions:
            if question.rule == rule:
                return question
        raise ScenarioError(f"scenario {self.id}: no question uses rule {rule!r}")

    def inject(self, inject_id: str) -> Inject:
        for inject in self.injects:
            if inject.id == inject_id:
                return inject
        raise ScenarioError(f"scenario {self.id}: no inject {inject_id!r}")

    def constant(self, name: str) -> float:
        if name not in self.constants:
            raise ScenarioError(f"scenario {self.id}: no constant {name!r}")
        return self.constants[name]

    def ref(self, name: str) -> str:
        """How a sentence cites one of this scenario's numbers: `<id>/<name>`."""
        return f"{self.id}/{name}"

    def refs(self) -> set[str]:
        """Every `scenario` citation this scenario licenses.

        Its constants, its injects and its questions by id — and the bare
        scenario id, which is how a report cites the scenario itself.
        """
        names = (
            set(self.constants)
            | {i.id for i in self.injects}
            | {q.id for q in self.questions}
        )
        return {self.id} | {self.ref(name) for name in names}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "source_doc": self.source_doc,
            "constants": dict(self.constants),
            "injects": [i.to_dict() for i in self.injects],
            "questions": [q.to_dict() for q in self.questions],
            "fail_closed_on": list(self.fail_closed_on),
        }

    # -- loading -----------------------------------------------------------

    @classmethod
    def from_json(cls, raw: Mapping, *, where: str = "<scenario>") -> "Scenario":
        for key in ("id", "title", "source_doc", "constants", "injects",
                    "questions", "fail_closed_on"):
            if key not in raw:
                raise ScenarioError(f"{where}: missing {key!r}")
        constants = {}
        for name, value in raw["constants"].items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ScenarioError(f"{where}: constant {name!r} must be a number")
            constants[str(name)] = value
        injects = tuple(
            Inject(i["id"], int(i["hour"]), i["kind"], dict(i.get("params", {})),
                   str(i.get("text", "")))
            for i in raw["injects"]
        )
        questions = tuple(
            Question(q["id"], q["text"], q["rule"], tuple(q.get("requires", ())))
            for q in raw["questions"]
        )
        if not questions:
            raise ScenarioError(f"{where}: a scenario with no question tests nothing")
        seen: set[str] = set()
        for question in questions:
            if question.id in seen:
                raise ScenarioError(f"{where}: duplicate question id {question.id!r}")
            seen.add(question.id)
        return cls(
            id=raw["id"], title=raw["title"], injects=injects, questions=questions,
            fail_closed_on=tuple(raw["fail_closed_on"]), source_doc=raw["source_doc"],
            constants=constants,
        )

    @classmethod
    def from_path(cls, path: pathlib.Path | str) -> "Scenario":
        path = pathlib.Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ScenarioError(f"{path}: cannot be read ({exc})") from None
        except ValueError as exc:
            raise ScenarioError(f"{path}: not valid JSON ({exc})") from None
        return cls.from_json(raw, where=str(path))


# --------------------------------------------------------------------------- #
# The library on disk
# --------------------------------------------------------------------------- #


def scenarios_dir(directory: pathlib.Path | None = None) -> pathlib.Path:
    return pathlib.Path(directory) if directory is not None else SCENARIOS_DIR


def load(
    scenario_id: str = DEFAULT_SCENARIO, directory: pathlib.Path | None = None
) -> Scenario:
    path = scenarios_dir(directory) / f"{scenario_id}.json"
    if not path.exists():
        known = ", ".join(sorted(s.id for s in load_all(directory))) or "none"
        raise ScenarioError(
            f"no scenario {scenario_id!r} in {scenarios_dir(directory)} (known: {known})"
        )
    return Scenario.from_path(path)


def load_all(directory: pathlib.Path | None = None) -> list[Scenario]:
    """Every scenario in the library, by id."""
    root = scenarios_dir(directory)
    return [Scenario.from_path(p) for p in sorted(root.glob("*.json"))] if root.exists() else []


def markdown_path(scenario: Scenario, directory: pathlib.Path | None = None) -> pathlib.Path:
    """The specification beside the JSON: `<id>.md` in the same directory."""
    return scenarios_dir(directory) / f"{scenario.id}.md"


_BULLET = re.compile(r"^-\s+(.*)$")


def strip_emphasis(text: str) -> str:
    """Markdown bold and italic markers removed, whitespace collapsed."""
    return " ".join(text.replace("**", "").replace("*", "").split())


def questions_from_markdown(path: pathlib.Path | str) -> list[str]:
    """The "The plan must answer" bullets of a scenario markdown, in order.

    A bullet may wrap over several lines; a continuation line is indented. The
    list ends at the first blank-line-then-non-bullet, which in the
    specification is the "**Pass condition.**" paragraph.
    """
    lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, line in enumerate(lines)
                     if line.strip() == QUESTIONS_HEADING)
    except StopIteration:
        raise ScenarioError(
            f"{path}: no {QUESTIONS_HEADING!r} heading, so the questions cannot be read"
        ) from None
    bullets: list[str] = []
    for line in lines[start + 1:]:
        match = _BULLET.match(line.strip()) if line.strip().startswith("- ") else None
        if match:
            bullets.append(match.group(1).strip())
        elif line.strip() and bullets:
            if line.startswith((" ", "\t")):
                bullets[-1] += " " + line.strip()
            else:
                break
        elif line.strip() and not bullets:
            break
    return [strip_emphasis(b) for b in bullets]


def statuses() -> tuple[str, ...]:
    """The finding statuses a case study's `expected_findings` may name."""
    return _STATUSES


def summarise(scenario: Scenario) -> str:
    return f"{scenario.id:<28} {len(scenario.questions):>2} question(s)  {scenario.title}"


def as_known(scenarios: list[Scenario]) -> set[str]:
    """Every `scenario` citation the library licenses, for a `cite` resolver."""
    refs: set[str] = set()
    for scenario in scenarios:
        refs |= scenario.refs()
    return refs


def check_shape(scenario: Scenario, rules: Mapping[str, Any]) -> list[str]:
    """Problems that would make a scenario unrunnable, as plain sentences."""
    problems = []
    for question in scenario.questions:
        if question.rule not in rules:
            problems.append(
                f"{scenario.id}: question {question.id} names rule {question.rule!r}, "
                f"which is not in RULES"
            )
    return problems


__all__ = [
    "DEFAULT_SCENARIO",
    "QUESTIONS_HEADING",
    "SCENARIOS_DIR",
    "Inject",
    "Question",
    "Scenario",
    "ScenarioError",
    "as_known",
    "check_shape",
    "load",
    "load_all",
    "markdown_path",
    "questions_from_markdown",
    "scenarios_dir",
    "statuses",
    "strip_emphasis",
    "summarise",
]
