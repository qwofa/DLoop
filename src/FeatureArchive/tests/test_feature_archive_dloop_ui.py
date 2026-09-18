from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from unittest import mock

try:
    from _feature_archive_support import FeatureArchiveCliTestCase, write_candidate
except ModuleNotFoundError:
    from ._feature_archive_support import FeatureArchiveCliTestCase, write_candidate

from archive_configuration import ArchiveConfigurationError, configuration_evaluation


_write_candidate = write_candidate


def write_candidate(path, candidate_id):
    _write_candidate(path, candidate_id)
    evidence = path.parent / "material-verification.txt"
    evidence.write_text("材料交接用验证记录：通过", encoding="utf-8")
    value = json.loads(path.read_bytes())
    value["delivery_materials"] = [{"requirement": "result", "kind": "verification",
                                    "path": str(evidence), "locator": "本次验证", "status": "passed"}]
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


PREFAB_TEXT = """%YAML 1.1
--- !u!1 &1000
GameObject:
  m_Name: RewardPanel
--- !u!224 &2000
RectTransform:
  m_GameObject: {fileID: 1000}
  m_TransformParent: {fileID: 0}
"""


class CaptureFixture:
    def __init__(self, failed_names: tuple[str, ...] = ()) -> None:
        self.failed_names = failed_names

    def capture(self, request_path: Path, project_root: Path, output_dir: Path) -> Path:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        captured = []
        errors = []
        for item in request["prefabs"]:
            prefab = Path(item["asset_path"])
            if prefab.name in self.failed_names:
                errors.append(
                    {"prefab_id": item["prefab_id"], "message": "测试截图失败。"}
                )
                continue
            screenshot = output_dir / f"{item['prefab_id']}-raw.png"
            screenshot.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            meta = Path(str(prefab) + ".meta").read_text(encoding="utf-8")
            guid = next(
                line.split(":", 1)[1].strip()
                for line in meta.splitlines()
                if line.startswith("guid:")
            )
            captured.append(
                {
                    "prefab_id": item["prefab_id"],
                    "asset_path": prefab.as_posix(),
                    "asset_guid": guid,
                    "asset_sha256": "sha256:"
                    + hashlib.sha256(prefab.read_bytes()).hexdigest(),
                    "screenshot_path": screenshot.as_posix(),
                    "width": 800,
                    "height": 600,
                    "regions": [
                        {
                            "object_id": f"region-{prefab.stem}",
                            "node_name": "ReceiveButton",
                            "hierarchy_path": f"{prefab.stem}/ReceiveButton",
                            "rect": {"x": 100, "y": 220, "width": 180, "height": 72},
                        }
                    ],
                }
            )
        manifest = output_dir / "capture-manifest.json"
        manifest.write_text(
            json.dumps(
                {"schema_version": 1, "prefabs": captured, "errors": errors},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return manifest


class FeatureArchiveDloopUiTests(FeatureArchiveCliTestCase):
    def write_package(self, *args, **kwargs):
        path = super().write_package(*args, **kwargs)
        value = json.loads(path.read_bytes())
        value["delivery_requirements"] = [{"key": "result", "scenario": "结果可观察",
                                            "materials": ["verification"], "required": True}]
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def init_ui(self, feature_id: str = "unity-ui-delivery") -> tuple[Path, Path]:
        source = self.project_root / "requirements.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# 需求\n用户点击领取后看到剩余次数。\n", encoding="utf-8")
        self.run_cli(
            "init",
            "--feature-id",
            feature_id,
            "--title",
            "Unity UI 交付",
            "--configuration",
            "dloop-ui-v1",
            "--source-document",
            source,
        )
        return self.root / feature_id, source

    def write_prefab(self, name: str = "RewardPanel.prefab", guid: str = "1" * 32) -> Path:
        path = self.project_root / "Assets" / "UI" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PREFAB_TEXT.replace("RewardPanel", path.stem), encoding="utf-8")
        Path(str(path) + ".meta").write_text(
            f"fileFormatVersion: 2\nguid: {guid}\n",
            encoding="utf-8",
        )
        return path

    def write_brief(self, requirements: list[dict], name: str = "ui-brief.json") -> Path:
        path = self.project_root / name
        path.write_text(
            json.dumps(
                {"input_version": 1, "requirements": requirements},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def requirement(
        self,
        source: Path,
        *,
        key: str = "reward.receive",
        name: str = "领取奖励",
        match: dict,
    ) -> dict:
        return {
            "key": key,
            "name": name,
            "statement": f"用户完成{name}后能看到最新结果。",
            "source": {"path": source.as_posix(), "locator": f"需求文档/{name}"},
            "target_clues": [name, "奖励界面"],
            "inference_level": "explicit",
            "confidence": "high",
            "match": match,
        }

    def investigate(self, archive: Path, brief: Path, fixture: CaptureFixture | None = None):
        with mock.patch("archive_ui._capture_adapter", return_value=fixture):
            return self.run_cli(
                "ui-investigate",
                "--feature-id",
                archive.name,
                "--input",
                brief,
            )

    def write_review(self, investigation: dict, outcomes: list[dict]) -> Path:
        path = self.project_root / "ui-review.json"
        path.write_text(
            json.dumps(
                {
                    "input_version": 1,
                    "investigation_token": investigation["investigation_token"],
                    "outcomes": outcomes,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def change_outcome(self, investigation: dict, requirement: str = "reward.receive") -> dict:
        capture = investigation["captures"][0]
        return {
            "key": f"{requirement}.button",
            "requirements": [requirement],
            "prefab": capture["prefab"],
            "result": "change",
            "title": "领取按钮",
            "target": {
                "region": capture["regions"][0]["object_id"],
                "label": "领取按钮区域",
            },
            "instruction": "接入领取意图，并在成功结果返回后刷新剩余次数。",
            "expected": "用户点击领取后能看到最新剩余次数。",
            "inference_level": "explicit",
            "confidence": "high",
        }

    def complete_plan(self):
        archive, source = self.init_ui()
        self.run_cli("snapshot-save", "--feature-id", archive.name,
                     "--reason", "stage-start", "--stage", "investigation")
        prefab = self.write_prefab()
        brief = self.write_brief(
            [
                self.requirement(
                    source,
                    match={
                        "status": "matched",
                        "prefabs": [
                            {
                                "path": "Assets/UI/RewardPanel.prefab",
                                "name": "奖励界面",
                                "reason": "需求名称与资产名称唯一对应。",
                            }
                        ],
                    },
                )
            ]
        )
        investigation = self.investigate(archive, brief, CaptureFixture())
        self.run_cli("snapshot-save", "--feature-id", archive.name,
                     "--reason", "stage-start", "--stage", "plan")
        review = self.write_review(investigation, [self.change_outcome(investigation)])
        published = self.run_cli(
            "ui-publish",
            "--feature-id",
            archive.name,
            "--input",
            review,
        )
        return archive, source, prefab, investigation, published

    def test_ui_skill_stage_inputs_can_be_restored_and_republished(self) -> None:
        archive, _, _, investigation, _ = self.complete_plan()
        history = self.run_cli("snapshot-list", "--feature-id", archive.name)["snapshots"]
        starts = {item["stage"]: item["id"] for item in history if item["reason"] == "stage-start"}
        self.assertEqual({"requirements", "investigation", "plan"}, set(starts))
        plan = archive / "04-plan/ui-annotation-plan.md"
        published = plan.read_bytes()
        restored = self.run_cli("snapshot-restore", "--feature-id", archive.name,
                                "--stage", "plan", "--execute")
        self.assertEqual(starts["plan"], restored["snapshot"]["id"])
        self.assertFalse(plan.exists())
        self.run_cli("snapshot-save", "--feature-id", archive.name,
                     "--reason", "stage-start", "--stage", "plan")
        review = self.write_review(investigation, [self.change_outcome(investigation)])
        self.run_cli("ui-publish", "--feature-id", archive.name, "--input", review)
        self.assertEqual(published, plan.read_bytes())
        restored = self.run_cli("snapshot-restore", "--feature-id", archive.name,
                                "--stage", "investigation", "--execute")
        self.assertEqual(starts["investigation"], restored["snapshot"]["id"])
        self.assertFalse(plan.exists())
        model = json.loads((archive / "ui-model.json").read_bytes())
        self.assertEqual([], model["requirements"])

    def test_configuration_is_explicit_and_model_uses_only_current_schema(self) -> None:
        ordinary = self.init_complex("ordinary-delivery")
        ordinary_state = json.loads(
            ordinary.joinpath("workflow-state.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("configuration", ordinary_state)

        archive, _ = self.init_ui()
        state = json.loads(archive.joinpath("workflow-state.json").read_text(encoding="utf-8"))
        model = json.loads(archive.joinpath("ui-model.json").read_text(encoding="utf-8"))
        self.assertEqual("dloop-ui-v1", state["configuration"]["id"])
        self.assertEqual(5, model["schema_version"])

    def test_ui_snapshot_exports_registered_source_bytes_without_restoring_sources(self) -> None:
        sources = {
            (self.project_root / "中文需求" / "需求.txt").resolve(): "初始需求\r\n领取后刷新次数。\r\n".encode("utf-8"),
            (self.base / "参考资料" / "需求.txt").resolve(): b"external input\r\n\x00\xff",
        }
        for path, content in sources.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        unrelated = self.project_root / "unregistered.txt"
        unrelated.write_bytes(b"not a registered input")
        arguments = [value for source in sources for value in ("--source-document", source)]
        self.run_cli("init", "--feature-id", "source-delivery", "--title", "需求正文快照",
                     "--configuration", "dloop-ui-v1", *arguments)
        history = self.run_cli("snapshot-list", "--feature-id", "source-delivery")
        point = history["latest"]["id"]
        exported = self.base / "initial export"
        self.run_cli("snapshot-export", "--feature-id", "source-delivery",
                     "--snapshot-id", point, "--destination", exported)
        manifest = json.loads((exported / "snapshot.json").read_bytes())
        self.assertEqual(set(path.as_posix() for path in sources), set(manifest["inputs"]))
        self.assertEqual([], manifest["scopes"])
        self.assertIsNone(manifest["workspace"])
        self.assertEqual(2, len(set(manifest["inputs"].values())))
        for source, content in sources.items():
            self.assertEqual(content, (exported / manifest["inputs"][source.as_posix()]).read_bytes())
        self.assertFalse(any(path.startswith("workspace/") for path in manifest["files"]))
        import archive_snapshots
        with mock.patch.object(archive_snapshots, "_atomic", wraps=archive_snapshots._atomic) as writes:
            restored = self.run_cli("snapshot-restore", "--feature-id", "source-delivery",
                                    "--snapshot-id", point, "--execute")
        self.assertFalse(any(call.args[0] in sources for call in writes.call_args_list))
        self.assertFalse(any(path.startswith("inputs/") for path in restored["changed_files"]))
        forked = self.run_cli("snapshot-fork", "--source-feature-id", "source-delivery",
                              "--snapshot-id", point, "--feature-id", "source-rework", "--title", "重做需求")
        for source, content in sources.items():
            self.assertEqual(content, source.read_bytes())
            self.assertEqual(content, (Path(forked["origin"]["export"]) / manifest["inputs"][source.as_posix()]).read_bytes())
        self.assertEqual(b"not a registered input", unrelated.read_bytes())

    def test_ui_snapshot_records_missing_registered_input_without_inventing_content(self) -> None:
        source = (self.project_root / "missing.md").resolve()
        self.run_cli("init", "--feature-id", "missing-source", "--title", "待补充需求",
                     "--configuration", "dloop-ui-v1", "--source-document", source)
        point = self.run_cli("snapshot-list", "--feature-id", "missing-source")["latest"]["id"]
        exported = self.base / "missing input export"
        self.run_cli("snapshot-export", "--feature-id", "missing-source",
                     "--snapshot-id", point, "--destination", exported)
        manifest = json.loads((exported / "snapshot.json").read_bytes())
        self.assertEqual({source.as_posix(): None}, manifest["inputs"])
        self.assertFalse((exported / "inputs").exists())

    def test_open_editor_request_and_manifest_complete_existing_publish_flow(self) -> None:
        archive, source, _, _, _ = self.complete_plan()
        brief = self.write_brief([self.requirement(source, match={
            "status": "matched", "prefabs": [{
                "path": "Assets/UI/RewardPanel.prefab", "name": "奖励界面", "reason": "对应领取需求",
            }],
        })])
        before = {path: path.read_bytes() for path in archive.rglob("*") if path.is_file()}
        original_run = subprocess.run
        def without_editor(command, *arguments, **options):
            self.assertNotIn("-batchmode", command, "不得启动第二个编辑器")
            return original_run(command, *arguments, **options)
        with mock.patch.dict("os.environ", {"DLOOP_UNITY_EXE": "unused-Unity.exe"}), \
                mock.patch("subprocess.run", side_effect=without_editor):
            prepared = self.run_cli("ui-investigate", "--feature-id", archive.name, "--input", brief)
        self.assertEqual("awaiting_capture", prepared["status"])
        self.assertNotIn("investigation_token", prepared)
        self.assertEqual(before, {path: path.read_bytes() for path in archive.rglob("*") if path.is_file()})
        request_path = Path(prepared["capture_request"])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        manifest = CaptureFixture().capture(request_path, self.project_root, Path(request["output_dir"]))
        with mock.patch("subprocess.run", side_effect=without_editor):
            investigation = self.run_cli("ui-investigate", "--feature-id", archive.name,
                                         "--input", brief, "--capture-manifest", manifest)
        self.assertEqual("ready_for_review", investigation["status"])
        review = self.write_review(investigation, [self.change_outcome(investigation)])
        published = self.run_cli("ui-publish", "--feature-id", archive.name, "--input", review)
        self.assertEqual("complete", published["status"])
        self.assertTrue((archive / investigation["captures"][0]["screenshot"]).is_file())

    def test_manifest_from_previous_asset_content_is_rejected_without_archive_changes(self) -> None:
        archive, source = self.init_ui()
        prefab = self.write_prefab()
        brief = self.write_brief([self.requirement(source, match={
            "status": "matched", "prefabs": [{
                "path": "Assets/UI/RewardPanel.prefab", "name": "奖励界面", "reason": "对应领取需求",
            }],
        })])
        prepared = self.run_cli("ui-investigate", "--feature-id", archive.name, "--input", brief)
        request_path = Path(prepared["capture_request"])
        manifest = CaptureFixture().capture(request_path, self.project_root, request_path.parent)
        prefab.write_text(prefab.read_text(encoding="utf-8") + "\n# 实施前调整资源\n", encoding="utf-8")
        before = {path: path.read_bytes() for path in archive.rglob("*") if path.is_file()}
        rejected = self.run_cli("ui-investigate", "--feature-id", archive.name, "--input", brief,
                                "--capture-manifest", manifest, expected=1)
        self.assertEqual("DLOOP_UI_CAPTURE_PROTOCOL_INVALID", rejected["code"])
        self.assertIn("内容摘要不一致", rejected["message"])
        self.assertEqual(before, {path: path.read_bytes() for path in archive.rglob("*") if path.is_file()})
        pending = self.run_cli("ui-investigate", "--feature-id", archive.name, "--input", brief)
        self.assertNotIn("resolved_friction_ids", pending)
        summary = self.run_cli("friction-summary", "--feature-id", archive.name)
        self.assertEqual("failed", summary["incidents"][0]["operation_status"])
        request_path = Path(pending["capture_request"])
        manifest = CaptureFixture().capture(request_path, self.project_root, request_path.parent)
        recovered = self.run_cli("ui-investigate", "--feature-id", archive.name, "--input", brief,
                                 "--capture-manifest", manifest)
        self.assertIn(rejected["friction_id"], recovered["resolved_friction_ids"])

    def test_noncurrent_ui_model_is_rejected_without_compatibility(self) -> None:
        archive, _ = self.init_ui()
        model_path = archive / "ui-model.json"
        model = json.loads(model_path.read_text(encoding="utf-8"))
        model["schema_version"] = 2
        model_path.write_text(json.dumps(model), encoding="utf-8")
        state = json.loads(archive.joinpath("workflow-state.json").read_text(encoding="utf-8"))

        with self.assertRaises(ArchiveConfigurationError) as raised:
            configuration_evaluation(archive, state, "final")

        self.assertEqual("UNSUPPORTED_DLOOP_UI_FORMAT", raised.exception.code)

    def test_missing_prefab_is_published_as_one_nonblocking_result(self) -> None:
        archive, source = self.init_ui()
        brief = self.write_brief(
            [
                self.requirement(
                    source,
                    match={
                        "status": "not-found",
                        "detail": "奖励界面 Prefab 不存在。",
                        "searched_paths": ["Assets/UI"],
                    },
                )
            ]
        )

        investigation = self.investigate(archive, brief)
        review = self.write_review(investigation, [])
        published = self.run_cli(
            "ui-publish", "--feature-id", archive.name, "--input", review
        )

        self.assertEqual("ready_to_publish", investigation["status"])
        self.assertEqual("no_applicable_prefab", published["status"])
        self.assertEqual(0, published["computed_counts"]["matched_prefabs"])
        plan = archive.joinpath("04-plan", "ui-annotation-plan.md").read_text(encoding="utf-8")
        self.assertIn("没有可用截图", plan)
        self.assertIn("用户完成领取奖励后能看到最新结果", plan)
        self.assertIn("requirements.md / 需求文档/领取奖励", plan)
        self.assertNotIn(str(self.project_root), plan)
        _, baseline = self.submit_baseline(archive)
        self.assertEqual("blocked", baseline["status"])
        self.assertIn("prefabs", [item["category"] for item in baseline["blockers"]])

    def test_existing_prefab_uses_two_business_commands_and_publishes_one_plan(self) -> None:
        archive, _, _, investigation, published = self.complete_plan()

        self.assertEqual("ready_for_review", investigation["status"])
        self.assertEqual("complete", published["status"])
        self.assertTrue(archive.joinpath(investigation["captures"][0]["screenshot"]).is_file())
        plan_path = archive / "04-plan" / "ui-annotation-plan.md"
        plan = plan_path.read_text(encoding="utf-8")
        self.assertIn("内部草稿", plan)
        self.assertIn("Assets/UI/RewardPanel.prefab", plan)
        self.assertIn("编号截图](ui-annotations/", plan)
        self.assertNotIn("原始截图：[查看]", plan)
        self.assertIn("dependencies: [unity-ui-delivery.requirements.overview]", plan)
        self.assertIn("document_id: unity-ui-delivery.plan.ui-annotation-plan", plan)
        self.assertIn("category: plan", plan)
        self.assertIn("采集与定位缺口", plan)
        self.assertNotIn("保留既有行为 |", plan)
        model = json.loads(archive.joinpath("ui-model.json").read_text(encoding="utf-8"))
        self.assertNotIn("preserve", model["annotations"][0])
        self.assertEqual(3, len(published["artifacts"]))
        self.assertEqual(1, len(list((archive / "04-plan").rglob("*annotated.svg"))))
        annotation = next((archive / "04-plan").rglob("*annotated.svg"))
        self.assertIn('<image href="data:image/png;base64,', annotation.read_text(encoding="utf-8"))

    def test_ui_contract_registration_requires_design_material(self) -> None:
        archive, _, _, _, _ = self.complete_plan()
        package = self.write_package(
            "ui-slice",
            feature_id=archive.name,
            approve_plan=False,
        )

        rejected = self.run_cli(
            "prepare-slice-contract",
            "--feature-id", archive.name,
            "--package-file", package,
            expected=1,
        )
        self.assertEqual("CONFIGURATION_TASK_MATERIAL_REQUIRED", rejected["code"])

        value = json.loads(package.read_text(encoding="utf-8"))
        value["context_materials"][0].update(mode="full")
        value["context_materials"][0].pop("sections", None)
        value["context_materials"].append({
            "source": f"{archive.name}.design.overview",
            "purpose": "读取 UI 设计与实施要求",
            "mode": "full",
        })
        package.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        prepared = self.run_cli(
            "prepare-slice-contract",
            "--feature-id", archive.name,
            "--package-file", package,
        )

        self.assertEqual("contract_draft", prepared["status"])

    def test_reuse_cannot_hide_implementation_work(self) -> None:
        archive, source = self.init_ui()
        self.write_prefab()
        brief = self.write_brief(
            [
                self.requirement(
                    source,
                    match={
                        "status": "matched",
                        "prefabs": [
                            {
                                "path": "Assets/UI/RewardPanel.prefab",
                                "name": "奖励界面",
                                "reason": "唯一对应。",
                            }
                        ],
                    },
                )
            ]
        )
        investigation = self.investigate(archive, brief, CaptureFixture())
        capture = investigation["captures"][0]
        invalid = {
            "key": "reward.receive.reuse",
            "requirements": ["reward.receive"],
            "prefab": capture["prefab"],
            "result": "reuse",
            "title": "领取按钮",
            "target": {"region": capture["regions"][0]["object_id"], "label": "领取按钮"},
            "instruction": "接入领取意图。",
            "inference_level": "explicit",
            "confidence": "high",
        }
        review = self.write_review(investigation, [invalid])

        rejected = self.run_cli(
            "ui-publish", "--feature-id", archive.name, "--input", review, expected=1
        )

        self.assertEqual("DLOOP_UI_INPUT_INVALID", rejected["code"])
        self.assertFalse((archive / "04-plan" / "ui-annotation-plan.md").exists())

    def test_new_investigation_replaces_old_matches_annotations_skips_and_media(self) -> None:
        archive, source, _, _, published = self.complete_plan()
        old_svg = archive / next(path for path in published["artifacts"] if path.endswith(".svg"))
        self.assertTrue(old_svg.is_file())
        replacement_brief = self.write_brief(
            [
                self.requirement(
                    source,
                    match={
                        "status": "not-found",
                        "detail": "重新核对后没有对应 Prefab。",
                        "searched_paths": ["Assets/UI"],
                    },
                )
            ],
            "replacement-brief.json",
        )

        investigation = self.investigate(archive, replacement_brief)

        model = json.loads(archive.joinpath("ui-model.json").read_text(encoding="utf-8"))
        self.assertEqual([], model["prefabs"])
        self.assertEqual([], model["annotations"])
        self.assertEqual(1, len(model["skips"]))
        self.assertFalse(old_svg.exists())
        self.assertFalse((archive / "04-plan" / "ui-annotation-plan.md").exists())
        self.assertEqual(0, investigation["computed_counts"]["matched_prefabs"])

    def test_one_capture_failure_keeps_other_prefab_publishable(self) -> None:
        archive, source = self.init_ui()
        self.write_prefab("RewardPanel.prefab", "1" * 32)
        self.write_prefab("DetailPanel.prefab", "2" * 32)
        requirements = [
            self.requirement(
                source,
                match={
                    "status": "matched",
                    "prefabs": [{"path": "Assets/UI/RewardPanel.prefab", "name": "奖励界面", "reason": "唯一对应。"}],
                },
            ),
            self.requirement(
                source,
                key="reward.detail",
                name="查看详情",
                match={
                    "status": "matched",
                    "prefabs": [{"path": "Assets/UI/DetailPanel.prefab", "name": "详情界面", "reason": "唯一对应。"}],
                },
            ),
        ]
        brief = self.write_brief(requirements)
        investigation = self.investigate(
            archive,
            brief,
            CaptureFixture(("DetailPanel.prefab",)),
        )
        review = self.write_review(investigation, [self.change_outcome(investigation)])

        published = self.run_cli(
            "ui-publish", "--feature-id", archive.name, "--input", review
        )

        self.assertEqual("completed_with_skips", published["status"])
        self.assertEqual("capture-failed", published["skips"][0]["reason"])
        self.assertEqual(1, published["computed_counts"]["annotations"])

    def test_source_and_screenshot_drift_reopen_their_own_gates(self) -> None:
        archive, source, _, investigation, _ = self.complete_plan()
        state = json.loads(archive.joinpath("workflow-state.json").read_text(encoding="utf-8"))
        source.write_text("# 已改变\n", encoding="utf-8")
        requirements = configuration_evaluation(archive, state, "final")
        self.assertEqual("BLOCKED", requirements["status"])
        source.write_text("# 需求\n用户点击领取后看到剩余次数。\n", encoding="utf-8")
        screenshot = archive / investigation["captures"][0]["screenshot"]
        screenshot.unlink()
        plan = configuration_evaluation(archive, state, "final")
        self.assertEqual("BLOCKED", plan["status"])

    def test_share_contains_internal_model_raw_capture_plan_and_annotation(self) -> None:
        archive, _, _, investigation, published = self.complete_plan()

        exported = self.run_cli("export-share", "--feature-id", archive.name)
        share = Path(exported["share_location"]["path"])

        self.assertTrue((share / "ui-model.json").is_file())
        self.assertTrue((share / investigation["captures"][0]["screenshot"]).is_file())
        for artifact in published["artifacts"]:
            self.assertTrue((share / artifact).is_file())

    def prepare_ui_execution(self, approve_baseline=True):
        archive, _, _, investigation, _ = self.complete_plan()
        self.set_status(archive / "01-requirements/terminology.md", "confirmed")
        self.set_status(archive / "01-requirements/README.md", "confirmed")
        self.prepare_execution_inputs(archive.name)
        self.run_cli("transition-lifecycle", "--feature-id", archive.name, "--to", "active")
        package = self.write_package("ui-slice", feature_id=archive.name)
        value = json.loads(package.read_text(encoding="utf-8"))
        value["context_materials"][0].update(mode="full")
        value["context_materials"][0].pop("sections", None)
        value["context_materials"].append({
            "source": f"{archive.name}.design.overview", "purpose": "读取 UI 内部设计", "mode": "full",
        })
        package.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        if approve_baseline:
            self.submit_baseline(archive, approve=True)
        return archive, investigation, package

    def submit_baseline(self, archive, *, approve=False, value=None, semantic_change=True):
        prepared = self.run_cli("prepare-action-input", "--feature-id", archive.name, "--input-kind", "ui-baseline")
        if value is None:
            value = prepared["template"]
            for category in ("protocols", "configurations"):
                source = self.project_root / (category + ".md")
                source.write_text("业务输入定义：领取请求返回最新剩余次数；领取次数上限来自配置。", encoding="utf-8")
                for item in value["items"]:
                    item[category] = [{"status": "verified", "reference": str(source), "purpose": "领取操作使用定义中的次数字段与限制"}]
        path = Path(prepared["target"])
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        result = self.run_cli("ui-baseline", "--feature-id", archive.name, "--input", path,
                              *(() if semantic_change else ("--semantic-change", "false")))
        if approve:
            self.run_cli("stage-action", "--feature-id", archive.name, "--stage", "ui-baseline", "--decision", "approve",
                         "--reviewed-digest", result["reviewed_digest"], "--user-confirmation", "用户本轮回复：同意按展示的材料开工")
        return value, result

    def ui_approval_view(self, archive):
        return self.run_cli("workflow-status", "--feature-id", archive.name)["delivery_view"]

    def test_internal_requirement_problem_points_to_document_not_human_approval(self):
        archive, _, _, _, _ = self.complete_plan()
        _, baseline = self.submit_baseline(archive)
        rejected = self.run_cli(
            "stage-action", "--feature-id", archive.name, "--stage", "ui-baseline", "--decision", "approve",
            "--reviewed-digest", baseline["reviewed_digest"], "--user-confirmation", "用户确认当前开工清单", expected=1,
        )
        self.assertEqual("UI_REQUIREMENTS_NOT_READY", rejected["code"])
        self.assertIn("requirements.overview", rejected["message"])
        self.assertIn("draft", rejected["message"])
        context = self.run_cli("context-summary", "--feature-id", archive.name,
                               "--action", "investigation", "--role", "coordinator")
        self.assertIn("UI_REQUIREMENTS_NOT_READY", [item["code"] for item in context["blockers"]])
        self.set_status(archive / "01-requirements/README.md", "confirmed")
        _, baseline = self.submit_baseline(archive)
        approved = self.run_cli(
            "stage-action", "--feature-id", archive.name, "--stage", "ui-baseline", "--decision", "approve",
            "--reviewed-digest", baseline["reviewed_digest"], "--user-confirmation", "用户确认当前开工清单",
        )
        self.assertEqual("approve", self.ui_approval_view(archive)["conclusions"]["approvals"]["ui-baseline"]["status"])

    def test_internal_draft_reuses_publication_without_granting_final_acceptance(self):
        archive, _, _, investigation, published = self.complete_plan()
        page = archive / "06-validation/ui-delivery.html"
        before = page.read_bytes()
        rejected = self.run_cli("prepare-action-input", "--feature-id", archive.name,
                                "--input-kind", "ui-delivery", expected=1)
        self.assertEqual("ACCEPTED_IMPLEMENTATION_REQUIRED", rejected["code"])
        self.assertIn("内部草稿", rejected["message"])
        self.assertIn("ui-publish", rejected["message"])
        review = self.write_review(investigation, [self.change_outcome(investigation)])
        repeated = self.run_cli("ui-publish", "--feature-id", archive.name, "--input", review)
        self.assertFalse(repeated["delivery_ready"])
        self.assertEqual(before, page.read_bytes())
        state = json.loads((archive / "workflow-state.json").read_bytes())
        self.assertIsNone(state["approvals"]["final"])
        evaluated = configuration_evaluation(archive, state, "final")
        self.assertEqual("BLOCKED", evaluated["status"])
        self.assertNotIn("UI 截图或需求证据已变化或缺失", evaluated["message"])

    def test_capture_request_during_implementation_stays_in_managed_version_directory(self):
        archive, _, package = self.prepare_ui_execution()
        self.run_cli("start-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                     "--package-file", package, "--workspace-root", self.workspace)
        pending = self.run_cli("ui-investigate", "--feature-id", archive.name,
                               "--input", self.project_root / "ui-brief.json")
        request = Path(pending["capture_request"])
        self.assertEqual((archive.parent.parent / "captures" / archive.name).resolve(), request.parent.resolve())
        manifest = CaptureFixture().capture(request, self.project_root, request.parent)
        captured = self.run_cli("ui-investigate", "--feature-id", archive.name,
                                "--input", self.project_root / "ui-brief.json", "--capture-manifest", manifest)
        review = self.write_review(captured, [self.change_outcome(captured)])
        self.run_cli("ui-publish", "--feature-id", archive.name, "--input", review)
        checkpoint = self.record_checkpoint("ui-impl", feature_id=archive.name)
        self.assertFalse(checkpoint["checkpoint"]["boundary_crossed"])

    def test_ui_baseline_waiting_keeps_material_write_targets_without_allowing_implementation(self):
        archive, _, package = self.prepare_ui_execution(approve_baseline=False)
        for stage, writable in (
            ("ui-inputs", {"investigation"}),
            ("ui-inputs-blocked", {"investigation"}),
            ("ui-baseline-review", {"investigation", "design", "plan"}),
        ):
            with self.subTest(stage=stage):
                if stage != "ui-inputs":
                    value, _ = self.submit_baseline(archive)
                    if stage == "ui-inputs-blocked":
                        value["items"][0]["protocols"] = [{"status": "missing", "reference": "已检查协议目录",
                                                            "purpose": "尚未明确领取返回字段，需继续调查"}]
                        self.submit_baseline(archive, value=value)
                for action, directory in (
                    ("investigation", "02-investigation"), ("design", "03-design"),
                    ("plan", "04-plan"), ("implementation", "05-implementation"),
                    ("validation", "06-validation"),
                ):
                    with self.subTest(action=action):
                        context = self.run_cli("context-summary", "--feature-id", archive.name,
                                               "--action", action, "--role", "coordinator")
                        self.assertEqual(stage, context["delivery_view"]["current_stage"])
                        self.assertEqual([], context["blockers"])
                        targets = context["role_view"]["write_targets"]
                        self.assertEqual(
                            [(archive / directory / "README.md").resolve()] if action in writable else [],
                            [Path(target["path"]) for target in targets],
                        )
                rejected = self.run_cli("start-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                                        "--package-file", package, "--workspace-root", self.workspace, expected=1)
                self.assertEqual("UI_BASELINE_APPROVAL_REQUIRED", rejected["code"])
                handoff = self.run_cli("prepare-handoff", "--feature-id", archive.name, "--action", "implementation",
                                       "--role", "implementation", "--execution-id", "ui-impl")
                self.assertFalse(handoff["handoff_ready"])
                self.assertIn("UI_BASELINE_APPROVAL_REQUIRED", json.dumps(handoff))

    def test_ui_baseline_wording_revision_preserves_approval_and_handoff(self):
        archive, _, package = self.prepare_ui_execution()
        value = json.loads((archive / "04-plan/ui-baseline-input.json").read_bytes())
        before = json.loads((archive / "workflow-state.json").read_bytes())["approvals"]["ui-baseline"]
        for punctuation in ("。", "（原意不变）"):
            value["items"][0]["protocols"][0]["purpose"] += punctuation
            _, result = self.submit_baseline(archive, value=value, semantic_change=False)
            self.assertTrue(result["approval_preserved"])
            self.assertEqual(before["reviewed_digest"], result["reviewed_digest"])
            self.assertEqual("workflow-status", result["next_action"]["command"])
            self.assertIn(value["items"][0]["protocols"][0]["purpose"],
                          Path(result["materials"][0]).read_text(encoding="utf-8"))
            self.assertEqual(before, json.loads((archive / "workflow-state.json").read_bytes())["approvals"]["ui-baseline"])
        # 原样重交不会把之前的纯文案修订重新当作业务变化。
        self.assertTrue(self.submit_baseline(archive, value=value)[1]["approval_preserved"])
        self.run_cli("start-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                     "--package-file", package, "--workspace-root", self.workspace)
        handoff = self.run_cli("prepare-handoff", "--feature-id", archive.name, "--action", "implementation",
                               "--role", "implementation", "--execution-id", "ui-impl")
        self.assertTrue(handoff["handoff_ready"])

    def test_ui_baseline_wording_revision_does_not_grant_or_revive_approval(self):
        archive, _, package = self.prepare_ui_execution(approve_baseline=False)
        value, result = self.submit_baseline(archive)
        for decision, expected in ((None, "pending"), ("reject", "reject"), ("approve", "stale")):
            with self.subTest(status=expected):
                if decision:
                    self.run_cli("stage-action", "--feature-id", archive.name, "--stage", "ui-baseline",
                                 "--decision", decision, "--reviewed-digest", result["reviewed_digest"],
                                 "--user-confirmation", "用户本轮实际回复：" + decision)
                if expected == "stale":
                    value["items"][0]["protocols"][0]["purpose"] = "改为订阅推送并按新字段刷新"
                    self.submit_baseline(archive, value=value)
                value["items"][0]["protocols"][0]["purpose"] += "。"
                _, result = self.submit_baseline(archive, value=value, semantic_change=False)
                self.assertFalse(result["approval_preserved"])
                self.assertEqual(expected, self.ui_approval_view(archive)["conclusions"]["approvals"]["ui-baseline"]["status"])
                rejected = self.run_cli("start-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                                        "--package-file", package, "--workspace-root", self.workspace, expected=1)
                self.assertEqual("UI_BASELINE_APPROVAL_REQUIRED", rejected["code"])

    def test_ui_baseline_wording_revision_rejects_changed_materials(self):
        archive, _, _ = self.prepare_ui_execution()
        original = json.loads((archive / "04-plan/ui-baseline-input.json").read_bytes())
        before = (archive / "04-plan/ui-implementation-baseline.json").read_bytes()
        replacement = self.project_root / "replacement-protocol.md"
        replacement.write_text("另一份业务协议", encoding="utf-8")
        for field, changed in (("reference", str(replacement)), ("status", "missing")):
            with self.subTest(field=field):
                value = json.loads(json.dumps(original))
                value["items"][0]["protocols"][0][field] = changed
                path = archive / "04-plan/ui-baseline-input.json"
                path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                result = self.run_cli("ui-baseline", "--feature-id", archive.name, "--input", path,
                                      "--semantic-change", "false", expected=1)
                self.assertEqual("INVALID_UI_BASELINE", result["code"])
                self.assertEqual(before, (archive / "04-plan/ui-implementation-baseline.json").read_bytes())

    def test_ui_baseline_each_missing_category_blocks_whole_delivery(self):
        archive, _, package = self.prepare_ui_execution(approve_baseline=False)
        complete, _ = self.submit_baseline(archive)
        for category in ("prefabs", "protocols", "configurations", "requirement_sources"):
            with self.subTest(category=category):
                value = json.loads(json.dumps(complete))
                value["items"][0][category] = [{"status": "missing", "reference": "已检查项目相关目录",
                                                "purpose": "无法确定领取行为，请补充对应材料"}]
                _, result = self.submit_baseline(archive, value=value)
                self.assertEqual("blocked", result["status"])
                self.assertIn(category, [item["category"] for item in result["blockers"]])
                view = self.ui_approval_view(archive)
                self.assertEqual("ui-inputs-blocked", view["current_stage"])
                self.assertTrue(view["requires_human"]["required"])
                before = (archive / "workflow-state.json").read_bytes()
                rejected = self.run_cli("start-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                                        "--package-file", package, "--workspace-root", self.workspace, expected=1)
                self.assertEqual("UI_BASELINE_APPROVAL_REQUIRED", rejected["code"])
                self.assertNotIn("friction_id", rejected)
                self.assertEqual(before, (archive / "workflow-state.json").read_bytes())
                rejected = self.run_cli("stage-action", "--feature-id", archive.name, "--stage", "ui-baseline",
                                        "--decision", "approve", "--reviewed-digest", result["reviewed_digest"],
                                        "--user-confirmation", "用户要求继续", expected=1)
                self.assertEqual("UI_BASELINE_INCOMPLETE", rejected["code"])

    def test_ui_baseline_complete_waits_for_current_user_reply(self):
        archive, _, package = self.prepare_ui_execution(approve_baseline=False)
        value, result = self.submit_baseline(archive)
        self.assertEqual("ready", result["status"])
        view = self.ui_approval_view(archive)
        self.assertEqual("ui-baseline-review", view["current_stage"])
        self.assertIn("user_confirmation", view["next_action_contract"]["required_inputs"])
        start = ("start-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                 "--package-file", package, "--workspace-root", self.workspace)
        self.assertEqual("UI_BASELINE_APPROVAL_REQUIRED", self.run_cli(*start, expected=1)["code"])
        approve = ("stage-action", "--feature-id", archive.name, "--stage", "ui-baseline", "--decision", "approve")
        self.assertEqual("USER_CONFIRMATION_REQUIRED", self.run_cli(*approve, "--reviewed-digest", result["reviewed_digest"], expected=1)["code"])
        value["items"][0]["protocols"][0]["purpose"] = "使用领取回包里的最新次数，并说明失败不刷新"
        _, current = self.submit_baseline(archive, value=value)
        self.assertEqual("UI_BASELINE_STALE", self.run_cli(*approve, "--reviewed-digest", result["reviewed_digest"],
                         "--user-confirmation", "用户前轮回复：同意", expected=1)["code"])
        self.run_cli("stage-action", "--feature-id", archive.name, "--stage", "ui-baseline", "--decision", "reject",
                     "--reviewed-digest", current["reviewed_digest"], "--user-confirmation", "用户回复：暂不开工")
        self.assertEqual("UI_BASELINE_APPROVAL_REQUIRED", self.run_cli(*start, expected=1)["code"])
        self.run_cli(*approve, "--reviewed-digest", current["reviewed_digest"], "--user-confirmation", "用户新回复：同意当前方案")
        self.run_cli(*start)
        self.assertFalse(self.ui_approval_view(archive)["requires_human"]["required"])
        handoff = self.run_cli("prepare-handoff", "--feature-id", archive.name, "--action", "implementation",
                               "--role", "implementation", "--execution-id", "ui-impl", "--include-role-view")
        self.assertTrue(handoff["handoff_ready"])
        self.assertIn("ui-implementation-baseline.json", json.dumps(handoff))
        self.assertIn("protocols.md", Path(result["materials"][0]).read_text(encoding="utf-8"))

    def test_ui_baseline_change_blocks_resume_handoff_and_retry(self):
        archive, _, package = self.prepare_ui_execution()
        start = ("start-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                 "--package-file", package, "--workspace-root", self.workspace)
        self.run_cli(*start)
        value, _ = self.submit_baseline(archive)
        value["items"][0]["protocols"][0]["purpose"] = "改为订阅推送并按新字段刷新"
        self.submit_baseline(archive, value=value)
        self.assertEqual("UI_BASELINE_APPROVAL_REQUIRED", self.run_cli(*start, expected=1)["code"])
        context = self.run_cli("context-summary", "--feature-id", archive.name, "--action", "implementation",
                               "--role", "implementation", "--execution-id", "ui-impl")
        self.assertIn("UI_BASELINE_APPROVAL_REQUIRED", json.dumps(context))
        handoff = self.run_cli("prepare-handoff", "--feature-id", archive.name, "--action", "implementation",
                               "--role", "implementation", "--execution-id", "ui-impl")
        self.assertFalse(handoff["handoff_ready"])
        self.run_cli("submit-slice", "--feature-id", archive.name, "--execution-id", "ui-impl", "--status", "interrupted")
        rejected = self.run_cli("resolve-slice", "--feature-id", archive.name, "--package-id", "ui-slice",
                                "--action", "retry", "--execution-id", "ui-retry", expected=1)
        self.assertEqual("UI_BASELINE_APPROVAL_REQUIRED", rejected["code"])
        self.submit_baseline(archive, value=value, approve=True)
        self.run_cli("resolve-slice", "--feature-id", archive.name, "--package-id", "ui-slice",
                     "--action", "retry", "--execution-id", "ui-retry")

    def test_ui_baseline_not_applicable_requires_reason_and_cannot_hide_prefab(self):
        archive, _, _ = self.prepare_ui_execution(approve_baseline=False)
        value, _ = self.submit_baseline(archive)
        value["items"][0]["protocols"] = [{"status": "not_applicable", "reference": "", "purpose": ""}]
        self.assertEqual("blocked", self.submit_baseline(archive, value=value)[1]["status"])
        value["items"][0]["protocols"][0]["purpose"] = "此场景只使用本地状态，不调用后端接口"
        self.assertEqual("ready", self.submit_baseline(archive, value=value)[1]["status"])
        value["items"][0]["prefabs"] = [{"status": "not_applicable", "reference": "", "purpose": "稍后新建"}]
        self.assertEqual("blocked", self.submit_baseline(archive, value=value)[1]["status"])

    def test_ui_baseline_ambiguity_nonexistent_reference_and_omitted_requirement_block(self):
        archive, _, _ = self.prepare_ui_execution(approve_baseline=False)
        complete, _ = self.submit_baseline(archive)
        for status, reference in (("ambiguous", "协议候选一、协议候选二"), ("verified", "missing-protocol.md")):
            value = json.loads(json.dumps(complete))
            value["items"][0]["protocols"][0].update(status=status, reference=reference)
            self.assertEqual("blocked", self.submit_baseline(archive, value=value)[1]["status"])
        self.assertEqual("blocked", self.submit_baseline(archive, value={"input_version": 1, "items": []})[1]["status"])
        complete["items"][0]["prefabs"][0]["status"] = "planned"
        path = archive / "04-plan/ui-baseline-input.json"
        path.write_text(json.dumps(complete), encoding="utf-8")
        self.assertEqual("INVALID_UI_BASELINE", self.run_cli("ui-baseline", "--feature-id", archive.name, "--input", path, expected=1)["code"])

    def test_ui_baseline_new_requirement_invalidates_approval_without_resubmission(self):
        archive, _, package = self.prepare_ui_execution()
        match = {"status": "matched", "prefabs": [{"path": "Assets/UI/RewardPanel.prefab", "name": "奖励界面", "reason": "同一界面"}]}
        source = self.project_root / "requirements.md"
        brief = self.write_brief([self.requirement(source, match=match), self.requirement(source, key="reward.preview", name="预览奖励", match=match)])
        self.investigate(archive, brief, CaptureFixture())
        view = self.ui_approval_view(archive)
        self.assertEqual("stale", view["conclusions"]["approvals"]["ui-baseline"]["status"])
        self.assertIn("reward.preview", json.dumps(view["blockers"]))
        rejected = self.run_cli("start-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                                "--package-file", package, "--workspace-root", self.workspace, expected=1)
        self.assertEqual("UI_BASELINE_APPROVAL_REQUIRED", rejected["code"])

    def prepare_material_candidate(self):
        archive, investigation, package = self.prepare_ui_execution()
        value = json.loads(package.read_bytes())
        value["delivery_requirements"][0]["materials"] = ["interaction", "ui-location", "screenshot", "verification"]
        package.write_text(json.dumps(value), encoding="utf-8")
        self.run_cli("start-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                     "--package-file", package, "--workspace-root", self.workspace)
        implementation = self.workspace / "ui-slice.txt"
        implementation.write_text("点击领取，成功后刷新剩余次数", encoding="utf-8")
        material = archive / "05-implementation/interactions.json"
        outcome = self.change_outcome(investigation)
        outcome["interaction"] = self.interaction(implementation)
        material.write_text(json.dumps({"input_version": 1, "investigation_token": investigation["investigation_token"],
                                       "outcomes": [outcome]}, ensure_ascii=False), encoding="utf-8")
        self.record_checkpoint("ui-impl", feature_id=archive.name)
        prepared = self.run_cli("prepare-action-input", "--feature-id", archive.name,
                                "--input-kind", "candidate", "--execution-id", "ui-impl")
        candidate = Path(prepared["target"])
        value = prepared["template"]
        value["candidate_id"] = "ui-candidate"
        value["verification"] = ["领取后刷新结果已验证"]
        for item in value["delivery_materials"]:
            item["locator"] = "领取场景"
            item["path"] = str(implementation if item["kind"] == "verification" else
                               archive / investigation["captures"][0]["screenshot"] if item["kind"] == "screenshot" else material)
            if item["kind"] == "verification":
                item["status"] = "passed"
        candidate.write_text(json.dumps(value), encoding="utf-8")
        return archive, investigation, candidate, material, implementation

    def submit_material_candidate(self, archive, candidate, expected=0):
        return self.run_cli("submit-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                            "--status", "completed", "--candidate-file", candidate, expected=expected)

    def accept_material_candidate(self, archive, candidate, evidence):
        self.submit_material_candidate(archive, candidate)
        self.run_cli("review-slice", "--feature-id", archive.name, "--package-id", "ui-slice",
                     "--candidate-id", "ui-candidate", "--review-execution-id", "ui-review", "--result", "passed")
        prepared = self.run_cli("prepare-action-input", "--feature-id", archive.name, "--input-kind", "ui-delivery")
        value = prepared["template"]
        self.assertNotIn("outcomes", value)
        value["delivery_review"] = {"requirements_check": "完整需求已核对",
                                    "implementation_check": "已从实现反查交互",
                                    "evidence": [{"path": str(evidence), "locator": "领取结果"}]}
        path = Path(prepared["target"])
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_scenario_delivery_reuses_records_and_keeps_static_results_out_of_final_approval(self):
        archive, _, package = self.prepare_ui_execution()
        value = json.loads(package.read_bytes())
        value["delivery_requirements"][0]["materials"] = ["acceptance"]
        value["slice_contract"]["acceptance_scenarios"].append("跨日刷新")
        value["delivery_requirements"].append({"key": "daily", "scenario": "跨日刷新", "materials": ["acceptance"],
                                                "required": False, "reason": "用户明确将跨日联调留至下一期"})
        package.write_text(json.dumps(value), encoding="utf-8")
        self.run_cli("start-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                     "--package-file", package, "--workspace-root", self.workspace)
        prepared = self.run_cli("prepare-action-input", "--feature-id", archive.name,
                                "--input-kind", "acceptance", "--execution-id", "ui-impl")
        evidence = self.workspace / "ui-slice.txt"
        evidence.write_text("受控数据下入口显示，实际环境尚未验证", encoding="utf-8")
        scene = {"scenario": "结果可观察", "changes": "接通领取与剩余次数", "entry": "主界面 → 奖励",
                 "setup": "测试账号具备可领取次数", "steps": ["进入奖励界面", "点击领取"],
                 "expected": "次数减少且奖励到账", "level": "static", "status": "passed",
                 "actual": "代码路径已核对", "pending": "真实账号验证",
                 "evidence": [{"path": str(evidence), "locator": "入口检查"}]}
        material = Path(prepared["target"])
        material.write_text(json.dumps({"scenarios": [scene]}), encoding="utf-8")
        again = self.run_cli("prepare-action-input", "--feature-id", archive.name,
                            "--input-kind", "acceptance", "--execution-id", "ui-impl")
        self.assertFalse(again["written"])
        self.record_checkpoint("ui-impl", feature_id=archive.name)
        candidate = archive / "05-implementation/scenario-candidate.json"
        write_candidate(candidate, "ui-candidate")
        value = json.loads(candidate.read_bytes())
        value["delivery_materials"] = [{"requirement": "result", "kind": "acceptance", "path": str(material), "locator": "结果可观察"}]
        candidate.write_text(json.dumps(value), encoding="utf-8")
        final = self.accept_material_candidate(archive, candidate, evidence)
        result = self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final)
        self.assertFalse(result["delivery_ready"])
        model = json.loads((archive / "ui-model.json").read_bytes())
        self.assertEqual("static", model["acceptance_scenarios"][0]["level"])
        self.assertIsNone(model["annotations"][0]["interaction"])
        self.assertEqual("pending", self.ui_approval_view(archive)["conclusions"]["approvals"]["ui-delivery"]["status"])
        page = (archive / "06-validation/ui-delivery.html").read_text(encoding="utf-8")
        self.assertIn("验收步骤与证据", page)
        self.assertIn("主界面 → 奖励", page)
        self.assertIn("跨日刷新", page)
        self.assertIn("用户明确将跨日联调留至下一期", page)
        self.assertEqual([], model["acceptance_scenarios"][1]["evidence"])
        # 原始记录就地更新，通过已有材料修订入口重新汇总，不补第二套交互文件。
        scene.update(level="runtime", actual="测试账号操作通过", pending="无")
        material.write_text(json.dumps({"scenarios": [scene]}), encoding="utf-8")
        self.assertEqual("DELIVERY_MATERIAL_STALE", self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final, expected=1)["code"])
        value = json.loads(final.read_bytes())
        value["material_updates"] = [{"package_id": "ui-slice", "requirement": "result", "kind": "acceptance", "path": str(material), "locator": "结果可观察：实际环境验证"}]
        final.write_text(json.dumps(value), encoding="utf-8")
        self.assertTrue(self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final)["delivery_ready"])
        model = json.loads((archive / "ui-model.json").read_bytes())
        self.assertEqual(["结果可观察：实际环境验证"],
                         [item["locator"] for item in model["evidence"] if Path(item["path"]) == material])
        page = (archive / "06-validation/ui-delivery.html").read_text(encoding="utf-8")
        self.assertIn('"ready": true', page)
        review = self.ui_approval_view(archive)["conclusions"]["approvals"]["ui-delivery"]
        self.assertEqual("ready", review["status"])
        self.set_status(archive / "06-validation/README.md", "completed")
        confirmation = self.write_integration_confirmation(feature_id=archive.name)
        self.run_cli("stage-action", "--feature-id", archive.name, "--stage", "final", "--decision", "approve",
                     "--reviewed-digest", review["review"]["reviewed_digest"],
                     "--user-confirmation", "测试用户回复：已在产品中验收通过", "--integration-confirmation", confirmation)
        self.assertEqual("approve", self.ui_approval_view(archive)["conclusions"]["approvals"]["final"]["status"])

    def test_deferred_scenario_without_material_stays_visible_and_can_receive_results(self):
        archive, investigation, package = self.prepare_ui_execution()
        value = json.loads(package.read_bytes())
        value["delivery_requirements"][0].update(materials=["acceptance"], required=False,
                                                  reason="用户明确本期只交接实现，实际联调延期")
        package.write_text(json.dumps(value), encoding="utf-8")
        self.run_cli("start-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                     "--package-file", package, "--workspace-root", self.workspace)
        evidence = self.workspace / "ui-slice.txt"
        evidence.write_text("已完成本期实现，运行联调延期", encoding="utf-8")
        self.record_checkpoint("ui-impl", feature_id=archive.name)
        prepared = self.run_cli("prepare-action-input", "--feature-id", archive.name,
                                "--input-kind", "candidate", "--execution-id", "ui-impl")
        value = prepared["template"]
        self.assertEqual([], value["delivery_materials"])
        value.update(candidate_id="ui-candidate", verification=["已核对本期实现"])
        candidate = Path(prepared["target"])
        candidate.write_text(json.dumps(value), encoding="utf-8")
        final = self.accept_material_candidate(archive, candidate, evidence)
        self.assertTrue(self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final)["delivery_ready"])
        model = json.loads((archive / "ui-model.json").read_bytes())
        scene = model["acceptance_scenarios"][0]
        self.assertEqual("结果可观察", scene["scenario"])
        self.assertEqual("unverified", scene["status"])
        self.assertEqual([], scene["evidence"])
        self.assertFalse(scene["required"])
        page = (archive / "06-validation/ui-delivery.html").read_text(encoding="utf-8")
        self.assertIn(scene["reason"], page)
        self.assertIn("未提供验证结果", page)
        # 延期场景的展示不能绕过另行声明的必要局部交互验证。
        value = json.loads(final.read_bytes())
        outcome = self.change_outcome(investigation)
        outcome["interaction"] = self.interaction(evidence, status="unverified", detail="局部交互尚未验证")
        value.update(investigation_token=investigation["investigation_token"], outcomes=[outcome])
        final.write_text(json.dumps(value), encoding="utf-8")
        self.assertFalse(self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final)["delivery_ready"])
        # 后续提交真实记录时替换范围说明投影，不产生同名场景冲突或重复。
        outcome["interaction"]["required_for_acceptance"] = False
        scene.update(changes="接通领取", entry="主界面 → 奖励", setup="受控测试数据", steps=["点击领取"],
                     level="isolated", status="passed", actual="隔离验证通过", pending="实际联调延期",
                     evidence=[{"path": str(evidence), "locator": "本期验证记录"}])
        material = archive / "06-validation/ui-slice-acceptance.json"
        material.write_text(json.dumps({"scenarios": [{k: v for k, v in scene.items() if k not in {"required", "reason"}}]}), encoding="utf-8")
        value["material_updates"] = [{"package_id": "ui-slice", "requirement": "result", "kind": "acceptance",
                                      "path": str(material), "locator": "结果可观察"}]
        final.write_text(json.dumps(value), encoding="utf-8")
        self.assertTrue(self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final)["delivery_ready"])
        model = json.loads((archive / "ui-model.json").read_bytes())
        self.assertEqual(1, len(model["acceptance_scenarios"]))
        self.assertEqual("隔离验证通过", model["acceptance_scenarios"][0]["actual"])

    def test_delivery_readiness_checks_retained_interaction_evidence(self):
        archive, investigation, package = self.prepare_ui_execution()
        implementation = self.accept_ui(archive, package)
        evidence = archive / "06-validation/interaction-result.txt"
        evidence.write_text("领取操作已验证", encoding="utf-8")
        outcome = self.change_outcome(investigation)
        outcome["interaction"] = self.interaction(evidence)
        self.assertTrue(self.publish_delivery(archive, investigation, [outcome], implementation)["delivery_ready"])
        prepared = self.run_cli("prepare-action-input", "--feature-id", archive.name, "--input-kind", "ui-delivery")
        value = prepared["template"]
        value["investigation_token"] = investigation["investigation_token"]
        value["delivery_review"] = {
            "requirements_check": "已核对原需求", "implementation_check": "已核对实现和保留的局部交互",
            "evidence": [{"path": str(implementation), "locator": "本次完整实现与验证"}],
        }
        final = Path(prepared["target"])
        final.write_text(json.dumps(value), encoding="utf-8")
        self.assertTrue(self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final)["delivery_ready"])
        model = json.loads((archive / "ui-model.json").read_bytes())
        retained_ids = model["annotations"][0]["interaction"]["evidence_ids"]
        self.assertEqual([evidence.resolve()], [Path(item["path"]).resolve() for item in model["evidence"] if item["id"] in retained_ids])
        # 重新运行检查产生新证据；保留的局部说明仍引用上次结果，不能提前报告就绪。
        evidence.write_text("重新验证领取与失败后恢复", encoding="utf-8")
        published = self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final)
        self.assertFalse(published["delivery_ready"])
        self.assertEqual("blocked", published["status"])
        page = (archive / "06-validation/ui-delivery.html").read_text(encoding="utf-8")
        self.assertIn('"ready": false', page)
        review = self.ui_approval_view(archive)["conclusions"]["approvals"]["ui-delivery"]
        self.assertEqual("pending", review["status"])
        self.assertIn("证据文件内容已变化", review["review_blocker"]["message"])
        self.assertTrue(self.publish_delivery(archive, investigation, [outcome], implementation)["delivery_ready"])
        self.assertEqual("ready", self.ui_approval_view(archive)["conclusions"]["approvals"]["ui-delivery"]["status"])

    def test_ui_material_requirements_are_mandatory_before_start(self):
        archive, _, package = self.prepare_ui_execution()
        value = json.loads(package.read_bytes())
        del value["delivery_requirements"]
        package.write_text(json.dumps(value), encoding="utf-8")
        rejected = self.run_cli("start-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                                "--package-file", package, "--workspace-root", self.workspace, expected=1)
        self.assertEqual("DELIVERY_REQUIREMENTS_REQUIRED", rejected["code"])

    def test_candidate_reports_missing_material_and_keeps_work_for_resubmission(self):
        archive, _, candidate, material, evidence = self.prepare_material_candidate()
        complete = json.loads(candidate.read_bytes())
        incomplete = json.loads(candidate.read_bytes())
        incomplete["delivery_materials"] = [m for m in incomplete["delivery_materials"] if m["kind"] != "screenshot"]
        candidate.write_text(json.dumps(incomplete), encoding="utf-8")
        rejected = self.submit_material_candidate(archive, candidate, expected=1)
        self.assertEqual("DELIVERY_MATERIALS_INCOMPLETE", rejected["code"])
        self.assertIn("结果可观察：缺少截图", rejected["message"])
        state = json.loads((archive / "workflow-state.json").read_bytes())
        self.assertEqual("active", state["execution"]["slices"]["ui-slice"]["status"])
        self.assertTrue(evidence.is_file())
        candidate.write_text(json.dumps(complete), encoding="utf-8")
        submitted = self.submit_material_candidate(archive, candidate)
        self.assertIn(str(material.resolve()), [m.get("path") for m in submitted["required_materials"]])
        handoff = self.run_cli("prepare-handoff", "--feature-id", archive.name, "--action", "implementation",
                               "--role", "review", "--execution-id", "independent-review")
        self.assertTrue(handoff["handoff_ready"])
        handoff = self.run_cli("context-summary", "--feature-id", archive.name, "--action", "implementation",
                               "--role", "review", "--execution-id", "independent-review")
        self.assertIn(str(material.resolve()), json.dumps(handoff).replace("\\\\", "\\"))

    def test_final_html_assembles_candidate_materials_and_reuses_partial_repairs(self):
        archive, _, candidate, material, evidence = self.prepare_material_candidate()
        final_input = self.accept_material_candidate(archive, candidate, evidence)
        self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final_input)
        model = json.loads((archive / "ui-model.json").read_bytes())
        self.assertEqual(1, len(model["annotations"]))
        self.assertEqual(4, len(model["delivery_materials"]))
        self.assertIn("ReceiveButton", (archive / "06-validation/ui-delivery.html").read_text(encoding="utf-8"))
        untouched = [m for m in model["delivery_materials"] if m["kind"] != "interaction"]
        repaired = material.with_name("interactions-revised.json")
        value = json.loads(material.read_bytes())
        value["outcomes"][0]["interaction"]["feedback"] = "请求期间禁止重复点击；失败后恢复按钮"
        repaired.write_text(json.dumps(value), encoding="utf-8")
        final = json.loads(final_input.read_bytes())
        final["material_updates"] = [{"package_id": "ui-slice", "requirement": "result", "kind": "interaction",
                                      "path": str(repaired), "locator": "补充失败反馈"}]
        final_input.write_text(json.dumps(final), encoding="utf-8")
        self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final_input)
        final["material_updates"] = []
        final_input.write_text(json.dumps(final), encoding="utf-8")
        self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final_input)
        current = json.loads((archive / "ui-model.json").read_bytes())
        self.assertEqual(untouched, [m for m in current["delivery_materials"] if m["kind"] != "interaction"])
        self.assertIn("失败后恢复按钮", current["annotations"][0]["interaction"]["feedback"])

    def test_material_reference_checks_use_current_capture_and_reject_unknown_updates(self):
        archive, _, candidate, material, evidence = self.prepare_material_candidate()
        original = material.read_bytes()
        value = json.loads(original)
        value["outcomes"][0]["target"]["region"] = "unknown-node"
        material.write_text(json.dumps(value), encoding="utf-8")
        rejected = self.submit_material_candidate(archive, candidate, expected=1)
        self.assertEqual("DLOOP_UI_INPUT_INVALID", rejected["code"])
        self.assertIn("未知截图区域", rejected["message"])
        material.write_bytes(original)
        final_input = self.accept_material_candidate(archive, candidate, evidence)
        final = json.loads(final_input.read_bytes())
        final["material_updates"] = [{"package_id": "unknown-task", "requirement": "result", "kind": "verification",
                                      "path": str(evidence), "locator": "验证", "status": "passed"}]
        final_input.write_text(json.dumps(final), encoding="utf-8")
        self.assertEqual("INVALID_DELIVERY_UPDATE", self.run_cli(
            "ui-publish", "--feature-id", archive.name, "--input", final_input, expected=1)["code"])
        final["material_updates"] = []
        final_input.write_text(json.dumps(final), encoding="utf-8")
        value = json.loads(original)
        value["outcomes"][0]["interaction"]["feedback"] = "更新交付说明"
        material.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual("DELIVERY_MATERIAL_STALE", self.run_cli(
            "ui-publish", "--feature-id", archive.name, "--input", final_input, expected=1)["code"])
        final["material_updates"] = [{"package_id": "ui-slice", "requirement": "result", "kind": kind,
                                      "path": str(material), "locator": "更新说明"} for kind in ("interaction", "ui-location")]
        final_input.write_text(json.dumps(final), encoding="utf-8")
        self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final_input)

    def test_selective_capture_only_requests_changed_ui_and_keeps_other_screenshot(self):
        archive, source, _, initial, _ = self.complete_plan()
        second = self.write_prefab("OtherPanel.prefab", "2" * 32)
        brief = self.write_brief([
            self.requirement(source, match={"status": "matched", "prefabs": [
                {"path": "Assets/UI/RewardPanel.prefab", "name": "奖励界面", "reason": "原有领取界面"}]}),
            self.requirement(source, key="reward.other", name="其他奖励", match={"status": "matched", "prefabs": [
                {"path": "Assets/UI/OtherPanel.prefab", "name": "其他奖励界面", "reason": "补充展示位置"}]}),
        ])
        value = json.loads(brief.read_bytes())
        value["refresh_prefabs"] = ["Assets/UI/OtherPanel.prefab"]
        brief.write_text(json.dumps(value), encoding="utf-8")
        existing = archive / initial["captures"][0]["screenshot"]
        original = existing.read_bytes()
        pending = self.run_cli("ui-investigate", "--feature-id", archive.name, "--input", brief)
        request_path = Path(pending["capture_request"])
        request = json.loads(request_path.read_bytes())
        self.assertEqual([second.resolve()], [Path(item["asset_path"]).resolve() for item in request["prefabs"]])
        manifest = CaptureFixture().capture(request_path, self.project_root, request_path.parent)
        result = self.run_cli("ui-investigate", "--feature-id", archive.name, "--input", brief, "--capture-manifest", manifest)
        self.assertEqual(2, len(result["captures"]))
        self.assertEqual(original, existing.read_bytes())
        value["refresh_prefabs"] = []
        brief.write_text(json.dumps(value), encoding="utf-8")
        second.write_text(second.read_text(encoding="utf-8") + "\n# 界面变化\n", encoding="utf-8")
        rejected = self.run_cli("ui-investigate", "--feature-id", archive.name, "--input", brief, expected=1)
        self.assertEqual("DLOOP_UI_CAPTURE_REFRESH_REQUIRED", rejected["code"])
        self.assertIn("OtherPanel.prefab", rejected["message"])

    def test_text_only_prefab_change_reuses_capture_through_candidate_and_final(self):
        archive, _, candidate, material, evidence = self.prepare_material_candidate()
        original = json.loads((archive / "ui-model.json").read_bytes())
        prefab = Path(original["prefabs"][0]["asset_path"])
        # The product change leaves every node and rectangle unchanged.
        prefab.write_text(prefab.read_text(encoding="utf-8") + "\n# button copy: Claim\n", encoding="utf-8")
        brief = self.project_root / "ui-brief.json"
        value = json.loads(brief.read_bytes())
        value["refresh_prefabs"] = []
        brief.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual("DLOOP_UI_CAPTURE_REFRESH_REQUIRED", self.run_cli(
            "ui-investigate", "--feature-id", archive.name, "--input", brief, expected=1)["code"])
        value["unchanged_layout"] = [{"prefab": str(prefab),
                                      "reason": "只修改按钮文案；节点、布局、画布及依赖的定位均未变化。"}]
        brief.write_text(json.dumps(value), encoding="utf-8")
        adapter = mock.Mock()
        adapter.capture.side_effect = AssertionError("定位未变不应补拍")
        with mock.patch("archive_ui._capture_adapter", return_value=adapter):
            current = self.run_cli("ui-investigate", "--feature-id", archive.name, "--input", brief)
        adapter.capture.assert_not_called()
        model = json.loads((archive / "ui-model.json").read_bytes())
        capture = model["prefabs"][0]["capture"]
        self.assertEqual(original["prefabs"][0]["asset_sha256"], capture["reuse"]["captured_asset_sha256"])
        self.assertEqual(original["prefabs"][0]["capture"]["regions"], capture["regions"])
        value = json.loads(material.read_bytes())
        value["investigation_token"] = current["investigation_token"]
        material.write_text(json.dumps(value), encoding="utf-8")
        final = self.accept_material_candidate(archive, candidate, evidence)
        self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final)
        self.assertIn("历史位置参考", (archive / "06-validation/ui-delivery.html").read_text(encoding="utf-8"))
        self.assertIn("只修改按钮文案", (archive / "04-plan/ui-annotation-plan.md").read_text(encoding="utf-8"))
        # A later wording change reuses the accepted task's unchanged location materials.
        prefab.write_text(prefab.read_text(encoding="utf-8") + "\n# button copy: Collect\n", encoding="utf-8")
        with mock.patch("archive_ui._capture_adapter", return_value=adapter):
            self.run_cli("ui-investigate", "--feature-id", archive.name, "--input", brief)
        self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final)
        # With no further asset change the historical origin and note remain intact.
        brief_value = json.loads(brief.read_bytes())
        del brief_value["unchanged_layout"]
        brief.write_text(json.dumps(brief_value), encoding="utf-8")
        with mock.patch("archive_ui._capture_adapter", return_value=adapter):
            self.run_cli("ui-investigate", "--feature-id", archive.name, "--input", brief)
        self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final)
        current_model = json.loads((archive / "ui-model.json").read_bytes())
        self.assertEqual(capture["reuse"]["captured_asset_sha256"], current_model["prefabs"][0]["capture"]["reuse"]["captured_asset_sha256"])
        # An actual node/layout change still needs a fresh capture without a new review.
        prefab.write_text(prefab.read_text(encoding="utf-8").replace("RewardPanel", "MovedPanel"), encoding="utf-8")
        rejected = self.run_cli("ui-investigate", "--feature-id", archive.name, "--input", brief, expected=1)
        self.assertEqual("DLOOP_UI_CAPTURE_REFRESH_REQUIRED", rejected["code"])

    def test_grouped_hidden_operation_keeps_its_location_gap_in_archive(self):
        archive, _, _, investigation, _ = self.complete_plan()
        visible = self.change_outcome(investigation)
        visible["feature_point"] = {"key": "reward.actions", "title": "奖励操作"}
        hidden = json.loads(json.dumps(visible))
        hidden.update(key="reward.close", title="关闭后清理")
        hidden["target"] = {"label": "关闭后清理", "unavailable_reason": "当前截图未包含关闭入口，不能用领取按钮代替。"}
        review = self.write_review(investigation, [visible, hidden])
        self.run_cli("ui-publish", "--feature-id", archive.name, "--input", review)
        plan = (archive / "04-plan/ui-annotation-plan.md").read_text(encoding="utf-8")
        self.assertIn("关闭后清理：当前截图未包含关闭入口", plan)
        model = json.loads((archive / "ui-model.json").read_bytes())
        self.assertEqual(2, len(model["annotations"]))
        from archive_ui import _runtime
        page = _runtime()._presentation_pages(model, archive)[0]
        self.assertEqual(1, len(page["items"]))
        self.assertIsNotNone(page["items"][0]["marker"])

    def test_reinvestigation_preserves_repaired_material_references(self):
        archive, _, candidate, material, evidence = self.prepare_material_candidate()
        final_input = self.accept_material_candidate(archive, candidate, evidence)
        repaired = material.with_name("repaired.json")
        value = json.loads(material.read_bytes())
        value["outcomes"][0]["interaction"]["feedback"] = "保留补充后的失败反馈"
        repaired.write_text(json.dumps(value), encoding="utf-8")
        final = json.loads(final_input.read_bytes())
        final["material_updates"] = [{"package_id": "ui-slice", "requirement": "result", "kind": "interaction",
                                      "path": str(repaired), "locator": "补充失败反馈"}]
        final_input.write_text(json.dumps(final), encoding="utf-8")
        self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final_input)
        original = json.loads((archive / "ui-model.json").read_bytes())["delivery_materials"]
        brief = self.project_root / "ui-brief.json"
        value = json.loads(brief.read_bytes())
        value["refresh_prefabs"] = []
        brief.write_text(json.dumps(value), encoding="utf-8")
        adapter = mock.Mock()
        adapter.capture.side_effect = AssertionError("复用截图不应触发 Unity 采集")
        with mock.patch("archive_ui._capture_adapter", return_value=adapter):
            investigated = self.run_cli("ui-investigate", "--feature-id", archive.name, "--input", brief)
        adapter.capture.assert_not_called()
        self.assertNotEqual("awaiting_capture", investigated["status"])
        self.assertEqual(original, json.loads((archive / "ui-model.json").read_bytes())["delivery_materials"])
        final["material_updates"] = []
        final_input.write_text(json.dumps(final), encoding="utf-8")
        self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final_input)
        self.assertIn("保留补充后的失败反馈", (archive / "06-validation/ui-delivery.html").read_text(encoding="utf-8"))

    def test_additional_materials_stay_in_scope_and_required_verification_blocks_final(self):
        archive, _, candidate, _, evidence = self.prepare_material_candidate()
        value = json.loads(candidate.read_bytes())
        additional = {"key": "result.failure", "scenario": "结果可观察", "materials": ["verification"], "required": True}
        value["additional_delivery_requirements"] = [additional]
        candidate.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual("DELIVERY_MATERIALS_INCOMPLETE", self.submit_material_candidate(archive, candidate, expected=1)["code"])
        value["delivery_materials"].append({"requirement": "result.failure", "kind": "verification",
                                           "path": str(evidence), "locator": "失败分支", "status": "unverified"})
        additional["scenario"] = "未经登记的新业务"
        candidate.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual("DELIVERY_SCENARIO_OUTSIDE_SCOPE", self.submit_material_candidate(archive, candidate, expected=1)["code"])
        additional["scenario"] = "结果可观察"
        candidate.write_text(json.dumps(value), encoding="utf-8")
        final_input = self.accept_material_candidate(archive, candidate, evidence)
        published = self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final_input)
        self.assertFalse(published["delivery_ready"])
        self.assertEqual("pending", self.ui_approval_view(archive)["conclusions"]["approvals"]["ui-delivery"]["status"])
        from archive_candidates import candidate_input_template
        state = json.loads((archive / "workflow-state.json").read_bytes())
        repair_input = candidate_input_template(state["execution"]["slices"]["ui-slice"])
        self.assertIn("result.failure", [m["requirement"] for m in repair_input["delivery_materials"]])
        final = json.loads(final_input.read_bytes())
        final["material_updates"] = [{"package_id": "ui-slice", **value["delivery_materials"][-1], "status": "passed"}]
        final_input.write_text(json.dumps(final), encoding="utf-8")
        self.run_cli("ui-publish", "--feature-id", archive.name, "--input", final_input)

    def interaction(self, evidence, **overrides):
        return {
            "conditions": "后端返回可领取状态", "action": "点击领取按钮", "feedback": "等待结果期间按钮不可重复点击",
            "status": "verified", "detail": "已核对实现和本次验证记录", "required_for_acceptance": True,
            "evidence": [{"path": str(evidence), "locator": "本次实现与验证结果"}], **overrides,
        }

    def accept_ui(self, archive, package):
        self.run_cli("start-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                     "--package-file", package, "--workspace-root", self.workspace)
        implementation = self.workspace / "ui-slice.txt"
        implementation.write_text("领取成功刷新次数；点击对勾恢复原文", encoding="utf-8")
        self.record_checkpoint("ui-impl", feature_id=archive.name)
        candidate = write_candidate(archive / "05-implementation/ui-candidate.json", "ui-candidate")
        self.run_cli("submit-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                     "--status", "completed", "--candidate-file", candidate)
        self.run_cli("review-slice", "--feature-id", archive.name, "--package-id", "ui-slice",
                     "--candidate-id", "ui-candidate", "--review-execution-id", "ui-review", "--result", "passed")
        self.set_status(archive / "06-validation/README.md", "completed")
        return implementation

    def publish_delivery(self, archive, investigation, outcomes, evidence, expected=0):
        path = self.write_review(investigation, outcomes)
        value = json.loads(path.read_bytes())
        value["delivery_review"] = {
            "requirements_check": "已从原始需求逐项核对实现及缺口",
            "implementation_check": "已读取实际实现，补齐入口条件、操作、反馈和结果",
            "evidence": [{"path": str(evidence), "locator": "本次完整实现与验证"}],
        }
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return self.run_cli("ui-publish", "--feature-id", archive.name, "--input", path, expected=expected)

    def test_ui_cannot_start_without_investigation_and_human_approval(self):
        archive, _ = self.init_ui()
        self.set_status(archive / "01-requirements/terminology.md", "confirmed")
        self.set_status(archive / "01-requirements/README.md", "confirmed")
        self.prepare_execution_inputs(archive.name)
        self.run_cli("transition-lifecycle", "--feature-id", archive.name, "--to", "active")
        package = self.write_package("ui-slice", feature_id=archive.name)
        value = json.loads(package.read_bytes())
        value["context_materials"][0].update(mode="full")
        value["context_materials"][0].pop("sections", None)
        value["context_materials"].append({"source": f"{archive.name}.design.overview", "purpose": "内部设计", "mode": "full"})
        package.write_text(json.dumps(value), encoding="utf-8")
        result = self.run_cli("start-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                              "--package-file", package, "--workspace-root", self.workspace, expected=1)
        self.assertEqual("UI_BASELINE_APPROVAL_REQUIRED", result["code"])
        state = json.loads((archive / "workflow-state.json").read_bytes())
        self.assertTrue(all(value is None for value in state["approvals"].values()))
        view = self.ui_approval_view(archive)
        self.assertEqual("ready", view["conclusions"]["approvals"]["requirements"]["status"])
        self.assertNotIn("ui-interaction", view["conclusions"]["approvals"])
        self.assertFalse(view["requires_human"]["required"])

    def test_ui_changes_during_implementation_do_not_reopen_human_gate(self):
        archive, investigation, package = self.prepare_ui_execution()
        self.run_cli("start-slice", "--feature-id", archive.name, "--execution-id", "ui-impl",
                     "--package-file", package, "--workspace-root", self.workspace)
        outcome = self.change_outcome(investigation)
        outcome["expected"] = "领取后刷新剩余次数与分类提示"
        review = self.write_review(investigation, [outcome])
        self.run_cli("ui-publish", "--feature-id", archive.name, "--input", review)
        self.record_checkpoint("ui-impl", feature_id=archive.name)
        view = self.ui_approval_view(archive)
        self.assertFalse(view["requires_human"]["required"])
        self.assertTrue((archive / "06-validation/ui-delivery.html").is_file())

    def test_final_delivery_requires_complete_interactions_and_current_user_acceptance(self):
        archive, investigation, package = self.prepare_ui_execution()
        evidence = self.accept_ui(archive, package)
        outcome = self.change_outcome(investigation)
        published = self.publish_delivery(archive, investigation, [outcome], evidence)
        self.assertFalse(published["delivery_ready"])
        outcome["interaction"] = self.interaction(evidence)
        self.publish_delivery(archive, investigation, [outcome], evidence)
        html = (archive / "06-validation/ui-delivery.html").read_text(encoding="utf-8")
        self.assertIn("后端返回可领取状态", html)
        confirmation = self.write_integration_confirmation(feature_id=archive.name)
        args = ("stage-action", "--feature-id", archive.name, "--stage", "final", "--decision", "approve",
                "--integration-confirmation", confirmation)
        self.assertEqual("USER_CONFIRMATION_REQUIRED", self.run_cli(*args, expected=1)["code"])
        review = self.ui_approval_view(archive)["conclusions"]["approvals"]["ui-delivery"]["review"]
        self.run_cli(*args, "--reviewed-digest", review["reviewed_digest"], "--user-confirmation", "测试用户实际回复：接受当前交付")
        self.assertEqual("approve", self.ui_approval_view(archive)["conclusions"]["approvals"]["final"]["status"])
        outcome["expected"] = "领取后同步分类提示"
        self.publish_delivery(archive, investigation, [outcome], evidence)
        self.assertEqual("stale", self.ui_approval_view(archive)["conclusions"]["approvals"]["final"]["status"])
        self.assertEqual("UI_DELIVERY_OUTDATED", self.run_cli(*args, "--reviewed-digest", review["reviewed_digest"],
                         "--user-confirmation", "旧展示回复", expected=1)["code"])
        current = self.ui_approval_view(archive)["conclusions"]["approvals"]["ui-delivery"]["review"]
        self.run_cli("stage-action", "--feature-id", archive.name, "--stage", "final", "--decision", "reject",
                     "--reviewed-digest", current["reviewed_digest"], "--user-confirmation", "测试用户回复：退回，请补充失败提示")
        self.assertEqual("reject", self.ui_approval_view(archive)["conclusions"]["approvals"]["final"]["status"])
        outcome["expected"] = "领取成功同步分类提示；失败时明确提示原因"
        self.publish_delivery(archive, investigation, [outcome], evidence)
        self.assertEqual("stale", self.ui_approval_view(archive)["conclusions"]["approvals"]["final"]["status"])

    def test_hidden_interaction_does_not_remove_visible_operation(self):
        archive, investigation, package = self.prepare_ui_execution()
        evidence = self.accept_ui(archive, package)
        visible = self.change_outcome(investigation)
        visible["interaction"] = self.interaction(evidence)
        hidden = {**visible, "key": "reward.receive.hidden", "title": "隐藏状态说明",
                  "target": {"label": "成功状态", "unavailable_reason": "领取成功后才显示，静态截图未展示"}}
        self.publish_delivery(archive, investigation, [visible, hidden], evidence)
        model = json.loads((archive / "ui-model.json").read_bytes())
        self.assertEqual(2, len(model["annotations"]))
        html = (archive / "06-validation/ui-delivery.html").read_text(encoding="utf-8")
        self.assertIn("领取成功后才显示", html)
        self.assertIn("领取按钮", html)

    def test_required_verification_cannot_be_replaced_by_static_image(self):
        archive, investigation, package = self.prepare_ui_execution()
        evidence = self.accept_ui(archive, package)
        outcome = self.change_outcome(investigation)
        outcome["interaction"] = self.interaction(evidence, status="unverified", detail="仅完成代码核对，必要运行验证未执行")
        self.assertFalse(self.publish_delivery(archive, investigation, [outcome], evidence)["delivery_ready"])
        outcome["interaction"]["required_for_acceptance"] = False
        outcome["interaction"]["detail"] = "原需求明确将运行联调留给后续，本次只交付接线"
        self.publish_delivery(archive, investigation, [outcome], evidence)
        self.assertIn("尚未验证", (archive / "06-validation/ui-delivery.html").read_text(encoding="utf-8"))

    def test_no_prefab_still_has_text_delivery(self):
        archive, _, package = self.prepare_ui_execution()
        evidence = self.accept_ui(archive, package)
        brief = self.write_brief([self.requirement(self.project_root / "requirements.md", match={
            "status": "not-found", "detail": "当前交互动态生成，没有独立 Prefab", "searched_paths": ["Assets/UI"],
        })])
        investigation = self.investigate(archive, brief)
        outcome = {
            "key": "reward.receive.dynamic", "requirements": ["reward.receive"], "prefab": "",
            "result": "change", "title": "动态领取入口",
            "target": {"label": "领取入口", "unavailable_reason": "运行时生成，没有可用静态截图"},
            "instruction": "接入领取及成功后的次数更新", "expected": "成功后显示最新次数",
            "inference_level": "explicit", "confidence": "high", "interaction": self.interaction(evidence),
        }
        result = self.publish_delivery(archive, investigation, [outcome], evidence)
        self.assertTrue(result["delivery_ready"])
        self.assertEqual(2, len(result["artifacts"]))
        self.assertIn("动态领取入口", (archive / "06-validation/ui-delivery.html").read_text(encoding="utf-8"))

    def test_final_ui_acceptance_is_required_for_freeze(self):
        archive, investigation, package = self.prepare_ui_execution()
        evidence = self.accept_ui(archive, package)
        for category in ("02-investigation", "05-implementation"):
            self.set_status(archive / category / "README.md", "completed")
        outcome = self.change_outcome(investigation)
        outcome["interaction"] = self.interaction(evidence)
        self.publish_delivery(archive, investigation, [outcome], evidence)
        self.run_cli("transition-lifecycle", "--feature-id", archive.name, "--to", "validating")
        view = self.ui_approval_view(archive)
        self.assertEqual("final-review", view["current_stage"])
        self.assertTrue(view["requires_human"]["required"])
        self.assertIn("reviewed_digest", view["next_action_contract"]["arguments"])
        confirmation = self.write_integration_confirmation(feature_id=archive.name)
        freeze_args = ("transition-lifecycle", "--feature-id", archive.name, "--to", "frozen",
                       "--validation-conclusion", "passed", "--unverified-boundaries", "none", "--residual-risks", "none",
                       "--integration-confirmation", confirmation)
        handoff = self.run_cli("prepare-handoff", "--feature-id", archive.name, "--action", "validation",
                               "--role", "final-review", "--integration-confirmation", confirmation)
        self.assertTrue(handoff["handoff_ready"])
        context = self.run_cli("context-summary", "--feature-id", archive.name, "--action", "validation",
                              "--role", "final-review", "--integration-confirmation", confirmation)
        self.assertIn("ui-delivery.html", json.dumps(context, ensure_ascii=False))
        rejected = self.run_cli(*freeze_args, expected=1)
        self.assertIn("用户验收", rejected["message"])
        self.run_cli("stage-action", "--feature-id", archive.name, "--stage", "final", "--decision", "approve",
                     "--reviewed-digest", view["next_action_contract"]["arguments"]["reviewed_digest"],
                     "--user-confirmation", "测试用户回复：已查看当前交付，验收通过", "--integration-confirmation", confirmation)
        self.assertEqual("frozen", self.run_cli(*freeze_args)["lifecycle"])
        evidence.write_text("交付后由用户独立调试的代码", encoding="utf-8")
        historical = self.ui_approval_view(archive)
        self.assertEqual("frozen", historical["current_stage"])
        self.assertEqual("approve", historical["delivery_summary"]["acceptance"])

    def test_design_change_invalidates_delivery_until_reconciled(self):
        archive, investigation, package = self.prepare_ui_execution()
        evidence = self.accept_ui(archive, package)
        outcome = self.change_outcome(investigation)
        outcome["interaction"] = self.interaction(evidence)
        self.publish_delivery(archive, investigation, [outcome], evidence)
        self.assertEqual("ready", self.ui_approval_view(archive)["conclusions"]["approvals"]["ui-delivery"]["status"])
        design = archive / "03-design/README.md"
        design.write_text(design.read_text(encoding="utf-8") + "\n补充领取失败后的恢复行为。\n", encoding="utf-8")
        self.run_cli("confirm-change", "--document-id", f"{archive.name}.design.overview", "--semantic-change", "false")
        self.assertEqual("pending", self.ui_approval_view(archive)["conclusions"]["approvals"]["ui-delivery"]["status"])

    def test_new_accepted_implementation_requires_new_delivery_reconciliation(self):
        archive, investigation, package = self.prepare_ui_execution()
        evidence = self.accept_ui(archive, package)
        outcome = self.change_outcome(investigation)
        outcome["interaction"] = self.interaction(evidence)
        self.publish_delivery(archive, investigation, [outcome], evidence)
        old_review = self.ui_approval_view(archive)["conclusions"]["approvals"]["ui-delivery"]["review"]
        package = self.write_package("ui-supplement", feature_id=archive.name)
        value = json.loads(package.read_bytes())
        value["context_materials"][0].update(mode="full")
        value["context_materials"][0].pop("sections", None)
        value["context_materials"].append({"source": f"{archive.name}.design.overview", "purpose": "补充设计", "mode": "full"})
        package.write_text(json.dumps(value), encoding="utf-8")
        self.run_cli("start-slice", "--feature-id", archive.name, "--execution-id", "ui-supplement-impl",
                     "--package-file", package, "--workspace-root", self.workspace)
        supplement = self.workspace / "ui-supplement.txt"
        supplement.write_text("补充失败后恢复按钮的实现与验证", encoding="utf-8")
        self.record_checkpoint("ui-supplement-impl", feature_id=archive.name)
        candidate = write_candidate(archive / "05-implementation/supplement-candidate.json", "supplement-candidate")
        self.run_cli("submit-slice", "--feature-id", archive.name, "--execution-id", "ui-supplement-impl",
                     "--status", "completed", "--candidate-file", candidate)
        self.run_cli("review-slice", "--feature-id", archive.name, "--package-id", "ui-supplement",
                     "--candidate-id", "supplement-candidate", "--review-execution-id", "supplement-review", "--result", "passed")
        view = self.ui_approval_view(archive)["conclusions"]["approvals"]["ui-delivery"]
        self.assertEqual("pending", view["status"])
        self.assertEqual("UI_DELIVERY_OUTDATED", view["review_blocker"]["code"])
        outcome["interaction"] = self.interaction(supplement, feedback="失败后恢复按钮，并显示失败原因")
        self.publish_delivery(archive, investigation, [outcome], supplement)
        new_review = self.ui_approval_view(archive)["conclusions"]["approvals"]["ui-delivery"]["review"]
        self.assertNotEqual(old_review["reviewed_digest"], new_review["reviewed_digest"])
