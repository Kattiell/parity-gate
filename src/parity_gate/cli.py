"""Command line entry point.

Exit codes are part of the contract with CI:

``0``
    Gate passed. Warnings may still be present unless ``--strict`` was used.
``1``
    The tool could not run: bad arguments, malformed suite, missing credential.
``2``
    The gate failed: a breaking contract change, a value difference or a failed
    assertion. This is the code that should stop a pipeline.
``3``
    The run was refused by the safety policy before any request was sent.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from parity_gate import __version__, evidence, report
from parity_gate import suite as suite_module
from parity_gate.evidence import ERROR, FAIL, PASS, WARN
from parity_gate.runner import Runner
from parity_gate.safety import SafetyError

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_GATE_FAILED = 2
EXIT_REFUSED = 3

DEMO_SUITE = Path(__file__).resolve().parent.parent.parent / "suites" / "demo-catalog.toml"

_COLORS = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m", ERROR: "\033[35m"}


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE
    return args.handler(args)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parity-gate",
        description="Prove an API rewrite did not break the contract.",
    )
    parser.add_argument("--version", action="version", version=f"parity-gate {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run a suite against both targets")
    run.add_argument("--suite", required=True, type=Path, help="path to a suite .toml file")
    run.add_argument(
        "--evidence",
        type=Path,
        default=Path("evidence"),
        help="directory to write the evidence bundle into (default: ./evidence)",
    )
    run.add_argument(
        "--with-mock",
        action="store_true",
        help="start the bundled mock API on the port named by the suite (offline demo)",
    )
    run.add_argument(
        "--strict", action="store_true", help="treat warnings as failures (exit 2 on WARN)"
    )
    run.add_argument(
        "--allow-mutations",
        action="store_true",
        help="permit cases marked `mutating = true` to issue writes",
    )
    run.add_argument(
        "--allow-production",
        action="store_true",
        help="permit hosts that look like production; say it out loud or it will not happen",
    )
    run.add_argument("--quiet", action="store_true", help="only print the final summary")
    run.set_defaults(handler=_cmd_run)

    demo = sub.add_parser(
        "demo", help="run the bundled offline demo suite against the bundled mock API"
    )
    demo.add_argument("--evidence", type=Path, default=Path("evidence"))
    demo.add_argument("--strict", action="store_true")
    demo.add_argument("--quiet", action="store_true")
    demo.set_defaults(handler=_cmd_demo)

    verify = sub.add_parser("verify", help="re-check an evidence bundle against its hash chain")
    verify.add_argument("directory", type=Path)
    verify.set_defaults(handler=_cmd_verify)

    scan = sub.add_parser("scan", help="fail if a file contains something shaped like a credential")
    scan.add_argument("paths", nargs="+", type=Path)
    scan.set_defaults(handler=_cmd_scan)

    mock = sub.add_parser("mock", help="serve the bundled two-variant mock API")
    mock.add_argument("--port", type=int, default=8799)
    mock.set_defaults(handler=_cmd_mock)

    return parser


def _cmd_demo(args: argparse.Namespace) -> int:
    fallback = Path.cwd() / "suites" / DEMO_SUITE.name
    suite_path = next((p for p in (DEMO_SUITE, fallback) if p.is_file()), None)
    if suite_path is None:
        _err(f"demo suite not found at {DEMO_SUITE}; run from a clone of the repository")
        return EXIT_USAGE
    namespace = argparse.Namespace(
        suite=suite_path,
        evidence=args.evidence,
        with_mock=True,
        strict=args.strict,
        allow_mutations=False,
        allow_production=False,
        quiet=args.quiet,
    )
    return _cmd_run(namespace)


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        loaded = suite_module.load(args.suite)
    except suite_module.SuiteError as exc:
        _err(str(exc))
        return EXIT_USAGE

    loaded.policy.allow_mutations = loaded.policy.allow_mutations or args.allow_mutations
    loaded.policy.allow_production = loaded.policy.allow_production or args.allow_production

    server = None
    if args.with_mock:
        from parity_gate.mock import start as start_mock

        port = urlsplit(loaded.candidate.base_url).port or 8799
        try:
            server = start_mock(port)
        except OSError as exc:
            _err(f"could not start the mock API on port {port}: {exc}")
            return EXIT_USAGE
        _say(f"mock api on {server.base_url}", args.quiet)

    try:
        runner = Runner(loaded, on_case=None if args.quiet else _print_case)
        headline = f"suite  {loaded.name}  ({len(loaded.cases)} cases, {loaded.repeats} repeats)"
        _say(headline, args.quiet)
        _say(f"  baseline  {loaded.baseline.base_url}", args.quiet)
        _say(f"  candidate {loaded.candidate.base_url}\n", args.quiet)
        run = runner.execute()
    except SafetyError as exc:
        _err(f"refused before sending anything: {exc}")
        return EXIT_REFUSED
    except suite_module.SuiteError as exc:
        _err(str(exc))
        return EXIT_USAGE
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()

    bundle = Path(args.evidence) / run.run_id
    files = evidence.write_run(run, bundle)

    files["report.md"] = evidence.write_text(bundle / "report.md", report.render_markdown(run))
    files["report.html"] = evidence.write_text(bundle / "report.html", report.render_html(run))

    evidence.write_manifest(bundle, files, run.chain_head, run.run_id)

    summary = run.to_dict()["summary"]
    print()
    print(f"verdict   {_colour(run.verdict)}{run.verdict}{_reset()}")
    print(
        f"cases     {summary['cases']} - "
        + ", ".join(f"{count} {name}" for name, count in summary["by_verdict"].items() if count)
    )
    print(
        f"findings  {summary['breaking_drifts']} breaking drift, "
        f"{summary['differences']} value diff, {summary['unstable_cases']} unstable"
    )
    print(f"evidence  {bundle}")
    print(f"chain     {run.chain_head[:16]}")

    if run.verdict in {FAIL, ERROR}:
        return EXIT_GATE_FAILED
    if args.strict and run.verdict == WARN:
        print("strict mode: warnings are failures")
        return EXIT_GATE_FAILED
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    problems = evidence.verify(Path(args.directory))
    if problems:
        _err("evidence bundle does not verify:")
        for problem in problems:
            _err(f"  - {problem}")
        return EXIT_GATE_FAILED
    print(f"evidence bundle at {args.directory} is intact")
    return EXIT_OK


def _cmd_scan(args: argparse.Namespace) -> int:
    from parity_gate.safety import scan_for_secrets

    findings: list[str] = []
    for path in args.paths:
        target = Path(path)
        if target.is_dir():
            candidates = [p for p in target.rglob("*") if p.is_file()]
        else:
            candidates = [target]
        for candidate in candidates:
            try:
                text = candidate.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            findings.extend(scan_for_secrets(text, origin=str(candidate)))

    if findings:
        _err("possible credentials found:")
        for finding in findings:
            _err(f"  - {finding}")
        _err("Move them to environment variables and rotate anything that was committed.")
        return EXIT_GATE_FAILED
    print("no credential-shaped strings found")
    return EXIT_OK


def _cmd_mock(args: argparse.Namespace) -> int:
    from parity_gate.mock import serve_forever

    serve_forever(args.port)
    return EXIT_OK


def _print_case(case, record) -> None:  # type: ignore[no-untyped-def]
    marks: list[str] = []
    breaking = sum(1 for d in record.drifts if d["severity"] == "breaking")
    if breaking:
        marks.append(f"{breaking} breaking drift")
    if record.differences:
        marks.append(f"{len(record.differences)} diff")
    stability = record.stability.get("candidate", {}).get("verdict")
    if stability and stability != "STABLE":
        marks.append(stability.lower())
    failed = [c["name"] for c in record.checks if not c["passed"]]
    if failed:
        marks.append("failed: " + ", ".join(failed))

    # ASCII only: this line is printed to consoles that are not always UTF-8.
    title = case.title if len(case.title) <= 50 else case.title[:47] + "..."
    line = f"  {_colour(record.verdict)}{record.verdict:<5}{_reset()} {case.id:<9} {title}"
    if marks:
        line += " " * max(2, 52 - len(title)) + "- " + "; ".join(marks)
    print(line)


def _say(message: str, quiet: bool) -> None:
    if not quiet:
        print(message)


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _use_colour() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _colour(verdict: str) -> str:
    return _COLORS.get(verdict, "") if _use_colour() else ""


def _reset() -> str:
    return "\033[0m" if _use_colour() else ""


if __name__ == "__main__":
    raise SystemExit(main())
