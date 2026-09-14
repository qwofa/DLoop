"""准备有真实 SVN 工作区、真实 CLI 和可执行代码的独立编码样本。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
sys.path.insert(0, str(REPOSITORY / "src" / "FeatureArchive" / "tests"))
from _feature_archive_support import FeatureArchiveCliTestCase


REQUIREMENTS = """# 批量处理条目

用户选择多个条目后确认，一次移除选择的条目并汇总材料；取消不改变任何数据。
空选择返回 0，重复选中的同一条目只处理一次。受保护条目、已不存在的条目使本次操作失败，全部条目与材料保持原状；关联数据更新失败也必须全部恢复。
结果返回本次增加的材料数。既有单件分解行为、共享存储接口和界面持有的条目集合引用保持有效。
本次只实现业务逻辑和直接测试，不修改共享存储、不新增并发或持久化机制。业务条件已经确认，不需要重新询问。
"""
DESIGN = """# 设计

在 inventory.py 的既有批量入口完成业务处理，沿用当前存储与单件操作惯例。
tests 中加入可复核的业务测试，保留既有行为。必要时只读核对调用边界；不改变共享存储接口。
"""


class RealCliFixture(FeatureArchiveCliTestCase):
    def run_cli(self, *arguments, expected=0, **unused):
        process = subprocess.run(
            [sys.executable, "-B", str(self.project_root / "Tools" / "FeatureArchive" / "feature_archive.py"),
             *map(str, arguments)], cwd=self.project_root, capture_output=True, text=True, encoding="utf-8",
        )
        if process.returncode != expected:
            raise RuntimeError(process.stdout + process.stderr)
        return json.loads(process.stdout or process.stderr)


def prepare(source: Path, run_dir: Path):
    source = source.resolve()
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    project = run_dir / "project"
    repository = run_dir / "svn-repository"
    svn, svnadmin = shutil.which("svn"), shutil.which("svnadmin")
    if not svn or not svnadmin:
        raise RuntimeError("svn and svnadmin are required")
    subprocess.run([svnadmin, "create", str(repository)], check=True, capture_output=True)
    subprocess.run([svn, "checkout", repository.as_uri(), str(project)], check=True, capture_output=True)
    shutil.copytree(HERE / "fixture", project, dirs_exist_ok=True)
    shutil.copytree(source / "src" / "FeatureArchive", project / "Tools" / "FeatureArchive",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"))
    shutil.copytree(source / "plugin" / "dloop" / "skills", project / ".agents" / "skills")
    subprocess.run([svn, "propset", "svn:ignore", ".scratch\n__pycache__\n", str(project)], check=True, capture_output=True)
    subprocess.run([svn, "add", "--force", str(project)], check=True, capture_output=True)
    subprocess.run([svn, "commit", str(project), "-m", "isolated coding task initial state"], check=True, capture_output=True)
    fixture = RealCliFixture()
    fixture._project_root = project
    fixture.root = project / ".scratch" / "dloop-v3" / "outputs"
    fixture.workspace = project
    feature = fixture.init_complex()
    for suffix, body in (("01-requirements", REQUIREMENTS), ("03-design", DESIGN)):
        path = feature / suffix / "README.md"
        content = path.read_text(encoding="utf-8")
        metadata = content.split("---", 2)[1]
        path.write_text("---" + metadata + "---\n\n" + body, encoding="utf-8", newline="\n")
        fixture.set_status(path, "confirmed")
        fixture.run_cli("confirm-change", "--document-id",
                        "reliable-delivery." + ("requirements.overview" if suffix == "01-requirements" else "design.overview"),
                        "--semantic-change", "true")
    fixture.approve_requirements()
    fixture.prepare_execution_inputs()
    fixture.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active")
    path = fixture.write_package("bulk-salvage", scope=["inventory.py", "tests"])
    package = json.loads(path.read_text(encoding="utf-8"))
    package["context_materials"] = [
        {"source": "reliable-delivery.requirements.overview", "purpose": "已批准业务要求", "mode": "full"},
        {"source": "inventory.py", "purpose": "现有实现与本次入口", "mode": "full"},
    ]
    contract = package["slice_contract"]
    contract.update({"business_outcome": "批量处理条目", "acceptance_scenarios": ["批量确认、取消和失败时数据保持一致"],
                     "validation_methods": ["python -B -m unittest discover -s tests -v"],
                     "validation_rationale": "局部业务逻辑及其直接调用边界", "rollback_point": "恢复本次切片基线"})
    path.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8", newline="\n")
    fixture.run_cli("start-slice", "--feature-id", "reliable-delivery", "--execution-id", "implement-1",
                    "--package-file", path, "--workspace-root", project)
    handoff = fixture.run_cli("prepare-handoff", "--feature-id", "reliable-delivery",
                              "--action", "implementation", "--role", "implementation")
    (run_dir / "handoff.json").write_text(json.dumps(handoff, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    files = {p.relative_to(project).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in project.rglob("*.py") if "Tools" not in p.parts and ".agents" not in p.parts}
    (run_dir / "manifest.json").write_text(json.dumps({
        "source": str(source), "version": (source / "VERSION").read_text().strip(), "project": str(project),
        "prepared_at": datetime.now(timezone.utc).isoformat(), "initial_product_sha256": files,
        "execution_mode": "native-agent-real-code-and-cli", "metrics": {"tokens": None, "tool_calls": None},
    }, indent=2), encoding="utf-8", newline="\n")
    print(json.dumps({"project": str(project), "handoff": str(run_dir / "handoff.json")}, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    prepare(args.source, args.run_dir)
