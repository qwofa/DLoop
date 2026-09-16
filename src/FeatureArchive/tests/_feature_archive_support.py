from __future__ import annotations

from contextlib import nullcontext, redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from typing import Callable, Mapping


CLI = Path(__file__).resolve().parents[1] / "feature_archive.py"
sys.path.insert(0, str(CLI.parent))

from archive_approvals import accepted_candidate_summary, last_accepted_candidate
import archive_snapshots
import archive_workspace
import feature_archive


def _svn_guard_test_double(workspace_root: Path) -> Mapping[str, object]:
    """为业务测试提供可漂移的隔离 SVN 状态，不启用产品回退。"""

    normalized_root = workspace_root.expanduser().resolve()
    entries = {}
    for path in sorted(normalized_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(normalized_root)
        if (
            {".git", ".svn", "__pycache__"}.intersection(relative.parts)
            or relative.as_posix().startswith(".scratch/dloop-v3/")
            or relative.name in {
                ".feature-archive-workspace-state.json",
                ".feature-archive-workspace-state.lock",
            }
        ):
            continue
        entries[relative.as_posix()] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    normalized_entries = {key: entries[key] for key in sorted(entries)}
    payload = {
        "source": "svn",
        "revision": "test-double-r1",
        "entries": normalized_entries,
    }
    payload["digest"] = "sha256:" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {"workspace_root": str(normalized_root), **payload}


def invoke_feature_archive(
    arguments: list[str],
    *,
    project_root: Path,
) -> subprocess.CompletedProcess[str]:
    """在临时项目位置调用当前公开命令合同。"""

    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with mock.patch.object(
            feature_archive,
            "project_root_from_entrypoint",
            return_value=project_root,
        ), mock.patch.object(
            archive_workspace,
            "workspace_guard_snapshot",
            side_effect=_svn_guard_test_double,
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            returncode = feature_archive.main(arguments)
    except SystemExit as exception:
        returncode = int(exception.code or 0)
    return subprocess.CompletedProcess(
        args=arguments,
        returncode=returncode,
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
    )


class FeatureArchiveCliTestCase(unittest.TestCase):
    real_snapshots = True

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self._project_root = self.base / "project"
        self.root = self._project_root / ".scratch" / "dloop-v3" / "v3.8.0" / "outputs"
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        if not self.real_snapshots:
            # 业务规则测试隔离历史写入；工作区保护、审批及事务仍使用真实实现。
            # Git/SVN、UI、命令行和快照集成测试保留默认的真实历史。
            for name, replacement in (
                ("snapshot_operation", lambda *args, **kwargs: nullcontext()),
                ("initialize_snapshots", lambda *args, **kwargs: None),
                ("register_snapshot_scopes", lambda *args, **kwargs: None),
            ):
                patcher = mock.patch.object(archive_snapshots, name, replacement)
                patcher.start()
                self.addCleanup(patcher.stop)

    @property
    def project_root(self) -> Path:
        return self._project_root

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(
        self,
        *arguments: object,
        expected: int = 0,
        project_root: Path | None = None,
    ):
        normalized = [str(item) for item in arguments]
        completed = invoke_feature_archive(
            normalized,
            project_root=project_root or self.project_root,
        )
        self.assertEqual(
            expected,
            completed.returncode,
            completed.stderr or completed.stdout,
        )
        stream = completed.stdout or completed.stderr
        return json.loads(stream)

    def run_help(self, command: str | None = None) -> str:
        arguments = ["python", str(CLI)]
        if command is not None:
            arguments.append(command)
        arguments.append("--help")
        completed = subprocess.run(
            arguments,
            text=True,
            capture_output=True,
            encoding="utf-8",
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return completed.stdout

    def init_complex(self, feature_id: str = "reliable-delivery") -> Path:
        self.run_cli(
            "init", "--feature-id", feature_id,
            "--title", "可靠交付",
        )
        return self.root / feature_id

    def set_status(self, path: Path, status: str) -> None:
        content = path.read_text(encoding="utf-8")
        content = content.replace("content_status: draft", f"content_status: {status}")
        path.write_text(content, encoding="utf-8")

    def approve_requirements(self, feature_id: str = "reliable-delivery") -> None:
        archive = self.root / feature_id
        self.set_status(archive / "01-requirements" / "terminology.md", "confirmed")
        self.set_status(archive / "01-requirements" / "README.md", "confirmed")
        self.run_cli(
            "stage-action", "--feature-id", feature_id,
            "--stage", "requirements", "--decision", "approve",
        )

    def prepare_execution_inputs(self, feature_id: str = "reliable-delivery") -> None:
        archive = self.root / feature_id
        self.set_status(archive / "03-design" / "README.md", "confirmed")
        self.set_status(archive / "04-plan" / "README.md", "completed")

    def artifact_path(self, feature_id: str, stage: str, name: str) -> Path:
        return self.root / feature_id / stage / name

    def write_integration_confirmation(
        self,
        *,
        feature_id: str = "reliable-delivery",
        **overrides: object,
    ) -> Path:
        archive = self.root / feature_id
        state = json.loads(
            archive.joinpath("workflow-state.json").read_text(encoding="utf-8")
        )
        execution = state["execution"]
        candidate_summary = accepted_candidate_summary(execution)
        accepted = last_accepted_candidate(execution)
        if accepted is None:
            raise AssertionError("集成确认只用于存在已接受实现候选的测试。")
        _, last_candidate = accepted
        report = {
            "result": "passed",
            "candidate_summary_digest": candidate_summary["digest"],
            "workspace_guard_digest": last_candidate["workspace_guard_digest"],
        }
        report.update(overrides)
        path = archive / "06-validation" / "integration-confirmation.json"
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def approve_final_with_confirmation(
        self,
        *,
        feature_id: str = "reliable-delivery",
        expected: int = 0,
        confirmation_overrides: dict[str, object] | None = None,
    ):
        confirmation = self.write_integration_confirmation(
            feature_id=feature_id,
            **(confirmation_overrides or {}),
        )
        return self.run_cli(
            "stage-action",
            "--feature-id",
            feature_id,
            "--stage",
            "final",
            "--decision",
            "approve",
            "--integration-confirmation",
            confirmation,
            expected=expected,
        )

    def accept_test_implementation(
        self,
        *,
        feature_id: str = "reliable-delivery",
    ) -> Path:
        return complete_test_implementation(
            self.run_cli,
            self.root,
            feature_id,
            self.workspace,
        )

    def record_checkpoint(
        self,
        execution_id: str,
        *,
        feature_id: str = "reliable-delivery",
        identifier: str = "final-checkpoint",
        status: str = "passed",
        observed_breakers: list[dict] | None = None,
        expected: int = 0,
    ):
        state = json.loads(
            self.root.joinpath(feature_id, "workflow-state.json").read_text(encoding="utf-8")
        )
        record = next(
            item for item in state["execution"]["slices"].values()
            if item.get("execution_id") == execution_id
        )
        methods = record["package"]["slice_contract"]["validation_methods"]
        value = {
            "contract_version": record["package"]["slice_contract"]["version"],
            "hypothesis": "当前契约事实仍成立",
            "change_summary": f"记录 {identifier} 的当前实现与完整验证结果",
            "validation_results": [
                {"method": method, "status": status, "evidence": [f"{method} 已执行并记录结果"]}
                for method in methods
            ],
            "discoveries": [],
            "next_step": "根据系统判定继续、提交候选或停止实施",
            "observed_breakers": observed_breakers or [],
        }
        path = (
            self.root
            / feature_id
            / "05-implementation"
            / f"{execution_id}-{identifier}.json"
        )
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return self.run_cli(
            "checkpoint-slice", "--feature-id", feature_id,
            "--execution-id", execution_id, "--checkpoint-file", path, expected=expected,
        )

    def write_package(
        self,
        package_id: str,
        *,
        scope: list[str] | None = None,
        legacy_context: bool = False,
        feature_id: str = "reliable-delivery",
        approve_plan: bool = True,
        validation_level: str = "targeted",
        validation_rationale: str = "当前变化只影响单一切片及其直接边界",
    ) -> Path:
        if approve_plan:
            self.ensure_approved_slice_plan(package_id, feature_id=feature_id)
        path = self.root / feature_id / "05-implementation" / f"{package_id}.json"
        non_goals = ["不处理相邻问题"]
        acceptance = ["结果可观察"]
        validations = ["执行聚焦验证"]
        rollback = "异常时保留现场并人工处理"
        contract = {
            "schema_version": 3,
            "version": 1,
            "revision_summary": "初始契约",
            "business_outcome": "完成当前切片",
            "acceptance_scenarios": acceptance,
            "in_scope": {
                "business_behaviors": ["完成当前切片约定的业务行为"],
                "data_responsibilities": ["只处理当前切片数据责任"],
                "system_boundaries": ["当前测试责任域"],
                "lifecycle": ["从启动到独立验收"],
            },
            "out_of_scope": non_goals,
            "dependency_assumptions": [],
            "expected_impact_areas": {
                "modules": [package_id],
                "resources": [],
                "data_boundaries": [],
            },
            "invariants": ["现有正常切片行为保持不变"],
            "validation_level": validation_level,
            "validation_rationale": validation_rationale,
            "validation_methods": validations,
            "unknowns": [],
            "rollback_point": rollback,
            "circuit_breaker_conditions": ["当前切片无法独立验收时熔断"],
            "decision_owner": "测试负责人",
            "approval_status": "approved",
        }
        contract_check = {
            criterion: {"passed": True, "evidence": "测试夹具提供明确证据"}
            for criterion in (
                "independently_acceptable",
                "strong_dependencies_available",
                "structural_unknowns_resolved",
                "impact_closed_loop",
                "observable_acceptance_covered",
                "regression_protection",
                "safe_rollback",
            )
        }
        path.write_text(
            json.dumps(
                {
                    "package_id": package_id,
                    "write_scope": scope if scope is not None else [f"{package_id}.txt"],
                    **({} if legacy_context else {"context_contract_version": 2}),
                    "context_materials": (
                        ["需求总览"]
                        if legacy_context
                        else [{
                            "source": f"{feature_id}.requirements.overview",
                            "purpose": "读取验收条件",
                            "mode": "sections",
                            "sections": ["当前摘要"],
                        }]
                    ),
                    "slice_contract": contract,
                    "contract_check": contract_check,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def ensure_approved_slice_plan(
        self,
        *slice_ids: str,
        feature_id: str = "reliable-delivery",
        prerequisites: dict[str, list[str]] | None = None,
        replacements: dict[str, list[str]] | None = None,
    ) -> dict:
        """显式检查并批准包含指定切片的最小版本化方案。"""

        state_path = self.root / feature_id / "workflow-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        plan_state = state["execution"]["slice_plan"]
        current_version = plan_state["current_version"]
        history = plan_state["history"]
        current = next(
            (item for item in history if item["version"] == current_version),
            None,
        )
        declarations = {
            item["slice_id"]: dict(item)
            for item in current["plan"]["slices"]
        } if current is not None else {}
        missing = [slice_id for slice_id in slice_ids if slice_id not in declarations]
        for slice_id in missing:
            declarations[slice_id] = {
                "slice_id": slice_id,
                "prerequisites": [],
                "replacements": [],
            }
        changed = bool(missing)
        for field, requested in (
            ("prerequisites", prerequisites or {}),
            ("replacements", replacements or {}),
        ):
            for slice_id, related in requested.items():
                declaration = declarations.get(slice_id)
                if declaration is None:
                    raise AssertionError(f"方案未声明切片 {slice_id}")
                normalized = sorted(set(related))
                if declaration[field] != normalized:
                    declaration[field] = normalized
                    changed = True
        if not changed:
            return current
        version = current_version + 1 if current_version is not None else 1
        plan_path = (
            self.root
            / feature_id
            / "04-plan"
            / f"{feature_id}-approved-plan-{version}.json"
        )
        plan_path.write_text(json.dumps({
            "schema_version": 1,
            "plan_id": feature_id,
            "version": version,
            "slices": list(declarations.values()),
        }, ensure_ascii=False), encoding="utf-8")
        checked = self.run_cli(
            "check-slice-plan", "--feature-id", feature_id, "--plan-file", plan_path,
        )
        self.run_cli(
            "approve-slice-plan", "--feature-id", feature_id, "--plan-file", plan_path,
            "--plan-digest", checked["plan_digest"],
        )
        return json.loads(state_path.read_text(encoding="utf-8"))["execution"]["slice_plan"]["history"][-1]


def complete_test_implementation(
    run_cli: Callable[..., Mapping[str, object]],
    archive_root: Path,
    feature_id: str,
    workspace_root: Path,
) -> Path:
    """为非实施主题的测试建立一个真实通过的最小实现候选。"""

    feature = archive_root / feature_id
    workspace_root.mkdir(parents=True, exist_ok=True)
    package_id = "test-implementation"
    execution_id = f"{feature_id}-implementation"
    review_execution_id = f"{feature_id}-review"

    plan_path = feature / "04-plan" / f"{package_id}-plan.json"
    plan_path.write_text(json.dumps({
        "schema_version": 1,
        "plan_id": feature_id,
        "version": 1,
        "slices": [{
            "slice_id": package_id,
            "prerequisites": [],
            "replacements": [],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    checked = run_cli(
        "check-slice-plan", "--feature-id", feature_id, "--plan-file", plan_path,
    )
    run_cli(
        "approve-slice-plan", "--feature-id", feature_id, "--plan-file", plan_path,
        "--plan-digest", checked["plan_digest"],
    )

    validation_method = "执行测试夹具聚焦验证"
    package_path = feature / "05-implementation" / f"{package_id}.json"
    package_path.write_text(json.dumps({
        "package_id": package_id,
        "write_scope": ["test-implementation.txt"],
        "context_contract_version": 2,
        "context_materials": [{
            "source": f"{feature_id}.requirements.overview",
            "purpose": "读取测试夹具验收条件",
            "mode": "sections",
            "sections": ["当前摘要"],
        }],
        "slice_contract": {
            "schema_version": 3,
            "version": 1,
            "revision_summary": "建立测试夹具实现候选",
            "business_outcome": "形成可供后续测试使用的已接受实现候选",
            "acceptance_scenarios": ["候选完成并通过独立评审"],
            "in_scope": {
                "business_behaviors": ["写入测试夹具实现结果"],
                "data_responsibilities": ["只处理测试夹具文件"],
                "system_boundaries": ["当前临时测试工作区"],
                "lifecycle": ["从启动到独立验收"],
            },
            "out_of_scope": ["不修改被测业务之外的文件"],
            "dependency_assumptions": [],
            "expected_impact_areas": {
                "modules": [package_id],
                "resources": [],
                "data_boundaries": [],
            },
            "invariants": ["被测流程语义保持不变"],
            "validation_level": "targeted",
            "validation_rationale": "只建立后续测试所需的最小真实候选",
            "validation_methods": [validation_method],
            "unknowns": [],
            "rollback_point": "删除测试临时目录",
            "circuit_breaker_conditions": ["候选无法独立验收时停止"],
            "decision_owner": "测试夹具",
            "approval_status": "approved",
        },
        "contract_check": {
            criterion: {"passed": True, "evidence": "测试夹具提供明确证据"}
            for criterion in (
                "independently_acceptable",
                "strong_dependencies_available",
                "structural_unknowns_resolved",
                "impact_closed_loop",
                "observable_acceptance_covered",
                "regression_protection",
                "safe_rollback",
            )
        },
    }, ensure_ascii=False), encoding="utf-8")
    run_cli(
        "start-slice", "--feature-id", feature_id,
        "--execution-id", execution_id, "--package-file", package_path,
        "--workspace-root", workspace_root,
    )
    workspace_root.joinpath("test-implementation.txt").write_text(
        "implemented", encoding="utf-8"
    )

    checkpoint_path = feature / "05-implementation" / f"{execution_id}-checkpoint.json"
    checkpoint_path.write_text(json.dumps({
        "contract_version": 1,
        "hypothesis": "测试夹具候选满足当前合同",
        "change_summary": "写入最小实现结果",
        "validation_results": [{
            "method": validation_method,
            "status": "passed",
            "evidence": ["测试夹具结果已检查"],
        }],
        "discoveries": [],
        "next_step": "提交候选",
        "observed_breakers": [],
    }, ensure_ascii=False), encoding="utf-8")
    run_cli(
        "checkpoint-slice", "--feature-id", feature_id,
        "--execution-id", execution_id, "--checkpoint-file", checkpoint_path,
    )
    candidate_id = f"{feature_id}-candidate"
    candidate_path = feature / "05-implementation" / f"{candidate_id}.json"
    candidate_path.write_text(json.dumps({
        "candidate_id": candidate_id,
        "verification": ["测试夹具聚焦验证通过"],
        "unverified_boundaries": [],
    }, ensure_ascii=False), encoding="utf-8")
    run_cli(
        "submit-slice", "--feature-id", feature_id,
        "--execution-id", execution_id, "--status", "completed",
        "--candidate-file", candidate_path,
    )
    run_cli(
        "review-slice", "--feature-id", feature_id,
        "--package-id", package_id, "--candidate-id", candidate_id,
        "--review-execution-id", review_execution_id, "--result", "passed",
    )

    state = json.loads(feature.joinpath("workflow-state.json").read_text(encoding="utf-8"))
    summary = accepted_candidate_summary(state["execution"])
    accepted = last_accepted_candidate(state["execution"])
    if accepted is None:
        raise AssertionError("测试夹具未形成已接受候选。")
    confirmation_path = feature / "06-validation" / "integration-confirmation.json"
    confirmation_path.write_text(json.dumps({
        "result": "passed",
        "candidate_summary_digest": summary["digest"],
        "workspace_guard_digest": accepted[1]["workspace_guard_digest"],
    }, ensure_ascii=False), encoding="utf-8")
    return confirmation_path


def write_candidate(path: Path, candidate_id: str) -> Path:
    value = {
        "candidate_id": candidate_id,
        "verification": ["聚焦验证通过"],
        "unverified_boundaries": [],
    }
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def write_issues(path: Path, *issues: str) -> Path:
    path.write_text(json.dumps({"issues": list(issues)}, ensure_ascii=False), encoding="utf-8")
    return path


def write_review_verification(path: Path, *verification: str) -> Path:
    path.write_text(
        json.dumps({"verification": list(verification)}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path
