"""规范档案定位与只读分享快照回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

try:
    from _feature_archive_support import FeatureArchiveCliTestCase
except ModuleNotFoundError:
    from ._feature_archive_support import FeatureArchiveCliTestCase
import archive_share
import archive_lifecycle
import feature_archive
from archive_paths import ArchivePathError, project_root_from_entrypoint
from archive_workspace import workspace_state_lock

SOURCE_TOOL_ROOT = Path(__file__).resolve().parents[1]


class InstalledPathContractTests(unittest.TestCase):
    PUBLIC_COMMANDS = (
        "workflow-rules", "init", "ui-investigate", "ui-publish", "validate",
        "workflow-status", "export-share", "context-summary", "stage-action",
        "check-slice-plan", "approve-slice-plan", "prepare-slice-contract",
        "check-slice-contract", "start-slice", "checkpoint-slice", "submit-slice",
        "review-slice", "resolve-slice", "audit", "rebuild-indexes",
        "confirm-change", "refresh-queue", "assert-fresh", "rename-term",
        "analyze-impact", "transition-lifecycle", "detect-cleanup", "purge",
    )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        self.tool_root = self.project / "Tools" / "FeatureArchive"
        shutil.copytree(SOURCE_TOOL_ROOT, self.tool_root)
        self.cli = self.tool_root / "feature_archive.py"
        self.cwd = self.base / "unrelated-working-directory"
        self.cwd.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: object, expected: int = 0) -> dict:
        completed = subprocess.run(
            [sys.executable, str(self.cli), *(str(item) for item in arguments)],
            cwd=self.cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(expected, completed.returncode, completed.stderr or completed.stdout)
        return json.loads(completed.stdout or completed.stderr)

    def init_feature(self) -> Path:
        result = self.run_cli(
            "init",
            "--feature-id",
            "canonical-paths",
            "--title",
            "规范路径",
        )
        expected = (
            self.project
            / ".scratch"
            / "dloop-v3"
            / "v3.9.3"
            / "outputs"
            / "canonical-paths"
        )
        self.assertEqual(expected.resolve().as_posix(), result["archive_location"]["feature_root"])
        return expected

    def test_public_commands_derive_the_archive_root_from_the_installed_entrypoint(self) -> None:
        feature = self.init_feature()

        self.assertTrue(feature.joinpath("workflow-state.json").is_file())
        help_result = subprocess.run(
            [sys.executable, str(self.cli), "workflow-status", "--help"],
            cwd=self.cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, help_result.returncode, help_result.stderr)
        self.assertNotIn("--root", help_result.stdout)

        rejected = subprocess.run(
            [
                sys.executable,
                str(self.cli),
                "workflow-status",
                "--root",
                str(self.project / ".scratch" / "outputs" / "dloop-v3"),
            ],
            cwd=self.cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(2, rejected.returncode)
        self.assertIn("不能由调用者指定", rejected.stderr)

        rejected_equals = subprocess.run(
            [
                sys.executable,
                str(self.cli),
                "workflow-status",
                f"--root={self.base / 'other-root'}",
            ],
            cwd=self.cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(2, rejected_equals.returncode)
        self.assertIn("不能由调用者指定", rejected_equals.stderr)

        rejected_abbreviation = subprocess.run(
            [sys.executable, str(self.cli), "workflow-status", "--roo", "ignored"],
            cwd=self.cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(2, rejected_abbreviation.returncode)
        self.assertIn("unrecognized arguments", rejected_abbreviation.stderr)

    def test_source_tree_is_not_a_supported_project_entrypoint(self) -> None:
        with self.assertRaises(ArchivePathError) as raised:
            project_root_from_entrypoint(SOURCE_TOOL_ROOT / "feature_archive.py")

        self.assertEqual("PROJECT_ROOT_UNAVAILABLE", raised.exception.code)

    def test_relative_artifact_paths_are_resolved_from_the_project_root(self) -> None:
        feature = self.init_feature()
        plan = feature / "04-plan" / "slice-plan.json"
        plan.write_text(
            json.dumps({
                "schema_version": 1,
                "plan_id": "canonical-paths",
                "version": 1,
                "slices": [{
                    "slice_id": "slice-1",
                    "prerequisites": [],
                    "replacements": [],
                }],
            }),
            encoding="utf-8",
        )

        result = self.run_cli(
            "check-slice-plan",
            "--feature-id",
            "canonical-paths",
            "--plan-file",
            plan.relative_to(self.project),
        )

        self.assertEqual("valid", result["status"])
        self.assertEqual(1, result["slice_count"])

    def test_every_public_command_help_omits_removed_root_arguments(self) -> None:
        for command in self.PUBLIC_COMMANDS:
            with self.subTest(command=command):
                completed = subprocess.run(
                    [sys.executable, str(self.cli), command, "--help"],
                    cwd=self.cwd,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertNotIn("--root", completed.stdout)
                self.assertNotIn("--project-root", completed.stdout)

    def test_share_and_other_external_files_cannot_drive_the_workflow(self) -> None:
        self.init_feature()
        share = self.base / "copied-share"
        share.mkdir()
        share.joinpath(".dloop-share.json").write_text(
            json.dumps({
                "kind": "dloop-share-snapshot",
                "feature_id": "canonical-paths",
                "exported_at": "2026-08-31T00:00:00Z",
                "source_state_digest": "sha256:" + "1" * 64,
            }),
            encoding="utf-8",
        )
        shared_package = share / "package.json"
        shared_package.write_text("{}", encoding="utf-8")

        rejected_share = self.run_cli(
            "prepare-slice-contract",
            "--feature-id",
            "canonical-paths",
            "--package-file",
            shared_package,
            expected=1,
        )
        self.assertEqual("SHARE_SNAPSHOT_NOT_EXECUTABLE", rejected_share["code"])
        self.assertEqual("canonical", rejected_share["archive_location"]["kind"])
        self.assertEqual("canonical-paths", rejected_share["share_snapshot"]["feature_id"])
        self.assertEqual(
            "2026-08-31T00:00:00Z",
            rejected_share["share_snapshot"]["exported_at"],
        )
        self.assertEqual(
            "05-implementation/package.json",
            rejected_share["canonical_target"]["relative_path"],
        )
        self.assertEqual(
            (
                self.project
                / ".scratch"
                / "dloop-v3"
                / "v3.9.3"
                / "outputs"
                / "canonical-paths"
                / "05-implementation"
                / "package.json"
            ).resolve().as_posix(),
            rejected_share["canonical_target"]["path"],
        )
        self.assertEqual("use-canonical-artifact", rejected_share["recovery"]["action"])

        external = self.base / "external-package.json"
        external.write_text("{}", encoding="utf-8")
        rejected_external = self.run_cli(
            "prepare-slice-contract",
            "--feature-id",
            "canonical-paths",
            "--package-file",
            external,
            expected=1,
        )
        self.assertEqual("NON_CANONICAL_WORKFLOW_ARTIFACT", rejected_external["code"])

    def test_invalid_share_manifest_is_stably_rejected(self) -> None:
        self.init_feature()
        share = self.base / "invalid-share"
        share.mkdir()
        share.joinpath(".dloop-share.json").write_text("{", encoding="utf-8")
        shared_package = share / "package.json"
        shared_package.write_text("{}", encoding="utf-8")

        rejected = self.run_cli(
            "prepare-slice-contract",
            "--feature-id",
            "canonical-paths",
            "--package-file",
            shared_package,
            expected=1,
        )

        self.assertEqual("SHARE_SNAPSHOT_NOT_EXECUTABLE", rejected["code"])
        self.assertEqual(
            "05-implementation/package.json",
            rejected["canonical_target"]["relative_path"],
        )
        self.assertIsNone(rejected["share_snapshot"]["exported_at"])

    def test_global_commands_do_not_add_a_feature_location(self) -> None:
        self.init_feature()

        validated = self.run_cli("validate")
        status = self.run_cli("workflow-status")

        self.assertNotIn("archive_location", validated)
        self.assertNotIn("archive_location", status)

    def test_share_identity_never_overrides_the_current_feature_recovery_target(self) -> None:
        self.init_feature()
        share = self.base / "other-feature-share"
        share.mkdir()
        share.joinpath(".dloop-share.json").write_text(
            json.dumps({
                "kind": "dloop-share-snapshot",
                "feature_id": "another-feature",
                "exported_at": "2026-08-31T00:00:00Z",
                "source_state_digest": "sha256:" + "2" * 64,
            }),
            encoding="utf-8",
        )
        package = share / "package.json"
        package.write_text("{}", encoding="utf-8")

        rejected = self.run_cli(
            "prepare-slice-contract",
            "--feature-id",
            "canonical-paths",
            "--package-file",
            package,
            expected=1,
        )

        self.assertEqual("another-feature", rejected["share_snapshot"]["feature_id"])
        self.assertIn(
            "/outputs/canonical-paths/05-implementation/package.json",
            rejected["canonical_target"]["path"],
        )

    def test_exported_share_is_identified_and_cannot_be_executed(self) -> None:
        feature = self.init_feature()
        feature.joinpath("04-plan", "evidence.png").write_bytes(b"image evidence")

        exported = self.run_cli("export-share", "--feature-id", "canonical-paths")
        share = Path(exported["share_location"]["path"])
        manifest = json.loads(
            share.joinpath(".dloop-share.json").read_text(encoding="utf-8")
        )

        self.assertEqual("dloop-share-snapshot", manifest["kind"])
        self.assertEqual("3.9.3", manifest["workflow_version"])
        self.assertRegex(exported["snapshot_id"], r"^\d{8}T\d{12}Z-[0-9a-f]{8}$")
        self.assertTrue(manifest["read_only"])
        self.assertFalse(manifest["executable"])
        self.assertTrue(share.joinpath("delivery-status.json").is_file())
        delivery_status = json.loads(
            share.joinpath("delivery-status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {"schema_version", "source_kind", "feature_id", "delivery_status"},
            set(delivery_status),
        )
        self.assertEqual(
            {
                "current_stage",
                "can_advance",
                "blockers",
                "requires_human",
                "approval_statuses",
                "slice_statuses",
                "accepted_candidate_count",
                "delivery_summary",
            },
            set(delivery_status["delivery_status"]),
        )
        self.assertTrue(share.joinpath("friction.jsonl").is_file())
        self.assertTrue(share.joinpath("04-plan", "evidence.png").is_file())
        self.assertFalse(share.joinpath("workflow-state.json").exists())
        self.assertFalse(share.joinpath(".dloop-root.json").exists())

        package = share / "05-implementation" / "package.json"
        package.write_text("{}", encoding="utf-8")
        rejected = self.run_cli(
            "prepare-slice-contract",
            "--feature-id",
            "canonical-paths",
            "--package-file",
            package,
            expected=1,
        )
        self.assertEqual("SHARE_SNAPSHOT_NOT_EXECUTABLE", rejected["code"])

    def test_share_material_gets_the_explicit_non_executable_diagnostic(self) -> None:
        self.init_feature()
        exported = self.run_cli("export-share", "--feature-id", "canonical-paths")
        shared_requirements = (
            Path(exported["share_location"]["path"])
            / "01-requirements"
            / "README.md"
        )

        rejected = self.run_cli(
            "context-summary",
            "--feature-id",
            "canonical-paths",
            "--action",
            "requirements",
            "--role",
            "cold-read",
            "--entry-question",
            "核对分享材料",
            "--allowed-material",
            shared_requirements,
        )

        reasons = rejected["role_blocker"]["reasons"]
        self.assertIn("SHARE_SNAPSHOT_NOT_EXECUTABLE", [item["code"] for item in reasons])
        reason = next(
            item for item in reasons
            if item["code"] == "SHARE_SNAPSHOT_NOT_EXECUTABLE"
        )
        self.assertEqual(
            "01-requirements/README.md",
            reason["canonical_target"]["relative_path"],
        )
        self.assertEqual(
            exported["share_location"]["source_state_digest"],
            reason["share_snapshot"]["source_state_digest"],
        )
        self.assertEqual("use-canonical-artifact", reason["recovery"]["action"])

        relative_rejected = self.run_cli(
            "context-summary",
            "--feature-id",
            "canonical-paths",
            "--action",
            "requirements",
            "--role",
            "cold-read",
            "--entry-question",
            "核对项目相对分享材料",
            "--allowed-material",
            shared_requirements.relative_to(
                Path(exported["archive_location"]["project_root"])
            ),
        )
        relative_reason = next(
            item for item in relative_rejected["role_blocker"]["reasons"]
            if item["code"] == "SHARE_SNAPSHOT_NOT_EXECUTABLE"
        )
        self.assertEqual(
            "01-requirements/README.md",
            relative_reason["canonical_target"]["relative_path"],
        )
        self.assertEqual(
            exported["share_location"]["source_state_digest"],
            relative_reason["share_snapshot"]["source_state_digest"],
        )


class ShareConsistencyTests(FeatureArchiveCliTestCase):
    real_snapshots = False

    def test_lifecycle_commit_waits_for_the_share_publication_lock(self) -> None:
        self.init_complex("share-publication-lock")
        commit_started = threading.Event()
        commit_finished = threading.Event()
        failure: list[Exception] = []
        original_replace = archive_lifecycle._replace_files_atomically

        def observed_replace(*arguments, **keyword_arguments) -> None:
            try:
                original_replace(*arguments, **keyword_arguments)
            finally:
                commit_finished.set()

        def transition() -> None:
            try:
                commit_started.set()
                feature_archive.transition_lifecycle(
                    self.root,
                    "share-publication-lock",
                    "active",
                    "2026-08-31T00:00:00Z",
                )
            except Exception as exception:
                failure.append(exception)

        with mock.patch.object(
            archive_lifecycle,
            "_replace_files_atomically",
            side_effect=observed_replace,
        ):
            with workspace_state_lock(self.root):
                worker = threading.Thread(target=transition)
                worker.start()
                commit_started_in_time = commit_started.wait(timeout=5)
                commit_was_blocked = (
                    commit_started_in_time
                    and not commit_finished.wait(timeout=0.1)
                )
            worker.join(timeout=5)

        self.assertTrue(commit_started_in_time)
        self.assertTrue(commit_was_blocked)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], failure)
        self.assertTrue(commit_finished.is_set())

    def test_export_refuses_a_source_state_change_before_publication(self) -> None:
        archive = self.init_complex("share-source-change")
        self.set_status(archive / "01-requirements" / "terminology.md", "confirmed")
        self.set_status(archive / "01-requirements" / "README.md", "confirmed")
        original_write = archive_share._write_staging_snapshot

        def write_then_advance(*arguments, **keyword_arguments) -> None:
            original_write(*arguments, **keyword_arguments)
            feature_archive.stage_action(
                self.root,
                "share-source-change",
                "requirements",
                "approve",
            )

        with mock.patch.object(
            archive_share,
            "_write_staging_snapshot",
            side_effect=write_then_advance,
        ):
            rejected = self.run_cli(
                "export-share",
                "--feature-id",
                "share-source-change",
                expected=1,
            )

        self.assertEqual("SHARE_SOURCE_CHANGED", rejected["code"])
        share_root = (
            self.project_root
            / ".scratch"
            / "dloop-v3"
            / "v3.9.3"
            / "shares"
            / "share-source-change"
        )
        self.assertTrue(share_root.is_dir())
        self.assertEqual([], list(share_root.iterdir()))

    def test_export_refuses_a_lifecycle_change_before_publication(self) -> None:
        self.init_complex("share-lifecycle-change")
        original_write = archive_share._write_staging_snapshot

        def write_then_transition(*arguments, **keyword_arguments) -> None:
            original_write(*arguments, **keyword_arguments)
            feature_archive.transition_lifecycle(
                self.root,
                "share-lifecycle-change",
                "active",
                "2026-08-31T00:00:00Z",
            )

        with mock.patch.object(
            archive_share,
            "_write_staging_snapshot",
            side_effect=write_then_transition,
        ):
            rejected = self.run_cli(
                "export-share",
                "--feature-id",
                "share-lifecycle-change",
                expected=1,
            )

        self.assertEqual("SHARE_SOURCE_CHANGED", rejected["code"])
        share_root = (
            self.project_root
            / ".scratch"
            / "dloop-v3"
            / "v3.9.3"
            / "shares"
            / "share-lifecycle-change"
        )
        self.assertTrue(share_root.is_dir())
        self.assertEqual([], list(share_root.iterdir()))


class RoleWriteTargetTests(FeatureArchiveCliTestCase):
    real_snapshots = False

    def test_role_projection_returns_the_canonical_stage_overview_target(self) -> None:
        archive = self.init_complex("write-targets")

        result = self.run_cli(
            "context-summary",
            "--feature-id",
            "write-targets",
            "--action",
            "requirements",
            "--role",
            "coordinator",
        )

        target = result["role_view"]["write_targets"]
        self.assertEqual(1, len(target))
        self.assertEqual("01-requirements/README.md", target[0]["relative_path"])
        self.assertEqual(
            archive.joinpath("01-requirements", "README.md").resolve().as_posix(),
            target[0]["path"],
        )

    def test_blocked_or_read_only_actions_do_not_expose_write_targets(self) -> None:
        self.init_complex("write-targets")

        blocked = self.run_cli(
            "context-summary",
            "--feature-id",
            "write-targets",
            "--action",
            "design",
            "--role",
            "coordinator",
        )

        self.assertTrue(blocked["blockers"])
        self.assertEqual([], blocked["role_view"]["write_targets"])

    def test_future_stage_does_not_expose_a_write_target(self) -> None:
        self.init_complex("write-targets")
        self.approve_requirements("write-targets")
        self.run_cli(
            "transition-lifecycle",
            "--feature-id",
            "write-targets",
            "--to",
            "active",
        )

        future = self.run_cli(
            "context-summary",
            "--feature-id",
            "write-targets",
            "--action",
            "plan",
            "--role",
            "coordinator",
        )
        current = self.run_cli(
            "context-summary",
            "--feature-id",
            "write-targets",
            "--action",
            "design",
            "--role",
            "coordinator",
        )

        self.assertEqual([], future["blockers"])
        self.assertEqual("design", future["delivery_view"]["current_stage"])
        self.assertEqual(
            ["APPROVAL_DOCUMENT_NOT_READY"],
            [item["code"] for item in future["delivery_view"]["blockers"]],
        )
        self.assertEqual([], future["role_view"]["write_targets"])
        self.assertEqual(
            "03-design/README.md",
            current["role_view"]["write_targets"][0]["relative_path"],
        )

if __name__ == "__main__":
    unittest.main()
