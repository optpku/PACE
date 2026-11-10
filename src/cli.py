from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import sys
from pathlib import Path

from pace.contracts.problem import Problem, load_problem
from pace.orchestrator.recovery import RecoveryError, resume_existing_run
from pace.reporting.postmortem import postmortem_rescan
from pace.reporting.research_report import RESEARCH_OUTLINE_REF
from pace.reporting.theory_paper import PAPER_OUTLINE_REF
from pace.reporting.writing import active_writing_request
from pace.runtime.output.workspace import RunWorkspace, list_problems

from .pipeline import ProofPipeline
from .pipeline_types import DEFAULT_CALL_TIMEOUT_SECONDS, PipelineConfig


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    progress_logger = logging.getLogger("pace.runtime.output.events")
    handler = logging.StreamHandler()
    old_level = progress_logger.level
    progress_logger.addHandler(handler)
    progress_logger.setLevel(logging.INFO)
    previous_sigterm = None
    if threading.current_thread() is threading.main_thread():
        def interrupt(signum, frame):
            raise KeyboardInterrupt
        previous_sigterm = signal.signal(signal.SIGTERM, interrupt)
    try:
        try:
            return _main(arguments)
        except KeyboardInterrupt:
            print("已中断；可在原目录使用 pace resume 继续。", file=sys.stderr)
            return 130
        except Exception as exc:
            from pace.runtime.llm import CodexCallError

            if "--json" in arguments:
                error = exc.to_json() if isinstance(exc, CodexCallError) else {
                    "type": type(exc).__name__, "message": str(exc),
                }
                print(json.dumps({"event": "run_failed", "error": error},
                                 ensure_ascii=False), flush=True)
            print(str(exc), file=sys.stderr)
            return 2
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        progress_logger.removeHandler(handler)
        progress_logger.setLevel(old_level)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pace")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="run the pipeline on one self-contained UTF-8 text problem")
    run_parser.add_argument("--problem", required=True, type=Path)
    run_parser.add_argument("--problem-id", default="")
    add_common_args(run_parser)

    continue_parser = sub.add_parser("continue", help="start another exploration of an existing problem")
    continue_parser.add_argument("--run-dir", required=True, type=Path)
    continue_parser.add_argument("--message", required=True, type=Path)
    add_common_args(continue_parser)

    problems_parser = sub.add_parser("problems", help="list persistent problem workspaces")
    problems_parser.add_argument("--output", type=Path, default=Path("outputs"))
    problems_parser.add_argument("--json", action="store_true")

    resume_parser = sub.add_parser(
        "resume",
        help="continue one existing run in its original directory",
    )
    resume_parser.add_argument("--run-dir", required=True, type=Path)
    resume_parser.add_argument("--finalize", action="store_true",
                               help="finish research at a saved Director boundary and deliver current results")
    add_execution_args(resume_parser)
    resume_parser.set_defaults(model=None, reasoning_effort=None, web_search_mode=None)

    outline_parser = sub.add_parser("outline", help="generate a complete research report outline from an existing run")
    outline_parser.add_argument("--run-dir", required=True, type=Path)
    outline_parser.add_argument("--message", required=True, type=Path, help="UTF-8 writing request")
    outline_parser.add_argument("--requirements", type=Path, help="additional UTF-8 writing requirements")
    add_execution_args(outline_parser)
    outline_parser.set_defaults(model="gpt-5.6-sol", reasoning_effort="high")

    report_parser = sub.add_parser("report", help="write a complete research report from a selected outline")
    report_parser.add_argument("--run-dir", required=True, type=Path)
    report_parser.add_argument("--outline", default=RESEARCH_OUTLINE_REF, help="outline path relative to the run, or absolute")
    report_parser.add_argument("--message", type=Path, help="new UTF-8 report request; original outline requirements are retained")
    report_parser.add_argument("--requirements", type=Path, help="additional UTF-8 report requirements")
    add_execution_args(report_parser)
    report_parser.set_defaults(model="gpt-5.6-sol", reasoning_effort="high")

    for action, help_text in (
        ("paper-outline", "outline a theory paper from an existing run"),
        ("paper", "write a complete LaTeX theory paper from a selected outline"),
    ):
        paper_parser = sub.add_parser(action, help=help_text)
        paper_parser.add_argument("--run-dir", required=True, type=Path)
        paper_parser.add_argument("--message", type=Path, required=action == "paper-outline",
                                  help="UTF-8 writing request; original paper outline requirements are retained")
        paper_parser.add_argument("--requirements", type=Path, help="additional UTF-8 writing requirements")
        if action == "paper":
            paper_parser.add_argument("--outline", default=PAPER_OUTLINE_REF,
                                      help="paper outline path relative to the run, or absolute")
        add_execution_args(paper_parser)
        paper_parser.set_defaults(model="gpt-5.6-sol", reasoning_effort="high")

    postmortem_parser = sub.add_parser("postmortem", help="rescan a batch output directory and rewrite runner summary")
    postmortem_parser.add_argument("--batch-dir", required=True, type=Path)
    postmortem_parser.add_argument("--expected-problem", action="append", default=[])
    postmortem_parser.add_argument("--expected-problems-file", type=Path)
    activity = postmortem_parser.add_mutually_exclusive_group()
    activity.add_argument("--active", action="store_true", help="mark missing or unfinished runs as still active")
    activity.add_argument("--inactive", action="store_true", help="mark missing or unfinished runs as settled")

    index = sub.add_parser("index", help="rebuild factual navigation without model calls or evidence changes")
    index.add_argument("--run-dir", required=True, type=Path)

    doctor = sub.add_parser("doctor", help="check local executables without model inference")
    doctor.add_argument("--pdf", action="store_true", help="also compile a paper and check its fonts")
    doctor.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if getattr(args, "run_dir", None) is not None and args.command != "index":
        from pace.runtime.pinned_runtime import reexec_pinned_runtime

        reexec_pinned_runtime(args.run_dir, list(sys.argv[1:] if argv is None else argv))
    if args.command == "index":
        from pace.runtime.output.navigation import refresh_navigation
        refresh_navigation(args.run_dir.resolve(), rebuild=True)
        entry = args.run_dir.resolve() / "README.md"
        print(entry)
        return 0 if entry.is_file() else 2
    if args.command == "doctor":
        from pace.reporting.diagnostics import check_environment
        checks = check_environment(pdf=args.pdf)
        if args.json:
            print(json.dumps(checks, ensure_ascii=False))
        else:
            for row in checks["checks"]:
                print(f"{'OK' if row['ok'] else 'FAIL'} {row['name']}: {row['detail']}")
        return 0 if checks["ok"] else 2
    if args.command == "problems":
        rows = list_problems(args.output)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            for row in rows:
                print(f"{row['title']}\t{row['turn_id']}\t{row['status']}\t{row['run_dir']}")
        return 0
    if args.command == "postmortem":
        expected_problem_ids = list(args.expected_problem or [])
        if args.expected_problems_file:
            expected_problem_ids.extend(_read_expected_problem_ids(args.expected_problems_file))
        summary = postmortem_rescan(args.batch_dir, expected_problem_ids=expected_problem_ids, active=bool(args.active))
        summary_path = args.batch_dir / "_runner_summary.json"
        print(f"postmortem summary: {summary_path}; run_status={summary.get('run_status')}")
        return 0
    if args.command in {"run", "continue"}:
        run_dir = args.run_dir.expanduser().resolve() if args.command == "continue" else None
        config = make_config(args, output_root=run_dir.parent if run_dir is not None else None)
        problem = load_problem(
            args.message if run_dir is not None else args.problem,
            run_dir.name if run_dir is not None else args.problem_id or None,
        )
        print(f"开始运行：{problem.problem_id}；模型：{config.model}", file=sys.stderr, flush=True)
        result = ProofPipeline(config).run(problem, run_dir=run_dir,
            on_started=lambda ws: _started(ws, machine=args.json))
        print_result(result, machine=args.json)
        return _result_exit_code(result)
    if args.command in {"outline", "report", "paper-outline", "paper"}:
        run_dir = args.run_dir.expanduser().resolve()
        problem = load_problem(args.message, run_dir.name) if args.message else Problem(run_dir.name, "", "")
        requirements = args.requirements.read_text(encoding="utf-8") if args.requirements else ""
        output = ProofPipeline(make_config(args, output_root=run_dir.parent)).run(
            problem, run_dir=run_dir, writing_action=args.command,
            outline_ref=getattr(args, "outline", ""), additional_requirements=requirements,
        )
        print_writing_result(output, run_dir=run_dir, action=args.command, machine=args.json)
        return 0
    if args.command == "resume":
        run_dir = args.run_dir.expanduser().resolve()
        print(f"恢复运行：{run_dir}", file=sys.stderr, flush=True)
        try:
            ws = RunWorkspace.open_existing(run_dir)
            with ws.execution_lock():
                from pace.reporting.writing import release_completed_writing_task
                release_completed_writing_task(ws.root)
                writing = active_writing_request(ws.root)
                args.model = args.model or "gpt-5.6-sol"
                args.reasoning_effort = args.reasoning_effort or ("high" if writing else "xhigh")
                args.web_search_mode = args.web_search_mode or "on"
                pipeline = ProofPipeline(make_config(args, output_root=run_dir.parent))

                def callback(request):
                    return pipeline.resume(request.workspace, request.checkpoint, request.continuation_action,
                                           finalize=args.finalize)

                if writing:
                    result = resume_existing_run(run_dir, continue_pipeline=callback)
                else:
                    ws.finish_turn("running")
                    try:
                        _started(ws, machine=args.json)
                        result = resume_existing_run(run_dir, continue_pipeline=callback)
                    except BaseException:
                        ws.finish_turn("interrupted")
                        raise
                    ws.finish_turn("completed")
        except RecoveryError as exc:
            print(f"resume failed [{exc.code}]: {exc}", file=sys.stderr)
            return 2
        if isinstance(result, Path):
            print_writing_result(result, run_dir=run_dir, action=writing["action"], machine=args.json)
            return 0
        print_result(result, machine=args.json)
        return _result_exit_code(result)
    raise AssertionError(args.command)


def _result_exit_code(result) -> int:
    return 0 if result.run_status == "completed" else 2


def print_writing_result(output: Path, *, run_dir: Path, action: str, machine: bool) -> None:
    key = {
        "outline": "research_outline", "report": "research_report",
        "paper-outline": "paper_outline", "paper": "paper_source",
    }[action]
    details = {}
    if action == "paper":
        build = json.loads((output.parent / "build.json").read_text())
        details = {"paper_build": build["status"]}
        if output.with_name("references.bib").is_file():
            details["paper_bibliography"] = str(output.with_name("references.bib"))
        if build["status"] == "COMPLETE" and output.with_suffix(".pdf").is_file():
            details["paper_pdf"] = str(output.with_suffix(".pdf"))
    if machine:
        print(json.dumps({"event": f"{action.replace('-', '_')}_completed", "run_dir": str(run_dir),
                          "navigation": str(run_dir / "README.md"), key: str(output), **details},
                         ensure_ascii=False), flush=True)
    else:
        print(f"{key}={output}", flush=True)
        if details:
            print(f"paper_build={details['paper_build']}; diagnostics={output.parent / 'build.json'}", flush=True)


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", type=Path, default=Path("outputs"))
    parser.add_argument(
        "--resource-variant",
        choices=["standard", "weak"],
        default=None,
        help="override model/effort guidance defaults; resume uses the run snapshot",
    )
    parser.add_argument(
        "--max-proof-states",
        type=int,
        default=None,
        help="proof loop limit (default 5), or research proof-state cap; resume uses its saved limit",
    )
    parser.add_argument(
        "--mode",
        choices=["proof", "research"],
        default=None,
        help="proof: direct proving; research: dynamic research; omit to select during initialization",
    )
    parser.add_argument(
        "--max-calls", type=int, default=None, help="total semantic-call soft budget"
    )
    parser.add_argument(
        "--max-seconds", type=int, default=None, help="research wall-time soft budget"
    )
    add_execution_args(parser)



def add_execution_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit started and completed JSONL records")
    parser.add_argument("--profile", action="store_true", help="include per-step timing events in work/<turn>/events.jsonl")
    parser.add_argument("--provider", choices=["codex", "newapi", "llmmelon"], default="codex")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument(
        "--reasoning-effort", default="xhigh",
        help="model-supported effort; none disables explicit reasoning effort and summaries",
    )
    parser.add_argument(
        "--web-search-mode",
        choices=["off", "on"],
        default="on",
        help="on enables Survey retrieval only (default); off disables it; all other roles always have retrieval disabled",
    )
    parser.add_argument(
        "--web-search-model",
        default=None,
        help="model for web-search calls; defaults to --model",
    )
    parser.add_argument(
        "--web-search-reasoning-effort",
        default=None,
        help="reasoning effort for web-search calls; defaults to --reasoning-effort",
    )
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_CALL_TIMEOUT_SECONDS)



def make_config(
    args: argparse.Namespace,
    *,
    output_root: Path | None = None,
) -> PipelineConfig:
    return PipelineConfig(
        output_root=Path(output_root) if output_root is not None else args.output,
        mode=getattr(args, "mode", None),
        max_calls=getattr(args, "max_calls", None),
        max_seconds=getattr(args, "max_seconds", None),
        profile=bool(args.profile),
        provider=args.provider,
        model=args.model,
        resource_variant=getattr(args, "resource_variant", None),
        max_proof_states=getattr(args, "max_proof_states", None),
        reasoning_effort=args.reasoning_effort,
        web_search_mode=args.web_search_mode,
        web_search_model=getattr(args, "web_search_model", None) or args.model,
        web_search_reasoning_effort=(
            getattr(args, "web_search_reasoning_effort", None) or args.reasoning_effort
        ),
        timeout_seconds=args.timeout_seconds or DEFAULT_CALL_TIMEOUT_SECONDS,
    )



def _read_expected_problem_ids(path: Path) -> list[str]:
    rows: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        rows.append(text.split()[0].strip(","))
    return [row for row in rows if row]


def _started(ws: RunWorkspace, *, machine: bool) -> None:
    from pace.runtime.output.setup import load_setup

    policy_path = ws.path(ws.work_ref("state/research_execution_policy.json"))
    mode = (
        json.loads(policy_path.read_text()).get("mode")
        if policy_path.is_file()
        else (load_setup(ws.root) or {}).get("policy", {}).get("mode")
    )
    if machine:
        print(json.dumps({"event": "run_started", "execution_mode": mode, "problem_id": ws.problem_id,
            "run_dir": str(ws.root), "turn_id": ws.turn_id, "navigation": str(ws.root / "README.md")}, ensure_ascii=False), flush=True)
    else:
        print(f"{ws.problem_id} / {ws.turn_id} / {mode or 'selecting mode'}: {ws.root}", flush=True)


def print_result(result, *, machine: bool = False) -> None:
    from pace.runtime.output.results import verification_components

    terminal = json.loads(result.final_report_file.read_text(encoding="utf-8"))
    components = verification_components(result.run_dir, terminal)
    execution = {key: terminal[key] for key in (
        "execution_mode", "mode_source", "mode_reason", "proof_loops_used", "max_proof_loops",
    ) if key in terminal}
    publications = {
        "research_outline": str(result.outline_file) if result.outline_file else "",
        "research_report": str(result.research_report_file) if result.research_report_file else "",
        "paper_outline": str(result.paper_outline_file) if result.paper_outline_file else "",
        "paper_source": str(result.paper_source_file) if result.paper_source_file else "",
        "paper_pdf": str(result.paper_pdf_file) if result.paper_pdf_file else "",
    }
    if machine:
        error = {}
        if result.run_status == "completed_with_operational_error":
            report = json.loads(result.final_report_file.read_text(encoding="utf-8"))
            diagnostic = report.get("operational_diagnostics", {})
            code = report.get("operational_reason_code") or report.get("reason_code", "")
            error = {"error": {
                **diagnostic, "error_code": code,
                "message": diagnostic.get("message") or "; ".join(diagnostic.get("messages", ())) or code,
            }}
        print(json.dumps({"event": "run_completed", "problem_id": result.problem_id,
            "run_dir": str(result.run_dir), "final_report": str(result.final_report_file),
            "navigation": str(result.run_dir / "README.md"),
            "summary": str(result.report_file), **publications,
            "result": result.result, "run_status": result.run_status,
            "verification_components": components, **execution, **error}, ensure_ascii=False), flush=True)
        return
    profile = f"; profile={result.profile_file}" if getattr(result, "profile_file", None) else ""
    proof = str(result.proof_file) if result.proof_file else ""
    print(
        f"{result.problem_id}: result={result.result}; run_status={result.run_status}; "
        f"entry={result.run_dir / 'README.md'}; "
        f"proof={proof}; summary={result.report_file}; "
        + "; ".join(f"{key}={value}" for key, value in publications.items())
        + f"; final_report={result.final_report_file}{profile}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
