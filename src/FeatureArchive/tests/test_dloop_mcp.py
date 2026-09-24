"""真实 stdio MCP 协议、结构化提交和 CLI 独立评审闭环。"""

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client, StdioServerParameters
from jsonschema import Draft202012Validator

from archive_initialization import initialize_archive
from archive_paths import canonical_archive_root
from archive_tool_api import TOOLS
from _feature_archive_support import FeatureArchiveCliTestCase
from test_feature_archive_git import GitProjectTestCase


class McpTransportTests(GitProjectTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.project.resolve()
        self.root = canonical_archive_root(self.project)
        initialize_archive(self.root, "delivery", "结构化工具交付")
        self.tool = self.project / "Tools/FeatureArchive"
        shutil.copytree(Path(__file__).resolve().parents[1], self.tool, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.helper = FeatureArchiveCliTestCase()
        self.helper.root = self.root
        self.helper.workspace = self.project
        self.helper._project_root = self.project
        self.helper.run_cli = self.cli

    def cli(self, *arguments, expected=0, **options):
        result = subprocess.run([sys.executable, str(self.tool / "feature_archive.py"), *map(str, arguments)],
                                cwd=self.base, capture_output=True, encoding="utf-8")
        self.assertEqual(expected, result.returncode, result.stderr or result.stdout)
        return json.loads(result.stdout or result.stderr)

    def client(self, mode="auto"):
        return Client(StdioServerParameters(command=sys.executable, args=[str(self.tool / "dloop_mcp.py")],
                                            cwd=str(self.base), env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}),
                      mode=mode, read_timeout_seconds=60)

    async def call(self, client, name, arguments, *, error=False):
        response = await client.call_tool(name, arguments)
        self.assertEqual(error, bool(response.is_error), response.content)
        result = response.structured_content
        self.assertEqual(result, json.loads(response.content[0].text))
        self.assertEqual(str(self.project), result["tool_context"]["project_root"])
        return result

    def test_legacy_and_current_clients_discover_tools_and_reject_mixed_identity(self):
        async def scenario():
            for mode in ("auto", "legacy"):
                async with self.client(mode) as client:
                    listed = await client.list_tools()
                    self.assertEqual(set(TOOLS), {item.name for item in listed.tools})
                    for item in listed.tools:
                        Draft202012Validator.check_schema(item.input_schema)
                    result = await self.call(client, "dloop_status", {})
                    self.assertIsNone(result["modification_lease"])
                    rejected = await self.call(client, "dloop_prepare_input", {
                        "feature_id": "delivery", "input_kind": "task-package", "package_id": "slice",
                        "execution_id": "wrong-extra-identity"}, error=True)
                    self.assertEqual("INVALID_TOOL_ARGUMENTS", rejected["code"])
        asyncio.run(scenario())

    def test_structured_submissions_reach_independent_review_and_preserve_checkpoint_identity(self):
        self.helper.approve_requirements("delivery")
        self.helper.prepare_execution_inputs("delivery")
        self.cli("transition-lifecycle", "--feature-id", "delivery", "--to", "active")
        package_path = self.helper.write_package("slice", feature_id="delivery", scope=["main.txt"])
        package = json.loads(package_path.read_text(encoding="utf-8"))

        async def scenario():
            async with self.client() as client:
                await self.call(client, "dloop_submit_task_package", {"feature_id": "delivery", "package": package})
                self.cli("check-slice-contract", "--feature-id", "delivery", "--package-id", "slice")
                self.cli("start-slice", "--feature-id", "delivery", "--package-id", "slice",
                         "--execution-id", "implementation", "--workspace-root", self.project)
                prepared = await self.call(client, "dloop_prepare_input", {
                    "feature_id": "delivery", "input_kind": "candidate", "execution_id": "implementation"})
                candidate = prepared["template"]
                candidate["verification"] = ["真实临时 Git 项目验证"]
                (self.project / "main.txt").write_text("implemented", encoding="utf-8")
                rejected = await self.call(client, "dloop_submit_candidate", {
                    "feature_id": "delivery", "execution_id": "implementation", "candidate": candidate}, error=True)
                self.assertEqual("FINAL_CHECKPOINT_REQUIRED", rejected["code"])
                prepared = await self.call(client, "dloop_prepare_input", {
                    "feature_id": "delivery", "input_kind": "checkpoint", "execution_id": "implementation"})
                checkpoint = prepared["template"]
                checkpoint.update(hypothesis="契约仍成立", change_summary="完成当前实现", next_step="提交候选")
                for validation in checkpoint["validation_results"]:
                    validation.update(status="passed", evidence=["聚焦验证通过"])
                arguments = {"feature_id": "delivery", "execution_id": "implementation", "checkpoint": checkpoint}
                first, second = await asyncio.gather(
                    self.call(client, "dloop_submit_checkpoint", arguments),
                    self.call(client, "dloop_submit_checkpoint", arguments))
                self.assertEqual(first["checkpoint"]["checkpoint_id"], second["checkpoint"]["checkpoint_id"])
                self.assertEqual(first["input_artifact"], second["input_artifact"])
                submitted = await self.call(client, "dloop_submit_candidate", {
                    "feature_id": "delivery", "execution_id": "implementation", "candidate": candidate})
                self.assertEqual("candidate", submitted["status"])
                repeated = await self.call(client, "dloop_submit_candidate", {
                    "feature_id": "delivery", "execution_id": "implementation", "candidate": candidate}, error=True)
                self.assertEqual("EXECUTION_NOT_ACTIVE", repeated["code"])
                status = await self.call(client, "dloop_status", {"feature_id": "delivery"})
                self.assertEqual("review-slice", status["delivery_view"]["next_action_contract"]["command"])
                self.cli("review-slice", "--feature-id", "delivery", "--package-id", "slice",
                         "--candidate-id", candidate["candidate_id"], "--review-execution-id", "independent-review", "--result", "passed")
                state = json.loads((self.root / "delivery/workflow-state.json").read_text(encoding="utf-8"))
                record = state["execution"]["slices"]["slice"]
                self.assertEqual("accepted", record["status"])
                self.assertEqual(1, len(record["checkpoints"]))
                self.assertTrue(self.cli("snapshot-list", "--feature-id", "delivery")["snapshots"])
        asyncio.run(scenario())

    def test_upgrade_requires_server_restart_before_processing_another_request(self):
        async def scenario():
            async with self.client() as client:
                await self.call(client, "dloop_status", {})
                # 模拟正常升级时安装器替换版本标记，旧进程必须停止处理。
                (self.tool / "VERSION").write_text("9.9.9\n", encoding="utf-8")
                rejected = await self.call(client, "dloop_status", {}, error=True)
                self.assertEqual("DLOOP_RESTART_REQUIRED", rejected["code"])
        asyncio.run(scenario())
