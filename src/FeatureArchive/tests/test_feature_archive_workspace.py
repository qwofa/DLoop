"""DLoop 全局修改资格与 SVN 工作区测试。"""

from __future__ import annotations

import json
import base64
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


FEATURE_ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FEATURE_ARCHIVE_ROOT))

from archive_transactions import workflow_status
import archive_workspace
from archive_workspace import (
    ArchiveWorkspaceError,
    acquire_modification_lease,
    active_modification_lease,
    _write_workspace_state,
    load_workspace_state,
    snapshot_changes,
    snapshot_workspace,
    workspace_guard_snapshot,
    workspace_state_lock,
)


class FeatureArchiveWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        def guard(workspace: Path):
            normalized = workspace.expanduser().resolve()
            return {
                "workspace_root": str(normalized),
                "source": "svn",
                "revision": "test-double-r1",
                "entries": {},
                "digest": "sha256:test-double-r1",
            }

        patcher = mock.patch.object(
            archive_workspace,
            "workspace_guard_snapshot",
            side_effect=guard,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_baseline_reads_each_file_once_and_keeps_matching_recovery_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory) / "workspace"
            workspace.mkdir()
            archive = Path(temporary_directory) / "archive"
            archive.mkdir()
            source = workspace / "module" / "asset.bin"
            source.parent.mkdir()
            content = b"asset-content" * 1000
            source.write_bytes(content)
            original = Path.read_bytes
            reads = []

            def counted(path):
                if path.resolve() == source.resolve():
                    reads.append(path)
                return original(path)

            with mock.patch.object(Path, "read_bytes", counted):
                lease = acquire_modification_lease(
                    archive, "delivery", "slice", workspace, ["module", "module/asset.bin"], "now",
                )
            self.assertEqual(1, len(reads))
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            self.assertEqual(digest, lease["baseline_snapshot"]["entries"]["module/asset.bin"])
            restored = lease["baseline_content_snapshot"]["entries"]["module/asset.bin"]
            self.assertEqual(content, base64.b64decode(restored["content_base64"]))
            self.assertEqual(digest, restored["digest"])
            self.assertEqual(lease["baseline_digest"], lease["baseline_content_snapshot"]["digest"])

    def test_scope_snapshot_ignores_python_bytecode_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            scope = workspace / "Tools"
            cache = scope / "__pycache__"
            cache.mkdir(parents=True)
            (scope / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
            (scope / "loose.pyc").write_bytes(b"loose-cache")
            (cache / "source.cpython-310.pyc").write_bytes(b"nested-cache")

            snapshot = snapshot_workspace(workspace, ["Tools"])

            self.assertEqual({"Tools/source.py"}, set(snapshot["entries"]))

            baseline = snapshot
            (cache / "later.cpython-310.pyc").write_bytes(b"later-cache")
            current = snapshot_workspace(workspace, ["Tools"])
            self.assertEqual((), snapshot_changes(baseline, current))

    def test_guard_ignores_current_and_legacy_dloop_managed_scratch_roots(self) -> None:
        for path in (
            ".scratch/dloop-v3/v4.0.0/outputs/item/state.json",
            ".scratch/dloop-history/v3.9.1/item/state.json",
            ".scratch/dloop-v1/job.json",
            ".scratch/feature-archive/index.json",
        ):
            with self.subTest(path=path):
                self.assertTrue(archive_workspace._is_guard_excluded(path))
        self.assertFalse(archive_workspace._is_guard_excluded(".scratch/outputs/product.txt"))

    def test_snapshot_changes_ignores_cache_keys_from_historical_baseline(self) -> None:
        baseline = {
            "entries": {
                "Tools/source.py": "sha256:source",
                "Tools/loose.pyc": "sha256:loose-cache",
                "Tools/__pycache__/source.cpython-310.pyc": "sha256:nested-cache",
            }
        }
        current = {"entries": {"Tools/source.py": "sha256:source"}}

        self.assertEqual((), snapshot_changes(baseline, current))

    def test_scope_snapshot_ignores_internal_workspace_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            scope = workspace / "archive"
            scope.mkdir()
            scope.joinpath("delivery.md").write_text("delivery\n", encoding="utf-8")
            scope.joinpath(".feature-archive-workspace-state.json").write_text(
                "{}\n", encoding="utf-8"
            )
            scope.joinpath(".feature-archive-workspace-state.lock").write_text(
                "locked\n", encoding="utf-8"
            )

            snapshot = snapshot_workspace(workspace, ["archive"])

            self.assertEqual({"archive/delivery.md"}, set(snapshot["entries"]))

            baseline = {
                "entries": {
                    **snapshot["entries"],
                    "archive/.feature-archive-workspace-state.json": "sha256:old",
                    "archive/.feature-archive-workspace-state.lock": "sha256:old",
                }
            }
            self.assertEqual((), snapshot_changes(baseline, snapshot))

    def test_workspace_guard_rejects_missing_svn_client(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / ".svn").mkdir()
            with mock.patch.object(archive_workspace, "_svn_executable", return_value=None):
                with self.assertRaises(ArchiveWorkspaceError) as raised:
                    workspace_guard_snapshot(workspace)
            self.assertEqual("WORKSPACE_SVN_UNAVAILABLE", raised.exception.code)

    def test_workspace_guard_rejects_non_svn_workspace_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / ".svn").mkdir()
            failed_info = subprocess.CompletedProcess([], 1, "", "not a working copy")
            with mock.patch.object(archive_workspace, "_svn_executable", return_value="svn"), mock.patch.object(
                archive_workspace.subprocess,
                "run",
                return_value=failed_info,
            ):
                with self.assertRaises(ArchiveWorkspaceError) as raised:
                    workspace_guard_snapshot(workspace)
            self.assertEqual("WORKSPACE_NOT_SVN", raised.exception.code)

    def test_workspace_guard_uses_only_svn_test_doubles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / ".svn").mkdir()
            with mock.patch.object(
                archive_workspace,
                "_svn_guard_entries",
                return_value={"tracked.txt": "sha256:changed"},
            ), mock.patch.object(
                archive_workspace,
                "_svn_revision",
                return_value="42",
            ):
                snapshot = workspace_guard_snapshot(workspace)

            self.assertEqual("svn", snapshot["source"])
            self.assertEqual("42", snapshot["revision"])
            self.assertEqual({"tracked.txt": "sha256:changed"}, snapshot["entries"])

    def test_global_workflow_status_reports_active_modification_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "outputs"
            workspace = base / "workspace"
            workspace.mkdir()

            self.assertEqual("ready", workflow_status(root)["status"])
            acquire_modification_lease(
                root, "feature-a", "package-a", workspace, ["target.txt"], "now"
            )
            status = workflow_status(root)

            self.assertEqual("ready", status["status"])
            self.assertEqual("feature-a", status["modification_lease"]["feature_id"])

    def test_previous_workspace_state_is_rejected_without_compatibility_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "outputs"
            workspace = base / "workspace"
            workspace.mkdir()
            _write_workspace_state(root, {
                "schema_version": 2,
                "modification_leases": [{
                    "feature_id": "legacy-feature",
                    "package_id": "legacy-slice",
                }],
            })

            with self.assertRaisesRegex(ArchiveWorkspaceError, "结构不合法"):
                active_modification_lease(root)

    def test_distinct_workspaces_share_one_global_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "outputs"
            first = base / "workspace-a"
            second = base / "workspace-b"
            first.mkdir()
            second.mkdir()

            acquire_modification_lease(
                root, "feature-a", "package-a", first, ["target.txt"], "now"
            )
            with self.assertRaises(ArchiveWorkspaceError) as raised:
                acquire_modification_lease(
                    root, "feature-b", "package-b", second, ["target.txt"], "now"
                )

            status = workflow_status(root)
            self.assertEqual("MODIFICATION_LEASE_CONFLICT", raised.exception.code)
            self.assertEqual("feature-a", status["modification_lease"]["feature_id"])
            self.assertEqual("feature-a", active_modification_lease(root, "feature-a")["feature_id"])
            self.assertIsNone(active_modification_lease(root, "feature-b"))

    def test_other_process_waits_for_workspace_state_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "outputs"
            workspace = base / "workspace"
            workspace.mkdir()
            (workspace / "target.txt").write_text("baseline\n", encoding="utf-8")
            script = (
                "import json, pathlib, sys;"
                f"sys.path.insert(0, {str(FEATURE_ARCHIVE_ROOT)!r});"
                "import archive_workspace;"
                "from archive_workspace import acquire_modification_lease;"
                f"root=pathlib.Path({str(root)!r});"
                f"workspace=pathlib.Path({str(workspace)!r});"
                "archive_workspace.workspace_guard_snapshot=lambda value:{"
                "'workspace_root':str(value.resolve()),'source':'svn',"
                "'revision':'test-double-r1','entries':{},'digest':'sha256:test-double-r1'};"
                "lease=acquire_modification_lease("
                "root,'feature-child','PKG-100',workspace,['target.txt'],'now');"
                "print(json.dumps(lease))"
            )
            with workspace_state_lock(root):
                child = subprocess.Popen(
                    [sys.executable, "-c", script],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                time.sleep(0.2)
                self.assertIsNone(child.poll(), "子进程不应越过工作区状态事务。")
            stdout, stderr = child.communicate(timeout=5)

            self.assertEqual(0, child.returncode, stderr)
            self.assertEqual("feature-child", json.loads(stdout)["feature_id"])
            self.assertEqual(
                "feature-child",
                load_workspace_state(root)["modification_lease"]["feature_id"],
            )


if __name__ == "__main__":
    unittest.main()
