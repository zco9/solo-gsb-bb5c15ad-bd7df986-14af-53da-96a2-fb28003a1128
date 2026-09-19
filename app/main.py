"""食品过敏原标签审查台 —— FastAPI 入口。

功能：
- 过敏原字典管理（原料 → 过敏原集合）；
- 原料别名管理（别名 → 标准名，有向无环链，仅接入正式审查与时效复核）；
- 解析带嵌套子配料的配方文本，传播并汇总过敏原；
- 对照标签声明，指出遗漏 / 多报的过敏原；
- 支持两版配方对比，输出过敏原变化；
- 生成并导出标签审查报告（HTML / Markdown）；
- 临时预览工具：供应商方案风险区间、配料占比区间顺序校验（只读，不写历史）。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from . import db
from .aliases import (AliasImportError, build_resolution_preview,
                      parse_and_validate, validate_upsert)
from .proportion import analyze_proportions
from .recheck import build_recheck
from .report import render_html_report, render_markdown_report
from .review import analyze
from .supplier_risk import SupplierPlanError, analyze_supplier_risk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# 参考 GB 7718 致敏物质推荐目录
PRESET_ALLERGENS = [
    "麸质谷物", "甲壳类", "鱼类", "蛋类", "花生", "大豆", "乳",
    "坚果", "芝麻", "亚硫酸盐", "芹菜", "芥末", "羽扇豆", "软体动物",
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="食品过敏原标签审查台", version="1.1.0", lifespan=lifespan)


# ---------- 请求模型 ----------

class DictEntry(BaseModel):
    ingredient: str
    allergens: List[str] = Field(default_factory=list)


class BulkImport(BaseModel):
    text: str


class AliasEntryIn(BaseModel):
    alias: str
    target: str


class BulkAliases(BaseModel):
    text: str


class AliasPreview(BaseModel):
    text: str


class ReviewCreate(BaseModel):
    name: str = "未命名审查"
    text_a: str
    text_b: Optional[str] = None
    label_a: str = "现版配方"
    label_b: str = "新版配方"
    declared: List[str] = Field(default_factory=list)


class RecheckRequest(BaseModel):
    # false：仅预览前后差异；true：确认后覆盖结果并更新指纹
    confirm: bool = False


class SupplierRiskPreview(BaseModel):
    text: str
    plans_text: str = ""
    declared: List[str] = Field(default_factory=list)


class ProportionPreview(BaseModel):
    text: str


# ---------- 页面 ----------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/preset-allergens")
def preset_allergens():
    return {"allergens": PRESET_ALLERGENS}


# ---------- 过敏原字典 ----------

@app.get("/api/dict")
def get_dict():
    return {"entries": db.list_dict()}


@app.post("/api/dict")
def upsert_entry(entry: DictEntry):
    name = entry.ingredient.strip()
    if not name:
        raise HTTPException(400, "原料名不能为空")
    allergens = sorted({a.strip() for a in entry.allergens if a.strip()})
    db.upsert_entry(name, allergens)
    return {"ok": True}


@app.delete("/api/dict/{ingredient}")
def delete_entry(ingredient: str):
    db.delete_entry(ingredient)
    return {"ok": True}


@app.post("/api/dict/bulk")
def bulk_import(payload: BulkImport):
    imported = db.bulk_import(payload.text)
    return {"ok": True, "imported": imported}


# ---------- 原料别名（仅接入正式审查与字典时效复核） ----------

@app.get("/api/aliases")
def get_aliases():
    return {"aliases": db.list_aliases()}


@app.post("/api/aliases/bulk")
def bulk_aliases(payload: BulkAliases):
    """整批原子维护「别名：标准名」。

    把整批映射视为有向图统一校验：自环、同一别名多目标、任意长度环
    （返回冲突行号与最短环路）任一存在即整批拒绝（HTTP 400），
    不写入任何数据；全部合法时在一个事务内替换现有别名集。
    """
    try:
        entries = parse_and_validate(payload.text)
    except AliasImportError as exc:
        raise HTTPException(400, {"message": str(exc), "errors": exc.errors})
    count = db.replace_aliases(entries)
    return {"ok": True, "imported": count}


@app.post("/api/aliases")
def upsert_alias(payload: AliasEntryIn):
    alias, target = payload.alias.strip(), payload.target.strip()
    if not alias or not target:
        raise HTTPException(400, "别名与标准名均不能为空")
    errors = validate_upsert(alias, target, db.alias_map(),
                             db.alias_display_map())
    if errors:
        raise HTTPException(400, {"message": str(errors[0]["message"]),
                                  "errors": errors})
    db.upsert_alias(alias, target)
    return {"ok": True}


@app.delete("/api/aliases/{alias}")
def remove_alias(alias: str):
    db.delete_alias(alias)
    return {"ok": True}


@app.post("/api/alias-resolve-preview")
def alias_resolve_preview(payload: AliasPreview):
    """别名链解析预览（只读）：把配方中每种原料沿别名链解析到最终标准名。

    返回每种原料的解析链、最终标准名、是否已在字典登记及其过敏原；
    只用于页面预览，不改变配方树（树与遗漏路径始终显示原始名称）。
    """
    if not payload.text.strip():
        raise HTTPException(400, "配方文本不能为空")
    return build_resolution_preview(
        payload.text, db.alias_map(), db.alias_display_map(), db.dict_lookup())


# ---------- 供应商方案风险区间（临时预览） ----------

@app.post("/api/supplier-risk-preview")
def supplier_risk_preview(payload: SupplierRiskPreview):
    """互斥过敏原方案的风险区间预览。

    仅基于当前字典做只读计算：在配料树上分别传播必然（方案交集）与
    可能（方案并集）过敏原，输出确定遗漏 / 潜在遗漏 / 确定多报 /
    潜在多报 / 一致五类结论。不枚举方案组合，也不写入字典或历史记录。
    """
    if not payload.text.strip():
        raise HTTPException(400, "配方文本不能为空")
    declared = sorted({a.strip() for a in payload.declared if a.strip()})
    try:
        return analyze_supplier_risk(
            text=payload.text, plans_text=payload.plans_text,
            declared=declared)
    except SupplierPlanError as exc:
        raise HTTPException(400, str(exc))


# ---------- 配料占比区间顺序校验（临时预览） ----------

@app.post("/api/proportion-check-preview")
def proportion_check_preview(payload: ProportionPreview):
    """配料占比区间顺序校验（临时预览）。

    复用嵌套配方解析，在每个原料名后读取 ``[最小-最大%]`` / ``[值%]``
    占比标注，对每组同级配料按标签顺序建立占比非递增约束，用区间传播
    （前向收紧上界、后向收紧下界）判断整组可行性：可行时返回每项收紧后
    的精确可行区间并标出单值锁定项，不可行时返回最短冲突片段及完整嵌套
    路径；标注问题（缺少区间 / 格式错误 / 负值 / 下界大于上界）逐条定位
    到具体路径。只读计算，不写入审查历史。
    """
    if not payload.text.strip():
        raise HTTPException(400, "配方文本不能为空")
    return analyze_proportions(payload.text)


# ---------- 审查 ----------

@app.post("/api/reviews", status_code=201)
def create_review(payload: ReviewCreate):
    if not payload.text_a.strip():
        raise HTTPException(400, "配方文本不能为空")
    declared = sorted(set(payload.declared))
    result, fingerprint = analyze(
        text_a=payload.text_a, text_b=payload.text_b,
        label_a=payload.label_a, label_b=payload.label_b,
        declared=declared)
    rid = db.create_review(
        name=payload.name.strip() or "未命名审查",
        declared=declared,
        text_a=payload.text_a, text_b=payload.text_b,
        label_a=payload.label_a, label_b=payload.label_b,
        result=result, dict_fingerprint=fingerprint,
    )
    return {"id": rid, **result}


@app.get("/api/reviews")
def list_reviews():
    return {"reviews": db.list_reviews()}


@app.get("/api/reviews/{rid}")
def review_detail(rid: int):
    review = db.get_review(rid)
    if not review:
        raise HTTPException(404, "审查记录不存在")
    return review


@app.delete("/api/reviews/{rid}")
def remove_review(rid: int):
    if not db.get_review(rid):
        raise HTTPException(404, "审查记录不存在")
    db.delete_review(rid)
    return {"ok": True}


@app.post("/api/reviews/{rid}/recheck")
def recheck_review(rid: int, payload: RecheckRequest):
    """字典时效复核。

    始终先用当前字典重新解析记录的原始 A/B 配方与标签声明并计算前后差异；
    confirm=false 时仅返回差异供页面预览，不改动记录；
    confirm=true 时确认覆盖结果、刷新字典指纹（含旧记录补录指纹）。
    """
    review = db.get_review(rid)
    if not review:
        raise HTTPException(404, "审查记录不存在")
    outcome = build_recheck(review)
    if payload.confirm:
        db.update_review_result(
            rid, outcome["new_result"], outcome["fingerprint"])
        updated = db.get_review(rid)
        outcome["review"] = updated
    outcome["id"] = rid
    return outcome


@app.get("/api/reviews/{rid}/report")
def review_report(rid: int, format: str = Query("html", pattern="^(html|md)$")):
    review = db.get_review(rid)
    if not review:
        raise HTTPException(404, "审查记录不存在")
    if format == "md":
        return Response(
            render_markdown_report(review),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="allergen_review_{rid}.md"'},
        )
    return HTMLResponse(
        render_html_report(review),
        headers={"Content-Disposition":
                 f'attachment; filename="allergen_review_{rid}.html"'},
    )
