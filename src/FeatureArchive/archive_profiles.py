"""DLoop 严格交付合同常量。"""

WORKFLOW_VERSION = "3.0.0"
CONTEXT_CONTRACT_VERSION = 2
STRICT_PROFILE = "strict-v1"
ROOT_MANIFEST_NAME = ".dloop-root.json"
ROOT_SCHEMA_VERSION = 1
ARCHIVE_SCHEMA_VERSION = 3
WORKFLOW_STATE_NAME = "workflow-state.json"
WORKFLOW_STATE_SCHEMA_VERSION = 8
APPROVAL_STAGES = ("requirements", "architecture", "ui-baseline", "final")

# 交付分级只决定是否维护调查、设计和计划环节；需求共识、切片契约、
# 修改租约、独立评审、集成确认和冻结门禁在两级中完全相同。
STANDARD_TIER = "standard"
LITE_TIER = "lite"
DELIVERY_TIERS = (STANDARD_TIER, LITE_TIER)
LITE_SKIPPED_CATEGORIES = frozenset({"investigation", "design", "plan"})


def execution_input_categories(delivery_tier: str) -> frozenset[str]:
    """返回实施前必须确认的阶段文档类别。"""

    if delivery_tier == LITE_TIER:
        return frozenset({"requirements"})
    return frozenset({"requirements", "design", "plan"})
