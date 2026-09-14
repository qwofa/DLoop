"""业务术语文档、Markdown 引用和术语影响分析的确定性能力。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple
from urllib.parse import unquote, urlsplit


TERMINOLOGY_SCHEMA_VERSION = 2
TERMINOLOGY_RELATIVE_PATH = Path("01-requirements") / "terminology.md"
TERM_SECTION_STATUSES = {
    "已确认术语": "已确认",
    "候选术语": "候选",
}
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FIELD_PATTERN = re.compile(r"^-\s*([^：:]+)[：:]\s*(.*?)\s*$")
FENCE_PATTERN = re.compile(r"^\s*(`{3,}|~{3,})")
INLINE_CODE_PATTERN = re.compile(r"`+[^`\n]*`+")
LINK_PATTERN = re.compile(
    r"(?P<prefix>!|\*\*)?"
    r"\[(?P<label>[^\]\n]+)\]"
    r"\((?P<target>[^)\n]+)\)"
    r"(?P<suffix>\*\*)?"
)


class ArchiveTerminologyError(Exception):
    """表示术语结构、链接或术语命令中的确定性错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MarkdownHeading:
    """Markdown 标题及其稳定锚点。"""

    text: str
    slug: str
    level: int
    line: int


@dataclass(frozen=True)
class MarkdownLink:
    """Markdown 正文中的真实链接。"""

    label: str
    target: str
    bold: bool
    line: int
    section: str
    markup: str


@dataclass(frozen=True)
class MarkdownScan:
    """忽略代码内容后得到的标题与链接。"""

    headings: Tuple[MarkdownHeading, ...]
    links: Tuple[MarkdownLink, ...]


@dataclass(frozen=True)
class TermRecord:
    """术语表中的一个候选或已确认术语。"""

    name: str
    status: str
    definition: str
    boundary: str | None
    aliases: Tuple[str, ...]
    line: int
    slug: str


@dataclass(frozen=True)
class TerminologyDocument:
    """已解析的单个交付项术语表。"""

    path: Path
    terms: Mapping[str, TermRecord]
    aliases: Mapping[str, str]


def _body_lines(content: str) -> Tuple[Tuple[int, str], ...]:
    lines = content.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    if not lines or lines[0] != "---":
        return tuple(enumerate(lines, start=1))
    for index in range(1, len(lines)):
        if lines[index] == "---":
            return tuple(
                (line_number, line)
                for line_number, line in enumerate(lines[index + 1 :], start=index + 2)
            )
    return tuple(enumerate(lines, start=1))


def _plain_heading(value: str) -> str:
    without_links = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    return re.sub(r"[*_~`]", "", without_links).strip()


def markdown_heading_slug(value: str) -> str:
    """生成当前工作流支持的确定性 Markdown 标题锚点。"""

    normalized = unicodedata.normalize("NFKC", _plain_heading(value)).lower()
    characters = []
    for character in normalized:
        category = unicodedata.category(character)
        if character in {"-", "_", " "} or category.startswith(("L", "N")):
            characters.append(character)
    return re.sub(r"-+", "-", "".join(characters).replace(" ", "-")).strip("-")


def _masked_inline_code(line: str) -> str:
    return INLINE_CODE_PATTERN.sub(lambda match: " " * len(match.group(0)), line)


def scan_markdown(path: Path, content: str | None = None) -> MarkdownScan:
    """扫描真实 Markdown 标题和链接，忽略代码围栏与行内代码。"""

    try:
        source = content if content is not None else path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exception:
        raise ArchiveTerminologyError(
            "UNREADABLE_DOCUMENT",
            f"无法读取 Markdown 文档“{path}”：{exception}",
        ) from exception

    headings: List[MarkdownHeading] = []
    links: List[MarkdownLink] = []
    slug_counts: Dict[str, int] = {}
    fence_marker: str | None = None
    current_section = ""

    for line_number, raw_line in _body_lines(source):
        fence_match = FENCE_PATTERN.match(raw_line)
        if fence_match:
            marker = fence_match.group(1)
            marker_kind = marker[0]
            if fence_marker is None:
                fence_marker = marker_kind
            elif fence_marker == marker_kind:
                fence_marker = None
            continue
        if fence_marker is not None:
            continue

        line = _masked_inline_code(raw_line)
        heading_match = HEADING_PATTERN.match(line)
        if heading_match:
            text = _plain_heading(heading_match.group(2))
            base_slug = markdown_heading_slug(text)
            occurrence = slug_counts.get(base_slug, 0)
            slug_counts[base_slug] = occurrence + 1
            slug = base_slug if occurrence == 0 else f"{base_slug}-{occurrence}"
            heading = MarkdownHeading(
                text=text,
                slug=slug,
                level=len(heading_match.group(1)),
                line=line_number,
            )
            headings.append(heading)
            current_section = text

        for match in LINK_PATTERN.finditer(line):
            prefix = match.group("prefix")
            suffix = match.group("suffix")
            if prefix == "!":
                continue
            bold = prefix == "**" and suffix == "**"
            links.append(
                MarkdownLink(
                    label=_plain_heading(match.group("label")),
                    target=match.group("target").strip(),
                    bold=bold,
                    line=line_number,
                    section=current_section,
                    markup=match.group(0),
                )
            )

    return MarkdownScan(headings=tuple(headings), links=tuple(links))


def _split_aliases(value: str) -> Tuple[str, ...]:
    if not value:
        return ()
    normalized = value.rstrip("。；;")
    aliases = tuple(
        item.strip()
        for item in re.split(r"[、,，]", normalized)
        if item.strip()
    )
    if len(aliases) != len(set(aliases)):
        raise ArchiveTerminologyError(
            "INVALID_TERMINOLOGY",
            f"术语别名包含重复项：{value}",
        )
    return aliases


def parse_terminology(path: Path, content: str | None = None) -> TerminologyDocument:
    """解析固定 Markdown 术语表，不进行自然语言推断。"""

    try:
        source = content if content is not None else path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exception:
        raise ArchiveTerminologyError(
            "UNREADABLE_DOCUMENT",
            f"无法读取术语表“{path}”：{exception}",
        ) from exception

    scan = scan_markdown(path, source)
    slug_by_line = {heading.line: heading.slug for heading in scan.headings}
    lines = dict(_body_lines(source))
    ordered_lines = tuple(sorted(lines))
    terms: Dict[str, TermRecord] = {}
    alias_owners: Dict[str, str] = {}
    active_section: str | None = None
    index = 0

    while index < len(ordered_lines):
        line_number = ordered_lines[index]
        line = lines[line_number]
        heading_match = HEADING_PATTERN.match(line)
        if heading_match is None:
            index += 1
            continue
        level = len(heading_match.group(1))
        heading_text = _plain_heading(heading_match.group(2))
        if level == 2:
            active_section = (
                heading_text if heading_text in TERM_SECTION_STATUSES else None
            )
            index += 1
            continue
        if level != 3 or active_section is None:
            index += 1
            continue

        name = heading_text
        if not name:
            raise ArchiveTerminologyError(
                "INVALID_TERMINOLOGY",
                f"术语表“{path}”第 {line_number} 行包含空术语标题。",
            )
        if name in terms:
            raise ArchiveTerminologyError(
                "INVALID_TERMINOLOGY",
                f"术语表“{path}”重复定义首选术语“{name}”。",
            )

        fields: Dict[str, str] = {}
        next_index = index + 1
        while next_index < len(ordered_lines):
            nested_line_number = ordered_lines[next_index]
            nested_line = lines[nested_line_number]
            nested_heading = HEADING_PATTERN.match(nested_line)
            if nested_heading and len(nested_heading.group(1)) <= 3:
                break
            field_match = FIELD_PATTERN.match(nested_line.strip())
            if field_match:
                field = field_match.group(1).strip()
                if field in fields:
                    raise ArchiveTerminologyError(
                        "INVALID_TERMINOLOGY",
                        f"术语“{name}”重复声明字段“{field}”。",
                    )
                fields[field] = field_match.group(2).strip()
            next_index += 1

        expected_status = TERM_SECTION_STATUSES[active_section]
        if fields.get("状态") != expected_status:
            raise ArchiveTerminologyError(
                "INVALID_TERMINOLOGY",
                f"术语“{name}”位于“{active_section}”，状态必须为“"
                f"{expected_status}”。",
            )
        definition = fields.get("一句话定义", "").strip()
        if not definition:
            raise ArchiveTerminologyError(
                "INVALID_TERMINOLOGY",
                f"术语“{name}”必须填写非空的一句话定义。",
            )
        aliases = _split_aliases(fields.get("别名", ""))
        for alias in aliases:
            if alias == name:
                raise ArchiveTerminologyError(
                    "INVALID_TERMINOLOGY",
                    f"术语“{name}”不能把首选术语自身声明为别名。",
                )
            previous = alias_owners.get(alias)
            if previous is not None and previous != name:
                raise ArchiveTerminologyError(
                    "INVALID_TERMINOLOGY",
                    f"别名“{alias}”同时指向术语“{previous}”和“{name}”。",
                )
            alias_owners[alias] = name

        terms[name] = TermRecord(
            name=name,
            status=expected_status,
            definition=definition,
            boundary=fields.get("相邻概念边界") or None,
            aliases=aliases,
            line=line_number,
            slug=slug_by_line.get(line_number, markdown_heading_slug(name)),
        )
        index = next_index

    required_sections = {
        heading.text
        for heading in scan.headings
        if heading.level == 2 and heading.text in TERM_SECTION_STATUSES
    }
    missing_sections = sorted(set(TERM_SECTION_STATUSES) - required_sections)
    if missing_sections:
        raise ArchiveTerminologyError(
            "INVALID_TERMINOLOGY",
            f"术语表“{path}”缺少固定章节：" + "、".join(missing_sections),
        )
    return TerminologyDocument(
        path=path,
        terms=dict(sorted(terms.items())),
        aliases=dict(sorted(alias_owners.items())),
    )


def _external_target(target: str) -> bool:
    parsed = urlsplit(target)
    return bool(parsed.scheme and parsed.scheme not in {"file"})


def _link_destination(source: Path, target: str) -> Tuple[Path, str]:
    target_without_title = target.split(maxsplit=1)[0]
    path_text, separator, fragment = target_without_title.partition("#")
    decoded_path = unquote(path_text)
    destination = (
        (source.parent / decoded_path).resolve()
        if decoded_path
        else source.resolve()
    )
    return destination, unquote(fragment) if separator else ""


def _heading_by_slug(path: Path) -> Mapping[str, MarkdownHeading]:
    return {heading.slug: heading for heading in scan_markdown(path).headings}


def _validate_link_target(source: Path, link: MarkdownLink) -> Tuple[Path, str]:
    if _external_target(link.target):
        return source, ""
    destination, fragment = _link_destination(source, link.target)
    if not destination.is_file():
        raise ArchiveTerminologyError(
            "BROKEN_DOCUMENT_LINK",
            f"文档“{source}”第 {link.line} 行的链接目标不存在：{link.target}",
        )
    if fragment and destination.suffix.lower() == ".md":
        headings = _heading_by_slug(destination)
        if fragment.lower() not in headings:
            raise ArchiveTerminologyError(
                "BROKEN_DOCUMENT_ANCHOR",
                f"文档“{source}”第 {link.line} 行的标题锚点不存在："
                f"{link.target}",
            )
    return destination, fragment


def validate_archive_markdown(
    features: Mapping[str, object],
    documents: Mapping[str, object],
) -> None:
    """校验普通 Markdown 链接及启用术语机制档案的正式术语引用。"""

    documents_by_feature: Dict[str, List[object]] = {
        feature_id: [] for feature_id in features
    }
    for document in documents.values():
        documents_by_feature[document.feature_id].append(document)

    for feature_id in sorted(features):
        feature = features[feature_id]
        terminology: TerminologyDocument | None = None
        terminology_path = feature.path / TERMINOLOGY_RELATIVE_PATH
        if feature.terminology_schema_version == TERMINOLOGY_SCHEMA_VERSION:
            terminology_id = f"{feature_id}.requirements.terminology"
            terminology_record = documents.get(terminology_id)
            if (
                terminology_record is None
                or terminology_record.path.resolve() != terminology_path.resolve()
            ):
                raise ArchiveTerminologyError(
                    "MISSING_TERMINOLOGY",
                    f"功能档案“{feature_id}”缺少标准术语文档"
                    f"“{TERMINOLOGY_RELATIVE_PATH.as_posix()}”。",
                )
            requirements_id = f"{feature_id}.requirements.overview"
            requirements = documents.get(requirements_id)
            if (
                requirements is None
                or terminology_id not in requirements.dependencies
            ):
                raise ArchiveTerminologyError(
                    "MISSING_TERMINOLOGY_DEPENDENCY",
                    f"需求总览“{requirements_id}”必须依赖术语表"
                    f"“{terminology_id}”。",
                )
            terminology = parse_terminology(terminology_path)

        for document in sorted(
            documents_by_feature[feature_id],
            key=lambda item: item.document_id,
        ):
            scan = scan_markdown(document.path)
            for link in scan.links:
                destination, fragment = _validate_link_target(document.path, link)
                if not link.bold or terminology is None:
                    continue
                term = terminology.terms.get(link.label)
                if (
                    destination != terminology.path.resolve()
                    or term is None
                    or term.status != "已确认"
                    or fragment.lower() != term.slug
                ):
                    raise ArchiveTerminologyError(
                        "INVALID_TERM_REFERENCE",
                        f"文档“{document.path}”第 {link.line} 行的粗体链接必须"
                        "指向当前交付项已确认术语，且链接文本必须等于首选术语："
                        f"{link.markup}",
                    )


def transitive_dependents(
    dependents: Mapping[str, Sequence[str]],
    source_id: str,
) -> Tuple[str, ...]:
    """按广度优先和稳定顺序返回全部传递依赖者。"""

    visited = set()
    queue = list(sorted(dependents.get(source_id, ())))
    ordered = []
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        ordered.append(current)
        queue.extend(
            dependent
            for dependent in sorted(dependents.get(current, ()))
            if dependent not in visited
        )
    return tuple(ordered)


def _require_feature(graph: object, feature_id: str) -> object:
    feature = graph.features.get(feature_id)
    if feature is None:
        raise ArchiveTerminologyError(
            "UNKNOWN_FEATURE",
            f"功能档案“{feature_id}”不存在。",
        )
    if feature.terminology_schema_version != TERMINOLOGY_SCHEMA_VERSION:
        raise ArchiveTerminologyError(
            "TERMINOLOGY_NOT_ENABLED",
            f"功能档案“{feature_id}”没有启用业务术语机制。",
        )
    return feature


def _validate_term_name(value: str, field: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or any(character in normalized for character in "\r\n[]()#*`")
    ):
        raise ArchiveTerminologyError(
            "INVALID_TERM_NAME",
            f"{field}必须是非空、无首尾空白且不包含 Markdown 结构字符的名称。",
        )
    return normalized


def _replace_term_definition(
    content: str,
    term: TermRecord,
    new_name: str,
) -> str:
    lines = content.splitlines()
    had_final_newline = content.endswith("\n")
    heading_index = term.line - 1
    expected_heading = f"### {term.name}"
    if heading_index >= len(lines) or lines[heading_index].strip() != expected_heading:
        raise ArchiveTerminologyError(
            "TERM_RENAME_CONFLICT",
            f"术语“{term.name}”的标题位置已经变化，请重新执行。",
        )
    lines[heading_index] = f"### {new_name}"

    block_end = heading_index + 1
    alias_index: int | None = None
    while block_end < len(lines):
        heading = HEADING_PATTERN.match(lines[block_end])
        if heading and len(heading.group(1)) <= 3:
            break
        field = FIELD_PATTERN.match(lines[block_end].strip())
        if field and field.group(1).strip() == "别名":
            alias_index = block_end
        block_end += 1

    aliases = [alias for alias in term.aliases if alias != new_name]
    if term.name not in aliases:
        aliases.append(term.name)
    alias_line = "- 别名：" + "、".join(aliases) + "。"
    if alias_index is None:
        insertion = block_end
        while insertion > heading_index + 1 and not lines[insertion - 1].strip():
            insertion -= 1
        lines.insert(insertion, alias_line)
    else:
        lines[alias_index] = alias_line

    result = "\n".join(lines)
    if had_final_newline:
        result += "\n"
    return result


def _replace_markdown_links(
    content: str,
    replacements: Sequence[Tuple[MarkdownLink, str]],
) -> str:
    lines = content.splitlines()
    had_final_newline = content.endswith("\n")
    for link, replacement in sorted(
        replacements,
        key=lambda item: (item[0].line, item[0].markup),
    ):
        index = link.line - 1
        if index >= len(lines) or link.markup not in lines[index]:
            raise ArchiveTerminologyError(
                "TERM_RENAME_CONFLICT",
                f"第 {link.line} 行的术语引用已经变化：{link.markup}",
            )
        lines[index] = lines[index].replace(link.markup, replacement, 1)
    result = "\n".join(lines)
    if had_final_newline:
        result += "\n"
    return result


def _term_link_replacement(link: MarkdownLink, new_name: str) -> str:
    target_without_title = link.target.split(maxsplit=1)[0]
    path_text, separator, _ = target_without_title.partition("#")
    if not separator:
        raise ArchiveTerminologyError(
            "INVALID_TERM_REFERENCE",
            f"术语引用缺少标题锚点：{link.markup}",
        )
    return (
        f"**[{new_name}]({path_text}#"
        f"{markdown_heading_slug(new_name)})**"
    )


def rename_term(
    root: Path,
    feature_id: str,
    old_name: str,
    new_name: str,
) -> Mapping[str, object]:
    """在单个活动交付项内原子执行无语义术语重命名。"""

    from archive_changes import (
        _body_fingerprint,
        _manifest_with_updated_at,
        _normalized_content,
        _replace_files_atomically,
        _rewrite_front_matter,
        _utc_now,
    )
    from archive_validation import validate_archive_root

    graph = validate_archive_root(root)
    feature = _require_feature(graph, feature_id)
    if feature.lifecycle in {"frozen", "pending_cleanup"}:
        raise ArchiveTerminologyError(
            "READ_ONLY_ARCHIVE",
            f"功能档案“{feature_id}”处于 {feature.lifecycle}，不能重命名术语。",
        )
    old_value = _validate_term_name(old_name, "旧术语")
    new_value = _validate_term_name(new_name, "新术语")
    if old_value == new_value:
        return {
            "status": "unchanged",
            "feature_id": feature_id,
            "term": old_value,
            "updated_documents": [],
        }

    terminology_path = feature.path / TERMINOLOGY_RELATIVE_PATH
    terminology = parse_terminology(terminology_path)
    term = terminology.terms.get(old_value)
    if term is None:
        raise ArchiveTerminologyError(
            "UNKNOWN_TERM",
            f"术语表中不存在首选术语“{old_value}”。",
        )
    if term.status != "已确认":
        raise ArchiveTerminologyError(
            "UNCONFIRMED_TERM",
            f"只有已确认术语可以执行无语义重命名：“{old_value}”。",
        )
    if new_value in terminology.terms or (
        new_value in terminology.aliases
        and terminology.aliases[new_value] != old_value
    ):
        raise ArchiveTerminologyError(
            "TERM_NAME_CONFLICT",
            f"新术语“{new_value}”已经被首选术语或别名占用。",
        )

    original_contents: Dict[Path, str] = {}
    updated_contents: Dict[Path, str] = {}
    terminology_content = _normalized_content(terminology_path)
    original_contents[terminology_path] = terminology_content
    updated_contents[terminology_path] = _replace_term_definition(
        terminology_content,
        term,
        new_value,
    )

    reference_count = 0
    for document in sorted(
        (
            item
            for item in graph.documents.values()
            if item.feature_id == feature_id
        ),
        key=lambda item: item.document_id,
    ):
        scan = scan_markdown(document.path)
        replacements = []
        for link in scan.links:
            if not link.bold or link.label != old_value:
                continue
            destination, fragment = _link_destination(document.path, link.target)
            if (
                destination != terminology_path.resolve()
                or fragment.lower() != term.slug
            ):
                continue
            replacements.append(
                (link, _term_link_replacement(link, new_value))
            )
        if not replacements:
            continue
        source = _normalized_content(document.path)
        original_contents.setdefault(document.path, source)
        updated_contents[document.path] = _replace_markdown_links(
            source,
            replacements,
        )
        reference_count += len(replacements)

    for path, content in tuple(updated_contents.items()):
        fingerprint = _body_fingerprint(content, path)
        updated_contents[path] = _rewrite_front_matter(
            content,
            path,
            {"content_fingerprint": fingerprint},
        )

    manifest_path = feature.path / "feature.json"
    original_contents[manifest_path] = _normalized_content(manifest_path)
    updated_contents[manifest_path] = _manifest_with_updated_at(
        manifest_path,
        _utc_now(),
    )

    _replace_files_atomically(graph.root, updated_contents)
    try:
        validate_archive_root(graph.root)
    except Exception:
        _replace_files_atomically(graph.root, original_contents)
        raise

    return {
        "status": "renamed",
        "feature_id": feature_id,
        "from": old_value,
        "to": new_value,
        "semantic_change": False,
        "updated_documents": sorted(
            path.relative_to(graph.root).as_posix()
            for path in updated_contents
            if path.suffix.lower() == ".md"
        ),
        "updated_references": reference_count,
    }


def analyze_term_impact(
    root: Path,
    feature_id: str,
    term_name: str,
) -> Mapping[str, object]:
    """组合术语引用位置与文档依赖图，输出只读影响分析。"""

    from archive_validation import validate_archive_root

    graph = validate_archive_root(root)
    feature = _require_feature(graph, feature_id)
    normalized_name = _validate_term_name(term_name, "术语")
    terminology_id = f"{feature_id}.requirements.terminology"
    terminology = parse_terminology(feature.path / TERMINOLOGY_RELATIVE_PATH)
    term = terminology.terms.get(normalized_name)
    if term is None:
        raise ArchiveTerminologyError(
            "UNKNOWN_TERM",
            f"术语表中不存在首选术语“{normalized_name}”。",
        )

    references = []
    for document in sorted(
        (
            item
            for item in graph.documents.values()
            if item.feature_id == feature_id
        ),
        key=lambda item: item.document_id,
    ):
        for link in scan_markdown(document.path).links:
            if not link.bold or link.label != normalized_name:
                continue
            destination, fragment = _link_destination(document.path, link.target)
            if (
                destination != terminology.path.resolve()
                or fragment.lower() != term.slug
            ):
                continue
            references.append(
                {
                    "document_id": document.document_id,
                    "path": document.path.relative_to(graph.root).as_posix(),
                    "line": link.line,
                    "section": link.section,
                }
            )

    direct = tuple(sorted(graph.dependents.get(terminology_id, ())))
    transitive = transitive_dependents(graph.dependents, terminology_id)
    return {
        "status": "analyzed",
        "feature_id": feature_id,
        "lifecycle": feature.lifecycle,
        "term": normalized_name,
        "term_status": term.status,
        "definition": {
            "path": terminology.path.relative_to(graph.root).as_posix(),
            "line": term.line,
        },
        "references": sorted(
            references,
            key=lambda item: (
                item["document_id"],
                item["line"],
            ),
        ),
        "direct_dependents": list(direct),
        "transitive_dependents": list(transitive),
        "refreshable": feature.lifecycle not in {"frozen", "pending_cleanup"},
    }
