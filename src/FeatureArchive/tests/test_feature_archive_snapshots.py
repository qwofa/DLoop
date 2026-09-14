"""真实仓库和公开工作流中的保存、阶段恢复、撤回及中断恢复。"""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archive_snapshots as snapshots
import feature_archive
from archive_initialization import initialize_archive
from archive_paths import canonical_archive_root
from archive_workspace import ArchiveWorkspaceError, active_modification_lease, acquire_modification_lease, release_modification_lease
from _feature_archive_support import FeatureArchiveCliTestCase, write_candidate
from test_feature_archive_git import GitProjectTestCase


class SnapshotTests(GitProjectTestCase):
    def setUp(self):
        super().setUp()
        self.root = canonical_archive_root(self.project)
        self.feature = self.root / "delivery"
        initialize_archive(self.root, "delivery", "快照交付")
        self.helper = FeatureArchiveCliTestCase()
        self.helper.root = self.root
        self.helper.workspace = self.project
        self.helper._project_root = self.project
        self.helper.run_cli = self.cli

    def cli(self, *arguments, expected=0, **options):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(feature_archive, "project_root_from_entrypoint", return_value=self.project), redirect_stdout(out), redirect_stderr(err):
            code = feature_archive.main([str(value) for value in arguments])
        self.assertEqual(expected, code, err.getvalue() or out.getvalue())
        return json.loads(out.getvalue() or err.getvalue())

    def prepare(self):
        self.helper.approve_requirements("delivery")
        self.helper.prepare_execution_inputs("delivery")

    def start(self, package="slice", execution="impl", scope=None):
        path = self.helper.write_package(package, feature_id="delivery", scope=scope or ["main.txt", "new.bin"])
        return self.cli("start-slice", "--feature-id", "delivery", "--execution-id", execution,
                        "--package-file", path, "--workspace-root", self.project)

    def save(self, **options):
        return snapshots.save_snapshot(self.root, "delivery", **options)["snapshot"]["id"]

    def test_original_bytes_deletions_late_scopes_and_index_survive_round_trip(self):
        main = self.project / "main.txt"
        main.write_bytes(b"staged\r\n")
        self.git("add", "main.txt")
        main.write_bytes(b"preexisting\r\n")
        before_index = (self.project / ".git/index").read_bytes()
        before_head = self.git("rev-parse", "HEAD")
        self.prepare()
        self.start()
        first = self.save(name="起点")
        main.write_bytes(b"first\r\n")
        (self.project / "new.bin").write_bytes(b"\x00\xff\x01")
        checkpoint = self.save(name="二进制中间点", request_id="manual-1")
        self.assertEqual(checkpoint, self.save(name="二进制中间点", request_id="manual-1"))
        main.unlink()
        (self.project / "new.bin").write_bytes(b"replacement")
        self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", checkpoint, "--execute")
        self.assertEqual(b"first\r\n", main.read_bytes())
        self.assertEqual(b"\x00\xff\x01", (self.project / "new.bin").read_bytes())
        self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", first, "--execute")
        self.assertEqual(b"preexisting\r\n", main.read_bytes())
        self.assertFalse((self.project / "new.bin").exists())
        self.assertEqual(before_index, (self.project / ".git/index").read_bytes())
        self.assertEqual(before_head, self.git("rev-parse", "HEAD"))
        late = self.project / "later.txt"
        late.write_bytes(b"existing before second scope")
        acquire_modification_lease(self.root, "delivery", "second", self.project, ["later.txt"], "now")
        late.write_bytes(b"second changed")
        self.save()
        self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", checkpoint, "--execute")
        self.assertEqual(b"existing before second scope", late.read_bytes())

    def test_managed_dependency_locks_export_and_restore_original_bytes(self):
        self.prepare()
        before_scope = self.save(name="纳入依赖文件之前")
        baseline = {"uv.lock": b"original\r\n", "中文 deps/Cargo.lock": b"dependencies\n"}
        for path, content in baseline.items():
            target = self.project / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.start(scope=["uv.lock", "中文 deps", "new.lock"])
        point = self.save(name="依赖基线")
        for path in baseline:
            (self.project / path).write_bytes(b"changed dependencies\r\n")
        (self.project / "new.lock").write_bytes(b"new dependencies")
        changed = self.save(name="依赖修改")
        destination = self.base / "dependency export"
        self.cli("snapshot-export", "--feature-id", "delivery", "--snapshot-id", point, "--destination", destination)
        for path, content in baseline.items():
            self.assertEqual(content, (destination / "workspace" / path).read_bytes())
        self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", point, "--execute")
        for path, content in baseline.items():
            self.assertEqual(content, (self.project / path).read_bytes())
        self.assertFalse((self.project / "new.lock").exists())
        self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", changed, "--execute")
        self.assertEqual(b"new dependencies", (self.project / "new.lock").read_bytes())
        self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", before_scope, "--execute")
        for path, content in baseline.items():
            self.assertEqual(content, (self.project / path).read_bytes())
        self.assertFalse((self.project / "new.lock").exists())

    def test_managed_assets_with_cache_directory_names_survive_restore(self):
        self.prepare()
        baseline = {
            "Assets/Models/obj/player.obj": b"v 0 0 0\nv 1 0 0\n",
            "Assets/Models/obj/player.obj.meta": b"guid: model\r\n",
            "Assets/Library/data.bin": b"\x00\xfflibrary",
            "Assets/Temp/data.bin": b"\xff\x00temporary-theme",
        }
        for path, content in baseline.items():
            target = self.project / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.start(scope=["Assets"])
        first = self.save(name="资源起点")
        for path in baseline:
            (self.project / path).write_bytes(b"changed resource")
        added = self.project / "Assets/Models/obj/new.obj"
        added.write_bytes(b"new model")
        second = self.save(name="修改后的资源")
        destination = self.base / "managed assets export"
        self.cli("snapshot-export", "--feature-id", "delivery", "--snapshot-id", first, "--destination", destination)
        for path, content in baseline.items():
            self.assertEqual(content, (destination / "workspace" / path).read_bytes())
        self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", first, "--execute")
        for path, content in baseline.items():
            self.assertEqual(content, (self.project / path).read_bytes())
        self.assertFalse(added.exists())
        self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", second, "--execute")
        for path in baseline:
            self.assertEqual(b"changed resource", (self.project / path).read_bytes())
        self.assertEqual(b"new model", added.read_bytes())

    def test_restore_pending_candidate_restarts_identity_and_keeps_full_slice_diff(self):
        self.prepare()
        self.start()
        (self.project / "main.txt").write_text("implemented", encoding="utf-8")
        self.helper.record_checkpoint("impl", feature_id="delivery")
        candidate = self.feature / "05-implementation/candidate.json"
        write_candidate(candidate, "candidate-1")
        submitted = self.cli("submit-slice", "--feature-id", "delivery", "--execution-id", "impl", "--status", "completed", "--candidate-file", candidate)
        point = self.save(name="待评审")
        restored = self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", point, "--execute")
        self.assertEqual(["slice"], restored["affected_slices"])
        self.assertIsNone(active_modification_lease(self.root))
        self.cli("start-slice", "--feature-id", "delivery", "--execution-id", "impl", "--package-id", "slice", "--workspace-root", self.project, expected=1)
        self.cli("start-slice", "--feature-id", "delivery", "--execution-id", "impl-2", "--package-id", "slice", "--workspace-root", self.project)
        self.helper.record_checkpoint("impl-2", feature_id="delivery")
        again = self.cli("submit-slice", "--feature-id", "delivery", "--execution-id", "impl-2", "--status", "completed", "--candidate-file", candidate)
        self.assertEqual(submitted["changes"], again["changes"])
        history = self.cli("snapshot-list", "--feature-id", "delivery")
        self.assertGreater(history["count"], 5)
        self.assertIn("checkpoint", {item["reason"] for item in history["snapshots"]})

    def test_stage_rework_preserves_old_versions_and_resets_later_approvals(self):
        point = self.save(reason="stage-start", stage="requirements", name="需求开始")
        self.prepare()
        document = self.feature / "01-requirements/README.md"
        confirmed = document.read_bytes()
        preview = self.cli("snapshot-restore", "--feature-id", "delivery", "--stage", "requirements")
        self.assertEqual(confirmed, document.read_bytes())
        self.assertEqual(point, preview["snapshot"]["id"])
        result = self.cli("snapshot-restore", "--feature-id", "delivery", "--stage", "requirements", "--execute")
        self.assertIn(b"content_status: draft", document.read_bytes())
        state = json.loads((self.feature / "workflow-state.json").read_bytes())
        self.assertIsNone(state["approvals"]["requirements"])
        self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", result["backup_snapshot"], "--execute")
        self.assertEqual(confirmed, document.read_bytes())

    def revise_scope(self, scopes):
        package = self.helper.write_package("slice", feature_id="delivery", scope=scopes)
        value = json.loads(package.read_bytes())
        value["slice_contract"].update(version=2, revision_summary="调整恢复后的授权范围")
        package.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        result = self.cli("check-slice-contract", "--feature-id", "delivery", "--package-file", package)
        self.assertEqual("ready", result["status"])

    def test_expanded_restored_scope_preserves_full_candidate_and_abort_baselines(self):
        outside = self.project / "outside.txt"
        outside.write_bytes(b"staged\r\n")
        self.git("add", "outside.txt")
        outside.write_bytes(b"preexisting\r\n")
        binary = self.project / "中文 deps/data.bin"
        binary.parent.mkdir()
        binary.write_bytes(b"\x00\xffbaseline")
        index = (self.project / ".git/index").read_bytes()
        main = self.project / "main.txt"
        baseline = main.read_bytes()
        self.prepare()
        self.start(scope=["main.txt"])
        main.write_bytes(b"partial implementation")
        middle = self.save()
        scopes = ["main.txt", "outside.txt", "中文 deps", "new.bin"]
        for reopen in (False, True):
            with self.subTest(reopen=reopen):
                self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", middle, "--execute")
                self.revise_scope(scopes)
                if reopen:
                    ready = self.save()
                    self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", ready, "--execute")
                execution = "impl-abort" if reopen else "impl-complete"
                self.cli("start-slice", "--feature-id", "delivery", "--execution-id", execution,
                         "--package-id", "slice", "--workspace-root", self.project)
                outside.write_bytes(b"changed outside")
                binary.write_bytes(b"\x00\xffchanged")
                (self.project / "new.bin").write_bytes(b"new product")
                if reopen:
                    self.cli("submit-slice", "--feature-id", "delivery", "--execution-id", execution, "--status", "interrupted")
                    self.assertEqual(baseline, main.read_bytes())
                    self.assertEqual(b"preexisting\r\n", outside.read_bytes())
                    self.assertEqual(b"\x00\xffbaseline", binary.read_bytes())
                    self.assertFalse((self.project / "new.bin").exists())
                else:
                    self.helper.record_checkpoint(execution, feature_id="delivery")
                    candidate = self.feature / "05-implementation/candidate.json"
                    write_candidate(candidate, "expanded-candidate")
                    result = self.cli("submit-slice", "--feature-id", "delivery", "--execution-id", execution,
                                      "--status", "completed", "--candidate-file", candidate)
                    self.assertEqual({"main.txt", "outside.txt", "中文 deps/data.bin", "new.bin"},
                                     {item["path"] for item in result["changes"]})
                self.assertEqual(index, (self.project / ".git/index").read_bytes())

    def test_narrowed_restored_scope_keeps_removed_work_when_aborted(self):
        self.prepare()
        self.start(scope=["main.txt", "outside.txt", "new.bin"])
        main = self.project / "main.txt"
        baseline = main.read_bytes()
        main.write_bytes(b"partial main")
        (self.project / "outside.txt").write_bytes(b"partial removed work")
        (self.project / "new.bin").write_bytes(b"new removed work")
        point = self.save()
        self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", point, "--execute")
        self.revise_scope(["main.txt"])
        self.cli("start-slice", "--feature-id", "delivery", "--execution-id", "impl-narrowed",
                 "--package-id", "slice", "--workspace-root", self.project)
        main.write_bytes(b"continued main")
        self.helper.record_checkpoint("impl-narrowed", feature_id="delivery")
        self.cli("submit-slice", "--feature-id", "delivery", "--execution-id", "impl-narrowed", "--status", "interrupted")
        self.assertEqual(baseline, main.read_bytes())
        self.assertEqual(b"partial removed work", (self.project / "outside.txt").read_bytes())
        self.assertEqual(b"new removed work", (self.project / "new.bin").read_bytes())
        self.assertIsNone(active_modification_lease(self.root))

    def test_restore_failure_and_process_interruption_preserve_recovery_source(self):
        self.prepare()
        self.start()
        point = self.save()
        main = self.project / "main.txt"
        main.write_bytes(b"latest work")
        original = snapshots._apply_files
        calls = 0
        def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            original(*args, **kwargs)
            if calls == 1:
                raise OSError("injected restore failure")
        with mock.patch.object(snapshots, "_apply_files", side_effect=fail_once):
            with self.assertRaises(OSError):
                snapshots.restore_snapshot(self.root, "delivery", point, execute=True)
        self.assertEqual(b"latest work", main.read_bytes())
        self.assertEqual("impl", active_modification_lease(self.root)["holder_execution_id"])
        def terminate(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt("process stopped")
        with mock.patch.object(snapshots, "_apply_files", side_effect=terminate):
            with self.assertRaises(KeyboardInterrupt):
                snapshots.restore_snapshot(self.root, "delivery", point, execute=True)
        self.assertTrue(snapshots.snapshot_status(self.root, "delivery")["recovery_required"])
        self.cli("snapshot-recover", "--feature-id", "delivery")
        self.assertEqual(b"latest work", main.read_bytes())
        self.assertFalse(snapshots.snapshot_status(self.root, "delivery")["recovery_required"])

    def test_save_failure_blocks_handoff_until_recovered(self):
        self.helper.set_status(self.feature / "01-requirements/terminology.md", "confirmed")
        self.helper.set_status(self.feature / "01-requirements/README.md", "confirmed")
        with mock.patch.object(snapshots, "_commit", side_effect=ArchiveWorkspaceError("SNAPSHOT_GIT_FAILED", "disk full")):
            self.cli("stage-action", "--feature-id", "delivery", "--stage", "requirements", "--decision", "approve", expected=1)
        result = self.cli("context-summary", "--feature-id", "delivery", "--action", "requirements", "--role", "coordinator", expected=1)
        self.assertEqual("SNAPSHOT_RECOVERY_REQUIRED", result["code"])
        self.cli("snapshot-recover", "--feature-id", "delivery")
        self.assertFalse(snapshots.snapshot_status(self.root, "delivery")["recovery_required"])

    def test_export_and_fork_do_not_overwrite_source_or_reuse_approvals(self):
        self.prepare()
        point = self.save()
        before = (self.feature / "workflow-state.json").read_bytes()
        destination = self.base / "导出 snapshot"
        self.cli("snapshot-export", "--feature-id", "delivery", "--snapshot-id", point, "--destination", destination)
        self.assertEqual(before, (destination / "archive/workflow-state.json").read_bytes())
        self.cli("snapshot-fork", "--source-feature-id", "delivery", "--snapshot-id", point, "--feature-id", "new-delivery", "--title", "重做")
        self.assertEqual(before, (self.feature / "workflow-state.json").read_bytes())
        new_state = json.loads((self.root / "new-delivery/workflow-state.json").read_bytes())
        self.assertIsNone(new_state["approvals"]["requirements"])
        self.assertEqual({}, new_state["execution"]["slices"])

    def test_revision_change_rejects_restore_but_keeps_export(self):
        self.prepare()
        self.start()
        point = self.save()
        self.git("commit", "--allow-empty", "-qm", "new revision")
        failure = self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", point, "--execute", expected=1)
        self.assertEqual("SNAPSHOT_SOURCE_CHANGED", failure["code"])
        self.cli("snapshot-export", "--feature-id", "delivery", "--snapshot-id", point, "--destination", self.base / "old-revision")

    def test_resume_save_after_commit_cannot_rebase_earlier_restore_scopes(self):
        initial = self.save(name="首次纳入实施范围之前")
        self.prepare()
        self.helper.accept_test_implementation(feature_id="delivery")
        accepted = self.save(name="已接受成果")
        product = self.project / "test-implementation.txt"
        self.git("add", "test-implementation.txt")
        self.git("commit", "-qm", "commit accepted result")
        resumed = self.cli("snapshot-save", "--feature-id", "delivery", "--reason", "resume")["snapshot"]["id"]
        self.cli("snapshot-save", "--feature-id", "delivery", "--name", "保留继续现场")
        before_state = (self.feature / "workflow-state.json").read_bytes()
        before_product = product.read_bytes()
        before_index = (self.project / ".git/index").read_bytes()
        before_head = self.git("rev-parse", "HEAD")
        before_history = self.cli("snapshot-list", "--feature-id", "delivery")
        for point in (initial, accepted, resumed):
            for execute in ((), ("--execute",)):
                with self.subTest(point=point, execute=bool(execute)):
                    failure = self.cli("snapshot-restore", "--feature-id", "delivery",
                                       "--snapshot-id", point, *execute, expected=1)
                    self.assertEqual("SNAPSHOT_SOURCE_CHANGED", failure["code"])
        self.assertEqual(before_state, (self.feature / "workflow-state.json").read_bytes())
        self.assertEqual(before_product, product.read_bytes())
        self.assertEqual(before_index, (self.project / ".git/index").read_bytes())
        self.assertEqual(before_head, self.git("rev-parse", "HEAD"))
        self.assertEqual(before_history, self.cli("snapshot-list", "--feature-id", "delivery"))
        for point in (initial, resumed):
            destination = self.base / ("export-" + point)
            self.cli("snapshot-export", "--feature-id", "delivery", "--snapshot-id", point,
                     "--destination", destination)
            if point == resumed:
                self.assertEqual(before_product, (destination / "workspace/test-implementation.txt").read_bytes())

    def test_later_slice_overlapping_accepted_product_can_restart_after_restore(self):
        self.prepare()
        self.helper.accept_test_implementation(feature_id="delivery")
        accepted = self.save(name="第一个切片结束")
        self.start(package="second", execution="impl-2", scope=["test-implementation.txt", "new.bin"])
        product = self.project / "test-implementation.txt"
        product.write_bytes(b"partly reworked")
        (self.project / "new.bin").write_bytes(b"second")
        middle = self.save(name="第二个切片中途")
        self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", middle, "--execute")
        self.cli("start-slice", "--feature-id", "delivery", "--execution-id", "impl-3", "--package-id", "second", "--workspace-root", self.project)
        self.helper.record_checkpoint("impl-3", feature_id="delivery")
        result = self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", accepted, "--execute")
        self.assertIn("second", result["affected_slices"])
        self.assertEqual(b"implemented", product.read_bytes())
        self.assertFalse((self.project / "new.bin").exists())
        state = json.loads((self.feature / "workflow-state.json").read_bytes())
        self.assertNotIn("second", state["execution"]["slices"])
        self.assertTrue(any(item["status"] == "accepted" for item in state["execution"]["slices"].values()))

    def test_frozen_history_exports_and_forks_but_cannot_reactivate(self):
        self.prepare()
        confirmation = self.helper.accept_test_implementation(feature_id="delivery")
        for category in ("02-investigation", "05-implementation", "06-validation"):
            self.helper.set_status(self.feature / category / "README.md", "completed")
        for state in ("active", "validating"):
            self.cli("transition-lifecycle", "--feature-id", "delivery", "--to", state)
        self.cli("transition-lifecycle", "--feature-id", "delivery", "--to", "frozen",
                 "--integration-confirmation", confirmation, "--validation-conclusion", "passed",
                 "--unverified-boundaries", "none", "--residual-risks", "none")
        point = self.cli("snapshot-list", "--feature-id", "delivery")["latest"]["id"]
        self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", point, "--execute", expected=1)
        self.cli("snapshot-fork", "--source-feature-id", "delivery", "--snapshot-id", point, "--feature-id", "after-freeze", "--title", "冻结后继续")
        self.assertEqual("frozen", json.loads((self.feature / "feature.json").read_bytes())["lifecycle"])

    def test_missing_git_stops_new_job_before_creating_documents(self):
        with mock.patch.object(snapshots.shutil, "which", return_value=None):
            result = self.cli("init", "--feature-id", "no-git", "--title", "无 Git", expected=1)
        self.assertEqual("SNAPSHOT_GIT_REQUIRED", result["code"])
        self.assertFalse((self.root / "no-git").exists())

    def test_nested_worktree_snapshot_preserves_shared_repository_index(self):
        linked = self.base / "linked worktree"
        self.git("worktree", "add", "-q", "-b", "snapshots", str(linked))
        project = linked / "nested project"
        project.mkdir()
        product = project / "中文.bin"
        product.write_bytes(b"\x00\xffbefore")
        root = canonical_archive_root(project)
        initialize_archive(root, "nested", "子目录交付")
        index = Path(self.git("-C", str(linked), "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
        before = index.read_bytes()
        acquire_modification_lease(root, "nested", "slice", project, ["中文.bin"], "now")
        point = snapshots.save_snapshot(root, "nested")["snapshot"]["id"]
        product.write_bytes(b"\x00\xffafter")
        snapshots.restore_snapshot(root, "nested", point, execute=True)
        self.assertEqual(b"\x00\xffbefore", product.read_bytes())
        self.assertEqual(before, index.read_bytes())
        self.assertEqual("original\n", (linked / "main.txt").read_text())

    def test_restoring_same_material_does_not_revive_revoked_approval(self):
        self.prepare()
        point = self.save(name="已批准需求")
        self.cli("stage-action", "--feature-id", "delivery", "--stage", "requirements", "--decision", "reject")
        self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", point, "--execute")
        state = json.loads((self.feature / "workflow-state.json").read_bytes())
        self.assertEqual("reject", state["approvals"]["requirements"]["decision"])

    def test_sequential_restores_preserve_rejection_until_explicit_reapproval(self):
        before = self.save(name="需求批准之前")
        self.prepare()
        approved = self.save(name="需求批准之后")
        self.cli("stage-action", "--feature-id", "delivery", "--stage", "requirements", "--decision", "reject")
        for expected in ("reject", "approve"):
            self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", before, "--execute")
            state = json.loads((self.feature / "workflow-state.json").read_bytes())
            self.assertIsNone(state["approvals"]["requirements"])
            preview = self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", approved)
            self.assertEqual("preview", preview["status"])
            self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", approved, "--execute")
            state = json.loads((self.feature / "workflow-state.json").read_bytes())
            self.assertEqual(expected, state["approvals"]["requirements"]["decision"])
            if expected == "reject":
                self.cli("stage-action", "--feature-id", "delivery", "--stage", "requirements", "--decision", "approve")

    def test_existing_interrupt_keeps_uncheckpointed_work_in_history(self):
        self.prepare()
        self.start()
        main = self.project / "main.txt"
        baseline = main.read_bytes()
        main.write_bytes(b"work since latest checkpoint")
        self.cli("submit-slice", "--feature-id", "delivery", "--execution-id", "impl", "--status", "interrupted")
        self.assertEqual(baseline, main.read_bytes())
        history = self.cli("snapshot-list", "--feature-id", "delivery")["snapshots"]
        before = next(item for item in history if item["reason"] == "before-candidate-submitted")
        destination = self.base / "before-interrupt"
        self.cli("snapshot-export", "--feature-id", "delivery", "--snapshot-id", before["id"], "--destination", destination)
        self.assertEqual(b"work since latest checkpoint", (destination / "workspace/main.txt").read_bytes())

    def test_repeated_contract_checks_reuse_saved_facts_but_changed_evidence_is_saved(self):
        self.prepare()
        package = self.helper.write_package("slice", feature_id="delivery")
        self.cli("prepare-slice-contract", "--feature-id", "delivery", "--package-file", package)
        arguments = ("check-slice-contract", "--feature-id", "delivery", "--package-file", package)
        value = json.loads(package.read_bytes())
        for passed in (True, False, True):
            with self.subTest(passed=passed):
                value["contract_check"]["regression_protection"] = {
                    "passed": passed, "evidence": "已完成回归保护" if passed else "尚未完成回归保护",
                }
                package.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                before = snapshots.snapshot_status(self.root, "delivery")["count"]
                expected = 0 if passed else 1
                with mock.patch("archive_slice_flow._utc_now", return_value="2026-09-11T01:00:00Z"):
                    first = self.cli(*arguments, expected=expected)
                self.assertEqual("passed" if passed else "failed", first["contract_check"]["status"])
                saved = snapshots.snapshot_status(self.root, "delivery")
                self.assertEqual(before + 1, saved["count"])
                state = (self.feature / "workflow-state.json").read_bytes()
                with mock.patch("archive_slice_flow._utc_now", return_value="2026-09-11T01:00:01Z"):
                    repeated = self.cli(*arguments, expected=expected)
                self.assertTrue(repeated["idempotent"])
                self.assertEqual(first["contract_check"], repeated["contract_check"])
                self.assertEqual(state, (self.feature / "workflow-state.json").read_bytes())
                self.assertEqual(saved["latest"], snapshots.snapshot_status(self.root, "delivery")["latest"])
                point = self.save(name="人工保存验证结论")
                with mock.patch("archive_slice_flow._utc_now", return_value="2026-09-11T01:00:02Z"):
                    self.cli(*arguments, expected=expected)
                self.assertEqual(point, snapshots.snapshot_status(self.root, "delivery")["latest"]["id"])

    def test_repeated_checkpoint_after_manual_save_reuses_history(self):
        self.prepare()
        self.start()
        main = self.project / "main.txt"
        main.write_bytes(b"first implementation")
        first = self.helper.record_checkpoint("impl", feature_id="delivery")
        point = self.save(name="人工保存检查点")
        count = snapshots.snapshot_status(self.root, "delivery")["count"]
        repeated = self.helper.record_checkpoint("impl", feature_id="delivery")
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(first["checkpoint"], repeated["checkpoint"])
        saved = snapshots.snapshot_status(self.root, "delivery")
        self.assertEqual(count, saved["count"])
        self.assertEqual(point, saved["latest"]["id"])
        main.write_bytes(b"next implementation")
        changed = self.helper.record_checkpoint("impl", feature_id="delivery")
        self.assertFalse(changed["idempotent"])
        self.assertEqual(count + 1, snapshots.snapshot_status(self.root, "delivery")["count"])

    def test_restore_does_not_adopt_out_of_scope_changes_as_a_new_baseline(self):
        self.prepare()
        self.start()
        point = self.save()
        (self.project / "outside.txt").write_bytes(b"out of scope")
        result = self.cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", point, "--execute", expected=1)
        self.assertEqual("SNAPSHOT_OUTSIDE_SCOPE_CHANGED", result["code"])
        self.assertEqual("impl", active_modification_lease(self.root)["holder_execution_id"])


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "需要 SVN")
class SvnSnapshotTests(unittest.TestCase):
    def test_svn_copies_preserve_history_offline_and_leave_source_unchanged(self):
        for kind in ("file", "directory", "replacement"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                repository, project = base / "repository", base / "project"
                subprocess.run(["svnadmin", "create", str(repository)], check=True, capture_output=True)
                subprocess.run(["svn", "checkout", repository.as_uri(), str(project)], check=True, capture_output=True)
                def svn(*args):
                    result = subprocess.run(["svn", *args], cwd=project, capture_output=True, encoding="utf-8")
                    self.assertEqual(0, result.returncode, result.stderr)
                    return result.stdout
                source, target = project / "source item", project / "copied item"
                if kind == "directory":
                    (source / "nested").mkdir(parents=True)
                    original = source / "nested/a.bin"
                else:
                    original = source
                original.write_bytes(b"committed\r\n")
                svn("add", "source item")
                if kind == "replacement":
                    target.write_bytes(b"replacement baseline")
                    svn("add", "copied item")
                svn("propset", "svn:ignore", ".scratch", ".")
                svn("commit", "-m", "fixture")
                svn("update")
                original.write_bytes(b"\x00\xffpreexisting\r\n")
                svn("propset", "test:property", "source", str(original))
                svn("changelist", "source-group", str(original))
                source_status = ET.canonicalize(svn("status", "--xml", "source item"))
                root = canonical_archive_root(project)
                initialize_archive(root, "delivery", "SVN 复制快照")
                def cli(*arguments, expected=0, **options):
                    out, err = io.StringIO(), io.StringIO()
                    with mock.patch.object(feature_archive, "project_root_from_entrypoint", return_value=project), redirect_stdout(out), redirect_stderr(err):
                        code = feature_archive.main([str(value) for value in arguments])
                    self.assertEqual(expected, code, err.getvalue() or out.getvalue())
                    return json.loads(out.getvalue() or err.getvalue())
                helper = FeatureArchiveCliTestCase()
                helper.root, helper.workspace, helper._project_root, helper.run_cli = root, project, project, cli
                helper.approve_requirements("delivery")
                helper.prepare_execution_inputs("delivery")
                package = helper.write_package("slice", feature_id="delivery", scope=["copied item"])
                cli("start-slice", "--feature-id", "delivery", "--execution-id", "impl", "--package-file", package, "--workspace-root", project)
                first = cli("snapshot-save", "--feature-id", "delivery")["snapshot"]["id"]
                if kind == "replacement":
                    svn("delete", "copied item")
                svn("copy", "source item", "copied item")
                changed = target / "nested/a.bin" if kind == "directory" else target
                changed.write_bytes(b"\xff\x00copied content\r\n")
                svn("propset", "test:property", "copied", str(changed))
                svn("changelist", "copy-group", str(changed))
                helper.record_checkpoint("impl", feature_id="delivery")
                second = cli("snapshot-save", "--feature-id", "delivery")["snapshot"]["id"]
                before = ET.fromstring(svn("info", "--xml", "copied item"))
                changed_status = ET.canonicalize(svn("status", "--xml", "copied item"))
                self.assertIsNotNone(before.findtext("entry/wc-info/copy-from-url"))
                # 临时仓库移至同一测试目录，确保恢复不依赖服务器或远端复制。
                repository.rename(base / "offline-repository")
                if kind == "file":
                    apply_files = snapshots._apply_files
                    calls = 0
                    def fail_once(*args, **kwargs):
                        nonlocal calls
                        calls += 1
                        if calls == 1:
                            raise OSError("模拟恢复正文失败")
                        return apply_files(*args, **kwargs)
                    with mock.patch.object(snapshots, "_apply_files", side_effect=fail_once):
                        with self.assertRaises(OSError):
                            snapshots.restore_snapshot(root, "delivery", first, execute=True)
                    self.assertEqual(changed_status, ET.canonicalize(svn("status", "--xml", "copied item")))
                    self.assertEqual(b"\xff\x00copied content\r\n", changed.read_bytes())
                cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", first, "--execute")
                if kind == "replacement":
                    self.assertEqual(b"replacement baseline", target.read_bytes())
                else:
                    self.assertFalse(target.exists())
                for point in (second, second):
                    cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", point, "--execute")
                    after = ET.fromstring(svn("info", "--xml", "copied item"))
                    for field in ("copy-from-url", "copy-from-rev", "moved-from"):
                        self.assertEqual(before.findtext("entry/wc-info/" + field), after.findtext("entry/wc-info/" + field))
                    self.assertEqual(changed_status, ET.canonicalize(svn("status", "--xml", "copied item")))
                    self.assertEqual(b"\xff\x00copied content\r\n", changed.read_bytes())
                    self.assertEqual("copied", svn("propget", "test:property", str(changed)).strip())
                self.assertEqual(source_status, ET.canonicalize(svn("status", "--xml", "source item")))
                self.assertEqual(b"\x00\xffpreexisting\r\n", original.read_bytes())
                self.assertEqual("source", svn("propget", "test:property", str(original)).strip())
                self.assertFalse(snapshots.snapshot_status(root, "delivery")["recovery_required"])
                self.assertEqual("1", svn("info", "--show-item", "revision").strip())

    def test_svn_moves_and_deleted_changelists_survive_restore(self):
        for kind in ("file", "directory", "replacement"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                repository, project = base / "repository", base / "project"
                subprocess.run(["svnadmin", "create", str(repository)], check=True, capture_output=True)
                subprocess.run(["svn", "checkout", repository.as_uri(), str(project)], check=True, capture_output=True)
                def svn(*args):
                    result = subprocess.run(["svn", *args], cwd=project, capture_output=True, encoding="utf-8")
                    self.assertEqual(0, result.returncode, result.stderr)
                    return result.stdout
                old, new = project / "old item", project / "new item"
                if kind == "directory":
                    (old / "nested").mkdir(parents=True)
                    original = old / "nested/a.bin"
                else:
                    original = old
                original.write_bytes(b"committed\r\n")
                removed = project / "deleted.txt"
                removed.write_bytes(b"deleted content")
                outside = project / "outside.txt"
                outside.write_bytes(b"outside")
                svn("add", "old item", "deleted.txt", "outside.txt")
                if kind == "replacement":
                    new.write_bytes(b"replacement baseline")
                    svn("add", "new item")
                svn("propset", "svn:ignore", ".scratch", ".")
                svn("commit", "-m", "fixture")
                svn("update")
                original.write_bytes(b"\x00\xffpreexisting\r\n")
                outside.write_bytes(b"outside local changes")
                svn("propset", "test:property", "before", str(original))
                svn("changelist", "previous", str(original), "deleted.txt")
                outside_status = ET.canonicalize(svn("status", "--xml", "outside.txt"))
                root = canonical_archive_root(project)
                initialize_archive(root, "delivery", "SVN 重命名快照")
                def cli(*arguments, expected=0, **options):
                    out, err = io.StringIO(), io.StringIO()
                    with mock.patch.object(feature_archive, "project_root_from_entrypoint", return_value=project), redirect_stdout(out), redirect_stderr(err):
                        code = feature_archive.main([str(value) for value in arguments])
                    self.assertEqual(expected, code, err.getvalue() or out.getvalue())
                    return json.loads(out.getvalue() or err.getvalue())
                helper = FeatureArchiveCliTestCase()
                helper.root, helper.workspace, helper._project_root, helper.run_cli = root, project, project, cli
                helper.approve_requirements("delivery")
                helper.prepare_execution_inputs("delivery")
                scopes = ["old item", "new item", "deleted.txt"]
                package = helper.write_package("slice", feature_id="delivery", scope=scopes)
                cli("start-slice", "--feature-id", "delivery", "--execution-id", "impl", "--package-file", package, "--workspace-root", project)
                first = cli("snapshot-save", "--feature-id", "delivery")["snapshot"]["id"]
                initial_status = ET.canonicalize(svn("status", "--xml", *scopes))
                if kind == "replacement":
                    svn("delete", "new item")
                svn("move", "old item", "new item")
                svn("delete", "--force", "deleted.txt")
                changed = new / "nested/a.bin" if kind == "directory" else new
                changed.write_bytes(b"\xff\x00renamed content\r\n")
                svn("propset", "test:property", "after", str(changed))
                helper.record_checkpoint("impl", feature_id="delivery")
                second = cli("snapshot-save", "--feature-id", "delivery")["snapshot"]["id"]
                changed_status = ET.canonicalize(svn("status", "--xml", *scopes))
                before = ET.fromstring(svn("info", "--xml", "new item"))
                self.assertIsNotNone(before.findtext("entry/wc-info/copy-from-url"))
                self.assertIsNotNone(before.findtext("entry/wc-info/moved-from"))
                if kind == "file":
                    apply_files = snapshots._apply_files
                    calls = 0
                    def fail_once(*args, **kwargs):
                        nonlocal calls
                        calls += 1
                        if calls == 1:
                            raise OSError("模拟恢复正文失败")
                        return apply_files(*args, **kwargs)
                    with mock.patch.object(snapshots, "_apply_files", side_effect=fail_once):
                        with self.assertRaises(OSError):
                            snapshots.restore_snapshot(root, "delivery", first, execute=True)
                    self.assertEqual(changed_status, ET.canonicalize(svn("status", "--xml", *scopes)))
                    self.assertEqual(b"\xff\x00renamed content\r\n", changed.read_bytes())
                cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", first, "--execute")
                self.assertEqual(initial_status, ET.canonicalize(svn("status", "--xml", *scopes)))
                self.assertEqual(b"\x00\xffpreexisting\r\n", original.read_bytes())
                for target in (second, second):
                    cli("snapshot-restore", "--feature-id", "delivery", "--snapshot-id", target, "--execute")
                    after = ET.fromstring(svn("info", "--xml", "new item"))
                    for field in ("copy-from-url", "copy-from-rev", "moved-from"):
                        self.assertEqual(before.findtext("entry/wc-info/" + field), after.findtext("entry/wc-info/" + field))
                    self.assertEqual(changed_status, ET.canonicalize(svn("status", "--xml", *scopes)))
                    self.assertEqual(b"\xff\x00renamed content\r\n", changed.read_bytes())
                    self.assertFalse(old.exists())
                    self.assertFalse(removed.exists())
                self.assertEqual("after", svn("propget", "test:property", str(changed)).strip())
                self.assertEqual(outside_status, ET.canonicalize(svn("status", "--xml", "outside.txt")))
                self.assertEqual(b"outside local changes", outside.read_bytes())
                self.assertFalse(snapshots.snapshot_status(root, "delivery")["recovery_required"])
                self.assertEqual("1", svn("info", "--show-item", "revision").strip())

    def test_svn_deleted_replaced_and_added_directories_round_trip(self):
        for mode in ("delete", "replace", "add"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                repository, project = base / "repository", base / "project"
                subprocess.run(["svnadmin", "create", str(repository)], check=True, capture_output=True)
                subprocess.run(["svn", "checkout", repository.as_uri(), str(project)], check=True, capture_output=True)
                def svn(*args):
                    result = subprocess.run(["svn", *args], cwd=project, capture_output=True, encoding="utf-8")
                    self.assertEqual(0, result.returncode, result.stderr)
                    return result.stdout
                directory = project / "managed tree"
                directory.mkdir()
                original = directory / "a.bin"
                original.write_bytes(b"committed")
                outside = project / "outside.txt"
                outside.write_bytes(b"outside")
                svn("add", "managed tree", "outside.txt")
                svn("propset", "svn:ignore", ".scratch", ".")
                svn("commit", "-m", "fixture")
                svn("update")
                original.write_bytes(b"\x00\xffpreexisting\r\n")
                outside.write_bytes(b"unrelated changes")
                svn("propset", "test:property", "before", "managed tree")
                svn("propset", "test:property", "child", "managed tree/a.bin")
                svn("changelist", "previous", "managed tree/a.bin")
                outside_status = ET.canonicalize(svn("status", "--xml", "outside.txt"))
                root = canonical_archive_root(project)
                initialize_archive(root, "delivery", "SVN 目录快照")
                scopes = ["managed tree", "new tree"]
                acquire_modification_lease(root, "delivery", "slice", project, scopes, "now")
                first = snapshots.save_snapshot(root, "delivery")["snapshot"]["id"]
                if mode in {"delete", "replace"}:
                    svn("delete", "--force", "managed tree")
                if mode in {"replace", "add"}:
                    replacement = directory if mode == "replace" else project / "new tree"
                    replacement.mkdir()
                    (replacement / "b.bin").write_bytes(b"\xff\x00replacement")
                    svn("add", str(replacement))
                second = snapshots.save_snapshot(root, "delivery")["snapshot"]["id"]
                changed_status = ET.canonicalize(svn("status", "--xml", *scopes))
                changed_files = {path.relative_to(project).as_posix(): path.read_bytes()
                                 for scope in scopes for path in (project / scope).rglob("*") if path.is_file()}
                snapshots.restore_snapshot(root, "delivery", first, execute=True)
                self.assertEqual(b"\x00\xffpreexisting\r\n", original.read_bytes())
                self.assertEqual("before", svn("propget", "test:property", "managed tree").strip())
                self.assertEqual("child", svn("propget", "test:property", "managed tree/a.bin").strip())
                self.assertIn("previous", svn("status", "--xml", "managed tree/a.bin"))
                self.assertFalse((project / "new tree").exists())
                snapshots.restore_snapshot(root, "delivery", second, execute=True)
                self.assertEqual(changed_status, ET.canonicalize(svn("status", "--xml", *scopes)))
                self.assertEqual(changed_files, {path.relative_to(project).as_posix(): path.read_bytes()
                                               for scope in scopes for path in (project / scope).rglob("*") if path.is_file()})
                self.assertEqual(b"unrelated changes", outside.read_bytes())
                self.assertEqual(outside_status, ET.canonicalize(svn("status", "--xml", "outside.txt")))
                self.assertFalse(snapshots.snapshot_status(root, "delivery")["recovery_required"])
                self.assertEqual("1", svn("info", "--show-item", "revision").strip())

    def test_svn_content_properties_group_and_added_file_are_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repository, project = base / "repository", base / "project"
            subprocess.run(["svnadmin", "create", str(repository)], check=True, capture_output=True)
            subprocess.run(["svn", "checkout", repository.as_uri(), str(project)], check=True, capture_output=True)
            def svn(*args):
                result = subprocess.run(["svn", *args], cwd=project, capture_output=True, encoding="utf-8")
                self.assertEqual(0, result.returncode, result.stderr)
                return result.stdout
            main = project / "main.txt"
            main.write_bytes(b"original")
            svn("add", "main.txt")
            svn("propset", "svn:ignore", ".scratch", ".")
            svn("commit", "-m", "fixture")
            svn("update")
            main.write_bytes(b"preexisting")
            svn("propset", "test:property", "before", "main.txt")
            svn("changelist", "previous", "main.txt")
            root = canonical_archive_root(project)
            initialize_archive(root, "delivery", "SVN 快照")
            acquire_modification_lease(root, "delivery", "slice", project, ["main.txt", "new.bin"], "now")
            first = snapshots.save_snapshot(root, "delivery")["snapshot"]["id"]
            main.write_bytes(b"changed")
            (project / "new.bin").write_bytes(b"\xff\x00")
            svn("add", "new.bin")
            svn("propset", "test:property", "after", "main.txt")
            svn("changelist", "new-group", "main.txt")
            second = snapshots.save_snapshot(root, "delivery")["snapshot"]["id"]
            snapshots.restore_snapshot(root, "delivery", first, execute=True)
            self.assertEqual(b"preexisting", main.read_bytes())
            self.assertEqual("before", svn("propget", "test:property", "main.txt").strip())
            self.assertFalse((project / "new.bin").exists())
            self.assertIn("previous", svn("status", "--xml"))
            snapshots.restore_snapshot(root, "delivery", second, execute=True)
            self.assertEqual(b"changed", main.read_bytes())
            self.assertEqual(b"\xff\x00", (project / "new.bin").read_bytes())
            self.assertIn("added", svn("status", "--xml", "new.bin"))
            self.assertEqual("1", svn("info", "--show-item", "revision").strip())


if __name__ == "__main__":
    unittest.main()
