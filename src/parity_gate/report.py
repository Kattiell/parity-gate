"""Human-readable output: a Markdown report for tickets, an HTML one for CI.

Both are rendered from the same sealed :class:`~parity_gate.evidence.Run`, so
what a reviewer reads and what the hash chain covers cannot drift apart.

The ordering is opinionated: the verdict first, then what broke, then what is
merely noisy, then the traceability matrix. A report that opens with a table of
green rows is a report nobody scrolls.
"""

from __future__ import annotations

import html
import json
from typing import Any

from parity_gate.evidence import ERROR, FAIL, PASS, SKIPPED, WARN, Run

VERDICT_ICON = {PASS: "PASS", FAIL: "FAIL", WARN: "WARN", ERROR: "ERROR", SKIPPED: "SKIP"}
SEVERITY_ORDER = {"breaking": 0, "risky": 1, "additive": 2}


def render_markdown(run: Run) -> str:
    data = run.to_dict()
    summary = data["summary"]
    lines: list[str] = []
    add = lines.append

    add(f"# parity-gate: {run.suite_name}")
    add("")
    add(f"**Verdict: {summary['verdict']}**")
    add("")
    add(f"- Run `{run.run_id}` finished {run.finished_at}")
    add(f"- Baseline `{run.baseline_url}`")
    add(f"- Candidate `{run.candidate_url}`")
    add(
        f"- {summary['cases']} case(s): "
        + ", ".join(f"{count} {name}" for name, count in summary["by_verdict"].items() if count)
    )
    values = (
        f"{summary['differences']} value difference(s)"
        if summary.get("values_compared")
        else "values not compared (gated against a recorded contract)"
    )
    add(
        f"- {summary['breaking_drifts']} breaking contract change(s), {values}, "
        f"{summary['unstable_cases']} unstable endpoint(s)"
    )
    add(f"- Evidence chain head `{data['chain_head'][:16]}`")
    add("")

    add("## Cases")
    add("")
    add("| Verdict | Case | Requirement | Risk | Drift | Diffs | Stability |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for record in run.records:
        case = record.case
        breaking = sum(1 for d in record.drifts if d["severity"] == "breaking")
        drift_cell = f"{len(record.drifts)}" + (f" ({breaking} breaking)" if breaking else "")
        stability = record.stability.get("candidate", {}).get("verdict", "-")
        icon = VERDICT_ICON.get(record.verdict, record.verdict)
        add(
            f"| {icon} | `{case['id']}` {case['title']} "
            f"| {case.get('requirement') or '-'} | {case.get('risk')} "
            f"| {drift_cell} | {len(record.differences)} | {stability} |"
        )
    add("")

    actionable = [
        r for r in run.records if r.verdict in {FAIL, ERROR, WARN} or r.unchecked_paths
    ]
    if actionable:
        add("## Findings")
        add("")
    for record in actionable:
        case = record.case
        add(f"### {VERDICT_ICON.get(record.verdict)} `{case['id']}` — {case['title']}")
        add("")
        add(
            f"`{case['method']} {case['path']}` "
            f"· requirement {case.get('requirement') or 'unmapped'} "
            f"· risk {case.get('risk')}"
        )
        add("")

        if record.error:
            add(f"> {record.error}")
            add("")

        failed = [c for c in record.checks if not c["passed"]]
        if failed:
            add("**Failed assertions**")
            add("")
            for check in failed:
                add(f"- `{check['name']}`: {check['detail']}")
            add("")

        if record.drifts:
            add("**Contract drift**")
            add("")
            add("| Severity | Kind | Path | Detail |")
            add("| --- | --- | --- | --- |")
            for drift in sorted(record.drifts, key=lambda d: SEVERITY_ORDER.get(d["severity"], 9)):
                add(
                    f"| {drift['severity']} | `{drift['kind']}` | `{drift['path']}` "
                    f"| {drift['detail']} |"
                )
            add("")

        if record.differences:
            add("**Value differences**")
            add("")
            add("| Kind | Path | Baseline | Candidate |")
            add("| --- | --- | --- | --- |")
            for difference in record.differences[:25]:
                add(
                    f"| `{difference['kind']}` | `{difference['path']}` "
                    f"| `{_short(difference['baseline'])}` | `{_short(difference['candidate'])}` |"
                )
            if len(record.differences) > 25:
                add(f"| ... | _{len(record.differences) - 25} more_ | | |")
            add("")
        if record.differences_suppressed:
            add(
                f"_{record.differences_suppressed} further value difference(s) are folded away: "
                "they are restatements of the contract drift listed above._"
            )
            add("")

        if record.unchecked_paths:
            add(
                f"**Not checked** — {len(record.unchecked_paths)} path(s) could not be "
                "compared because a collection was empty in every sample. Nothing is wrong "
                "with them; nothing is confirmed about them either."
            )
            add("")
            for path in record.unchecked_paths[:12]:
                add(f"- `{path}`")
            if len(record.unchecked_paths) > 12:
                add(f"- _and {len(record.unchecked_paths) - 12} more_")
            add("")

        for label, stability in _stability_blocks(record):
            add(
                f"**Stability ({label})**: `{stability['verdict']}` over "
                f"{stability['repeats']} calls — {stability['detail']}"
            )
            add("")
            if stability.get("volatile_paths"):
                add("Add to `policy.mask_paths` if this movement is expected:")
                add("")
                add("```toml")
                add("mask_paths = [")
                for path in stability["volatile_paths"][:12]:
                    add(f'  "{path}",')
                add("]")
                add("```")
                add("")

    add("## Traceability")
    add("")
    add("| Requirement | Cases | Worst verdict |")
    add("| --- | --- | --- |")
    for requirement, entries in run.traceability().items():
        worst = max(entries, key=lambda e: _rank(e["verdict"]))["verdict"]
        cases = ", ".join(f"`{e['case']}`" for e in entries)
        add(f"| {requirement} | {cases} | {VERDICT_ICON.get(worst, worst)} |")
    add("")

    add("## Evidence")
    add("")
    add(
        "Every record is hashed together with the hash of the record before it. "
        "Run `parity-gate verify <evidence-dir>` to confirm the files on disk "
        "still match the chain."
    )
    add("")
    return "\n".join(lines)


def render_html(run: Run) -> str:
    data = run.to_dict()
    summary = data["summary"]
    rows: list[str] = []

    for record in run.records:
        case = record.case
        breaking = sum(1 for d in record.drifts if d["severity"] == "breaking")
        stability = record.stability.get("candidate", {}).get("verdict", "-")
        details = _html_details(record)
        rows.append(
            f"""<tr class="v-{record.verdict.lower()}">
  <td><span class="badge {record.verdict.lower()}">{record.verdict}</span></td>
  <td><code>{_e(case['id'])}</code><div class="muted">{_e(case['title'])}</div></td>
  <td>{_e(case.get('requirement') or '—')}</td>
  <td>{_e(case.get('risk'))}</td>
  <td>{len(record.drifts)}{f' <b>({breaking} breaking)</b>' if breaking else ''}</td>
  <td>{len(record.differences)}</td>
  <td>{_e(stability)}</td>
</tr>
<tr class="detail"><td colspan="7">{details}</td></tr>"""
        )

    verdict = summary["verdict"]
    return _HTML_TEMPLATE.format(
        title=_e(f"parity-gate — {run.suite_name}"),
        suite=_e(run.suite_name),
        verdict=_e(verdict),
        verdict_class=verdict.lower(),
        run_id=_e(run.run_id),
        finished=_e(str(run.finished_at)),
        baseline=_e(run.baseline_url),
        candidate=_e(run.candidate_url),
        cases=summary["cases"],
        breaking=summary["breaking_drifts"],
        diffs=summary["differences"] if summary.get("values_compared") else "n/a",
        unstable=summary["unstable_cases"],
        chain=_e(data["chain_head"][:16]),
        rows="\n".join(rows),
        matrix=_html_matrix(run),
    )


def _html_details(record: Any) -> str:
    blocks: list[str] = []

    if record.error:
        blocks.append(f'<p class="err">{_e(record.error)}</p>')

    failed = [c for c in record.checks if not c["passed"]]
    if failed:
        items = "".join(
            f"<li><code>{_e(c['name'])}</code> &mdash; {_e(c['detail'])}</li>" for c in failed
        )
        blocks.append(f"<h4>Failed assertions</h4><ul>{items}</ul>")

    if record.drifts:
        body = "".join(
            f"<tr><td><span class=\"sev {_e(d['severity'])}\">{_e(d['severity'])}</span></td>"
            f"<td><code>{_e(d['kind'])}</code></td><td><code>{_e(d['path'])}</code></td>"
            f"<td>{_e(d['detail'])}</td></tr>"
            for d in sorted(record.drifts, key=lambda d: SEVERITY_ORDER.get(d["severity"], 9))
        )
        blocks.append(
            "<h4>Contract drift</h4><table class=\"inner\"><thead><tr>"
            "<th>Severity</th><th>Kind</th><th>Path</th><th>Detail</th>"
            f"</tr></thead><tbody>{body}</tbody></table>"
        )

    if record.differences or record.differences_suppressed:
        table = ""
        if record.differences:
            body = "".join(
                f"<tr><td><code>{_e(d['kind'])}</code></td><td><code>{_e(d['path'])}</code></td>"
                f"<td><code>{_e(_short(d['baseline']))}</code></td>"
                f"<td><code>{_e(_short(d['candidate']))}</code></td></tr>"
                for d in record.differences[:25]
            )
            table = (
                '<table class="inner"><thead><tr>'
                "<th>Kind</th><th>Path</th><th>Baseline</th><th>Candidate</th>"
                f"</tr></thead><tbody>{body}</tbody></table>"
            )
        folded = (
            f'<p class="muted">{record.differences_suppressed} further difference(s) folded '
            "away: they restate the contract drift above.</p>"
            if record.differences_suppressed
            else ""
        )
        blocks.append(f"<h4>Value differences</h4>{table}{folded}")

    if record.unchecked_paths:
        items = "".join(f"<li><code>{_e(p)}</code></li>" for p in record.unchecked_paths[:12])
        extra = (
            f"<li>and {len(record.unchecked_paths) - 12} more</li>"
            if len(record.unchecked_paths) > 12
            else ""
        )
        blocks.append(
            "<h4>Not checked</h4><p class=\"muted\">A collection was empty in every sample, "
            "so nothing could be learned about these paths. Not a finding, not a "
            f"confirmation.</p><ul>{items}{extra}</ul>"
        )

    for label, stability in _stability_blocks(record):
        suggestion = ""
        if stability.get("volatile_paths"):
            listed = ",\n  ".join(f'"{_e(p)}"' for p in stability["volatile_paths"][:12])
            suggestion = f"<pre>mask_paths = [\n  {listed}\n]</pre>"
        blocks.append(
            f"<h4>Stability &mdash; {_e(label)}</h4><p><code>{_e(stability['verdict'])}</code>"
            f" over {stability['repeats']} calls: {_e(stability['detail'])}</p>{suggestion}"
        )

    return "".join(blocks) or '<p class="muted">No findings.</p>'


def _stability_blocks(record: Any) -> list[tuple[str, dict[str, Any]]]:
    """Stability findings worth showing, collapsed when both sides agree.

    An endpoint whose baseline and candidate are equally noisy is one fact, not
    two, and printing it twice trains the reader to skim past it.
    """
    sides = {
        label: record.stability.get(label, {})
        for label in ("baseline", "candidate")
        if record.stability.get(label, {}).get("verdict") not in {None, "STABLE"}
    }
    if len(sides) == 2:
        left, right = sides["baseline"], sides["candidate"]
        if (left.get("verdict"), left.get("volatile_paths")) == (
            right.get("verdict"),
            right.get("volatile_paths"),
        ):
            return [("both sides", left)]
    return list(sides.items())


def _html_matrix(run: Run) -> str:
    rows = []
    for requirement, entries in run.traceability().items():
        worst = max(entries, key=lambda e: _rank(e["verdict"]))["verdict"]
        cases = ", ".join(f"<code>{_e(e['case'])}</code>" for e in entries)
        rows.append(
            f'<tr><td>{_e(requirement)}</td><td>{cases}</td>'
            f'<td><span class="badge {worst.lower()}">{_e(worst)}</span></td></tr>'
        )
    return "\n".join(rows)


def _rank(verdict: str) -> int:
    return {PASS: 0, SKIPPED: 1, WARN: 2, FAIL: 3, ERROR: 4}.get(verdict, 0)


def _short(value: Any, limit: int = 80) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, default=str)
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


_HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{
  --bg: #f7f7f5; --panel: #ffffff; --ink: #1b1b19; --muted: #6b6b66;
  --line: #e2e2dd; --pass: #2e7d4f; --warn: #a9741a; --fail: #b3392f; --error: #7a2620;
  --accent: #3b5bdb;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #16161a; --panel: #1e1e23; --ink: #ececea; --muted: #9a9a95;
    --line: #32323a; --pass: #5fbf85; --warn: #d8a74a; --fail: #e4685c; --error: #f08f84;
    --accent: #8aa2ff;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif; }}
.wrap {{ max-width: 1100px; margin: 0 auto; padding: 32px 20px 80px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }}
h2 {{ font-size: 16px; margin: 36px 0 12px; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--muted); }}
h4 {{ font-size: 13px; margin: 16px 0 6px; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--muted); }}
code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12.5px; }}
pre {{ background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
  padding: 10px 12px; overflow-x: auto; }}
.muted {{ color: var(--muted); font-size: 13px; }}
.err {{ color: var(--fail); }}
.hero {{ background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
  padding: 22px 24px; }}
.verdict {{ display: inline-block; font-size: 13px; font-weight: 700; letter-spacing: 0.1em;
  padding: 5px 12px; border-radius: 999px; border: 1px solid currentColor; }}
.verdict.pass {{ color: var(--pass); }} .verdict.warn {{ color: var(--warn); }}
.verdict.fail {{ color: var(--fail); }} .verdict.error {{ color: var(--error); }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 14px; margin-top: 20px; }}
.stat b {{ display: block; font-size: 26px; font-variant-numeric: tabular-nums; }}
.stat span {{ color: var(--muted); font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.06em; }}
.meta {{ margin-top: 18px; font-size: 13px; color: var(--muted); }}
.meta div {{ margin-top: 3px; }}
table {{ width: 100%; border-collapse: collapse; background: var(--panel);
  border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }}
th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--line);
  vertical-align: top; }}
th {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.07em; color: var(--muted); }}
tr.detail > td {{ background: transparent; padding-top: 0; }}
tr.v-pass + tr.detail {{ display: none; }}
table.inner {{ border-radius: 8px; margin-bottom: 4px; }}
table.inner th, table.inner td {{ padding: 6px 10px; font-size: 12.5px; }}
.badge {{ font-size: 11px; font-weight: 700; letter-spacing: 0.06em; padding: 3px 8px;
  border-radius: 999px; border: 1px solid currentColor; white-space: nowrap; }}
.badge.pass {{ color: var(--pass); }} .badge.warn {{ color: var(--warn); }}
.badge.fail {{ color: var(--fail); }} .badge.error {{ color: var(--error); }}
.badge.skipped {{ color: var(--muted); }}
.sev.breaking {{ color: var(--fail); font-weight: 700; }}
.sev.risky {{ color: var(--warn); font-weight: 700; }}
.sev.additive {{ color: var(--muted); }}
.scroll {{ overflow-x: auto; }}
footer {{ margin-top: 40px; font-size: 12px; color: var(--muted); }}
</style></head>
<body><div class="wrap">
<div class="hero">
  <h1>{suite}</h1>
  <p class="muted">Contract parity between two API versions</p>
  <p><span class="verdict {verdict_class}">{verdict}</span></p>
  <div class="stats">
    <div class="stat"><b>{cases}</b><span>cases</span></div>
    <div class="stat"><b>{breaking}</b><span>breaking drifts</span></div>
    <div class="stat"><b>{diffs}</b><span>value differences</span></div>
    <div class="stat"><b>{unstable}</b><span>unstable endpoints</span></div>
  </div>
  <div class="meta">
    <div>baseline &nbsp;<code>{baseline}</code></div>
    <div>candidate <code>{candidate}</code></div>
    <div>run <code>{run_id}</code> · finished {finished} · chain <code>{chain}</code></div>
  </div>
</div>

<h2>Cases</h2>
<div class="scroll"><table>
<thead><tr><th>Verdict</th><th>Case</th><th>Requirement</th><th>Risk</th>
<th>Drift</th><th>Diffs</th><th>Stability</th></tr></thead>
<tbody>
{rows}
</tbody></table></div>

<h2>Traceability</h2>
<div class="scroll"><table>
<thead><tr><th>Requirement</th><th>Cases</th><th>Worst verdict</th></tr></thead>
<tbody>
{matrix}
</tbody></table></div>

<footer>Generated by parity-gate. Records are hash-chained; run
<code>parity-gate verify</code> against the evidence directory to confirm this
report still matches the data it was rendered from.</footer>
</div></body></html>
"""
