"""真实 Git 仓库、worktree 和公开交付命令回归。"""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from archive_initialization import initialize_archive
from archive_paths import canonical_archive_root
from archive_workspace import (
    ArchiveWorkspaceError, acquire_modification_lease, active_modification_lease,
    matching_workspace_guard_snapshot, outside_scope_guard_changes,
    release_modification_lease, workspace_guard_snapshot,
)
from _feature_archive_support import FeatureArchiveCliTestCase, write_candidate


@unittest.skipUnless(shutil.which("git"), "需要 Git 客户端")
class GitProjectTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.project = self.base / "中文 Git project"
        self.project.mkdir()
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        (self.project / ".gitignore").write_text(".scratch/\n", encoding="utf-8")
        (self.project / "main.txt").write_text("original\n", encoding="utf-8")
        (self.project / "outside.txt").write_text("outside\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def git(self, *arguments, expected=0):
        result = subprocess.run(
            ["git", "-c", "user.name=DLoop test", "-c", "user.email=dloop@example.invalid", *arguments],
            cwd=self.project, capture_output=True, encoding="utf-8",
        )
        self.assertEqual(expected, result.returncode, result.stderr)
        return result.stdout


class GitWorkspaceTests(GitProjectTestCase):
    def test_unversioned_analysis_outputs_do_not_hide_tracked_outputs_or_product_changes(self):
        (self.project / ".gitignore").write_text("", encoding="utf-8")
        baseline = workspace_guard_snapshot(self.project)
        output = self.project / ".scratch/outputs/analysis.json"
        output.parent.mkdir(parents=True)
        output.write_text("{}", encoding="utf-8")
        self.assertEqual(baseline["digest"], workspace_guard_snapshot(self.project)["digest"])
        self.git("add", str(output))
        (self.project / "new.txt").write_text("product", encoding="utf-8")
        snapshot = workspace_guard_snapshot(self.project)
        self.assertIn(".scratch/outputs/analysis.json", snapshot["entries"])
        self.assertIn("new.txt", snapshot["entries"])

    def test_mixed_index_and_worktree_content_are_bound_without_index_writes(self):
        path = self.project / "main.txt"
        path.write_text("staged one\n", encoding="utf-8")
        self.git("add", "main.txt")
        path.write_text("working content\n", encoding="utf-8")
        index = self.project / ".git/index"
        before = index.read_bytes()
        snapshot = workspace_guard_snapshot(self.project)
        self.assertEqual("git", snapshot["source"])
        self.assertEqual(before, index.read_bytes())
        path.write_text("staged two\n", encoding="utf-8")
        self.git("add", "main.txt")
        path.write_text("working content\n", encoding="utf-8")
        changed = workspace_guard_snapshot(self.project)
        self.assertNotEqual(snapshot["digest"], changed["digest"])
        self.assertEqual(("main.txt",), outside_scope_guard_changes(snapshot, changed, ["outside.txt"]))
        path.write_text("working changed again\n", encoding="utf-8")
        self.assertNotEqual(changed["digest"], workspace_guard_snapshot(self.project)["digest"])

    def test_added_deleted_and_renamed_paths_include_both_sides(self):
        baseline = workspace_guard_snapshot(self.project)
        self.git("mv", "main.txt", "中文 renamed.txt")
        (self.project / "outside.txt").unlink()
        (self.project / "new folder").mkdir()
        (self.project / "new folder/新增.txt").write_text("new", encoding="utf-8")
        changed = workspace_guard_snapshot(self.project)
        self.assertEqual({"main.txt", "中文 renamed.txt", "outside.txt", "new folder/新增.txt"}, set(changed["entries"]))
        self.assertEqual(("main.txt", "outside.txt"), outside_scope_guard_changes(baseline, changed, ["中文 renamed.txt", "new folder"]))

    def test_managed_documents_and_ignored_caches_do_not_stale_product_guard(self):
        baseline = workspace_guard_snapshot(self.project)
        archive = self.project / ".scratch/dloop-v3/v4.0.1/outputs/delivery"
        archive.mkdir(parents=True)
        document = archive / "feature.json"
        document.write_text("{}", encoding="utf-8")
        self.git("add", "-f", str(document))
        document.write_text('{"updated": true}', encoding="utf-8")
        cache = self.project / "__pycache__"
        cache.mkdir()
        (cache / "test.pyc").write_bytes(b"cache")
        self.assertEqual(baseline["digest"], workspace_guard_snapshot(self.project)["digest"])

    def test_ui_managed_outputs_do_not_hide_product_scope_changes(self):
        baseline = workspace_guard_snapshot(self.project)
        version = canonical_archive_root(self.project).parent
        for relative in ("outputs/delivery/ui-model.json", "captures/delivery/capture-request.json", "captures/delivery/raw.png"):
            path = version / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"generated artifact")
            self.git("add", "-f", str(path))
        self.assertEqual(baseline["digest"], workspace_guard_snapshot(self.project)["digest"])
        (self.project / "outside.txt").write_text("product change", encoding="utf-8")
        self.assertEqual(("outside.txt",), outside_scope_guard_changes(baseline, workspace_guard_snapshot(self.project), ["main.txt"]))

    def test_head_and_branch_changes_invalidate_confirmation_including_detached_head(self):
        baseline = workspace_guard_snapshot(self.project)
        self.git("commit", "--allow-empty", "-qm", "next")
        self.assertIsNone(matching_workspace_guard_snapshot(self.project, str(self.project), baseline["digest"]))
        current = workspace_guard_snapshot(self.project)
        self.git("checkout", "--detach", "-q")
        detached = workspace_guard_snapshot(self.project)
        self.assertNotEqual(current["digest"], detached["digest"])
        self.assertIn("(detached)", detached["revision"])

    def test_unborn_worktree_and_project_subdirectory(self):
        empty = self.base / "empty"
        empty.mkdir()
        subprocess.run(["git", "init", "-q", str(empty)], check=True, capture_output=True)
        self.assertIn("(initial)", workspace_guard_snapshot(empty)["revision"])
        linked = self.base / "linked worktree"
        self.git("worktree", "add", "-q", "-b", "linked", str(linked))
        nested = linked / "nested project"
        nested.mkdir()
        (nested / "中文.txt").write_text("new", encoding="utf-8")
        (linked / "outside.txt").write_text("other project", encoding="utf-8")
        with mock.patch("archive_workspace._svn_executable", side_effect=AssertionError("Git 不应调用 SVN")):
            snapshot = workspace_guard_snapshot(nested)
        self.assertEqual({"中文.txt"}, set(snapshot["entries"]))
        self.assertEqual("git", snapshot["source"])

    def test_missing_client_and_non_repository_report_actionable_errors(self):
        with mock.patch("archive_workspace.shutil.which", return_value=None):
            with self.assertRaises(ArchiveWorkspaceError) as caught:
                workspace_guard_snapshot(self.project)
        self.assertEqual("WORKSPACE_GIT_UNAVAILABLE", caught.exception.code)
        with self.assertRaises(ArchiveWorkspaceError) as caught:
            workspace_guard_snapshot(self.base)
        self.assertEqual("WORKSPACE_VCS_REQUIRED", caught.exception.code)

    def test_dirty_submodule_requires_its_own_workflow(self):
        child = self.base / "child"
        self.git("clone", "-q", str(self.project), str(child))
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child), "module")
        self.git("commit", "-qam", "add submodule")
        (self.project / "module/main.txt").write_text("changed", encoding="utf-8")
        with self.assertRaises(ArchiveWorkspaceError) as caught:
            workspace_guard_snapshot(self.project)
        self.assertEqual("WORKSPACE_GIT_SUBMODULE_CHANGED", caught.exception.code)
        self.assertIn("main.txt", workspace_guard_snapshot(self.project / "module")["entries"])

    def test_submodule_scope_boundary_in_nested_worktree_and_independent_module(self):
        child = self.base / "child"
        self.git("clone", "-q", str(self.project), str(child))
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child), "nested/中文 module")
        self.git("commit", "-qam", "add submodule")
        linked = self.base / "linked worktree"
        self.git("worktree", "add", "-q", "-b", "linked", str(linked))
        root = self.base / "lease-state"
        for workspace in (self.project / "nested", linked / "nested"):
            with self.subTest(workspace=workspace):
                with self.assertRaises(ArchiveWorkspaceError) as caught:
                    acquire_modification_lease(
                        root, "delivery", "slice", workspace, ["中文 module/generated.txt"], "now",
                    )
                self.assertEqual("WORKSPACE_GIT_SUBMODULE_SCOPE", caught.exception.code)
                self.assertIsNone(active_modification_lease(root))
        module = self.project / "nested/中文 module"
        lease = acquire_modification_lease(root, "delivery", "slice", module, ["generated.txt"], "now")
        self.assertEqual(str(module.resolve()), lease["workspace_root"])
        release_modification_lease(root, "delivery", "slice")


class GitWorkflowTests(GitProjectTestCase):
    def setUp(self):
        super().setUp()
        self.root = canonical_archive_root(self.project)
        initialize_archive(self.root, "delivery", "Git 交付")
        tool = self.project / "Tools/FeatureArchive"
        shutil.copytree(Path(__file__).resolve().parents[1], tool, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.helper = FeatureArchiveCliTestCase()
        self.helper.root = self.root
        self.helper.workspace = self.project
        self.helper._project_root = self.project
        self.helper.run_cli = self.installed_cli
        self.helper.approve_requirements("delivery")
        self.helper.prepare_execution_inputs("delivery")

    def installed_cli(self, *arguments, expected=0, **options):
        result = subprocess.run(
            [sys.executable, str(self.project / "Tools/FeatureArchive/feature_archive.py"), *(str(value) for value in arguments)],
            cwd=self.base, capture_output=True, encoding="utf-8",
        )
        self.assertEqual(expected, result.returncode, result.stderr or result.stdout)
        return json.loads(result.stdout or result.stderr)

    def test_start_rejects_submodule_product_scopes_before_acquiring_lease(self):
        child = self.base / "child"
        self.git("clone", "-q", str(self.project), str(child))
        (child / ".gitignore").write_text(".scratch/\ngenerated.txt\n", encoding="utf-8")
        self.git("-C", str(child), "add", ".gitignore")
        self.git("-C", str(child), "commit", "-qm", "ignore generated product")
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child), "plugins/中文 module")
        self.git("commit", "-qam", "add submodule")
        module = self.project / "plugins/中文 module"
        module_index = Path(self.git(
            "-C", str(module), "rev-parse", "--path-format=absolute", "--git-path", "index",
        ).strip())
        index_before = (self.project / ".git/index").read_bytes()
        module_index_before = module_index.read_bytes()
        for number, scope in enumerate((
            "plugins/中文 module/generated.txt", "plugins/中文 module/plain.txt",
            "plugins/中文 module", "plugins",
        )):
            with self.subTest(scope=scope):
                package_id = f"blocked-{number}"
                self.installed_cli(
                    "prepare-action-input", "--feature-id", "delivery",
                    "--input-kind", "task-package", "--package-id", package_id,
                )
                package = self.helper.write_package(package_id, feature_id="delivery", scope=[scope])
                self.installed_cli("prepare-slice-contract", "--feature-id", "delivery", "--package-file", package)
                self.installed_cli("check-slice-contract", "--feature-id", "delivery", "--package-id", package_id)
                result = self.installed_cli(
                    "start-slice", "--feature-id", "delivery", "--execution-id", package_id,
                    "--package-file", package, "--workspace-root", self.project, expected=1,
                )
                self.assertEqual("WORKSPACE_GIT_SUBMODULE_SCOPE", result["code"])
                self.assertIn("plugins/中文 module", result["message"])
                self.assertIsNone(active_modification_lease(self.root))
                self.assertFalse((module / "generated.txt").exists())
                self.assertEqual(index_before, (self.project / ".git/index").read_bytes())
                self.assertEqual(module_index_before, module_index.read_bytes())
        package = self.helper.write_package(
            "sibling", feature_id="delivery", scope=["plugins/中文 module-sibling.txt"],
        )
        self.installed_cli(
            "start-slice", "--feature-id", "delivery", "--execution-id", "sibling",
            "--package-file", package, "--workspace-root", self.project,
        )
        self.installed_cli(
            "submit-slice", "--feature-id", "delivery", "--execution-id", "sibling", "--status", "interrupted",
        )
        self.assertIsNone(active_modification_lease(self.root))
        self.assertEqual(index_before, (self.project / ".git/index").read_bytes())
        self.assertEqual(module_index_before, module_index.read_bytes())

    def test_delivery_reaches_review_integration_and_automatic_freeze_without_staging(self):
        before = (self.project / ".git/index").read_bytes()
        confirmation = self.helper.accept_test_implementation(feature_id="delivery")
        for category in ("02-investigation", "05-implementation", "06-validation"):
            self.helper.set_status(self.root / "delivery" / category / "README.md", "completed")
        for state in ("active", "validating"):
            self.installed_cli("transition-lifecycle", "--feature-id", "delivery", "--to", state)
        self.installed_cli(
            "transition-lifecycle", "--feature-id", "delivery", "--to", "frozen",
            "--integration-confirmation", confirmation, "--validation-conclusion", "passed",
            "--unverified-boundaries", "none", "--residual-risks", "none",
        )
        grouped = self.installed_cli("sync-svn-changelist", "--feature-id", "delivery")
        self.assertEqual("not-applicable", grouped["status"])
        summary = self.installed_cli("workflow-status", "--feature-id", "delivery")["delivery_view"]["delivery_summary"]
        self.assertEqual("frozen", summary["archive_lifecycle"])
        self.assertEqual("pending", summary["acceptance"])
        self.assertEqual(before, (self.project / ".git/index").read_bytes())

    def test_interruption_restores_working_content_and_preserves_existing_index(self):
        path = self.project / "main.txt"
        path.write_text("prior staged", encoding="utf-8")
        self.git("add", "main.txt")
        path.write_text("prior unstaged", encoding="utf-8")
        before = (self.project / ".git/index").read_bytes()
        package = self.helper.write_package("slice", feature_id="delivery", scope=["main.txt", "new.txt"])
        self.installed_cli("start-slice", "--feature-id", "delivery", "--execution-id", "implement", "--package-file", package, "--workspace-root", self.project)
        path.unlink()
        (self.project / "new.txt").write_text("implementation", encoding="utf-8")
        self.helper.record_checkpoint("implement", feature_id="delivery")
        self.installed_cli("submit-slice", "--feature-id", "delivery", "--execution-id", "implement", "--status", "interrupted")
        self.assertEqual("prior unstaged", path.read_text(encoding="utf-8"))
        self.assertFalse((self.project / "new.txt").exists())
        self.assertEqual(before, (self.project / ".git/index").read_bytes())
        self.assertIsNone(active_modification_lease(self.root))

    def test_ignored_delivery_product_regeneration_invalidates_confirmation_and_freeze(self):
        (self.project / ".gitignore").write_text(
            ".scratch/\ntest-implementation.txt\nbuild-cache/\n", encoding="utf-8",
        )
        confirmation = self.helper.accept_test_implementation(feature_id="delivery")
        before = (self.project / ".git/index").read_bytes()
        confirmed_guard = workspace_guard_snapshot(self.project)["digest"]
        for category in ("02-investigation", "05-implementation", "06-validation"):
            self.helper.set_status(self.root / "delivery" / category / "README.md", "completed")
        for state in ("active", "validating"):
            self.installed_cli("transition-lifecycle", "--feature-id", "delivery", "--to", state)

        self.helper.approve_final_with_confirmation(feature_id="delivery")
        product = self.project / "test-implementation.txt"
        for action in ("regenerate", "delete"):
            with self.subTest(action=action):
                # 模拟最终验证中运行生成脚本更新或清理已交付产品，不改写档案状态。
                subprocess.run([
                    sys.executable, "-c",
                    "from pathlib import Path; import sys; p = Path(sys.argv[1]); "
                    + ("p.write_text('regenerated-different', encoding='utf-8')"
                       if action == "regenerate" else "p.unlink()"),
                    str(product),
                ], check=True, capture_output=True)
                self.assertEqual(confirmed_guard, workspace_guard_snapshot(self.project)["digest"])
                status = self.installed_cli("workflow-status", "--feature-id", "delivery")
                self.assertEqual("stale", status["delivery_view"]["conclusions"]["approvals"]["final"]["status"])
                approval = self.helper.approve_final_with_confirmation(feature_id="delivery", expected=1)
                self.assertEqual("FINAL_CANDIDATE_CHANGED", approval["code"])
                result = self.installed_cli(
                    "transition-lifecycle", "--feature-id", "delivery", "--to", "frozen",
                    "--integration-confirmation", confirmation, "--validation-conclusion", "passed",
                    "--unverified-boundaries", "none", "--residual-risks", "none", expected=1,
                )
                self.assertEqual("LIFECYCLE_GATE_BLOCKED", result["code"])
                self.assertIn("最终工作区已偏离", result["message"])
                product.write_text("implemented", encoding="utf-8")

        cache = self.project / "build-cache/result.txt"
        cache.parent.mkdir()
        cache.write_text("regenerated cache", encoding="utf-8")
        result = self.installed_cli(
            "transition-lifecycle", "--feature-id", "delivery", "--to", "frozen",
            "--integration-confirmation", confirmation, "--validation-conclusion", "passed",
            "--unverified-boundaries", "none", "--residual-risks", "none",
        )
        self.assertEqual("frozen", result["lifecycle"])
        self.assertEqual(before, (self.project / ".git/index").read_bytes())

    def test_accepted_ignored_products_follow_execution_order_and_preserve_deletions(self):
        (self.project / ".gitignore").write_text(
            ".scratch/\ntest-implementation.txt\nretained.txt\n", encoding="utf-8",
        )
        self.helper.accept_test_implementation(feature_id="delivery")
        before = (self.project / ".git/index").read_bytes()
        for package_id, changes in (
            ("z-update", {"test-implementation.txt": "updated", "retained.txt": "retained"}),
            ("a-delete", {"test-implementation.txt": None}),
        ):
            package = self.helper.write_package(package_id, feature_id="delivery", scope=list(changes))
            if package_id == "a-delete":
                product = self.project / "test-implementation.txt"
                product.write_text("regenerated-different", encoding="utf-8")
                result = self.installed_cli(
                    "start-slice", "--feature-id", "delivery", "--execution-id", package_id,
                    "--package-file", package, "--workspace-root", self.project, expected=1,
                )
                self.assertEqual("LAST_ACCEPTED_CANDIDATE_CHANGED", result["code"])
                self.assertIsNone(active_modification_lease(self.root))
                product.write_text("updated", encoding="utf-8")
            self.installed_cli(
                "start-slice", "--feature-id", "delivery", "--execution-id", package_id,
                "--package-file", package, "--workspace-root", self.project,
            )
            for relative, content in changes.items():
                product = self.project / relative
                if content is None:
                    product.unlink()
                else:
                    product.write_text(content, encoding="utf-8")
            self.helper.record_checkpoint(package_id, feature_id="delivery")
            candidate = write_candidate(self.root / "delivery/05-implementation/candidate.json", package_id)
            self.installed_cli(
                "submit-slice", "--feature-id", "delivery", "--execution-id", package_id,
                "--status", "completed", "--candidate-file", candidate,
            )
            self.installed_cli(
                "review-slice", "--feature-id", "delivery", "--package-id", package_id,
                "--candidate-id", package_id, "--review-execution-id", package_id + "-review", "--result", "passed",
            )

        for category in ("02-investigation", "05-implementation", "06-validation"):
            self.helper.set_status(self.root / "delivery" / category / "README.md", "completed")
        for relative in ("retained.txt", "test-implementation.txt"):
            with self.subTest(regenerated_product=relative):
                product = self.project / relative
                product.write_text("regenerated-different", encoding="utf-8")
                result = self.helper.approve_final_with_confirmation(feature_id="delivery", expected=1)
                self.assertEqual("FINAL_CANDIDATE_CHANGED", result["code"])
                if relative == "retained.txt":
                    product.write_text("retained", encoding="utf-8")
                else:
                    product.unlink()
        result = self.helper.approve_final_with_confirmation(feature_id="delivery")
        self.assertEqual("approve", result["decision"])
        self.assertEqual(before, (self.project / ".git/index").read_bytes())

    def test_outside_scope_change_triggers_checkpoint_breaker(self):
        package = self.helper.write_package("slice", feature_id="delivery", scope=["main.txt"])
        self.installed_cli("start-slice", "--feature-id", "delivery", "--execution-id", "implement", "--package-file", package, "--workspace-root", self.project)
        (self.project / "outside.txt").write_text("changed", encoding="utf-8")
        result = self.helper.record_checkpoint("implement", feature_id="delivery")
        self.assertIn("write_scope_violation", result["checkpoint"]["boundary_rules"])

    def test_index_change_after_checkpoint_rejects_stale_candidate(self):
        package = self.helper.write_package("slice", feature_id="delivery", scope=["main.txt"])
        self.installed_cli("start-slice", "--feature-id", "delivery", "--execution-id", "implement", "--package-file", package, "--workspace-root", self.project)
        (self.project / "main.txt").write_text("implementation", encoding="utf-8")
        self.helper.record_checkpoint("implement", feature_id="delivery")
        self.git("add", "main.txt")
        candidate = write_candidate(self.root / "delivery/05-implementation/candidate.json", "candidate")
        result = self.installed_cli(
            "submit-slice", "--feature-id", "delivery", "--execution-id", "implement",
            "--status", "completed", "--candidate-file", candidate, expected=1,
        )
        self.assertEqual("CHECKPOINT_WORKSPACE_DRIFT", result["code"])


if __name__ == "__main__":
    unittest.main()
