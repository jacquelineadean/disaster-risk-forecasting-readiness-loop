"""Review records: a rating bound to the sha256 of one blinded report.

Plan §4 is explicit that the Phase 3 exit is not mechanisable: nothing in this
repository can establish that a facility is real or that a reviewer is a
practising emergency manager. What *is* mechanisable is that a rating cannot
float free of the thing it rates. So a review record carries the sha256 of a
blinded rendering, `record()` recomputes that sha from the file it is handed
and refuses a mismatch, and `verify --phase 3` re-finds a blinded report with
exactly that sha before it counts the review. Change one number in a report
and every review of it stops counting, which is the property that makes
"blinded review by practising emergency managers" worth asserting at all.

The record is an attestation. The role and the years are what the reviewer
says they are; the blinding, the sha and the closed rating vocabulary are what
this module can actually enforce.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import os
import pathlib
from typing import Iterable, Mapping

from readiness.plans import gap_report as gap_report_mod

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
REVIEWS_DIR = REPO_ROOT / "plans" / "reviews"
REVIEWS_DIR_ENV = "READINESS_REVIEWS_DIR"

#: The closed vocabulary. A free-text rating could not be counted, and a scale
#: without a "not useful" rung would not be a review.
RATINGS: tuple[str, ...] = ("not useful", "somewhat useful", "useful", "very useful")

#: The ratings the exit criterion means by "useful or better".
USEFUL_OR_BETTER: frozenset[str] = frozenset({"useful", "very useful"})

#: The organisation types a reviewer may record.
ORG_TYPES: tuple[str, ...] = ("hospital", "county", "state", "ngo", "other")

#: What the exit criterion needs to see in a reviewer's own description of
#: their role. Checked case-insensitively, as a substring, and nothing more:
#: the attestation is theirs, not ours.
REQUIRED_ROLE = "emergency manager"


class ReviewError(ValueError):
    """A review we will not record, or a record we will not read."""


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def reviews_root(reviews_dir: pathlib.Path | None = None) -> pathlib.Path:
    if reviews_dir is not None:
        return pathlib.Path(reviews_dir)
    override = os.environ.get(REVIEWS_DIR_ENV)
    return pathlib.Path(override) if override else REVIEWS_DIR


@dataclasses.dataclass(frozen=True)
class Review:
    """One rating, bound to one blinded rendering of one report."""

    report_sha256: str
    facility_hash: str
    period: str
    reviewer_role: str
    organisation_type: str
    years_in_role: int
    rating: str
    blinded: bool = True
    comments: str = ""
    recorded_at: str = ""

    def __post_init__(self) -> None:
        if self.rating not in RATINGS:
            raise ReviewError(
                f"rating {self.rating!r} is not one of {list(RATINGS)}"
            )
        if self.organisation_type not in ORG_TYPES:
            raise ReviewError(
                f"organisation type {self.organisation_type!r} is not one of "
                f"{list(ORG_TYPES)}"
            )
        if not (isinstance(self.years_in_role, int) and not isinstance(self.years_in_role, bool)
                and self.years_in_role >= 0):
            raise ReviewError("years_in_role must be a whole number of years, zero or more")
        if len(self.report_sha256) != 64 or not all(
            c in "0123456789abcdef" for c in self.report_sha256
        ):
            raise ReviewError(
                f"report_sha256 {self.report_sha256!r} is not a sha256 hex digest"
            )
        if not self.reviewer_role.strip():
            raise ReviewError("a review must say what the reviewer does")

    @property
    def useful(self) -> bool:
        return self.rating in USEFUL_OR_BETTER

    @property
    def by_emergency_manager(self) -> bool:
        return REQUIRED_ROLE in self.reviewer_role.casefold()

    @property
    def counts(self) -> bool:
        """Whether the Phase 3 exit criterion counts this review at all."""
        return bool(self.blinded) and self.useful and self.by_emergency_manager

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, raw: Mapping, *, where: str = "<review>") -> "Review":
        missing = [
            k for k in (
                "report_sha256", "facility_hash", "period", "reviewer_role",
                "organisation_type", "years_in_role", "rating",
            ) if k not in raw
        ]
        if missing:
            raise ReviewError(f"{where}: missing {missing}")
        return cls(
            report_sha256=str(raw["report_sha256"]),
            facility_hash=str(raw["facility_hash"]),
            period=str(raw["period"]),
            reviewer_role=str(raw["reviewer_role"]),
            organisation_type=str(raw["organisation_type"]),
            years_in_role=int(raw["years_in_role"]),
            rating=str(raw["rating"]),
            blinded=bool(raw.get("blinded", True)),
            comments=str(raw.get("comments", "")),
            recorded_at=str(raw.get("recorded_at", "")),
        )

    @classmethod
    def read(cls, path: pathlib.Path) -> "Review":
        path = pathlib.Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ReviewError(f"{path}: cannot be read ({exc})") from None
        except ValueError as exc:
            raise ReviewError(f"{path}: not valid JSON ({exc})") from None
        return cls.from_dict(raw, where=str(path))


def sha256_of(path: pathlib.Path | str) -> str:
    """The sha256 of a file's bytes, which is what a review is bound to."""
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def record(
    blind_path: pathlib.Path | str,
    *,
    rating: str,
    reviewer_role: str,
    organisation_type: str,
    years_in_role: int,
    comments: str = "",
    reviews_dir: pathlib.Path | None = None,
    recorded_at: str | None = None,
    expected_sha256: str | None = None,
) -> tuple[pathlib.Path, Review]:
    """Bind a rating to a blinded rendering, recomputing its sha from the file.

    The file must actually be a blinded render — the `readiness-blind` meta tag
    a blinded page carries — because the exit criterion is about blinded
    review, and a rating of the named page would be a different claim entirely.
    `expected_sha256`, when given, must equal the sha recomputed here; that is
    the reviewer's own copy checked against ours.
    """
    path = pathlib.Path(blind_path)
    if not path.exists():
        raise ReviewError(f"{path}: no such blinded report")
    page = path.read_text(encoding="utf-8", errors="replace")
    details = gap_report_mod.blind_details(page)
    if details is None:
        raise ReviewError(
            f"{path}: not a blinded render (no readiness-blind meta tag). A review "
            "records a rating of the blinded page, which is what a reviewer sees; "
            "point this at <period>.blind.html"
        )
    label, period, kind = details
    if kind != gap_report_mod.KIND:
        raise ReviewError(f"{path}: blinded page is a {kind!r}, not a {gap_report_mod.KIND!r}")
    digest = sha256_of(path)
    if expected_sha256 is not None and expected_sha256 != digest:
        raise ReviewError(
            f"{path}: sha256 is {digest}, not the {expected_sha256} the review names; "
            "the report changed after it was reviewed, so the rating is of a "
            "different document"
        )
    review = Review(
        report_sha256=digest,
        facility_hash=label,
        period=period,
        reviewer_role=reviewer_role,
        organisation_type=organisation_type,
        years_in_role=int(years_in_role),
        rating=rating,
        blinded=True,
        comments=comments,
        recorded_at=recorded_at or utc_now(),
    )
    root = reviews_root(reviews_dir)
    root.mkdir(parents=True, exist_ok=True)
    out = root / f"{digest}.json"
    out.write_text(review.to_json(), encoding="utf-8")
    return out, review


def load_all(reviews_dir: pathlib.Path | None = None) -> list[Review]:
    """Every review record on disk, by file name; unreadable files are refused."""
    root = reviews_root(reviews_dir)
    if not root.exists():
        return []
    return [Review.read(path) for path in sorted(root.glob("*.json"))]


def counting(reviews: Iterable[Review]) -> list[Review]:
    """The reviews the Phase 3 exit criterion counts, in file order."""
    return [review for review in reviews if review.counts]


def distinct_facilities(reviews: Iterable[Review]) -> list[str]:
    """The facility hashes among a set of reviews, once each, sorted."""
    return sorted({review.facility_hash for review in reviews})


__all__ = [
    "ORG_TYPES",
    "RATINGS",
    "REQUIRED_ROLE",
    "REVIEWS_DIR",
    "REVIEWS_DIR_ENV",
    "USEFUL_OR_BETTER",
    "Review",
    "ReviewError",
    "counting",
    "distinct_facilities",
    "load_all",
    "record",
    "reviews_root",
    "sha256_of",
]
