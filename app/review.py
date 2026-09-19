"""审查核心流程：解析两版配方、传播过敏原、标签核对，并产出字典指纹。

新建审查（main.create_review）与历史复核（recheck / main.recheck_review）
共用本模块，保证“确认复核”后覆盖的结果与当时新建审查的计算口径完全一致。

原料先经别名链解析到最终标准名再参与字典查询；配料树与遗漏来源路径仍
保留原始名称。别名规则仅接入本正式审查流程，不影响只读的临时预览。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set

from . import db
from .analysis import (attach_allergens, collect_allergens, compare_label,
                       diff_allergens, diff_ingredients, flatten)
from .fingerprint import recipe_keys, scoped_fingerprint
from .parser import IngredientNode, parse_recipe


def build_version(tree: List[IngredientNode]) -> dict:
    return {
        "tree": [node.to_dict() for node in tree],
        "allergens": sorted(collect_allergens(tree)),
        "unknown_ingredients": sorted({n.name for n in flatten(tree) if n.unknown}),
    }


def _parse_version(text: str, dictionary: Dict[str, Set[str]],
                   aliases: Dict[str, str]):
    tree = parse_recipe(text)
    attach_allergens(tree, dictionary, aliases)
    return tree, build_version(tree)


def analyze(*, text_a: str, text_b: Optional[str], label_a: str, label_b: str,
            declared: List[str], dictionary: Optional[Dict[str, Set[str]]] = None,
            aliases: Optional[Dict[str, str]] = None):
    """用给定（默认当前）字典与别名规则解析配方并构建审查结果。

    返回 (result, fingerprint)：
    - result 结构与历史上持久化的结果 JSON 完全一致；配料树与遗漏来源
      路径保留原料原始名，过敏原查询与“未识别”判定先经别名链解析到
      最终标准名；
    - fingerprint 为 A/B 配方实际涉及原料解析到的最终标准名在当前字典上
      的稳定指纹（A、B 两版涉及原料取并集）。
    """
    dictionary = db.dict_lookup() if dictionary is None else dictionary
    aliases = db.alias_map() if aliases is None else aliases

    tree_a, version_a = _parse_version(text_a, dictionary, aliases)

    result = {
        "version_a": version_a,
        "version_b": None,
        "diff": None,
        "ingredient_changes": None,
        # 标签默认对照现版配方；若提供了新版配方，则对照新版
        "label_check_version": label_a,
    }
    keys = recipe_keys(text_a, aliases)
    check_allergens = version_a["allergens"]
    checked_tree = version_a["tree"]

    tree_b = None
    if text_b and text_b.strip():
        tree_b, version_b = _parse_version(text_b, dictionary, aliases)
        result["version_b"] = version_b
        result["diff"] = diff_allergens(
            version_a["allergens"], version_b["allergens"])
        result["ingredient_changes"] = diff_ingredients(tree_a, tree_b)
        result["label_check_version"] = label_b
        check_allergens = version_b["allergens"]
        checked_tree = version_b["tree"]
        keys |= recipe_keys(text_b, aliases)

    result["label_check"] = compare_label(
        check_allergens, declared, checked_tree)
    return result, scoped_fingerprint(keys, dictionary)
