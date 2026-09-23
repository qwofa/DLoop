from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PATH = REPOSITORY_ROOT / "plugin" / "dloop" / "runtime" / "dloop_ui.py"
SPECIFICATION = importlib.util.spec_from_file_location("dloop_ui", RUNTIME_PATH)
assert SPECIFICATION is not None and SPECIFICATION.loader is not None
dloop_ui = importlib.util.module_from_spec(SPECIFICATION)
SPECIFICATION.loader.exec_module(dloop_ui)

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
    def __init__(
        self,
        failed_names: tuple[str, ...] = (),
        screenshot_bytes: bytes = b"\x89PNG\r\n\x1a\nfixture",
        include_empty_errors: bool = True,
    ) -> None:
        self.failed_names = failed_names
        self.screenshot_bytes = screenshot_bytes
        self.include_empty_errors = include_empty_errors

    def capture(self, request_path: Path, project_root: Path, output_dir: Path) -> Path:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        captured = []
        errors = []
        for item in request["prefabs"]:
            prefab = Path(item["asset_path"])
            if prefab.name in self.failed_names:
                errors.append({"prefab_id": item["prefab_id"], "message": "截图失败"})
                continue
            screenshot = output_dir / f"{item['prefab_id']}-raw.png"
            screenshot.write_bytes(self.screenshot_bytes)
            captured.append(
                {
                    "prefab_id": item["prefab_id"],
                    "asset_path": prefab.as_posix(),
                    "asset_guid": "1" * 32,
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
                            "rect": {"x": 100, "y": 200, "width": 180, "height": 72},
                            "capabilities": ["click"],
                            "visual_states": ["normal", "pressed"],
                        },
                        {
                            "object_id": f"result-{prefab.stem}",
                            "node_name": "ResultText",
                            "hierarchy_path": f"{prefab.stem}/ResultText",
                            "rect": {"x": 100, "y": 300, "width": 220, "height": 40},
                        },
                    ],
                }
            )
        manifest_value = {"schema_version": 1, "prefabs": captured}
        if errors or self.include_empty_errors:
            manifest_value["errors"] = errors
        manifest = output_dir / "capture-manifest.json"
        manifest.write_text(
            json.dumps(manifest_value),
            encoding="utf-8",
        )
        return manifest


def create_model(root: Path, requirement_names: tuple[str, ...] = ("领取奖励",)):
    project = root / "project"
    feature = project / ".scratch" / "dloop-v3" / "v3.9.3" / "outputs" / "reward-feature"
    (project / "Assets" / "UI").mkdir(parents=True)
    feature.mkdir(parents=True)
    source = project / "requirements.md"
    source.write_text("# 需求\n", encoding="utf-8")
    model = dloop_ui.new_model("reward-feature", "奖励功能")
    evidence = dloop_ui.upsert_evidence(
        model,
        dloop_ui.build_file_evidence("source_document", source, summary="需求来源"),
    )
    dloop_ui.sync_ids(model)
    requirements = []
    for index, name in enumerate(requirement_names):
        requirements.append(
            {
                "key": f"reward.requirement-{index + 1}",
                "name": name,
                "statement": f"用户完成{name}后能看到最新结果。",
                "source": {"path": source.as_posix(), "locator": f"需求/{name}"},
                "target_clues": [name],
                "inference_level": "explicit",
                "confidence": "high",
            }
        )
    return project, feature, source, model, evidence, requirements


def write_prefab(project: Path, name: str, guid: str = "1" * 32) -> Path:
    path = project / "Assets" / "UI" / name
    path.write_text(PREFAB_TEXT.replace("RewardPanel", path.stem), encoding="utf-8")
    Path(str(path) + ".meta").write_text(f"guid: {guid}\n", encoding="utf-8")
    return path


def materialize_operation(feature: Path, operation: dict) -> None:
    for relative, content in operation["text_files"].items():
        path = feature / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    for relative, content in operation["binary_files"].items():
        path = feature / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


class DloopUILightweightTests(unittest.TestCase):
    def test_new_model_uses_current_lightweight_schema(self) -> None:
        model = dloop_ui.new_model("feature", "功能")

        self.assertEqual(5, model["schema_version"])
        self.assertEqual(
            {"requirements", "prefabs", "annotations", "skips", "evidence"},
            set(dloop_ui.ENTITY_SPECS),
        )
        serialized = json.dumps(model)
        for forbidden in (
            "planned-new",
            "component_semantics",
            "interactive_nodes",
            "visual_states",
            "capabilities",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_missing_prefab_is_a_complete_snapshot_without_generated_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, feature, _, model, _, requirements = create_model(Path(temporary))
            requirements[0]["match"] = {
                "status": "not-found",
                "detail": "没有对应 Prefab。",
                "searched_paths": ["Assets/UI"],
            }
            operation = dloop_ui.investigate(
                model,
                {"input_version": 1, "requirements": requirements},
                feature,
                project,
                None,
            )

            self.assertEqual("ready_to_publish", operation["status"])
            self.assertEqual([], model["prefabs"])
            self.assertEqual("prefab-not-found", model["skips"][0]["reason"])
            self.assertEqual("PASS", dloop_ui.validate_model(model, "plan")["status"])

    def test_real_capture_interface_filters_behaviour_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, feature, _, model, _, requirements = create_model(Path(temporary))
            write_prefab(project, "RewardPanel.prefab")
            requirements[0]["match"] = {
                "status": "matched",
                "prefabs": [
                    {"path": "Assets/UI/RewardPanel.prefab", "name": "奖励界面", "reason": "唯一对应。"}
                ],
            }
            operation = dloop_ui.investigate(
                model,
                {"input_version": 1, "requirements": requirements},
                feature,
                project,
                CaptureFixture(),
            )

            region = model["prefabs"][0]["capture"]["regions"][0]
            self.assertEqual(
                {"object_id", "node_name", "hierarchy_path", "rect"},
                set(region),
            )
            self.assertEqual("ready_for_review", operation["status"])

    def test_review_snapshot_enforces_change_and_reuse_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, feature, _, model, _, requirements = create_model(
                Path(temporary),
                ("领取奖励", "显示奖励标题"),
            )
            write_prefab(project, "RewardPanel.prefab")
            for requirement in requirements:
                requirement["match"] = {
                    "status": "matched",
                    "prefabs": [
                        {"path": "Assets/UI/RewardPanel.prefab", "name": "奖励界面", "reason": "唯一对应。"}
                    ],
                }
            operation = dloop_ui.investigate(
                model,
                {"input_version": 1, "requirements": requirements},
                feature,
                project,
                CaptureFixture(),
            )
            materialize_operation(feature, operation)
            capture = operation["captures"][0]
            target = {"region": capture["regions"][0]["object_id"], "label": "领取区域"}
            dloop_ui.apply_review_snapshot(
                model,
                {
                    "input_version": 1,
                    "investigation_token": operation["investigation_token"],
                    "outcomes": [
                        {
                            "key": "reward.change",
                            "requirements": ["reward.requirement-1"],
                            "prefab": capture["prefab"],
                            "result": "change",
                            "title": "领取按钮",
                            "target": target,
                            "related_elements": [{"region": capture["regions"][1]["object_id"], "role": "显示结果"}],
                            "instruction": "接入领取意图。",
                            "expected": "用户看到领取结果。",
                            "inference_level": "explicit",
                            "confidence": "high",
                        },
                        {
                            "key": "reward.reuse",
                            "requirements": ["reward.requirement-2"],
                            "prefab": capture["prefab"],
                            "result": "reuse",
                            "title": "奖励标题",
                            "target": target,
                            "inference_level": "explicit",
                            "confidence": "high",
                        },
                    ],
                },
                project,
            )

            report = dloop_ui.validate_model(model, "plan")
            self.assertEqual("PASS", report["status"])
            self.assertEqual(1, report["computed_counts"]["change_annotations"])
            self.assertEqual(1, report["computed_counts"]["reuse_annotations"])
            self.assertNotIn(
                "preserve",
                [annotation["disposition"] for annotation in model["annotations"]],
            )
            self.assertTrue(
                all("preserve" not in annotation for annotation in model["annotations"])
            )
            media = dloop_ui.render_annotation_media(model, feature)
            self.assertEqual(1, len(media))
            svg = ET.fromstring(next(iter(media.values())))
            self.assertEqual("852", svg.attrib["width"])
            self.assertEqual("600", svg.attrib["height"])
            circles = svg.findall("{http://www.w3.org/2000/svg}circle")
            self.assertEqual(1, len(circles))
            self.assertGreater(float(circles[0].attrib["cx"]) - 16, 800)
            self.assertNotIn("接入领取意图", next(iter(media.values())))
            screenshot = svg.find("{http://www.w3.org/2000/svg}image")
            self.assertIsNotNone(screenshot)
            reference = screenshot.attrib["href"]
            self.assertTrue(reference.startswith("data:image/png;base64,"))
            self.assertEqual(
                CaptureFixture().screenshot_bytes,
                base64.b64decode(reference.split(",", 1)[1], validate=True),
            )
            markdown = dloop_ui.render_views(model, report, feature)[
                "ui-annotation-plan.md"
            ]
            self.assertIn("![奖励界面编号截图](ui-annotations/", markdown)
            self.assertNotIn("原始截图：[查看]", markdown)
            self.assertIn("用户看到领取结果。", markdown)
            html = dloop_ui.render_delivery_html(model, feature, delivery_ready=False)
            self.assertIn("用户看到领取结果。", html)
            self.assertIn("无需产品修改", html)
            self.assertNotIn("{{DELIVERY_DATA}}", html)
            self.assertNotIn("<header", html)
            self.assertNotIn('id="review-section"', html)
            pages = dloop_ui._presentation_pages(model, feature)
            self.assertEqual(1, len(pages[0]["items"]))
            self.assertEqual(2, len(pages[0]["items"][0]["operations"]))
            self.assertEqual("RewardPanel", pages[0]["asset_name"])
            self.assertEqual("ReceiveButton", pages[0]["items"][0]["elements"][0]["name"])
            self.assertEqual({"name": "ResultText", "path": "RewardPanel/ResultText", "roles": ["显示结果"]},
                             pages[0]["items"][0]["elements"][1])
            self.assertEqual(2, len(pages[0]["items"][0]["elements"]))

            # Alternate states can have different targets but still form one functional point.
            first, second = model["annotations"]
            for annotation in (first, second):
                annotation["feature_point"] = {"key": "reward.action", "title": "领取奖励"}
            second["target"]["object_id"] = "alternate-state"
            second["target"]["rect"] = {"x": 300, "y": 570, "width": 100, "height": 30}
            pages = dloop_ui._presentation_pages(model, feature)
            self.assertEqual("领取奖励", pages[0]["items"][0]["title"])
            self.assertEqual(1, len(pages[0]["items"]))
            self.assertEqual(2, len(pages[0]["items"][0]["operations"]))

            # Separate functional points sharing a row must keep independent numbers without clipping.
            second["feature_point"]["key"] = "reward.other"
            first["target"]["rect"] = {"x": 100, "y": 570, "width": 100, "height": 30}
            svg = ET.fromstring(next(iter(dloop_ui.render_annotation_media(model, feature).values())))
            circles = svg.findall("{http://www.w3.org/2000/svg}circle")
            self.assertEqual(2, len(circles))
            for circle in circles:
                self.assertGreater(float(circle.attrib["cx"]) - 16, 800)
                self.assertLessEqual(float(circle.attrib["cy"]) + 16, float(svg.attrib["height"]))

    def test_review_token_tracks_source_and_screenshot_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, feature, source, model, _, requirements = create_model(Path(temporary))
            write_prefab(project, "RewardPanel.prefab")
            requirements[0]["match"] = {
                "status": "matched",
                "prefabs": [
                    {"path": "Assets/UI/RewardPanel.prefab", "name": "奖励界面", "reason": "唯一对应。"}
                ],
            }

            first = dloop_ui.investigate(
                model,
                {"input_version": 1, "requirements": requirements},
                feature,
                project,
                CaptureFixture(),
            )
            source.write_text("# 已更新需求\n", encoding="utf-8")
            source_changed = dloop_ui.investigate(
                model,
                {"input_version": 1, "requirements": requirements},
                feature,
                project,
                CaptureFixture(),
            )
            manifest_changed = dloop_ui.investigate(
                model,
                {"input_version": 1, "requirements": requirements},
                feature,
                project,
                CaptureFixture(include_empty_errors=False),
            )
            screenshot_changed = dloop_ui.investigate(
                model,
                {"input_version": 1, "requirements": requirements},
                feature,
                project,
                CaptureFixture(
                    screenshot_bytes=b"\x89PNG\r\n\x1a\nchanged",
                    include_empty_errors=False,
                ),
            )
            repeated = dloop_ui.investigate(
                model,
                {"input_version": 1, "requirements": requirements},
                feature,
                project,
                CaptureFixture(
                    screenshot_bytes=b"\x89PNG\r\n\x1a\nchanged",
                    include_empty_errors=False,
                ),
            )

            self.assertNotEqual(first["investigation_token"], source_changed["investigation_token"])
            self.assertNotEqual(
                source_changed["investigation_token"],
                manifest_changed["investigation_token"],
            )
            self.assertNotEqual(
                manifest_changed["investigation_token"],
                screenshot_changed["investigation_token"],
            )
            self.assertEqual(
                screenshot_changed["investigation_token"],
                repeated["investigation_token"],
            )

    def test_each_successful_requirement_prefab_pair_needs_an_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, feature, _, model, _, requirements = create_model(Path(temporary))
            write_prefab(project, "RewardPanel.prefab")
            write_prefab(project, "DetailPanel.prefab")
            requirements[0]["match"] = {
                "status": "matched",
                "prefabs": [
                    {"path": "Assets/UI/RewardPanel.prefab", "name": "奖励界面", "reason": "共同承载需求。"},
                    {"path": "Assets/UI/DetailPanel.prefab", "name": "详情界面", "reason": "共同承载需求。"},
                ],
            }
            operation = dloop_ui.investigate(
                model,
                {"input_version": 1, "requirements": requirements},
                feature,
                project,
                CaptureFixture(),
            )
            materialize_operation(feature, operation)
            captures = {
                Path(item["prefab"]).name: item for item in operation["captures"]
            }
            reward_capture = captures["RewardPanel.prefab"]
            detail_capture = captures["DetailPanel.prefab"]
            reward_outcome = {
                "key": "reward.change",
                "requirements": ["reward.requirement-1"],
                "prefab": reward_capture["prefab"],
                "result": "change",
                "title": "领取按钮",
                "target": {"region": reward_capture["regions"][0]["object_id"], "label": "领取区域"},
                "instruction": "接入领取意图。",
                "expected": "用户看到领取结果。",
                "inference_level": "explicit",
                "confidence": "high",
            }
            dloop_ui.apply_review_snapshot(
                model,
                {
                    "input_version": 1,
                    "investigation_token": operation["investigation_token"],
                    "outcomes": [reward_outcome],
                },
                project,
            )

            incomplete = dloop_ui.validate_model(model, "plan")
            self.assertEqual("BLOCKED", incomplete["status"])
            self.assertEqual(
                ["REQUIREMENT_OUTCOME_MISSING"],
                [item["code"] for item in incomplete["errors"]],
            )
            self.assertIn("详情界面", incomplete["errors"][0]["message"])

            dloop_ui.apply_review_snapshot(
                model,
                {
                    "input_version": 1,
                    "investigation_token": operation["investigation_token"],
                    "outcomes": [
                        reward_outcome,
                        {
                            "key": "reward.detail.reuse",
                            "requirements": ["reward.requirement-1"],
                            "prefab": detail_capture["prefab"],
                            "result": "reuse",
                            "title": "详情入口",
                            "target": {"region": detail_capture["regions"][0]["object_id"], "label": "详情区域"},
                            "inference_level": "explicit",
                            "confidence": "high",
                        },
                    ],
                },
                project,
            )

            self.assertEqual("PASS", dloop_ui.validate_model(model, "plan")["status"])

    def test_capture_failure_is_scoped_and_other_prefab_remains_reviewable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, feature, _, model, _, requirements = create_model(
                Path(temporary),
                ("领取奖励", "查看详情"),
            )
            write_prefab(project, "RewardPanel.prefab")
            write_prefab(project, "DetailPanel.prefab", "2" * 32)
            for requirement, prefab_name in zip(
                requirements,
                ("RewardPanel.prefab", "DetailPanel.prefab"),
            ):
                requirement["match"] = {
                    "status": "matched",
                    "prefabs": [
                        {"path": f"Assets/UI/{prefab_name}", "name": prefab_name, "reason": "唯一对应。"}
                    ],
                }
            operation = dloop_ui.investigate(
                model,
                {"input_version": 1, "requirements": requirements},
                feature,
                project,
                CaptureFixture(("DetailPanel.prefab",)),
            )

            self.assertEqual(1, len(operation["captures"]))
            self.assertEqual("capture-failed", operation["skips"][0]["reason"])
            self.assertEqual(
                "PASS",
                dloop_ui.validate_model(
                    model,
                    "investigation",
                    evidence_sources=operation["evidence_sources"],
                )["status"],
            )

    def test_target_not_visible_is_a_complete_outcome_only_after_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, feature, _, model, _, requirements = create_model(Path(temporary))
            write_prefab(project, "RewardPanel.prefab")
            requirements[0]["match"] = {
                "status": "matched",
                "prefabs": [
                    {"path": "Assets/UI/RewardPanel.prefab", "name": "奖励界面", "reason": "唯一对应。"}
                ],
            }
            operation = dloop_ui.investigate(
                model,
                {"input_version": 1, "requirements": requirements},
                feature,
                project,
                CaptureFixture(),
            )
            materialize_operation(feature, operation)

            dloop_ui.apply_review_snapshot(
                model,
                {
                    "input_version": 1,
                    "investigation_token": operation["investigation_token"],
                    "outcomes": [
                        {
                            "key": "reward.not-visible",
                            "requirements": ["reward.requirement-1"],
                            "prefab": operation["captures"][0]["prefab"],
                            "result": "target-not-visible",
                            "title": "领取入口",
                            "detail": "真实截图中没有可可靠定位的领取入口。",
                        }
                    ],
                },
                project,
            )

            self.assertEqual([], model["annotations"])
            self.assertEqual("target-not-visible", model["skips"][0]["reason"])
            self.assertEqual("PASS", dloop_ui.validate_model(model, "plan")["status"])

    def test_noncurrent_or_behaviour_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, _, source, model, evidence, _ = create_model(Path(temporary))
            model["schema_version"] = 2
            model["requirements"].append(
                {
                    "identity_key": "reward.receive",
                    "id": "",
                    "name_zh": "领取奖励",
                    "statement": "用户看到领取结果。",
                    "source_locator": "需求/领取",
                    "target_clues": [],
                    "inference_level": "explicit",
                    "confidence": "high",
                    "evidence_ids": [evidence["id"]],
                    "capabilities": ["click"],
                }
            )
            dloop_ui.sync_ids(model)

            report = dloop_ui.validate_model(model, "requirements")
            codes = {item["code"] for item in report["errors"]}
            self.assertIn("SCHEMA_VERSION", codes)
            self.assertIn("ENTITY_FIELDS_UNSUPPORTED", codes)
            self.assertTrue(source.is_file())

    def test_stale_review_token_is_rejected_without_changing_persisted_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, feature, _, model, _, requirements = create_model(Path(temporary))
            requirements[0]["match"] = {
                "status": "not-found",
                "detail": "没有对应 Prefab。",
                "searched_paths": ["Assets/UI"],
            }
            operation = dloop_ui.investigate(
                model,
                {"input_version": 1, "requirements": requirements},
                feature,
                project,
                None,
            )
            before = copy.deepcopy(model)

            with self.assertRaises(dloop_ui.DloopUiError) as raised:
                dloop_ui.apply_review_snapshot(
                    model,
                    {"input_version": 1, "investigation_token": "sha256:old", "outcomes": []},
                    project,
                )

            self.assertEqual("DLOOP_UI_INVESTIGATION_CHANGED", raised.exception.code)
            self.assertEqual(before, model)
            self.assertNotEqual("sha256:old", operation["investigation_token"])


if __name__ == "__main__":
    unittest.main()
