from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from dloop_eval import (
    compare_reports,
    grade_run,
    prepare_run,
    render_comparison,
    render_report,
)
from dloop_eval.core import EvalContractError


EVAL_ROOT = Path(__file__).resolve().parent


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="准备、判分和比较 DLoop 行为评测")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="建立隔离运行包和空 trace")
    prepare.add_argument("--variant", required=True)
    prepare.add_argument("--skill-root", type=Path, required=True)
    prepare.add_argument("--run-dir", type=Path, required=True)
    prepare.add_argument("--include-holdout", action="store_true")

    grade = commands.add_parser("grade", help="校验 trace 并生成报告")
    grade.add_argument("--run-dir", type=Path, required=True)
    grade.add_argument("--json-report", type=Path, required=True)
    grade.add_argument("--markdown-report", type=Path, required=True)

    compare = commands.add_parser("compare", help="比较两份已判分报告")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument("--markdown-output", type=Path, required=True)
    return parser


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            result = prepare_run(
                EVAL_ROOT,
                arguments.skill_root.resolve(),
                arguments.run_dir.resolve(),
                variant=arguments.variant,
                include_holdout=arguments.include_holdout,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if arguments.command == "grade":
            report = grade_run(EVAL_ROOT, arguments.run_dir.resolve())
            _write(arguments.json_report, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            _write(arguments.markdown_report, render_report(report))
            print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
            return 0
        baseline = json.loads(arguments.baseline.read_text(encoding="utf-8"))
        candidate = json.loads(arguments.candidate.read_text(encoding="utf-8"))
        comparison = compare_reports(baseline, candidate)
        _write(arguments.output, json.dumps(comparison, ensure_ascii=False, indent=2) + "\n")
        _write(arguments.markdown_output, render_comparison(comparison))
        print(json.dumps(comparison, ensure_ascii=False, indent=2))
        return 0
    except EvalContractError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
