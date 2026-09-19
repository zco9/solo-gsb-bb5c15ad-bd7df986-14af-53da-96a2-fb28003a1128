"""端到端集成冒烟测试：不依赖 FastAPI，直接走 db + parser + analysis + report 全链路。"""
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "smoke.db")

from app import db
from app.analysis import (attach_allergens, collect_allergens, compare_label,
                          diff_allergens, diff_ingredients, flatten)
from app.fingerprint import result_keys, scoped_fingerprint
from app.parser import parse_recipe
from app.report import render_html_report, render_markdown_report

db.init_db()

# 1. 导入字典
n = db.bulk_import("""\
小麦粉：麸质谷物
乳清粉：乳
乳糖：乳
奶粉：乳
可可脂：
大豆卵磷脂：大豆
鸡蛋：蛋类
椰子粉：坚果
芝麻：芝麻
食盐：
""")
assert n == 10, n
print(f"1. 字典导入 {n} 条 OK")

# 2. 两版配方
TEXT_A = "配料表：小麦粉，白砂糖，巧克力酱（可可脂，奶粉（乳清粉，乳糖），大豆卵磷脂），鸡蛋，食盐"
TEXT_B = "配料表：小麦粉，白砂糖，巧克力酱（可可脂，椰子粉），鸡蛋，芝麻酱（芝麻），食盐"
DECLARED = ["麸质谷物", "蛋类"]

dictionary = db.dict_lookup()

def build_version(tree):
    return {
        "tree": [x.to_dict() for x in tree],
        "allergens": sorted(collect_allergens(tree)),
        "unknown_ingredients": sorted({x.name for x in flatten(tree) if x.unknown}),
    }

tree_a = parse_recipe(TEXT_A)
attach_allergens(tree_a, dictionary)
va = build_version(tree_a)
tree_b = parse_recipe(TEXT_B)
attach_allergens(tree_b, dictionary)
vb = build_version(tree_b)

assert set(va["allergens"]) == {"麸质谷物", "乳", "大豆", "蛋类"}, va["allergens"]
assert set(vb["allergens"]) == {"麸质谷物", "坚果", "蛋类", "芝麻"}, vb["allergens"]
assert "白砂糖" in va["unknown_ingredients"]  # 未登记原料被标记
print("2. 两版配方解析与过敏原传播 OK")
print("   A 版过敏原:", va["allergens"])
print("   B 版过敏原:", vb["allergens"])

# 3. 标签核对（对照 B 版，含遗漏来源路径）
check = compare_label(vb["allergens"], DECLARED, vb["tree"])
assert check["missing"] == ["坚果", "芝麻"], check
assert check["over_reported"] == [], check
assert check["matched"] == ["蛋类", "麸质谷物"], check
assert check["missing_sources"]["坚果"] == [["巧克力酱", "椰子粉"]], check
assert check["missing_sources"]["芝麻"] == [["芝麻酱", "芝麻"]], check
print("3. 标签核对 OK  遗漏:", check["missing"], " 多报:", check["over_reported"])
print("   来源路径:", check["missing_sources"])

# 3b. 同名原料处于不同分支时来源路径分别保留，不合并
tree_c = parse_recipe("巧克力酱（奶粉（乳清粉）），夹心酱（奶粉（乳清粉））")
attach_allergens(tree_c, dictionary)
vc = build_version(tree_c)
check_c = compare_label(vc["allergens"], [], vc["tree"])
assert check_c["missing_sources"]["乳"] == [
    ["夹心酱", "奶粉"],
    ["夹心酱", "奶粉", "乳清粉"],
    ["巧克力酱", "奶粉"],
    ["巧克力酱", "奶粉", "乳清粉"],
], check_c["missing_sources"]
# 父节点自身标注与子配料传播各产生一条路径；同名分支分别保留、不合并
print("3b. 同名分支路径不合并 / 自身标注+子配料传播 OK")

# 4. 版本对比
diff = diff_allergens(va["allergens"], vb["allergens"])
assert diff["added"] == ["坚果", "芝麻"]
assert diff["removed"] == ["乳", "大豆"]
changes = diff_ingredients(tree_a, tree_b)
names = {c["ingredient"]: c["change"] for c in changes}
assert names.get("巧克力酱") == "modified" and names.get("芝麻酱") == "added", names
print("4. 版本对比 OK  新增:", diff["added"], " 减少:", diff["removed"])

# 5. 入库 + 报告导出
result = {
    "version_a": va, "version_b": vb, "diff": diff,
    "ingredient_changes": changes,
    "label_check": check, "label_check_version": "新版配方",
}
rid = db.create_review(name="巧克力饼干 配方变更审查", declared=DECLARED,
                       text_a=TEXT_A, text_b=TEXT_B,
                       label_a="现版配方", label_b="新版配方", result=result,
                       dict_fingerprint=scoped_fingerprint(
                           result_keys(result), dictionary))
review = db.get_review(rid)
html_report = render_html_report(review)
md_report = render_markdown_report(review)
assert "标签遗漏" in html_report and "坚果" in html_report and "芝麻" in html_report
assert "巧克力酱" in html_report and "来源路径" in html_report
assert "巧克力酱 › 椰子粉" in md_report and "芝麻酱 › 芝麻" in md_report
assert "# 食品过敏原标签审查报告" in md_report and "新增过敏原" in md_report
assert len(db.list_reviews()) == 1

# 5b. 模拟升级前的旧记录（结果中无 missing_sources），重新打开时自动补齐
old_result = dict(result)
old_result["label_check"] = {k: v for k, v in check.items()
                             if k != "missing_sources"}
old_rid = db.create_review(name="旧版记录", declared=DECLARED,
                           text_a=TEXT_A, text_b=TEXT_B,
                           label_a="现版配方", label_b="新版配方",
                           result=old_result)
old_review = db.get_review(old_rid)
assert old_review["result"]["label_check"]["missing_sources"]["坚果"] == \
    [["巧克力酱", "椰子粉"]]
print(f"5. 审查记录入库 (#{rid}) 与 HTML/Markdown 报告导出 OK；旧记录回填 OK")

# 6. 字典时效复核
from app.recheck import build_recheck

# 6a. 刚创建的记录指纹与当前字典一致，时效正常
assert db.get_review(rid)["needs_recheck"] is False

# 6b. 无关原料增删 / 顺序变化 / 同值重导不得误报
db.upsert_entry("无关原料", ["芥末"])
assert db.get_review(rid)["needs_recheck"] is False
db.delete_entry("无关原料")
db.bulk_import("\n".join(reversed("""\
小麦粉：麸质谷物
乳清粉：乳
乳糖：乳
奶粉：乳
可可脂：
大豆卵磷脂：大豆
鸡蛋：蛋类
椰子粉：坚果
芝麻：芝麻
食盐：
""".strip().splitlines())))
assert db.get_review(rid)["needs_recheck"] is False, "顺序/无关变化不得误报"

# 6c. 配方实际涉及原料的映射变化 -> 需复核（两版取并集，椰子粉仅在 B 版）
db.upsert_entry("椰子粉", ["坚果", "大豆"])
stale = db.get_review(rid)
assert stale["needs_recheck"] and stale["recheck_reason"] == "stale"
outcome = build_recheck(stale)
assert outcome["changed"]
# B 版（核对对象）新增遗漏“大豆”，来源路径为 巧克力酱 › 椰子粉
assert "大豆" in outcome["diff"]["label"]["missing_delta"]["added"]
assert ["巧克力酱", "椰子粉"] in \
    outcome["new_result"]["label_check"]["missing_sources"]["大豆"]

# 6d. 确认复核 -> 覆盖结果、刷新指纹、恢复时效正常
db.update_review_result(rid, outcome["new_result"], outcome["fingerprint"])
assert db.get_review(rid)["needs_recheck"] is False
# 复核后报告可继续导出，且包含新结论
assert "大豆" in render_markdown_report(db.get_review(rid))

# 6e. 升级前旧记录无指纹 -> 按需复核；复核确认后补上指纹
assert db.get_review(old_rid)["needs_recheck"] is True
assert db.get_review(old_rid)["recheck_reason"] == "legacy"
legacy_outcome = build_recheck(db.get_review(old_rid))
db.update_review_result(old_rid, legacy_outcome["new_result"],
                        legacy_outcome["fingerprint"])
assert db.get_review(old_rid)["needs_recheck"] is False
print("6. 字典时效复核（指纹/误报防护/前后差异/确认覆盖/旧记录） OK")

# 7. 配料占比区间顺序校验（临时预览，不写历史）
from app.proportion import analyze_proportions

# 7a. 可行：前向/后向区间传播收紧，鸡蛋与食盐互相锁定为 20%
prop = analyze_proportions(
    "配料表：小麦粉[40-55%]，巧克力酱[25-60%]（可可脂[10-18%]，奶粉[12-15%]），"
    "鸡蛋[5-20%]，食盐[20-30%]")
assert prop["status"] == "feasible" and prop["group_count"] == 2, prop
top = next(g for g in prop["groups"] if g["path"] == [])["items"]
assert top[1]["tightened"] == [25.0, 55.0]          # 上界被小麦粉收紧
assert top[2]["tightened"] == [20.0, 20.0] and top[2]["pinned"]
assert top[3]["tightened"] == [20.0, 20.0] and top[3]["pinned"]
child = next(g for g in prop["groups"] if g["path"] == ["巧克力酱"])["items"]
assert child[0]["tightened"] == [12.0, 18.0]        # 下界被奶粉收紧

# 7b. 不可行：返回最短冲突片段及完整嵌套路径
prop = analyze_proportions(
    "小麦粉[40-55%]，巧克力酱[25-35%]（可可脂[5-8%]，奶粉[12-15%]）")
assert prop["status"] == "infeasible", prop
[prop_conflict] = prop["conflicts"]
assert prop_conflict["path"] == ["巧克力酱"]
assert [f["name"] for f in prop_conflict["fragment"]] == ["可可脂", "奶粉"]

# 7c. 标注问题定位到具体路径，且全程不产生审查记录
prop = analyze_proportions("巧克力酱[25-60%]（可可脂，奶粉[12-15%]），鸡蛋")
assert prop["status"] == "error"
assert {tuple(e["path"]) for e in prop["errors"]} == {
    ("巧克力酱", "可可脂"), ("鸡蛋",)}
assert len(db.list_reviews()) == 2  # 仍只有第 5 步创建的两条
print("7. 配料占比区间顺序校验（收紧/锁定/最短冲突片段/路径定位/只读） OK")

# 8. 原料别名（别名：标准名，有向无环链；仅正式审查 / 时效复核）
from app.aliases import AliasImportError, parse_and_validate, validate_upsert
from app.review import analyze as review_analyze

# 8a. 整批原子校验：自环 / 多目标 / 环全部拒绝，并带冲突行与最短环路
for bad in ("whey：whey", "a：b\na：c", "a：b\nb：a"):
    try:
        parse_and_validate(bad)
        raise AssertionError("应拒绝非法别名批：" + bad)
    except AliasImportError:
        pass
try:
    parse_and_validate("a：b\nb：c\nc：a")
except AliasImportError as e:
    assert e.errors[0]["cycle"] == ["a", "b", "c", "a"], e.errors
    assert e.errors[0]["lines"] == [1, 2, 3]
print("8a. 别名整批原子校验（自环/多目标/任意环+最短环路+冲突行） OK")

# 8b. 合法链入库：whey→乳清粉；高钙脱脂奶粉→脱脂奶粉→奶粉
n = db.replace_aliases(parse_and_validate(
    "whey：乳清粉\n脱脂奶粉：奶粉\n高钙脱脂奶粉：脱脂奶粉"))
assert n == 3
# 单条写入跨批次防环：让乳清粉回指 whey 会成环，拒绝
assert validate_upsert("乳清粉", "whey", db.alias_map(), db.alias_display_map())

# 8c. 正式审查按最终标准名查字典，但树 / 路径保留原始名
ares, afp = review_analyze(
    text_a="配料表：高钙脱脂奶粉，whey，神秘香精", text_b=None,
    label_a="现版配方", label_b="新版配方", declared=[])
names = [x["name"] for x in ares["version_a"]["tree"]]
assert names == ["高钙脱脂奶粉", "whey", "神秘香精"], names
assert set(ares["version_a"]["allergens"]) == {"乳"}
assert ares["label_check"]["missing_sources"]["乳"] == \
    [["whey"], ["高钙脱脂奶粉"]]
assert ares["version_a"]["unknown_ingredients"] == ["神秘香精"]
print("8c. 别名链解析到最终标准名查字典 / 树与路径保留原始名 OK")

# 8d. 别名变化的时效判定：仅实际原料最终标准名或其过敏原映射改变才失效
sres, sfp = review_analyze(
    text_a="配料表：高钙脱脂奶粉，whey", text_b=None,
    label_a="现版配方", label_b="新版配方", declared=[])
arid = db.create_review(
    name="别名审查", declared=[],
    text_a="配料表：高钙脱脂奶粉，whey", text_b=None,
    label_a="现版配方", label_b="新版配方",
    result=sres, dict_fingerprint=sfp)
assert db.get_review(arid)["needs_recheck"] is False
# 无关别名变化不失效
db.replace_aliases(parse_and_validate(
    "whey：乳清粉\n脱脂奶粉：奶粉\n高钙脱脂奶粉：脱脂奶粉\n无关名：食盐"))
assert db.get_review(arid)["needs_recheck"] is False
# 删除别名链：高钙脱脂奶粉 / whey 退回未登记叶子 → 失效
db.replace_aliases(parse_and_validate(""))
assert db.get_review(arid)["needs_recheck"] is True
print("8d. 别名规则变化的时效复核判定（无关不误报 / 目标改变才失效） OK")

print("\n全部集成冒烟测试通过 ✔")
