from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
UNITY_PACKAGE = REPOSITORY_ROOT / "plugin" / "dloop" / "unity-editor"
UNITY_EXE = Path(os.environ["DLOOP_UNITY_EXE"]) if os.environ.get("DLOOP_UNITY_EXE") else None


class UnityCaptureContractTests(unittest.TestCase):
    def test_package_exports_only_screenshot_regions(self) -> None:
        package = json.loads(
            UNITY_PACKAGE.joinpath("package.json").read_text(encoding="utf-8")
        )
        assembly = json.loads(
            UNITY_PACKAGE.joinpath(
                "Editor", "Dloop.UiCapture.Editor.asmdef"
            ).read_text(encoding="utf-8")
        )
        source = UNITY_PACKAGE.joinpath(
            "Editor", "DloopUiCapture.cs"
        ).read_text(encoding="utf-8")

        self.assertEqual("com.dloop.ui-capture", package["name"])
        self.assertEqual("3.9.3", package["version"])
        self.assertEqual(["Unity.ugui"], assembly["references"])
        self.assertIn("RegionCapture[] regions", source)
        self.assertIn("string screenshot_path", source)
        for forbidden in (
            "capabilities",
            "component_facts",
            "event_bindings",
            "visual_states",
            "UnityEvent",
            "EventTrigger",
            "MonoScript",
            "Selectable",
        ):
            self.assertNotIn(forbidden, source)

    @unittest.skipUnless(
        UNITY_EXE is not None and UNITY_EXE.is_file(),
        "设置 DLOOP_UNITY_EXE 后运行真实 Unity 编辑器采集回归",
    )
    def test_editor_package_compiles_and_captures_without_changing_prefab(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            assets = project / "Assets"
            packages = project / "Packages"
            settings = project / "ProjectSettings"
            assets.mkdir()
            packages.mkdir()
            settings.mkdir()
            packages.joinpath("manifest.json").write_text(
                json.dumps(
                    {
                        "dependencies": {
                            "com.dloop.ui-capture": f"file:{UNITY_PACKAGE.as_posix()}",
                            "com.unity.ugui": "1.0.0",
                            "com.unity.modules.imageconversion": "1.0.0",
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            settings.joinpath("ProjectVersion.txt").write_text(
                "m_EditorVersion: 2022.3.62f1\n",
                encoding="utf-8",
            )
            output = assets / "Capture"
            prefab = assets / "CaptureProbe.prefab"
            request = project / "capture-request.json"
            request.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "project_root": project.resolve().as_posix(),
                        "output_dir": output.resolve().as_posix(),
                        "prefabs": [
                            {
                                "prefab_id": "PF-UNITYTEST001",
                                "asset_path": prefab.resolve().as_posix(),
                            }
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            assets.joinpath("DloopCaptureProbe.cs").write_text(
                """using System.IO;
using Dloop.Editor;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public static class DloopCaptureProbe
{
    public static void Run()
    {
        var root = new GameObject(
            "CapturePanel",
            typeof(RectTransform),
            typeof(Canvas),
            typeof(CanvasScaler),
            typeof(GraphicRaycaster));
        root.GetComponent<Canvas>().renderMode = RenderMode.ScreenSpaceOverlay;
        root.GetComponent<CanvasScaler>().referenceResolution = new Vector2(800, 600);
        var button = new GameObject(
            "ConfirmButton",
            typeof(RectTransform),
            typeof(Image),
            typeof(Button));
        button.transform.SetParent(root.transform, false);
        button.GetComponent<RectTransform>().sizeDelta = new Vector2(180, 64);
        try
        {
            PrefabUtility.SaveAsPrefabAsset(root, "Assets/CaptureProbe.prefab");
        }
        finally
        {
            Object.DestroyImmediate(root);
        }
        Directory.CreateDirectory("Assets/Capture");
        File.WriteAllBytes(
            "Assets/Capture/prefab-before.bytes",
            File.ReadAllBytes("Assets/CaptureProbe.prefab"));
        var original = SceneManager.GetActiveScene();
        var sentinel = new GameObject("UnsavedSentinel");
        sentinel.transform.position = new Vector3(123, 456, 789);
        EditorSceneManager.MarkSceneDirty(original);
        CaptureAndCheck(original, sentinel);
        EditorSceneManager.SaveScene(original, "Assets/SavedProbe.unity");
        CaptureAndCheck(original, sentinel);
        sentinel.transform.position = new Vector3(987, 654, 321);
        EditorSceneManager.MarkSceneDirty(original);
        CaptureAndCheck(original, sentinel);
    }

    private static void CaptureAndCheck(Scene original, GameObject sentinel)
    {
        var dirty = original.isDirty;
        var position = sentinel.transform.position;
        var roots = original.rootCount;
        var scenes = SceneManager.sceneCount;
        var previews = EditorSceneManager.previewSceneCount;
        DloopUiCapture.CaptureFromEnvironment();
        if (SceneManager.GetActiveScene() != original || original.isDirty != dirty
            || sentinel == null || sentinel.transform.position != position
            || original.rootCount != roots || SceneManager.sceneCount != scenes
            || EditorSceneManager.previewSceneCount != previews)
        {
            throw new System.InvalidOperationException("Capture changed the original scene.");
        }
        var manifest = File.ReadAllText("Assets/Capture/capture-manifest.json");
        if (!manifest.Contains("screenshot_path") || !manifest.Contains("ConfirmButton"))
        {
            throw new System.InvalidOperationException("Capture did not produce the expected UI evidence: " + manifest);
        }
        var image = new Texture2D(2, 2);
        try
        {
            image.LoadImage(File.ReadAllBytes("Assets/Capture/PF-UNITYTEST001-raw.png"));
            if (image.GetPixel(image.width / 2, image.height / 2).r < 0.8f
                || image.GetPixel(10, 10).r > 0.3f)
            {
                throw new System.InvalidOperationException("Capture did not render the button over the background.");
            }
        }
        finally
        {
            Object.DestroyImmediate(image);
        }
    }
}
""",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["DLOOP_UI_CAPTURE_REQUEST"] = str(request)

            completed = subprocess.run(
                [
                    str(UNITY_EXE),
                    "-batchmode",
                    "-quit",
                    "-projectPath",
                    str(project),
                    "-executeMethod",
                    "DloopCaptureProbe.Run",
                    "-logFile",
                    "-",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=600,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            manifest_path = output / "capture-manifest.json"
            self.assertTrue(manifest_path.is_file(), completed.stdout + completed.stderr)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual([], manifest["errors"])
            self.assertEqual(1, len(manifest["prefabs"]))
            capture = manifest["prefabs"][0]
            self.assertEqual(
                {
                    "prefab_id",
                    "asset_path",
                    "asset_guid",
                    "asset_sha256",
                    "screenshot_path",
                    "width",
                    "height",
                    "regions",
                },
                set(capture),
            )
            self.assertIn(
                "ConfirmButton",
                {item["node_name"] for item in capture["regions"]},
            )
            screenshot = Path(capture["screenshot_path"])
            self.assertEqual(
                b"\x89PNG\r\n\x1a\n",
                screenshot.read_bytes()[:8],
            )
            self.assertEqual(
                prefab.read_bytes(),
                output.joinpath("prefab-before.bytes").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
