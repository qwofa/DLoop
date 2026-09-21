"""同一份场景验收记录用于准备、候选交接和最终展示。"""

import json
from pathlib import Path

from archive_handoff import ArchiveHandoffError


LEVELS = {"static": "静态检查", "isolated": "客户端隔离验证", "runtime": "实际环境验证"}
STATUSES = {"passed": "通过", "failed": "未通过", "unverified": "未验证"}


def _evidence(value, workspace, fail):
    if not isinstance(value, list):
        fail("证据必须为列表。")
    evidence = []
    for ref in value:
        if not isinstance(ref, dict) or set(ref) != {"path", "locator"} or not all(isinstance(v, str) and v.strip() for v in ref.values()):
            fail("每条证据须包含文件路径和定位。")
        target = Path(ref["path"])
        if not target.is_absolute():
            target = Path(workspace) / target
        if not target.is_file():
            fail(f"验收证据不存在：{target}")
        evidence.append({"path": str(target.resolve()), "locator": ref["locator"]})
    return evidence


def read_acceptance(path, scenario, workspace):
    def fail(message):
        raise ArchiveHandoffError([{"code": "INVALID_ACCEPTANCE_RECORD", "message": message,
                                   "owner": "implementation", "recovery": "更新同一份场景记录，不补写逐控件交互说明。"}])

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        fail(f"无法读取场景验收记录：{error}")
    if (not isinstance(data, dict) or set(data) != {"runtime_smoke", "scenarios"}
            or not isinstance(data.get("scenarios"), list)):
        fail("验收记录须包含 runtime_smoke 和 scenarios 列表。")
    smoke = data["runtime_smoke"]
    smoke_fields = {"entry", "content", "exit", "refresh", "status", "pending", "evidence"}
    if not isinstance(smoke, dict) or set(smoke) != smoke_fields:
        fail("最小运行冒烟须记录进入、非空内容、退出、刷新、状态、待办及证据。")
    for field in smoke_fields - {"evidence"}:
        if not isinstance(smoke[field], str) or not smoke[field].strip() or smoke[field].startswith("待填写"):
            fail(f"最小运行冒烟的 {field} 尚未填写；无法执行时须写明实际缺项。")
    if smoke["status"] not in STATUSES:
        fail("最小运行冒烟状态须为 passed、failed 或 unverified。")
    smoke_evidence = _evidence(smoke["evidence"], workspace, fail)
    if smoke["status"] != "unverified" and not smoke_evidence:
        fail("已执行的最小运行冒烟须提供实际证据。")
    matches = [item for item in data["scenarios"] if isinstance(item, dict) and item.get("scenario") == scenario]
    if len(matches) != 1:
        fail(f"验收记录须唯一包含场景：{scenario}")
    item = matches[0]
    fields = {"scenario", "changes", "entry", "setup", "steps", "expected", "level", "status", "actual", "pending", "evidence"}
    if set(item) != fields:
        fail("场景须包含变化、入口、准备条件、步骤、预期、验证层级、状态、实际结果、待完成项及证据。")
    for field in fields - {"steps", "evidence"}:
        if not isinstance(item[field], str) or not item[field].strip() or item[field].startswith("待填写"):
            fail(f"场景 {scenario} 的 {field} 尚未填写；没有待办时明确写无。")
    if item["level"] not in LEVELS or item["status"] not in STATUSES:
        fail("验证层级须为 static、isolated 或 runtime；状态须为 passed、failed 或 unverified。")
    if not isinstance(item["steps"], list) or not item["steps"] or any(not isinstance(s, str) or not s.strip() or s.strip().startswith("待填写") for s in item["steps"]):
        fail("场景须提供可执行的简短验收步骤。")
    if not isinstance(item["evidence"], list) or (item["status"] != "unverified" and not item["evidence"]):
        fail("已执行的检查须提供实际证据；尚未执行可使用空列表。")
    evidence = _evidence(item["evidence"], workspace, fail)
    return {**item, "evidence": evidence, "runtime_smoke": {**smoke, "evidence": smoke_evidence}}


def acceptance_template(scenarios):
    return {"runtime_smoke": {
                "entry": "待填写：从真实产品入口打开本次界面",
                "content": "待填写：确认至少一项真实业务数据可见且非空",
                "exit": "待填写：关闭或离开界面且无卡死、残留遮挡",
                "refresh": "待填写：重新进入或触发刷新后内容状态正确",
                "status": "unverified", "pending": "待执行最小运行冒烟", "evidence": [],
            },
            "scenarios": [{"scenario": scenario, "changes": "待填写：本次变化",
                           "entry": "待填写：游戏或产品中的进入路径",
                           "setup": "待填写：账号、状态、数据及准备责任人；缺环境时写明缺项",
                           "steps": ["待填写：最短实际操作"], "expected": "待填写：应观察到的结果",
                           "level": "runtime", "status": "unverified", "actual": "尚未执行",
                           "pending": "待执行实际环境验证", "evidence": []} for scenario in scenarios]}
