"""原料别名管理：把「别名：标准名」映射视为有向图。

别名规则（仅接入正式审查与字典时效复核）：

- 每行一条 ``别名：标准名``（冒号中英文均可），# 开头为注释；
- 别名链可任意长（``别名A → 别名B → 标准名``），解析时沿有向边走到
  没有出边的终点（最终标准名），以终点参与字典查询与指纹计算；
- 配方树与遗漏来源路径始终保留并展示原料的**原始名称**，不做改名。

导入时对整批做**原子校验**（任何一条不合法都不写入）：

- 自环（别名与标准名归一化后相同）；
- 同一别名在一批中指向多个不同标准名；
- 任意长度的有向环 —— 返回冲突行号与最短环路（按名称序列展示）。

校验只依赖规范化（去空白、转小写）后的图，因此 ``Milk：乳清粉`` 与
``milk：xxx`` 也会被判定为同一别名的冲突目标。
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

from .analysis import flatten, normalize_name, resolve_alias
from .parser import parse_recipe

_ALIAS_SPLIT_RE = re.compile(r"[:：]")


class AliasImportError(ValueError):
    """别名批量校验未通过（映射为 HTTP 400），errors 为全部冲突明细。"""

    def __init__(self, errors: List[dict]):
        self.errors = errors
        super().__init__("；".join(e.get("message", "") for e in errors))


@dataclass(frozen=True)
class AliasEntry:
    """批量文本中的一条别名定义。"""

    lineno: int
    alias: str
    target: str


# ---------- 解析与整批校验 ----------

def parse_alias_lines(text: str) -> Tuple[List[AliasEntry], List[dict]]:
    """解析批量文本，返回 (合法形态条目, 格式错误)。

    格式错误不抛出，而是收集后与图校验错误一并返回，保证一次展示全部问题。
    """
    entries: List[AliasEntry] = []
    errors: List[dict] = []
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _ALIAS_SPLIT_RE.split(line, maxsplit=1)
        if len(parts) != 2:
            errors.append({
                "type": "format", "lines": [lineno],
                "message": f"第 {lineno} 行格式应为「别名：标准名」：{line}"})
            continue
        alias, target = parts[0].strip(), parts[1].strip()
        if not alias:
            errors.append({
                "type": "format", "lines": [lineno],
                "message": f"第 {lineno} 行别名不能为空：{line}"})
            continue
        if not target:
            errors.append({
                "type": "format", "lines": [lineno],
                "message": f"第 {lineno} 行别名「{alias}」缺少标准名"})
            continue
        entries.append(AliasEntry(lineno=lineno, alias=alias, target=target))
    return entries, errors


def _bfs_shortest_cycle(start: str, edges: Mapping[str, str]) -> Optional[List[str]]:
    """从 start 出发 BFS 找回到 start 的最短环，返回节点序列 [start, ..., start]。"""
    parent: Dict[str, Optional[str]] = {start: None}
    queue = deque([start])
    while queue:
        u = queue.popleft()
        v = edges.get(u)
        if v is None:
            continue
        if v == start:
            # 沿 parent 链重建 start → ... → u，再回到 start 闭环
            path: List[str] = []
            cur: Optional[str] = u
            while cur is not None:
                path.append(cur)
                cur = parent[cur]
            path.reverse()
            path.append(start)
            return path
        if v not in parent:
            parent[v] = u
            queue.append(v)
    return None


def _canonical_cycle(cycle: List[str]) -> tuple:
    """环路去重键：按最小旋转归一化，使同一环从不同节点出发也能识别为同一条。"""
    nodes = cycle[:-1]
    rotations = [tuple(nodes[i:] + nodes[:i]) for i in range(len(nodes))]
    return min(rotations)


def find_cycles(edges: Mapping[str, str],
                order: Mapping[str, int]) -> List[Tuple[List[str], List[int]]]:
    """找出图中相互不同的最短环路（BFS，不枚举路径）。

    edges 为每个别名**唯一**出边；order 为节点首次出现的行号（仅用于
    同长环路的稳定排序）。返回 [(节点序列 [s, ..., s], 冲突行号)]，
    按环路长度、最早冲突行排序。
    """
    found: List[Tuple[List[str], List[int]]] = []
    seen = set()
    starts = sorted(edges, key=lambda k: (order.get(k, 1 << 30), k))
    for start in starts:
        cycle = _bfs_shortest_cycle(start, edges)
        if cycle is None:
            continue
        key = _canonical_cycle(cycle)
        if key in seen:
            continue
        seen.add(key)
        lines = sorted(order[k] for k in cycle[:-1] if k in order)
        found.append((cycle, lines))
    found.sort(key=lambda item: (len(item[0]),
                                 min(item[1]) if item[1] else 1 << 30))
    return found


def _fmt_lines(lines: List[int]) -> str:
    return "、".join(f"第 {n} 行" for n in lines)


def validate_alias_entries(entries: List[AliasEntry]) -> List[dict]:
    """对解析后的整批条目做图校验：自环 / 同一别名多目标 / 环路。"""
    errors: List[dict] = []

    # 1) 自环：归一化后别名与标准名相同
    graph_entries: List[AliasEntry] = []
    for e in entries:
        if normalize_name(e.alias) == normalize_name(e.target):
            errors.append({
                "type": "self_loop", "lines": [e.lineno], "alias": e.alias,
                "message": f"第 {e.lineno} 行别名「{e.alias}」指向自身"
                           f"（标准名相同），自环不允许"})
        else:
            graph_entries.append(e)

    # 2) 同一别名多目标：一批中同一别名出现多个不同标准名
    by_key: Dict[str, List[AliasEntry]] = {}
    for e in graph_entries:
        by_key.setdefault(normalize_name(e.alias), []).append(e)
    for group in by_key.values():
        target_names: Dict[str, str] = {}
        for e in group:
            target_names.setdefault(normalize_name(e.target), e.target)
        if len(target_names) > 1:
            lines = [e.lineno for e in group]
            names = "、".join(dict.fromkeys(
                e.target for e in group))
            errors.append({
                "type": "multi_target", "lines": lines,
                "alias": group[0].alias, "targets": list(target_names.values()),
                "message": f"别名「{group[0].alias}」在{_fmt_lines(lines)}"
                           f"分别指向不同标准名（{names}），"
                           f"同一别名只能有一个目标"})

    # 3) 任意长度环（重复别名取首次出现的边参与建图）
    edges: Dict[str, str] = {}
    display: Dict[str, str] = {}
    order: Dict[str, int] = {}
    for e in graph_entries:
        k, t = normalize_name(e.alias), normalize_name(e.target)
        display.setdefault(k, e.alias)
        display.setdefault(t, e.target)
        order.setdefault(k, e.lineno)
        if k not in edges:
            edges[k] = t
    for cycle, lines in find_cycles(edges, order):
        chain = " → ".join(display[k] for k in cycle)
        where = f"（{_fmt_lines(lines)}）" if lines else ""
        errors.append({
            "type": "cycle", "lines": lines,
            "cycle": [display[k] for k in cycle],
            "message": f"检测到别名环路{where}：{chain}，"
                       f"别名链不允许出现任意长度的环路"})
    return errors


def parse_and_validate(text: str) -> List[AliasEntry]:
    """整批解析 + 校验：任何错误都抛 :class:`AliasImportError`（不做任何写入）。"""
    entries, errors = parse_alias_lines(text)
    errors.extend(validate_alias_entries(entries))
    if errors:
        raise AliasImportError(errors)
    return entries


def validate_upsert(alias: str, target: str, edges: Mapping[str, str],
                    display: Mapping[str, str]) -> List[dict]:
    """单条新增 / 更新别名后对整图校验（防止跨批次引入自环或环路）。"""
    alias, target = alias.strip(), target.strip()
    if not alias or not target:
        return [{"type": "format", "lines": [],
                 "message": "别名与标准名均不能为空"}]
    ka, kt = normalize_name(alias), normalize_name(target)
    if ka == kt:
        return [{"type": "self_loop", "lines": [], "alias": alias,
                 "message": f"别名「{alias}」与标准名相同，自环不允许"}]
    candidate = dict(edges)
    candidate[ka] = kt
    names = dict(display)
    names.setdefault(ka, alias)
    names.setdefault(kt, target)
    order = {k: 1 for k in candidate}
    order[ka] = 0  # 同长环路优先展示新写入的边
    errors = []
    for cycle, _lines in find_cycles(candidate, order):
        chain = " → ".join(names.get(k, k) for k in cycle)
        errors.append({
            "type": "cycle", "lines": [], "cycle": [names.get(k, k) for k in cycle],
            "message": f"别名「{alias} → {target}」会形成环路：{chain}，"
                       f"别名链不允许出现任意长度的环路"})
    return errors


# ---------- 链解析 ----------

def resolve_key(key: str, aliases: Optional[Mapping[str, str]]) -> str:
    """沿别名边走到最终标准名（归一化 key）。图在写入时已保证无环。"""
    return resolve_alias(key, aliases)


def build_resolution_preview(text: str, edges: Mapping[str, str],
                             display: Mapping[str, str],
                             dictionary: Mapping[str, "set"]) -> dict:
    """把一段配方文本中出现的每种原料解析到最终标准名（解析预览）。

    - chain 为原始名到最终标准名的名称序列（含原始名本身）；
    - 树 / 路径仍展示原始名，预览只用于核对别名走向与字典命中情况；
    - 同名原料只展示一次，按在配方中首次出现的顺序。
    """
    tree = parse_recipe(text)
    resolutions = []
    seen = set()
    for node in flatten(tree):
        key = normalize_name(node.name)
        if not key or key in seen:
            continue
        seen.add(key)
        chain = [node.name]
        cur = key
        while cur in edges:
            cur = edges[cur]
            chain.append(display.get(cur, cur))
        allergens = dictionary.get(cur)
        resolutions.append({
            "name": node.name,
            "chain": chain,
            "standard": chain[-1],
            "aliased": len(chain) > 1,
            "registered": allergens is not None,
            "allergens": sorted(allergens) if allergens is not None else None,
        })
    return {"resolutions": resolutions}
