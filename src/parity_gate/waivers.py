"""Waivers: accepting a known finding on the record, with a name and a date.

Every gate eventually meets a finding the team has decided to live with for a
while: a field being retired on purpose, a consumer that is already migrated.
Without a way to say so, the choices are all bad. Someone edits the suite until
the finding disappears, which deletes the check along with the noise; or the
gate is run without ``--strict`` forever; or it is switched off.

A waiver is the fourth option, borrowed from how security programmes handle
exceptions to a bug bar. It names the rule, the case and the path it covers,
**who owns it, when it expires and why**, and it is written in the suite, so it
is reviewed like code and hashed into the evidence with everything else.

What a waiver does, precisely:

* A matching finding still appears in every report and in SARIF (as a
  suppressed result, with the justification attached). It is never hidden.
* It no longer fails the case on its own; a case whose only failures are
  waived ends as ``WARN``, not ``PASS``.
* **After its expiry date it waives nothing.** The finding fails the build
  again, and the report says which waiver lapsed and whose it was.
* A waiver that matched nothing in a run is reported as unused, so the list
  does not silently outlive the problems it was written for.
* ``policy.max_waiver_days`` (90 by default) refuses a waiver dated further
  out than that, so ``expires = 2099-01-01`` is not a way to make one permanent.

Only findings about the API can be waived: drift, value differences and failed
assertions. A case that could not be judged, or an endpoint that is unstable, is
not something to accept on the record; it is something to fix first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from fnmatch import fnmatchcase
from typing import Any

from parity_gate import rules

WAIVABLE_FAMILIES = frozenset({rules.DRIFT, rules.DIFFERENCE, rules.CHECK})
DEFAULT_MAX_DAYS = 90


class WaiverError(ValueError):
    """A waiver entry is malformed, unknown, not waivable or dated too far out."""


@dataclass(frozen=True)
class Waiver:
    index: int
    rule: str
    case: str
    path: str
    owner: str
    expires: date
    reason: str
    ticket: str | None = None

    def active(self, on: date) -> bool:
        """A waiver covers its expiry day and lapses the day after."""
        return on <= self.expires

    def matches(self, case_id: str, rule_id: str | None, path: str) -> bool:
        return (
            rule_id == self.rule
            and fnmatchcase(case_id, self.case)
            and fnmatchcase(path, self.path)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "rule": self.rule,
            "case": self.case,
            "path": self.path,
            "owner": self.owner,
            "expires": self.expires.isoformat(),
            "reason": self.reason,
            "ticket": self.ticket,
        }


def today() -> date:
    """The date waivers are judged against. UTC, so a CI runner's zone is irrelevant."""
    return datetime.now(UTC).date()


def parse(raw: Any, *, max_days: int = DEFAULT_MAX_DAYS, on: date | None = None) -> list[Waiver]:
    """Validate ``[[waivers]]`` entries from a suite file."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise WaiverError("[[waivers]] must be an array of tables")
    reference = on or today()
    parsed: list[Waiver] = []
    for index, entry in enumerate(raw):
        where = f"[[waivers]] #{index + 1}"
        if not isinstance(entry, dict):
            raise WaiverError(f"{where} must be a table")

        missing = [key for key in ("rule", "owner", "expires", "reason") if not entry.get(key)]
        if missing:
            raise WaiverError(
                f"{where} is missing {', '.join(missing)}. A waiver without an owner, an "
                "expiry and a reason is a deleted check with extra steps."
            )

        try:
            rule = rules.resolve(str(entry["rule"]))
        except KeyError as exc:
            raise WaiverError(f"{where}: {exc.args[0]}") from None
        if rule.family not in WAIVABLE_FAMILIES:
            raise WaiverError(
                f"{where}: {rule.id} ({rule.name}) cannot be waived. Only findings about "
                "the API can be accepted on the record; this one needs fixing first."
            )

        expires = _date(entry["expires"], where)
        if (expires - reference).days > max_days:
            raise WaiverError(
                f"{where} expires {expires.isoformat()}, more than policy.max_waiver_days "
                f"({max_days}) from today. Pick a nearer date and renew it if it is still needed."
            )

        parsed.append(
            Waiver(
                index=index,
                rule=rule.id,
                case=str(entry.get("case") or "*"),
                path=str(entry.get("path") or "*"),
                owner=str(entry["owner"]),
                expires=expires,
                reason=str(entry["reason"]),
                ticket=str(entry["ticket"]) if entry.get("ticket") else None,
            )
        )
    return parsed


def apply(record: Any, waivers: list[Waiver], on: date) -> None:
    """Mark every finding in ``record`` that a waiver covers.

    An active waiver adds ``waiver`` to the finding; a lapsed one adds
    ``waiver_expired`` and leaves the finding counting. The first matching
    active waiver wins, so ordering in the suite is not significant beyond that.
    """
    if not waivers:
        return
    case_id = str(record.case.get("id"))
    findings = [c for c in record.checks if not c.get("passed")]
    findings += list(record.drifts) + list(record.differences)
    for finding in findings:
        path = str(finding.get("path") or "$")
        matching = [w for w in waivers if w.matches(case_id, finding.get("rule"), path)]
        active = next((w for w in matching if w.active(on)), None)
        if active is not None:
            finding["waiver"] = active.to_dict()
        elif matching:
            finding["waiver_expired"] = matching[0].to_dict()


def summarise(waivers: list[Waiver], records: list[Any], on: date) -> dict[str, Any]:
    """Which waivers did work in this run, which lapsed, which matched nothing."""
    applied: dict[int, int] = {}
    lapsed: dict[int, int] = {}
    for record in records:
        findings = [c for c in record.checks if not c.get("passed")]
        findings += list(record.drifts) + list(record.differences)
        for finding in findings:
            if finding.get("waiver"):
                index = finding["waiver"]["index"]
                applied[index] = applied.get(index, 0) + 1
            if finding.get("waiver_expired"):
                index = finding["waiver_expired"]["index"]
                lapsed[index] = lapsed.get(index, 0) + 1

    def entry(waiver: Waiver, hits: int) -> dict[str, Any]:
        return {**waiver.to_dict(), "findings": hits}

    return {
        "judged_on": on.isoformat(),
        "applied": [entry(w, applied[w.index]) for w in waivers if w.index in applied],
        "expired": [entry(w, lapsed[w.index]) for w in waivers if w.index in lapsed],
        "unused": [
            entry(w, 0) for w in waivers if w.index not in applied and w.index not in lapsed
        ],
    }


def _date(value: Any, where: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise WaiverError(f"{where}: expires must be a date like 2026-10-31") from None
