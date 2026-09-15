from __future__ import annotations

import json

try:
    from _feature_archive_support import FeatureArchiveCliTestCase
except ModuleNotFoundError:
    from ._feature_archive_support import FeatureArchiveCliTestCase


class FeatureArchiveTemplateTests(FeatureArchiveCliTestCase):
    real_snapshots = False

    def test_complex_profile_uses_seven_delivery_documents_without_fixed_companions(self) -> None:
        archive = self.init_complex()
        markdown = {path.relative_to(archive).as_posix() for path in archive.rglob("*.md")}
        self.assertEqual(
            {
                "README.md",
                "01-requirements/README.md",
                "01-requirements/terminology.md",
                "02-investigation/README.md",
                "03-design/README.md",
                "04-plan/README.md",
                "05-implementation/README.md",
                "06-validation/README.md",
            },
            markdown,
        )
        self.assertTrue((archive / "workflow-state.json").is_file())
        self.assertEqual(10, len([path for path in archive.rglob("*") if path.is_file()]))

    def test_complex_profile_uses_execution_identity_registry_without_metrics_state(self) -> None:
        archive = self.init_complex()
        state = json.loads((archive / "workflow-state.json").read_text(encoding="utf-8"))

        self.assertEqual([], state["execution"]["used_execution_ids"])

    def test_complex_documents_do_not_require_fixed_layout_fields(self) -> None:
        archive = self.init_complex()
        overview = archive / "03-design" / "README.md"
        content = overview.read_text(encoding="utf-8")
        self.assertNotIn("template_version:", content)
        self.assertNotIn("document_role:", content)
        content = content.replace("## 当前摘要", "## 设计结论")
        overview.write_text(content, encoding="utf-8")
        document_id = "reliable-delivery.design.overview"
        self.run_cli(
            "confirm-change", "--document-id", document_id,
            "--semantic-change", "false",
        )
        self.run_cli("validate")

    def test_default_profile_is_the_unique_strict_contract(self) -> None:
        self.run_cli(
            "init", "--feature-id", "simple-change", "--title", "简单修改",
        )
        self.assertTrue((self.root / "simple-change" / "workflow-state.json").exists())
        manifest = json.loads((self.root / "simple-change" / "feature.json").read_text(encoding="utf-8"))
        self.assertEqual("strict-v1", manifest["workflow_profile"])
        self.assertEqual(3, manifest["schema_version"])
        self.assertEqual(2, manifest["terminology_schema_version"])

    def test_unversioned_nonempty_root_is_rejected(self) -> None:
        legacy_root = self.root
        legacy_root.mkdir(parents=True)
        (legacy_root / "legacy.txt").write_text("legacy", encoding="utf-8")
        result = self.run_cli(
            "init", "--feature-id", "reliable-delivery", "--title", "可靠交付",
            expected=1,
        )
        self.assertEqual("INCOMPATIBLE_ARCHIVE_ROOT", result["code"])

    def test_validate_rejects_unknown_workflow_profile(self) -> None:
        archive = self.init_complex()
        manifest_path = archive / "feature.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["workflow_profile"] = "strict-v1-typo"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        result = self.run_cli("validate", expected=1)
        self.assertEqual("INVALID_MANIFEST", result["code"])

    def test_validate_rejects_old_archive_schema(self) -> None:
        archive = self.init_complex()
        manifest_path = archive / "feature.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = 1
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        result = self.run_cli("validate", expected=1)
        self.assertEqual("INVALID_MANIFEST", result["code"])

    def test_validate_rejects_missing_dependency_versions(self) -> None:
        archive = self.init_complex()
        document = archive / "02-investigation" / "README.md"
        content = document.read_text(encoding="utf-8")
        document.write_text(
            content.replace("dependency_versions: {}\n", ""),
            encoding="utf-8",
        )
        result = self.run_cli("validate", expected=1)
        self.assertEqual("INVALID_DOCUMENT", result["code"])

    def test_stale_document_requires_previous_status(self) -> None:
        archive = self.init_complex()
        design = archive / "03-design" / "README.md"
        design.write_text(
            design.read_text(encoding="utf-8").replace(
                "content_status: draft", "content_status: stale"
            ),
            encoding="utf-8",
        )
        result = self.run_cli("validate", expected=1)
        self.assertEqual("INVALID_DOCUMENT", result["code"])

    def test_validate_rejects_missing_complex_workflow_state(self) -> None:
        archive = self.init_complex()
        (archive / "workflow-state.json").unlink()
        result = self.run_cli("validate", expected=1)
        self.assertEqual("INVALID_WORKFLOW_STATE", result["code"])

    def test_validate_rejects_invalid_complex_workflow_state(self) -> None:
        archive = self.init_complex()
        (archive / "workflow-state.json").write_text("{}", encoding="utf-8")
        result = self.run_cli("validate", expected=1)
        self.assertEqual("INVALID_WORKFLOW_STATE", result["code"])

    def test_validate_rejects_incomplete_accepted_slice_state(self) -> None:
        archive = self.init_complex()
        state_path = archive / "workflow-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["execution"]["slices"]["fake"] = {"status": "accepted"}
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        result = self.run_cli("validate", expected=1)
        self.assertEqual("INVALID_WORKFLOW_STATE", result["code"])

    def test_validate_rejects_legacy_or_incomplete_persisted_task_package(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        package = self.write_package("slice-1")
        self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package,
            "--workspace-root", self.workspace,
        )
        state_path = archive / "workflow-state.json"
        original = json.loads(state_path.read_text(encoding="utf-8"))

        mutations = {
            "legacy-material": lambda task: task.__setitem__("context_materials", ["legacy.md"]),
            "missing-version": lambda task: task.pop("context_contract_version"),
            "missing-goal": lambda task: task.pop("goal"),
            "missing-non-goals": lambda task: task.pop("non_goals"),
            "missing-acceptance": lambda task: task.pop("acceptance_conditions"),
            "missing-validations": lambda task: task.pop("validations"),
            "missing-rollback": lambda task: task.pop("rollback"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                state = json.loads(json.dumps(original))
                mutate(state["execution"]["slices"]["slice-1"]["package"])
                state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                result = self.run_cli("validate", expected=1)
                self.assertEqual("INVALID_WORKFLOW_STATE", result["code"])

    def test_validate_rejects_missing_manifest_validation_statuses(self) -> None:
        archive = self.init_complex()
        manifest_path = archive / "feature.json"
        original = json.loads(manifest_path.read_text(encoding="utf-8"))

        mutations = {
            "missing-validation": lambda manifest: manifest.pop("validation"),
            "missing-conclusion": lambda manifest: manifest["validation"].pop("conclusion"),
            "missing-boundaries": lambda manifest: manifest["validation"].pop("unverified_boundaries"),
            "missing-risks": lambda manifest: manifest["validation"].pop("residual_risks"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                manifest = json.loads(json.dumps(original))
                mutate(manifest)
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                result = self.run_cli("validate", expected=1)
                self.assertEqual("INVALID_MANIFEST", result["code"])

    def test_validate_rejects_duplicate_execution_identity_in_state(self) -> None:
        self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        package = self.write_package("slice-1")
        self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package,
            "--workspace-root", self.workspace,
        )
        state_path = self.root / "reliable-delivery" / "workflow-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        duplicate = json.loads(json.dumps(state["execution"]["slices"]["slice-1"]))
        duplicate["package"]["package_id"] = "slice-2"
        duplicate["package"]["write_scope"] = ["slice-2.txt"]
        state["execution"]["slices"]["slice-2"] = duplicate
        state["execution"]["slices"]["slice-2"]["execution_id"] = "exec-1"
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        result = self.run_cli("validate", expected=1)
        self.assertEqual("INVALID_WORKFLOW_STATE", result["code"])

    def test_validate_rejects_workflow_state_without_complex_profile(self) -> None:
        archive = self.init_complex()
        manifest_path = archive / "feature.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("workflow_profile")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        result = self.run_cli("validate", expected=1)
        self.assertEqual("INVALID_MANIFEST", result["code"])
