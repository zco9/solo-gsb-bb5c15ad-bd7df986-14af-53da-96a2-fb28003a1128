"""字典时效：过敏原字典的稳定指纹（按配方实际涉及的原料取子集）。

复核失效判定只关心审查记录配方中真实出现的原料映射，因此：

- 指纹只包含配方配料树中出现过的原料（含复合父配料），配方未涉及的
  原料增删改不影响指纹，不会误报；
- 参与哈希的原料名与过敏原列表均排序后序列化，字典存储/展示顺序调整
  不会改变指纹；
- 原料是否已在字典中登记（present / absent）本身也是映射的一部分，
  所以为原先“未识别”的叶子原料补登记字典同样会判定为需复核；
- 同一原料以相同过敏原集合重复导入（updated_at 变化）不改变指纹。
"""
from __future__ import annotations

import hashlib
import json
from typing import Dict, Iterable, Set

from .analysis import flatten, normalize_name
from .parser import parse_recipe

# 指纹算法版本：未来指纹口径调整时可区分代际
FP_VERSION = "v1"


def dict_tree_keys(nodes: Iterable[dict]) -> Set[str]:
    """从已持久化的配料树（节点 dict）收集全部归一化原料名。"""
    keys: Set[str] = set()
    for node in nodes:
        name = node.get("name", "")
        if name:
            keys.add(normalize_name(name))
        keys |= dict_tree_keys(node.get("children", []))
    return keys


def recipe_keys(text: str) -> Set[str]:
    """解析配方文本，收集其中全部原料（含复合父配料）的归一化名。"""
    tree = parse_recipe(text)
    return {normalize_name(n.name) for n in flatten(tree) if n.name}


def result_keys(result: dict) -> Set[str]:
    """从审查结果的 A/B 配料树中收集全部归一化原料名。"""
    keys: Set[str] = set()
    for version in (result.get("version_a"), result.get("version_b")):
        if version:
            keys |= dict_tree_keys(version.get("tree", []))
    return keys


def scoped_fingerprint(keys: Iterable[str],
                       dictionary: Dict[str, Set[str]]) -> str:
    """按给定原料集合计算字典子集的稳定指纹。

    dictionary 的 key 必须是 normalize_name 之后的形式（与
    ``db.dict_lookup()`` 的返回一致）；未登记原料记录为 ``absent``，
    已登记原料记录为 ``present`` + 排序后的过敏原列表。
    """
    subset = {}
    for key in sorted(set(keys)):
        if key in dictionary:
            subset[key] = ["present", sorted(dictionary[key])]
        else:
            subset[key] = ["absent"]
    payload = json.dumps(
        [FP_VERSION, subset],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
