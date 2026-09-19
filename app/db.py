"""SQLite 持久层：过敏原字典 + 审查记录。"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

from . import aliases as alias_mod
from .analysis import missing_allergen_sources, normalize_name
from .fingerprint import result_keys, scoped_fingerprint

DB_PATH = os.environ.get("DB_PATH") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "app.db"))

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS allergen_dict (
    ingredient TEXT PRIMARY KEY,
    allergens  TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingredient_aliases (
    alias      TEXT PRIMARY KEY,
    target     TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    declared   TEXT NOT NULL DEFAULT '[]',
    text_a     TEXT NOT NULL,
    text_b     TEXT,
    label_a    TEXT NOT NULL DEFAULT '现版配方',
    label_b    TEXT NOT NULL DEFAULT '新版配方',
    result     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    dict_fingerprint TEXT
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.executescript(_SCHEMA)
        # 旧库迁移：为升级前创建的 reviews 表补齐字典指纹列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(reviews)")}
        if "dict_fingerprint" not in cols:
            conn.execute("ALTER TABLE reviews ADD COLUMN dict_fingerprint TEXT")


# ---------- 过敏原字典 ----------

def list_dict() -> List[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT ingredient, allergens, updated_at FROM allergen_dict "
            "ORDER BY ingredient").fetchall()
    return [{"ingredient": r["ingredient"],
             "allergens": json.loads(r["allergens"]),
             "updated_at": r["updated_at"]} for r in rows]


def dict_lookup() -> Dict[str, Set[str]]:
    """{归一化原料名: 过敏原集合}，供解析传播使用。"""
    return {normalize_name(e["ingredient"]): set(e["allergens"])
            for e in list_dict()}


def upsert_entry(ingredient: str, allergens: List[str]) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO allergen_dict (ingredient, allergens, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(ingredient) DO UPDATE SET "
            "allergens=excluded.allergens, updated_at=excluded.updated_at",
            (ingredient, json.dumps(allergens, ensure_ascii=False), _now()))


def delete_entry(ingredient: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM allergen_dict WHERE ingredient = ?",
                     (ingredient,))


def bulk_import(text: str) -> int:
    """按行导入，每行格式：原料名：过敏原1，过敏原2（冒号/逗号中英文均可）。"""
    count = 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[:：]", line, maxsplit=1)
        if len(parts) == 2:
            name, rest = parts[0].strip(), parts[1]
        else:
            # 没有冒号时，第一个空白前的词作为原料名
            tokens = line.split(None, 1)
            name, rest = tokens[0], (tokens[1] if len(tokens) > 1 else "")
        allergens = [a.strip() for a in re.split(r"[,，、;；\s]+", rest) if a.strip()]
        if name:
            upsert_entry(name, allergens)
            count += 1
    return count


# ---------- 原料别名（别名：标准名，有向无环图） ----------

def list_aliases() -> List[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT alias, target, updated_at FROM ingredient_aliases "
            "ORDER BY alias").fetchall()
    return [{"alias": r["alias"], "target": r["target"],
             "updated_at": r["updated_at"]} for r in rows]


def alias_map() -> Dict[str, str]:
    """{归一化别名: 归一化标准名}，供别名链解析使用。"""
    return {normalize_name(a["alias"]): normalize_name(a["target"])
            for a in list_aliases()}


def alias_display_map() -> Dict[str, str]:
    """{归一化名: 原始展示名}（别名与标准名统一命名空间，首次出现者为准）。"""
    names: Dict[str, str] = {}
    for a in list_aliases():
        names.setdefault(normalize_name(a["target"]), a["target"])
        names.setdefault(normalize_name(a["alias"]), a["alias"])
    return names


def replace_aliases(entries: List[alias_mod.AliasEntry]) -> int:
    """整批原子替换别名表：校验已通过，同事务清空重写，失败整批不生效。

    同一别名重复指向相同标准名（可能写法上有空白差异）只保留首次出现。
    """
    unique: Dict[str, tuple] = {}
    for e in entries:
        unique.setdefault(normalize_name(e.alias), (e.alias, e.target))
    now = _now()
    rows = [(alias, target, now) for alias, target in unique.values()]
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM ingredient_aliases")
        conn.executemany(
            "INSERT INTO ingredient_aliases (alias, target, updated_at) "
            "VALUES (?, ?, ?)", rows)
    return len(rows)


def upsert_alias(alias: str, target: str) -> None:
    """新增 / 更新单条别名。调用方需先用 aliases.validate_upsert 校验。"""
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO ingredient_aliases (alias, target, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(alias) DO UPDATE SET "
            "target=excluded.target, updated_at=excluded.updated_at",
            (alias, target, _now()))


def delete_alias(alias: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM ingredient_aliases WHERE alias = ?",
                     (alias,))


# ---------- 审查记录 ----------

def create_review(*, name: str, declared: List[str], text_a: str,
                  text_b: Optional[str], label_a: str, label_b: str,
                  result: dict, dict_fingerprint: Optional[str] = None) -> int:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO reviews (name, declared, text_a, text_b, label_a, "
            "label_b, result, created_at, dict_fingerprint) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name, json.dumps(declared, ensure_ascii=False), text_a, text_b,
             label_a, label_b, json.dumps(result, ensure_ascii=False), _now(),
             dict_fingerprint))
        return cur.lastrowid


def update_review_result(rid: int, result: dict,
                         dict_fingerprint: str) -> None:
    """字典时效复核确认后：覆盖审查结果并更新字典指纹。"""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE reviews SET result = ?, dict_fingerprint = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False), dict_fingerprint, rid))


def _row_to_review(row: sqlite.Row) -> dict:
    keys = row.keys()
    return {
        "id": row["id"],
        "name": row["name"],
        "declared": json.loads(row["declared"]),
        "text_a": row["text_a"],
        "text_b": row["text_b"],
        "label_a": row["label_a"],
        "label_b": row["label_b"],
        "result": json.loads(row["result"]),
        "created_at": row["created_at"],
        "dict_fingerprint": (row["dict_fingerprint"]
                             if "dict_fingerprint" in keys else None),
    }


def _recheck_status(r: dict, dictionary: Optional[Dict[str, Set[str]]] = None,
                    aliases: Optional[Dict[str, str]] = None):
    """计算记录的字典时效状态。

    返回 (needs_recheck, reason)：
    - 无指纹（升级前旧记录）→ (True, "legacy")，按需复核；
    - 以记录结果中 A/B 配料树实际涉及的原料（经当前别名链解析到最终
      标准名）对当前字典重算指纹，与保存值不一致 → (True, "stale")；
    - 一致 → (False, None)。别名规则变化后，只有当记录实际原料解析出的
      最终标准名、或其过敏原映射改变时才失效；无关别名变化不会误报。
    """
    saved = r.get("dict_fingerprint")
    if not saved:
        return True, "legacy"
    dictionary = dict_lookup() if dictionary is None else dictionary
    aliases = alias_map() if aliases is None else aliases
    current = scoped_fingerprint(
        result_keys(r.get("result") or {}, aliases), dictionary)
    if current != saved:
        return True, "stale"
    return False, None


def _backfill_missing_sources(r: dict) -> dict:
    """历史记录兼容：补齐旧版结果中缺失的遗漏来源路径（missing_sources）。

    依据记录内已持久化的配料树即时重算，无需修改库内 JSON，
    因此升级前产生的审查记录重新打开时同样可以展开来源路径。
    """
    result = r.get("result") or {}
    check = result.get("label_check")
    if not check or "missing_sources" in check:
        return r
    missing = check.get("missing", [])
    if not missing:
        check["missing_sources"] = {}
        return r
    checked_label = result.get("label_check_version", r.get("label_a"))
    version = (result.get("version_b")
               if result.get("version_b") and checked_label == r.get("label_b")
               else result.get("version_a"))
    tree = (version or {}).get("tree", [])
    check["missing_sources"] = missing_allergen_sources(tree, missing)
    return r


def get_review(rid: int) -> Optional[dict]:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM reviews WHERE id = ?",
                           (rid,)).fetchone()
    if not row:
        return None
    r = _backfill_missing_sources(_row_to_review(row))
    needs, reason = _recheck_status(r, aliases=alias_map())
    r["needs_recheck"] = needs
    r["recheck_reason"] = reason
    return r


def list_reviews() -> List[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reviews ORDER BY id DESC").fetchall()
    # 一次取出当前字典与别名供全部记录比对，避免逐条查询
    dictionary = dict_lookup()
    aliases = alias_map()
    reviews = []
    for row in rows:
        r = _row_to_review(row)
        check = r["result"].get("label_check", {})
        needs, reason = _recheck_status(r, dictionary, aliases)
        reviews.append({
            "id": r["id"], "name": r["name"], "created_at": r["created_at"],
            "has_two_versions": bool(r["text_b"]),
            "missing_count": len(check.get("missing", [])),
            "over_reported_count": len(check.get("over_reported", [])),
            "needs_recheck": needs,
            "recheck_reason": reason,
        })
    return reviews


def delete_review(rid: int) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM reviews WHERE id = ?", (rid,))
