using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

namespace Dloop.Editor
{
    public static class DloopUiCapture
    {
        private const int CaptureSchemaVersion = 1;
        private const int DefaultWidth = 1920;
        private const int DefaultHeight = 1080;

        [Serializable]
        private sealed class CaptureRequest
        {
            public int schema_version;
            public string project_root;
            public string output_dir;
            public PrefabRequest[] prefabs;
        }

        [Serializable]
        private sealed class PrefabRequest
        {
            public string prefab_id;
            public string asset_path;
        }

        [Serializable]
        private sealed class CaptureManifest
        {
            public int schema_version = CaptureSchemaVersion;
            public PrefabCapture[] prefabs;
            public CaptureError[] errors;
        }

        [Serializable]
        private sealed class PrefabCapture
        {
            public string prefab_id;
            public string asset_path;
            public string asset_guid;
            public string asset_sha256;
            public string screenshot_path;
            public int width;
            public int height;
            public RegionCapture[] regions;
        }

        [Serializable]
        private sealed class CaptureError
        {
            public string prefab_id;
            public string message;
        }

        [Serializable]
        private sealed class RegionCapture
        {
            public string object_id;
            public string node_name;
            public string hierarchy_path;
            public CaptureRect rect;
        }

        [Serializable]
        private sealed class CaptureRect
        {
            public float x;
            public float y;
            public float width;
            public float height;
        }

        [MenuItem("Tools/DLoop/Capture UI Evidence")]
        public static void CaptureWithFilePicker()
        {
            var requestPath = Environment.GetEnvironmentVariable("DLOOP_UI_CAPTURE_REQUEST");
            if (string.IsNullOrWhiteSpace(requestPath))
            {
                requestPath = EditorUtility.OpenFilePanel(
                    "选择 DloopUI 截图请求",
                    Application.dataPath,
                    "json");
            }
            if (!string.IsNullOrWhiteSpace(requestPath))
            {
                CaptureRequestFile(requestPath);
            }
        }

        public static void CaptureFromEnvironment()
        {
            var requestPath = Environment.GetEnvironmentVariable("DLOOP_UI_CAPTURE_REQUEST");
            if (string.IsNullOrWhiteSpace(requestPath))
            {
                throw new InvalidOperationException(
                    "DLOOP_UI_CAPTURE_REQUEST 未指向截图请求文件。");
            }
            CaptureRequestFile(requestPath);
        }

        private static void CaptureRequestFile(string requestPath)
        {
            requestPath = Path.GetFullPath(requestPath);
            var request = JsonUtility.FromJson<CaptureRequest>(
                File.ReadAllText(requestPath));
            if (request == null
                || request.schema_version != CaptureSchemaVersion
                || request.prefabs == null)
            {
                throw new InvalidDataException("DloopUI 截图请求格式无效。");
            }

            var projectRoot = Path.GetFullPath(
                Path.Combine(Application.dataPath, ".."));
            if (!PathsEqual(projectRoot, request.project_root))
            {
                throw new InvalidOperationException(
                    "截图请求不属于当前打开的 Unity 项目。");
            }
            var outputDirectory = Path.GetFullPath(request.output_dir);
            EnsureInside(
                outputDirectory,
                projectRoot,
                "截图输出目录必须位于当前 Unity 项目内。");
            Directory.CreateDirectory(outputDirectory);

            var captured = new List<PrefabCapture>();
            var errors = new List<CaptureError>();
            foreach (var prefab in request.prefabs)
            {
                try
                {
                    captured.Add(CapturePrefab(prefab, projectRoot, outputDirectory));
                }
                catch (Exception exception)
                {
                    errors.Add(new CaptureError
                    {
                        prefab_id = prefab == null ? string.Empty : prefab.prefab_id,
                        message = exception.Message,
                    });
                    Debug.LogError(
                        $"DloopUI 无法截图 {prefab?.asset_path}: {exception}");
                }
            }

            var manifest = new CaptureManifest
            {
                prefabs = captured.ToArray(),
                errors = errors.ToArray(),
            };
            var manifestPath = Path.Combine(
                outputDirectory,
                "capture-manifest.json");
            File.WriteAllText(
                manifestPath,
                JsonUtility.ToJson(manifest, true));
            AssetDatabase.Refresh();
            Debug.Log($"DloopUI 截图清单已写入：{manifestPath}");
        }

        private static PrefabCapture CapturePrefab(
            PrefabRequest request,
            string projectRoot,
            string outputDirectory)
        {
            if (request == null || string.IsNullOrWhiteSpace(request.prefab_id))
            {
                throw new InvalidDataException("Prefab 截图请求缺少 prefab_id。");
            }
            var absolutePath = Path.GetFullPath(request.asset_path);
            EnsureInside(
                absolutePath,
                Path.Combine(projectRoot, "Assets"),
                "Prefab 必须位于当前项目 Assets 内。");
            var assetPath = ToProjectAssetPath(absolutePath, projectRoot);
            if (!assetPath.EndsWith(
                    ".prefab",
                    StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidDataException("截图目标不是 .prefab 资产。");
            }

            var previousActiveScene = SceneManager.GetActiveScene();
            var captureScene = EditorSceneManager.NewPreviewScene();
            GameObject root = null;
            try
            {
                var prefabAsset = AssetDatabase.LoadAssetAtPath<GameObject>(assetPath);
                if (prefabAsset == null)
                {
                    throw new InvalidOperationException(
                        $"无法加载 Prefab：{assetPath}");
                }
                root = PrefabUtility.InstantiatePrefab(
                    prefabAsset,
                    captureScene) as GameObject;
                if (root == null)
                {
                    throw new InvalidOperationException(
                        $"无法实例化 Prefab：{assetPath}");
                }
                root.SetActive(true);
                var dimensions = ChooseDimensions(root);
                var screenshotPath = Path.Combine(
                    outputDirectory,
                    request.prefab_id + "-raw.png");
                var regions = RenderAndCollectRegions(
                    root,
                    assetPath,
                    screenshotPath,
                    dimensions.x,
                    dimensions.y);
                return new PrefabCapture
                {
                    prefab_id = request.prefab_id,
                    asset_path = absolutePath.Replace('\\', '/'),
                    asset_guid = AssetDatabase.AssetPathToGUID(assetPath),
                    asset_sha256 = Sha256(absolutePath),
                    screenshot_path = screenshotPath.Replace('\\', '/'),
                    width = dimensions.x,
                    height = dimensions.y,
                    regions = regions,
                };
            }
            finally
            {
                if (root != null)
                {
                    UnityEngine.Object.DestroyImmediate(root);
                }
                if (captureScene.IsValid())
                {
                    EditorSceneManager.ClosePreviewScene(captureScene);
                }
                if (previousActiveScene.IsValid() && previousActiveScene.isLoaded)
                {
                    SceneManager.SetActiveScene(previousActiveScene);
                }
            }
        }

        private static Vector2Int ChooseDimensions(GameObject root)
        {
            var scaler = root.GetComponentInChildren<CanvasScaler>(true);
            var resolution = scaler == null
                ? Vector2.zero
                : scaler.referenceResolution;
            if (resolution.x < 320 || resolution.y < 240)
            {
                var rootRect = root.transform as RectTransform;
                resolution = rootRect == null
                    ? Vector2.zero
                    : rootRect.rect.size;
            }
            var width = resolution.x < 320
                ? DefaultWidth
                : Mathf.RoundToInt(resolution.x);
            var height = resolution.y < 240
                ? DefaultHeight
                : Mathf.RoundToInt(resolution.y);
            return new Vector2Int(
                Mathf.Clamp(width, 320, 2560),
                Mathf.Clamp(height, 240, 2560));
        }

        private static RegionCapture[] RenderAndCollectRegions(
            GameObject root,
            string assetPath,
            string screenshotPath,
            int width,
            int height)
        {
            var cameraObject = new GameObject(
                "DloopUiCaptureCamera",
                typeof(Camera));
            SceneManager.MoveGameObjectToScene(cameraObject, root.scene);
            var camera = cameraObject.GetComponent<Camera>();
            camera.scene = root.scene;
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = new Color(0.12f, 0.12f, 0.12f, 1f);
            camera.orthographic = true;
            camera.nearClipPlane = 0.01f;
            camera.farClipPlane = 100f;
            camera.transform.position = new Vector3(0f, 0f, -10f);

            var canvases = root.GetComponentsInChildren<Canvas>(true);
            if (canvases.Length == 0)
            {
                var canvasObject = new GameObject(
                    "DloopUiCaptureCanvas",
                    typeof(RectTransform),
                    typeof(Canvas),
                    typeof(CanvasScaler));
                SceneManager.MoveGameObjectToScene(canvasObject, root.scene);
                var canvas = canvasObject.GetComponent<Canvas>();
                canvas.renderMode = RenderMode.ScreenSpaceCamera;
                canvas.worldCamera = camera;
                canvas.planeDistance = 1f;
                var scaler = canvasObject.GetComponent<CanvasScaler>();
                scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
                scaler.referenceResolution = new Vector2(width, height);
                root.transform.SetParent(canvasObject.transform, false);
                canvases = new[] { canvas };
            }
            foreach (var canvas in canvases)
            {
                if (canvas.transform.parent != null
                    && canvas.transform.parent.GetComponentInParent<Canvas>() != null)
                {
                    continue;
                }
                canvas.renderMode = RenderMode.ScreenSpaceCamera;
                canvas.worldCamera = camera;
                canvas.planeDistance = 1f;
            }

            var renderTexture = new RenderTexture(
                width,
                height,
                24,
                RenderTextureFormat.ARGB32);
            var texture = new Texture2D(
                width,
                height,
                TextureFormat.RGBA32,
                false);
            var previous = RenderTexture.active;
            try
            {
                camera.targetTexture = renderTexture;
                Canvas.ForceUpdateCanvases();
                camera.Render();
                RenderTexture.active = renderTexture;
                texture.ReadPixels(new Rect(0, 0, width, height), 0, 0);
                texture.Apply();
                File.WriteAllBytes(screenshotPath, texture.EncodeToPNG());
                return CollectVisibleRegions(
                    root,
                    assetPath,
                    camera,
                    width,
                    height);
            }
            finally
            {
                RenderTexture.active = previous;
                camera.targetTexture = null;
                UnityEngine.Object.DestroyImmediate(texture);
                renderTexture.Release();
                UnityEngine.Object.DestroyImmediate(renderTexture);
                UnityEngine.Object.DestroyImmediate(cameraObject);
            }
        }

        private static RegionCapture[] CollectVisibleRegions(
            GameObject root,
            string assetPath,
            Camera camera,
            int width,
            int height)
        {
            return root.GetComponentsInChildren<RectTransform>(true)
                .Where(item => IsVisibleRegion(item))
                .Select(item => BuildRegion(item, assetPath, camera, width, height))
                .Where(item => item.rect.width > 0f && item.rect.height > 0f)
                .OrderBy(item => item.hierarchy_path, StringComparer.Ordinal)
                .ToArray();
        }

        private static bool IsVisibleRegion(RectTransform value)
        {
            if (!value.gameObject.activeInHierarchy)
            {
                return false;
            }
            var graphic = value.GetComponent<Graphic>();
            if (graphic == null)
            {
                return false;
            }
            if (!graphic.enabled || graphic.color.a <= 0.001f)
            {
                return false;
            }
            foreach (var group in value.GetComponentsInParent<CanvasGroup>(true))
            {
                if (group.enabled && group.alpha <= 0.001f)
                {
                    return false;
                }
                if (group.enabled && group.ignoreParentGroups)
                {
                    break;
                }
            }
            return true;
        }

        private static RegionCapture BuildRegion(
            RectTransform value,
            string assetPath,
            Camera camera,
            int width,
            int height)
        {
            var sourceObject = PrefabUtility.GetCorrespondingObjectFromSource(
                value.gameObject);
            var objectId = GlobalObjectId.GetGlobalObjectIdSlow(
                sourceObject == null ? value.gameObject : sourceObject).ToString();
            if (string.IsNullOrWhiteSpace(objectId)
                || objectId.Contains("-00000000000000000000000000000000-"))
            {
                objectId = AssetDatabase.AssetPathToGUID(assetPath)
                    + ":"
                    + HierarchyPath(value);
            }
            return new RegionCapture
            {
                object_id = objectId,
                node_name = value.gameObject.name,
                hierarchy_path = HierarchyPath(value),
                rect = CaptureScreenRect(value, camera, width, height),
            };
        }

        private static CaptureRect CaptureScreenRect(
            RectTransform value,
            Camera camera,
            int width,
            int height)
        {
            var corners = new Vector3[4];
            value.GetWorldCorners(corners);
            var points = corners
                .Select(point => RectTransformUtility.WorldToScreenPoint(camera, point))
                .ToArray();
            var minX = Mathf.Clamp(points.Min(point => point.x), 0, width);
            var maxX = Mathf.Clamp(points.Max(point => point.x), 0, width);
            var minY = Mathf.Clamp(points.Min(point => point.y), 0, height);
            var maxY = Mathf.Clamp(points.Max(point => point.y), 0, height);
            return new CaptureRect
            {
                x = minX,
                y = height - maxY,
                width = Mathf.Max(0, maxX - minX),
                height = Mathf.Max(0, maxY - minY),
            };
        }

        private static string HierarchyPath(Transform value)
        {
            var names = new Stack<string>();
            for (var current = value; current != null; current = current.parent)
            {
                names.Push(current.name);
            }
            return string.Join("/", names.ToArray());
        }

        private static string ToProjectAssetPath(
            string absolutePath,
            string projectRoot)
        {
            var relative = absolutePath
                .Substring(projectRoot.TrimEnd(Path.DirectorySeparatorChar).Length)
                .TrimStart(
                    Path.DirectorySeparatorChar,
                    Path.AltDirectorySeparatorChar);
            return relative.Replace('\\', '/');
        }

        private static string Sha256(string path)
        {
            using (var algorithm = SHA256.Create())
            using (var stream = File.OpenRead(path))
            {
                return "sha256:"
                    + BitConverter.ToString(algorithm.ComputeHash(stream))
                        .Replace("-", string.Empty)
                        .ToLowerInvariant();
            }
        }

        private static bool PathsEqual(string left, string right)
        {
            return string.Equals(
                Path.GetFullPath(left)
                    .TrimEnd(
                        Path.DirectorySeparatorChar,
                        Path.AltDirectorySeparatorChar),
                Path.GetFullPath(right)
                    .TrimEnd(
                        Path.DirectorySeparatorChar,
                        Path.AltDirectorySeparatorChar),
                StringComparison.OrdinalIgnoreCase);
        }

        private static void EnsureInside(
            string candidate,
            string root,
            string message)
        {
            var normalizedCandidate = Path.GetFullPath(candidate);
            var normalizedRoot = Path.GetFullPath(root)
                .TrimEnd(
                    Path.DirectorySeparatorChar,
                    Path.AltDirectorySeparatorChar);
            if (!normalizedCandidate.StartsWith(
                    normalizedRoot + Path.DirectorySeparatorChar,
                    StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException(message);
            }
        }
    }
}
