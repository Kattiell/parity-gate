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
import json
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

from parity_gate import __version__, contracts, evidence, openapi, report, sarif
from parity_gate import suite as suite_module
from parity_gate.evidence import ERROR, FAIL, PASS, WARN
from parity_gate.runner import ContractRecordingError, Runner
from parity_gate.safety import SafetyError

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_GATE_FAILED = 2
EXIT_REFUSED = 3

_COLORS = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m", ERROR: "\033[35m"}

_FILTER_HELP = (
    "only run cases whose id or requirement matches this glob, or whose title "
    "contains it; repeatable"
)
_SARIF_HELP = (
    "also write the SARIF log to this fixed path, for a CI step that uploads it; "
    "every bundle carries its own report.sarif regardless"
)
_KEEP_HELP = (
    "after writing, keep only the N most recent evidence bundles in the "
    "directory and delete the rest"
)


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
    run.add_argument("--filter", action="append", default=[], metavar="PATTERN", help=_FILTER_HELP)
    run.add_argument("--keep", type=int, default=None, metavar="N", help=_KEEP_HELP)
    run.add_argument("--sarif", type=Path, default=None, metavar="PATH", help=_SARIF_HELP)
    run.add_argument("--quiet", action="store_true", help="only print the final summary")
    run.set_defaults(handler=_cmd_run)

    demo = sub.add_parser("demo", help="run a bundled offline demo against the bundled mock API")
    demo.add_argument(
        "--mode",
        choices=["differential", "contract", "stability"],
        default="differential",
        help="which of the three ways to use the tool to demonstrate",
    )
    demo.add_argument("--evidence", type=Path, default=Path("evidence"))
    demo.add_argument("--keep", type=int, default=None)
    demo.add_argument("--strict", action="store_true")
    demo.add_argument("--quiet", action="store_true")
    demo.set_defaults(handler=_cmd_demo)

    record = sub.add_parser(
        "record",
        help="capture the shape the API has today, to gate future deploys against",
    )
    record.add_argument("--suite", required=True, type=Path)
    record.add_argument(
        "--out",
        type=Path,
        default=None,
        help="where to write the contract (default: the suite's targets.baseline.snapshot)",
    )
    record.add_argument(
        "--from",
        dest="source_url",
        default=None,
        help="record from this base URL instead of targets.candidate (still allow-listed)",
    )
    record.add_argument(
        "--filter", action="append", default=[], metavar="PATTERN", help=_FILTER_HELP
    )
    record.add_argument("--with-mock", action="store_true")
    record.add_argument("--quiet", action="store_true")
    record.set_defaults(handler=_cmd_record)

    stability = sub.add_parser(
        "stability",
        help="measure how steady each endpoint is; assertions are not evaluated",
    )
    stability.add_argument("--suite", required=True, type=Path)
    stability.add_argument("--evidence", type=Path, default=Path("evidence"))
    stability.add_argument("--strict", action="store_true")
    stability.add_argument("--with-mock", action="store_true")
    stability.add_argument(
        "--filter", action="append", default=[], metavar="PATTERN", help=_FILTER_HELP
    )
    stability.add_argument("--keep", type=int, default=None, metavar="N", help=_KEEP_HELP)
    stability.add_argument("--sarif", type=Path, default=None, metavar="PATH", help=_SARIF_HELP)
    stability.add_argument("--quiet", action="store_true")
    stability.set_defaults(handler=_cmd_stability)

    importer = sub.add_parser(
        "import-openapi",
        help="generate a starter suite from an OpenAPI document (JSON)",
    )
    importer.add_argument(
        "--spec", required=True, help="path to an OpenAPI JSON file, or an http(s) URL"
    )
    importer.add_argument("--out", required=True, type=Path, help="suite file to write")
    importer.add_argument(
        "--base-url",
        default="",
        help="address to test against; defaults to the first server in the document",
    )
    importer.add_argument(
        "--include-writes",
        action="store_true",
        help="also emit POST/PUT/PATCH/DELETE cases, marked mutating",
    )
    importer.add_argument("--force", action="store_true", help="overwrite an existing suite")
    importer.set_defaults(handler=_cmd_import_openapi)

    verify = sub.add_parser("verify", help="re-check an evidence bundle against its hash chain")
    verify.add_argument("directory", type=Path)
    verify.set_defaults(handler=_cmd_verify)

    scan = sub.add_parser("scan", help="fail if a file contains something shaped like a credential")
    scan.add_argument("paths", nargs="+", type=Path)
    scan.set_defaults(handler=_cmd_scan)

    selftest = sub.add_parser(
        "selftest",
        help="inject a fixed fault model into real responses and measure what the gate catches",
    )
    source = selftest.add_mutually_exclusive_group(required=True)
    source.add_argument("--suite", type=Path, help="sample the live targets of this suite")
    source.add_argument(
        "--demo", action="store_true", help="use the bundled demo suite and mock API (offline)"
    )
    selftest.add_argument(
        "--side",
        choices=["candidate", "baseline"],
        default=None,
        help="which live target to sample (default: candidate; baseline for --demo)",
    )
    selftest.add_argument(
        "--mode",
        choices=["both", "differential", "contract"],
        default="both",
        help="which judging mode to measure",
    )
    selftest.add_argument(
        "--min-detection",
        type=float,
        default=1.0,
        metavar="RATE",
        help="fail when the detection rate of any mode is below this (default: 1.0)",
    )
    selftest.add_argument(
        "--max-false-alarms",
        type=float,
        default=0.0,
        metavar="RATE",
        help="fail when the false-alarm rate of any mode is above this (default: 0.0)",
    )
    selftest.add_argument(
        "--max-sites",
        type=int,
        default=25,
        metavar="N",
        help="at most N injection sites per fault per case (default: 25)",
    )
    selftest.add_argument(
        "--json", dest="json_out", type=Path, default=None, help="write every mutant here"
    )
    selftest.add_argument("--with-mock", action="store_true")
    selftest.add_argument(
        "--filter", action="append", default=[], metavar="PATTERN", help=_FILTER_HELP
    )
    selftest.add_argument("--quiet", action="store_true", help="only print the totals")
    selftest.set_defaults(handler=_cmd_selftest)

    mock = sub.add_parser("mock", help="serve the bundled two-variant mock API")
    mock.add_argument("--port", type=int, default=8799)
    mock.set_defaults(handler=_cmd_mock)

    return parser


def _cmd_demo(args: argparse.Namespace) -> int:
    from parity_gate import demo as demo_assets

    try:
        suite_path = demo_assets.suite_path(args.mode)
    except (ValueError, FileNotFoundError) as exc:
        _err(str(exc))
        return EXIT_USAGE

    if args.mode == "stability":
        return _cmd_stability(
            argparse.Namespace(
                suite=suite_path,
                evidence=args.evidence,
                strict=args.strict,
                with_mock=True,
                quiet=args.quiet,
                filter=[],
                keep=args.keep,
            )
        )

    return _cmd_run(
        argparse.Namespace(
            suite=suite_path,
            evidence=args.evidence,
            with_mock=True,
            strict=args.strict,
            allow_mutations=False,
            allow_production=False,
            quiet=args.quiet,
            filter=[],
            keep=args.keep,
        )
    )


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        loaded = suite_module.load(args.suite)
    except suite_module.SuiteError as exc:
        _err(str(exc))
        return EXIT_USAGE

    refused = _apply_filter(loaded, getattr(args, "filter", []))
    if refused:
        return refused

    loaded.policy.allow_mutations = loaded.policy.allow_mutations or args.allow_mutations
    loaded.policy.allow_production = loaded.policy.allow_production or args.allow_production

    server = None
    if args.with_mock:
        try:
            server = _start_mock(loaded, args.quiet)
        except OSError as exc:
            _err(f"could not start the mock API: {exc}")
            return EXIT_USAGE

    try:
        runner = Runner(loaded, on_case=None if args.quiet else _print_case)
        headline = f"suite  {loaded.name}  ({len(loaded.cases)} cases, {loaded.repeats} repeats)"
        _say(headline, args.quiet)
        baseline_label = (
            f"recorded contract {loaded.baseline.snapshot}"
            if loaded.baseline.is_recorded
            else loaded.baseline.base_url
        )
        _say(f"  baseline  {baseline_label}", args.quiet)
        _say(f"  candidate {loaded.candidate.base_url}\n", args.quiet)
        run = runner.execute()
    except SafetyError as exc:
        _err(f"refused before sending anything: {exc}")
        return EXIT_REFUSED
    except contracts.ContractError as exc:
        _err(str(exc))
        return EXIT_USAGE
    except suite_module.SuiteError as exc:
        _err(str(exc))
        return EXIT_USAGE
    finally:
        _stop_mock(server)

    bundle = _write_bundle(
        run, Path(args.evidence), getattr(args, "keep", None), getattr(args, "sarif", None)
    )

    summary = run.to_dict()["summary"]
    print()
    print(f"verdict   {_colour(run.verdict)}{run.verdict}{_reset()}")
    print(
        f"cases     {summary['cases']} - "
        + ", ".join(f"{count} {name}" for name, count in summary["by_verdict"].items() if count)
    )
    values = (
        f"{summary['differences']} value diff"
        if summary.get("values_compared")
        else "values not compared (contract mode)"
    )
    print(
        f"findings  {summary['breaking_drifts']} breaking drift, "
        f"{values}, {summary['unstable_cases']} unstable"
    )
    if summary.get("unchecked_paths"):
        print(
            f"unchecked {summary['unchecked_paths']} path(s) - a collection was empty, "
            "so nothing was learned about them"
        )
    print(f"evidence  {bundle}")
    print(f"chain     {run.chain_head[:16]}")

    if run.verdict in {FAIL, ERROR}:
        return EXIT_GATE_FAILED
    if args.strict and run.verdict == WARN:
        print("strict mode: warnings are failures")
        return EXIT_GATE_FAILED
    return EXIT_OK


def _start_mock(loaded, quiet: bool):  # type: ignore[no-untyped-def]
    """Start the bundled mock on the port the suite's candidate names."""
    from parity_gate.mock import start as start_mock

    port = urlsplit(loaded.candidate.base_url).port or 8799
    server = start_mock(port)
    _say(f"mock api on {server.base_url}", quiet)
    return server


def _stop_mock(server) -> None:  # type: ignore[no-untyped-def]
    if server is not None:
        server.shutdown()
        server.server_close()


def _apply_filter(loaded, patterns: list[str]) -> int:  # type: ignore[no-untyped-def]
    """Narrow the suite in place. Returns an exit code, or 0 to carry on."""
    if not patterns:
        return EXIT_OK
    chosen = loaded.select(patterns)
    if not chosen:
        _err(f"no case matches {patterns}. The suite has: " + ", ".join(c.id for c in loaded.cases))
        return EXIT_USAGE
    loaded.cases = chosen
    return EXIT_OK


def _prune_bundles(root: Path, keep: int | None) -> int:
    """Delete all but the ``keep`` newest bundles. Returns how many were removed.

    Only directories that carry a manifest are touched: an evidence directory
    someone also keeps notes in must not lose them to a retention flag.
    """
    if keep is None or keep < 1 or not root.is_dir():
        return 0
    bundles = sorted(
        (d for d in root.iterdir() if d.is_dir() and (d / "manifest.json").is_file()),
        key=lambda d: d.name,
    )
    removed = 0
    for stale in bundles[: max(0, len(bundles) - keep)]:
        shutil.rmtree(stale, ignore_errors=True)
        removed += 1
    return removed


def _write_bundle(  # type: ignore[no-untyped-def]
    run, root: Path, keep: int | None = None, sarif_copy: Path | None = None
) -> Path:
    """Write the evidence bundle for a run and return its directory."""
    bundle = root / run.run_id
    files = evidence.write_run(run, bundle)
    files["report.md"] = evidence.write_text(bundle / "report.md", report.render_markdown(run))
    files["report.html"] = evidence.write_text(bundle / "report.html", report.render_html(run))
    log = json.dumps(sarif.render(run), indent=2, ensure_ascii=False) + "\n"
    files["report.sarif"] = evidence.write_text(bundle / "report.sarif", log)
    if sarif_copy is not None:
        sarif_copy.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(sarif_copy, log)
    evidence.write_manifest(bundle, files, run.chain_head, run.run_id)
    _prune_bundles(root, keep)
    return bundle


def _cmd_record(args: argparse.Namespace) -> int:
    try:
        loaded = suite_module.load(args.suite)
    except suite_module.SuiteError as exc:
        _err(str(exc))
        return EXIT_USAGE

    refused = _apply_filter(loaded, getattr(args, "filter", []))
    if refused:
        return refused

    destination = args.out or loaded.contract_path
    if destination is None:
        _err(
            "nowhere to write the contract. Either pass --out, or point the suite at one:\n"
            '    [targets.baseline]\n    snapshot = "contracts/api.json"'
        )
        return EXIT_USAGE

    def announce(case, probe) -> None:  # type: ignore[no-untyped-def]
        verdict = probe.stability.verdict if probe.stability else "UNKNOWN"
        note = "" if verdict == "STABLE" else f"   <- {verdict.lower()}"
        print(f"  {case.id:<9} {len(probe.schema.fields):>4} fields{note}")

    server = None
    try:
        if args.with_mock:
            server = _start_mock(loaded, args.quiet)
        if args.source_url:
            loaded.candidate.base_url = args.source_url
        runner = Runner(loaded)
        _say(f"recording {loaded.name} from {loaded.candidate.base_url}\n", args.quiet)
        contract = runner.record_contract(on_case=None if args.quiet else announce)
    except SafetyError as exc:
        _err(f"refused before sending anything: {exc}")
        return EXIT_REFUSED
    except (ContractRecordingError, suite_module.SuiteError) as exc:
        _err(str(exc))
        return EXIT_USAGE
    except OSError as exc:
        _err(f"could not start the mock API: {exc}")
        return EXIT_USAGE
    finally:
        _stop_mock(server)

    contracts.save(contract, destination)

    unsteady = {
        case_id: case.stability
        for case_id, case in contract.cases.items()
        if case.stability != "STABLE"
    }
    print()
    print(f"contract  {destination}")
    print(f"cases     {len(contract.cases)} recorded (shape and statuses only, no values)")
    if unsteady:
        print(f"unsteady  {len(unsteady)} endpoint(s) did not answer identically on every call:")
        for case_id, verdict in sorted(unsteady.items()):
            print(f"            {case_id:<9} {verdict}")
        print("          Fix or mask those before treating this contract as a reference.")
    print("\nReview the file, commit it, then point the suite's baseline at it.")
    return EXIT_OK


def _cmd_stability(args: argparse.Namespace) -> int:
    try:
        loaded = suite_module.load(args.suite)
    except suite_module.SuiteError as exc:
        _err(str(exc))
        return EXIT_USAGE

    server = None
    try:
        if args.with_mock:
            server = _start_mock(loaded, args.quiet)
        runner = Runner(loaded)
        _say(
            f"sampling {loaded.name} at {loaded.candidate.base_url} "
            f"({len(loaded.cases)} cases x {loaded.repeats} calls)\n",
            args.quiet,
        )
        run = runner.measure_stability(on_case=None if args.quiet else _print_case)
    except SafetyError as exc:
        _err(f"refused before sending anything: {exc}")
        return EXIT_REFUSED
    except OSError as exc:
        _err(f"could not start the mock API: {exc}")
        return EXIT_USAGE
    finally:
        _stop_mock(server)

    bundle = _write_bundle(
        run, Path(args.evidence), getattr(args, "keep", None), getattr(args, "sarif", None)
    )
    suggestions = _mask_suggestions(run)
    summary = run.to_dict()["summary"]

    print()
    print(f"verdict   {_colour(run.verdict)}{run.verdict}{_reset()}")
    print(
        f"cases     {summary['cases']} - "
        + ", ".join(f"{count} {name}" for name, count in summary["by_verdict"].items() if count)
    )
    print(
        f"stability {summary['unstable_cases']} unstable, "
        f"{summary.get('volatile_cases', 0)} merely noisy"
    )
    if suggestions:
        print("\nNoise found. Add to [policy] in the suite so it stops being reported:")
        print("mask_paths = [")
        for path in suggestions:
            print(f'  "{path}",')
        print("]")
    print(f"\nevidence  {bundle}")

    if run.verdict in {FAIL, ERROR}:
        return EXIT_GATE_FAILED
    if args.strict and run.verdict == WARN:
        return EXIT_GATE_FAILED
    return EXIT_OK


def _mask_suggestions(run) -> list[str]:  # type: ignore[no-untyped-def]
    """Every volatile path the run found, de-duplicated and ready to paste."""
    found: set[str] = set()
    for record in run.records:
        for side in record.stability.values():
            if isinstance(side, dict):
                found.update(side.get("volatile_paths") or [])
    return sorted(found)


def _cmd_import_openapi(args: argparse.Namespace) -> int:
    destination = Path(args.out)
    if destination.exists() and not args.force:
        _err(f"{destination} already exists; pass --force to overwrite it")
        return EXIT_USAGE

    try:
        text, origin = _read_spec(args.spec)
        spec = openapi.load_spec(text, origin)
        contract = f"contracts/{destination.stem}.json"
        suite_text = openapi.to_suite(
            spec,
            name=destination.stem,
            base_url=args.base_url,
            contract_path=contract,
            include_writes=args.include_writes,
        )
    except openapi.OpenAPIError as exc:
        _err(str(exc))
        return EXIT_USAGE
    except SafetyError as exc:
        _err(f"refused: {exc}")
        return EXIT_REFUSED
    except OSError as exc:
        _err(f"could not read {args.spec}: {exc}")
        return EXIT_USAGE

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(suite_text, encoding="utf-8", newline="\n")

    ready = suite_text.count("\n[[cases]]") + suite_text.startswith("[[cases]]")
    todo = suite_text.count("# TODO:")
    print(f"suite     {destination}")
    print(f"cases     {ready} ready" + (f", {todo} need a path parameter" if todo else ""))
    print("\nRead it, delete what does not matter, then:")
    print(f"    parity-gate stability --suite {destination}")
    print(f"    parity-gate record    --suite {destination}")
    return EXIT_OK


def _read_spec(location: str) -> tuple[str, str]:
    """Read a spec from disk, or over HTTP under the usual safety policy."""
    if location.startswith(("http://", "https://")):
        from parity_gate.httpclient import Client
        from parity_gate.safety import Policy

        host = urlsplit(location).hostname or ""
        # Typing the URL is the authorisation; every other guard still applies.
        policy = Policy(allowed_hosts=[host], allow_private_networks=True, timeout_seconds=30)
        client = Client(policy)
        try:
            response = client.request("GET", location)
        finally:
            client.close()
        if response.transport_error:
            raise OSError(response.transport_error)
        if response.status != 200:
            raise OSError(f"the document answered {response.status}")
        return response.body_text, location

    path = Path(location)
    return path.read_text(encoding="utf-8"), str(path)


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


def _cmd_selftest(args: argparse.Namespace) -> int:
    from parity_gate import selftest

    if args.demo:
        from parity_gate import demo as demo_assets

        suite_path, with_mock, side = demo_assets.suite_path("differential"), True, "baseline"
    else:
        suite_path, with_mock, side = args.suite, args.with_mock, "candidate"
    side = args.side or side

    try:
        loaded = suite_module.load(suite_path)
    except suite_module.SuiteError as exc:
        _err(str(exc))
        return EXIT_USAGE
    refused = _apply_filter(loaded, args.filter)
    if refused:
        return refused

    samples: list[selftest.Sample] = []
    skipped: dict[str, str] = {}
    server = None
    try:
        if with_mock:
            server = _start_mock(loaded, quiet=True)
        runner = Runner(loaded)
        runner.preflight()
        for case in loaded.cases:
            response = runner.sample(case, side)
            if response.transport_error or response.status is None:
                skipped[case.id] = f"did not answer: {response.transport_error}"
            elif response.json_body is None:
                skipped[case.id] = f"no JSON body to inject into ({response.json_error})"
            else:
                samples.append(
                    selftest.Sample(case=case, status=response.status, payload=response.json_body)
                )
        runner.close()
    except SafetyError as exc:
        _err(f"refused before sending anything: {exc}")
        return EXIT_REFUSED
    except suite_module.SuiteError as exc:
        _err(str(exc))
        return EXIT_USAGE
    except OSError as exc:
        _err(f"could not start the mock API: {exc}")
        return EXIT_USAGE
    finally:
        _stop_mock(server)

    if not samples:
        _err("no case produced a JSON response to inject faults into")
        for case_id, reason in skipped.items():
            _err(f"  {case_id}: {reason}")
        return EXIT_USAGE

    modes = selftest.MODES if args.mode == "both" else (args.mode,)
    result = selftest.run(loaded, samples, modes=modes, max_sites=max(1, args.max_sites))
    result.skipped = skipped

    print(f"selftest  {loaded.name}  ({len(samples)} cases sampled from {side})")
    if not args.quiet:
        _print_selftest_table(result, selftest)
    print()
    breaches: list[str] = []
    for mode in modes:
        caught, faults = result.detection(mode)
        alarms, controls = result.false_alarms(mode)
        detection = caught / faults if faults else 1.0
        false_alarms = alarms / controls if controls else 0.0
        print(
            f"{mode:<13} detection {detection:6.1%} ({caught}/{faults})   "
            f"false alarms {false_alarms:6.1%} ({alarms}/{controls})"
        )
        if detection < args.min_detection:
            breaches.append(f"{mode} detection {detection:.1%} is below {args.min_detection:.1%}")
        if false_alarms > args.max_false_alarms:
            breaches.append(
                f"{mode} false alarms {false_alarms:.1%} exceed {args.max_false_alarms:.1%}"
            )
    for case_id, reason in skipped.items():
        print(f"skipped   {case_id}: {reason}")

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        print(f"mutants   {args.json_out}")

    if result.errors:
        breaches.append(f"{len(result.errors)} mutant(s) made the tool raise; that is a defect")
    if breaches:
        print()
        for breach in breaches:
            print(f"FAIL  {breach}")
        return EXIT_GATE_FAILED
    return EXIT_OK


def _print_selftest_table(result, selftest) -> None:  # type: ignore[no-untyped-def]
    counts: dict[tuple[str, str, str], dict[str, int]] = {}
    for mutant in result.mutants:
        key = (mutant.expectation, mutant.operator, mutant.mode)
        tally = counts.setdefault(key, {})
        tally[mutant.outcome] = tally.get(mutant.outcome, 0) + 1

    print()
    print(f"  {'fault (must fail)':<22}{'mode':<14}{'injected':>9}{'caught':>8}{'missed':>8}")
    for (expectation, operator, mode), tally in counts.items():
        if expectation == selftest.DETECT:
            print(
                f"  {operator:<22}{mode:<14}{sum(tally.values()):>9}"
                f"{tally.get(selftest.CAUGHT, 0):>8}{tally.get(selftest.MISSED, 0):>8}"
            )
    print()
    print(f"  {'control (must not)':<22}{'mode':<14}{'injected':>9}{'fine':>8}{'alarms':>8}")
    for (expectation, operator, mode), tally in counts.items():
        if expectation == selftest.TOLERATE:
            print(
                f"  {operator:<22}{mode:<14}{sum(tally.values()):>9}"
                f"{tally.get(selftest.TOLERATED, 0):>8}{tally.get(selftest.FALSE_ALARM, 0):>8}"
            )

    wrong = [
        m
        for m in result.mutants
        if m.outcome in {selftest.MISSED, selftest.FALSE_ALARM, selftest.TOOL_ERROR}
    ]
    if wrong:
        print()
        for mutant in wrong[:40]:
            detail = mutant.error or "; ".join(mutant.findings) or "no findings"
            print(
                f"  {mutant.outcome.upper():<12}{mutant.mode:<14}{mutant.operator:<20}"
                f"{mutant.case_id:<10}{mutant.site}  ->  {mutant.verdict}: {detail}"
            )
        if len(wrong) > 40:
            print(f"  ... and {len(wrong) - 40} more; pass --json to see them all")


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
