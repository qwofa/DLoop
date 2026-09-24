"""使用真实本地 SVN 仓库验证产物归组与工作区绑定。"""

from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from archive_initialization import initialize_archive
from archive_failure_attribution import record_friction_note
from archive_paths import canonical_archive_root, canonical_share_root
from archive_svn import sync_svn_changelist, _status
from archive_workspace import acquire_modification_lease, active_modification_lease, workspace_guard_snapshot, _svn_executable, ArchiveWorkspaceError
from _feature_archive_support import FeatureArchiveCliTestCase


@unittest.skipUnless(_svn_executable("svn") and _svn_executable("svnadmin"), "需要 SVN 客户端")
class SvnChangelistTests(unittest.TestCase):
    def test_unversioned_analysis_outputs_are_pruned_before_following_directory_links(self):
        self.svn("propdel", "svn:ignore", str(self.project))
        baseline = workspace_guard_snapshot(self.project)
        outputs = self.project / ".scratch/outputs"
        outputs.mkdir(parents=True)
        cache = Path(self.temp.name) / "external-cache"
        cache.mkdir()
        (cache / "dependency.json").write_text("external dependency", encoding="utf-8")
        link = outputs / "node_modules"
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(cache), str(link))
            self.addCleanup(link.rmdir)
        else:
            link.symlink_to(cache, target_is_directory=True)
        self.assertEqual(baseline["digest"], workspace_guard_snapshot(self.project)["digest"])
        # SVN 归组会纳管 .scratch 父目录；临时输出仍须排除。
        self.svn("add", "--depth", "empty", str(self.project / ".scratch"))
        self.assertEqual(baseline["digest"], workspace_guard_snapshot(self.project)["digest"])
        product = self.project / "Assets/new.cs"
        product.write_text("new product", encoding="utf-8")
        self.assertIn("Assets/new.cs", workspace_guard_snapshot(self.project)["entries"])
        tracked = outputs / "tracked.txt"
        tracked.write_text("tracked product", encoding="utf-8")
        self.svn("add", "--depth", "empty", str(outputs))
        self.svn("add", str(tracked))
        self.assertIn(".scratch/outputs/tracked.txt", workspace_guard_snapshot(self.project)["entries"])
        if os.name == "nt":
            product_link = self.project / "Assets/external"
            _winapi.CreateJunction(str(cache), str(product_link))
            self.addCleanup(product_link.rmdir)
            with self.assertRaises(ArchiveWorkspaceError) as caught:
                workspace_guard_snapshot(self.project)
            self.assertEqual("WORKSPACE_SCOPE_ESCAPE", caught.exception.code)

    def test_ui_managed_outputs_do_not_hide_product_scope_changes(self):
        self.svn("propdel", "svn:ignore", str(self.project))
        baseline = workspace_guard_snapshot(self.project)
        for relative in ("outputs/delivery/ui-model.json", "captures/delivery/capture-request.json", "captures/delivery/raw.png"):
            path = self.root.parent / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"generated artifact")
        self.assertEqual(baseline["digest"], workspace_guard_snapshot(self.project)["digest"])
        (self.project / "Assets/main.cs").write_text("product change", encoding="utf-8")
        from archive_workspace import outside_scope_guard_changes
        self.assertEqual(("Assets/main.cs",), outside_scope_guard_changes(baseline, workspace_guard_snapshot(self.project), ["unrelated.txt"]))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.project = base / "DEV"
        repo = base / "repository"
        subprocess.run([_svn_executable("svnadmin"), "create", str(repo)], check=True, capture_output=True)
        self.svn("checkout", repo.as_uri(), str(self.project))
        self.project = self.project.resolve()
        (self.project / "Assets").mkdir()
        for name in ("Assets/main.cs", "Assets/main.cs.meta", "Assets/remove@old.cs", "unrelated.txt"):
            (self.project / name).write_text("original\n", encoding="utf-8")
        self.svn("add", str(self.project / "Assets"), str(self.project / "unrelated.txt"))
        self.svn("propset", "svn:ignore", ".scratch", str(self.project))
        self.svn("commit", str(self.project), "-m", "fixture")
        self.root = canonical_archive_root(self.project)
        initialize_archive(self.root, "delivery", "建筑交互策划")

    def svn(self, *args):
        result = subprocess.run([_svn_executable("svn"), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def sync(self):
        return sync_svn_changelist(self.root, "delivery", self.project)

    def test_all_artifacts_and_actual_changes_share_one_group_without_unrelated_files(self):
        (self.project / "unrelated.txt").write_text("unrelated change", encoding="utf-8")
        self.svn("changelist", "用户原组", str(self.project / "unrelated.txt"))
        (self.project / "Assets/main.cs").write_text("prior change", encoding="utf-8")
        acquire_modification_lease(self.root, "delivery", "slice", self.project, ["Assets"], "now")
        (self.project / "Assets/main.cs").write_text("prior change plus implementation", encoding="utf-8")
        (self.project / "Assets/remove@old.cs").unlink()
        folder = self.project / "Assets/新资源"
        folder.mkdir()
        (folder / "item@2.prefab").write_text("prefab", encoding="utf-8")
        (folder / "item@2.prefab.meta").write_text("guid", encoding="utf-8")
        (self.project / "Assets/新资源.meta").write_text("folder guid", encoding="utf-8")
        evidence = self.root / "delivery/06-validation/screenshot.png"
        evidence.write_bytes(b"image evidence")
        cache = self.root / "delivery/__pycache__"
        cache.mkdir()
        (cache / "runtime.pyc").write_bytes(b"cache")
        share = canonical_share_root(self.project) / "delivery/snapshot"
        share.mkdir(parents=True)
        (share / "README.md").write_text("share", encoding="utf-8")
        record_friction_note(self.root, feature_id="delivery", summary="补齐材料", extra_work="重新定位截图", evidence=(str(evidence),))
        result = self.sync()
        self.assertEqual("grouped", result["status"])
        self.assertEqual("建筑交互策划 [delivery]", result["name"])
        self.assertFalse(result["committed"])
        self.assertIn("Assets/main.cs", result["pre_existing_changes"])
        for name in ("Assets/main.cs", "Assets/main.cs.meta", "Assets/remove@old.cs", "Assets/新资源/item@2.prefab", "Assets/新资源/item@2.prefab.meta", "Assets/新资源.meta"):
            self.assertIn(name, result["files"])
        self.assertIn(evidence.relative_to(self.project).as_posix(), result["files"])
        self.assertTrue(any("/shares/" in name for name in result["files"]))
        self.assertTrue(any(name.endswith("workflow-state.json") for name in result["files"]))
        self.assertIn(".scratch/dloop-v3/v4.0.0/friction.jsonl", result["files"])
        self.assertFalse(any("__pycache__" in name or name.endswith(".lock") for name in result["files"]))
        status = _status(self.project)
        self.assertEqual("用户原组", status["unrelated.txt"]["changelist"])
        self.assertEqual("deleted", status["Assets/remove@old.cs"]["item"])
        self.assertIn("Assets/新资源", result["directory_operations"])
        self.assertEqual("1", self.svn("info", "--show-item", "revision", str(self.project / "Assets/main.cs")).strip())
        self.assertEqual(result, self.sync())

    def test_document_and_share_sync_preserves_product_guard_but_product_changes_do_not(self):
        self.sync()
        before = workspace_guard_snapshot(self.project)
        (self.root / "delivery/06-validation/evidence.md").write_text("new evidence", encoding="utf-8")
        self.sync()
        self.assertEqual(before["digest"], workspace_guard_snapshot(self.project)["digest"])
        (self.project / "Assets/main.cs").write_text("changed", encoding="utf-8")
        self.assertNotEqual(before["digest"], workspace_guard_snapshot(self.project)["digest"])

    def test_same_named_unrelated_group_is_preserved_and_name_is_stable(self):
        self.svn("changelist", "建筑交互策划 [delivery]", str(self.project / "unrelated.txt"))
        result = self.sync()
        self.assertEqual("建筑交互策划 [delivery] (2)", result["name"])
        self.assertEqual(result["name"], self.sync()["name"])
        self.assertEqual("建筑交互策划 [delivery]", _status(self.project)["unrelated.txt"]["changelist"])

    def test_svn_unavailable_reports_failure(self):
        with mock.patch("archive_svn._svn_executable", return_value=None):
            with self.assertRaises(ArchiveWorkspaceError) as caught:
                self.sync()
        self.assertEqual("SVN_CHANGELIST_UNAVAILABLE", caught.exception.code)

    def installed_cli(self, *arguments, expected=0, **options):
        tool = self.project / "Tools/FeatureArchive"
        if not tool.exists():
            shutil.copytree(Path(__file__).resolve().parents[1], tool, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        result = subprocess.run(
            [sys.executable, str(tool / "feature_archive.py"), *(str(value) for value in arguments)],
            cwd=self.temp.name, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(expected, result.returncode, result.stderr or result.stdout)
        return json.loads(result.stdout or result.stderr)

    def harness(self):
        helper = FeatureArchiveCliTestCase()
        helper.root = self.root
        helper.workspace = self.project
        helper._project_root = self.project
        helper.run_cli = self.installed_cli
        helper.approve_requirements("delivery")
        helper.prepare_execution_inputs("delivery")
        return helper

    def test_installed_workflow_auto_groups_before_checkpoint_and_keeps_final_approval(self):
        helper = self.harness()
        helper.accept_test_implementation(feature_id="delivery")
        first = self.installed_cli("sync-svn-changelist", "--feature-id", "delivery")
        self.assertEqual("grouped", first["status"])
        helper.set_status(self.root / "delivery/06-validation/README.md", "completed")
        helper.approve_final_with_confirmation(feature_id="delivery")
        self.installed_cli("rebuild-indexes")
        final = self.installed_cli("sync-svn-changelist", "--feature-id", "delivery")
        self.assertEqual(first["name"], final["name"])
        status = self.installed_cli("workflow-status", "--feature-id", "delivery")
        self.assertEqual("approve", status["delivery_view"]["trusted_machine_facts"]["approval_statuses"]["final"])

    def test_installed_delivery_freezes_without_user_approval_and_groups_final_archive(self):
        helper = self.harness()
        confirmation = helper.accept_test_implementation(feature_id="delivery")
        for category in ("02-investigation", "05-implementation", "06-validation"):
            helper.set_status(self.root / "delivery" / category / "README.md", "completed")
        self.installed_cli("transition-lifecycle", "--feature-id", "delivery", "--to", "active")
        self.installed_cli("transition-lifecycle", "--feature-id", "delivery", "--to", "validating")
        self.installed_cli(
            "transition-lifecycle", "--feature-id", "delivery", "--to", "frozen",
            "--integration-confirmation", confirmation, "--validation-conclusion", "passed",
            "--unverified-boundaries", "none", "--residual-risks", "none",
        )
        guard = workspace_guard_snapshot(self.project)["digest"]
        grouped = self.installed_cli("sync-svn-changelist", "--feature-id", "delivery")
        self.assertEqual("grouped", grouped["status"])
        self.assertFalse(grouped["committed"])
        self.assertIn(".scratch/dloop-v3/v4.0.0/outputs/delivery/feature.json", grouped["files"])
        self.assertEqual(guard, workspace_guard_snapshot(self.project)["digest"])
        summary = self.installed_cli("workflow-status", "--feature-id", "delivery")["delivery_view"]["delivery_summary"]
        self.assertEqual("frozen", summary["archive_lifecycle"])
        self.assertEqual("pending", summary["acceptance"])

    def test_interrupted_slice_removes_withdrawn_additions_on_next_sync(self):
        helper = self.harness()
        package = helper.write_package("slice", feature_id="delivery", scope=["Assets/new.cs"])
        self.installed_cli("start-slice", "--feature-id", "delivery", "--execution-id", "implement", "--package-file", package, "--workspace-root", self.project)
        (self.project / "Assets/new.cs").write_text("new implementation", encoding="utf-8")
        helper.record_checkpoint("implement", feature_id="delivery")
        self.assertEqual("added", _status(self.project)["Assets/new.cs"]["item"])
        self.installed_cli("submit-slice", "--feature-id", "delivery", "--execution-id", "implement", "--status", "interrupted")
        self.assertFalse((self.project / "Assets/new.cs").exists())
        final = self.installed_cli("sync-svn-changelist", "--feature-id", "delivery")
        self.assertNotIn("Assets/new.cs", final["files"])
        self.assertNotIn("Assets/new.cs", _status(self.project))

    def test_interrupted_deletion_restores_svn_schedule_and_prior_content(self):
        helper = self.harness()
        target = self.project / "Assets/main.cs"
        target.write_bytes(b"prior uncommitted content")
        package = helper.write_package("slice", feature_id="delivery", scope=["Assets/main.cs"])
        self.installed_cli("start-slice", "--feature-id", "delivery", "--execution-id", "implement", "--package-file", package, "--workspace-root", self.project)
        target.unlink()
        helper.record_checkpoint("implement", feature_id="delivery")
        self.assertEqual("deleted", _status(self.project)["Assets/main.cs"]["item"])
        self.installed_cli("submit-slice", "--feature-id", "delivery", "--execution-id", "implement", "--status", "interrupted")
        self.installed_cli("sync-svn-changelist", "--feature-id", "delivery")
        self.assertEqual(b"prior uncommitted content", target.read_bytes())
        self.assertEqual("modified", _status(self.project)["Assets/main.cs"]["item"])

    def test_existing_parent_metadata_keeps_unrelated_group(self):
        meta = self.project / "Assets.meta"
        meta.write_bytes(b"existing folder guid")
        self.svn("add", str(meta))
        self.svn("commit", str(meta), "-m", "folder metadata fixture")
        self.svn("changelist", "原有目录组", str(meta))
        helper = self.harness()
        package = helper.write_package("slice", feature_id="delivery", scope=["Assets/main.cs"])
        self.installed_cli("start-slice", "--feature-id", "delivery", "--execution-id", "implement", "--package-file", package, "--workspace-root", self.project)
        (self.project / "Assets/main.cs").write_bytes(b"implementation")
        helper.record_checkpoint("implement", feature_id="delivery")
        result = self.installed_cli("sync-svn-changelist", "--feature-id", "delivery")
        self.assertNotIn("Assets.meta", result["files"])
        self.assertEqual("原有目录组", _status(self.project)["Assets.meta"]["changelist"])
        self.assertEqual(b"existing folder guid", meta.read_bytes())

    def test_unversioned_parent_metadata_does_not_block_interruption(self):
        meta = self.project / "Assets.meta"
        meta.write_bytes(b"prior unversioned folder guid")
        original = (self.project / "Assets/main.cs").read_bytes()
        helper = self.harness()
        package = helper.write_package("slice", feature_id="delivery", scope=["Assets/main.cs"])
        self.installed_cli("start-slice", "--feature-id", "delivery", "--execution-id", "implement", "--package-file", package, "--workspace-root", self.project)
        (self.project / "Assets/main.cs").write_bytes(b"implementation")
        helper.record_checkpoint("implement", feature_id="delivery")
        self.assertEqual("unversioned", _status(self.project)["Assets.meta"]["item"])
        self.installed_cli("submit-slice", "--feature-id", "delivery", "--execution-id", "implement", "--status", "interrupted")
        result = self.installed_cli("sync-svn-changelist", "--feature-id", "delivery")
        self.assertNotIn("Assets.meta", result["files"])
        self.assertEqual(original, (self.project / "Assets/main.cs").read_bytes())
        self.assertEqual(b"prior unversioned folder guid", meta.read_bytes())
        self.assertIsNone(active_modification_lease(self.root, "delivery"))

    def test_associated_metadata_reports_prior_changes_after_interruption(self):
        meta = self.project / "Assets/main.cs.meta"
        meta.write_bytes(b"prior metadata changes")
        self.svn("changelist", "原有资源组", str(meta))
        helper = self.harness()
        package = helper.write_package("slice", feature_id="delivery", scope=["Assets/main.cs"])
        self.installed_cli("start-slice", "--feature-id", "delivery", "--execution-id", "implement", "--package-file", package, "--workspace-root", self.project)
        (self.project / "Assets/main.cs").write_bytes(b"implementation")
        helper.record_checkpoint("implement", feature_id="delivery")
        result = self.installed_cli("sync-svn-changelist", "--feature-id", "delivery")
        self.assertIn("Assets/main.cs.meta", result["pre_existing_changes"])
        self.assertEqual(result["name"], _status(self.project)["Assets/main.cs.meta"]["changelist"])
        self.installed_cli("submit-slice", "--feature-id", "delivery", "--execution-id", "implement", "--status", "interrupted")
        final = self.installed_cli("sync-svn-changelist", "--feature-id", "delivery")
        self.assertIn("Assets/main.cs.meta", final["pre_existing_changes"])
        self.assertEqual(b"prior metadata changes", meta.read_bytes())

    def test_non_svn_project_skips_without_requiring_client(self):
        project = Path(self.temp.name) / "git-project"
        project.mkdir()
        with mock.patch("archive_svn._svn_executable", return_value=None):
            result = sync_svn_changelist(canonical_archive_root(project), "delivery", project)
        self.assertEqual("not-applicable", result["status"])


if __name__ == "__main__":
    unittest.main()
