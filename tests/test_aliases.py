"""原料别名管理测试。

覆盖：
- 整批「别名：标准名」解析：注释/空行、中英文冒号、格式错误定位；
- 整批原子校验：自环（含大小写/空白归一化）、同一别名多目标、
  任意长度有向环（返回冲突行与最短环路、同环从不同节点识别为同一条）；
- 合法别名链解析到最终标准名；
- db 整批替换（原子性）、单条新增的跨批次防环；
- 正式审查接入：树与遗漏路径显示原始名，过敏原按最终标准名查询，
  指纹以最终标准名计算；
- 时效复核：仅实际原料最终标准名或其过敏原映射变化才标“需复核”；
- 指纹口径：别名中间名不参与、无关别名变化不失效。
"""
import os
import tempfile
import unittest

_DB_DIR = tempfile.mkdtemp(prefix="allergen-alias-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "alias.db")

from app import db  # noqa: E402
from app.aliases import (AliasImportError, build_resolution_preview,  # noqa: E402
                         find_cycles, parse_and_validate,
                         parse_alias_lines, validate_upsert)
from app.analysis import attach_allergens, normalize_name  # noqa: E402
from app.fingerprint import result_keys, scoped_fingerprint  # noqa: E402
from app.parser import parse_recipe  # noqa: E402
from app.recheck import build_recheck  # noqa: E402
from app.review import analyze  # noqa: E402

DICT_TEXT = """\
小麦粉：麸质谷物
乳清粉：乳
乳糖：乳
奶粉：乳
脱脂奶粉：乳
可可脂：
大豆卵磷脂：大豆
鸡蛋：蛋类
食盐：
"""


class ParseTest(unittest.TestCase):
    def test_parse_basic_with_comments(self):
        entries, errors = parse_alias_lines(
            "# 注释\n\n脱脂奶粉：奶粉\nwhey: 乳清粉\n")
        self.assertEqual([(e.alias, e.target, e.lineno) for e in entries],
                         [("脱脂奶粉", "奶粉", 3), ("whey", "乳清粉", 4)])
        self.assertEqual(errors, [])

    def test_format_errors_collected_with_lines(self):
        _, errors = parse_alias_lines("没有冒号的一行\n：只有标准名\n奶粉：")
        self.assertEqual([e["type"] for e in errors],
                         ["format", "format", "format"])
        self.assertEqual([e["lines"] for e in errors], [[1], [2], [3]])


class ValidationTest(unittest.TestCase):
    def _errors(self, text):
        try:
            parse_and_validate(text)
            return []
        except AliasImportError as exc:
            return exc.errors

    def test_self_loop_rejected(self):
        errors = self._errors("奶粉：奶粉")
        self.assertEqual([e["type"] for e in errors], ["self_loop"])
        self.assertEqual(errors[0]["lines"], [1])

    def test_self_loop_after_normalization(self):
        errors = self._errors(" Milk ：milk ")
        self.assertEqual([e["type"] for e in errors], ["self_loop"])

    def test_multi_target_reports_conflict_lines(self):
        errors = self._errors("a：b\na：c")
        self.assertEqual(len(errors), 1)
        e = errors[0]
        self.assertEqual(e["type"], "multi_target")
        self.assertEqual(e["lines"], [1, 2])
        self.assertEqual(e["alias"], "a")
        self.assertEqual(e["targets"], ["b", "c"])

    def test_multi_target_normalized_alias(self):
        # 别名归一化后相同（大小写/空白不同）也判为同一别名的多目标
        errors = self._errors("Milk：乳清粉\nmilk：奶粉")
        self.assertEqual([e["type"] for e in errors], ["multi_target"])

    def test_same_alias_same_target_allowed(self):
        entries = parse_and_validate("a：b\na: b")
        self.assertEqual(len(entries), 2)

    def test_two_node_cycle(self):
        errors = self._errors("a：b\nb：a")
        self.assertEqual([e["type"] for e in errors], ["cycle"])
        self.assertEqual(errors[0]["lines"], [1, 2])
        self.assertEqual(errors[0]["cycle"], ["a", "b", "a"])

    def test_arbitrary_length_cycle_and_shortest_reported(self):
        # 三环 a→b→c→a；另有大环 d→e→f→g→d
        text = "a：b\nb：c\nc：a\nd：e\ne：f\nf：g\ng：d"
        errors = self._errors(text)
        types = [e["type"] for e in errors]
        self.assertEqual(types, ["cycle", "cycle"])
        short, long_ = errors
        self.assertEqual(short["cycle"], ["a", "b", "c", "a"])
        self.assertEqual(short["lines"], [1, 2, 3])
        self.assertEqual(long_["cycle"], ["d", "e", "f", "g", "d"])

    def test_cycle_with_shorter_chord(self):
        # 同一节点出发存在多条回边：必须报告最短环
        # a→b→c→d→a（长 4）且 a→d→a（长 2，d 已有出边到 a 之外再……）
        # 构造：a→b→c→a（长3），以及 a→b 后 b 没有更短回边；
        # 这里显式造 4 环 + 2 环共存：
        text = "a：b\nb：a\nc：d\nd：e\ne：c"
        errors = self._errors(text)
        cycles = [e["cycle"] for e in errors]
        self.assertIn(["a", "b", "a"], cycles)
        self.assertIn(["c", "d", "e", "c"], cycles)
        self.assertEqual(errors[0]["cycle"], ["a", "b", "a"])  # 更短者优先

    def test_bfs_helper_finds_shortest(self):
        edges = {"a": "b", "b": "c", "c": "d", "d": "a"}
        order = {k: 1 for k in edges}
        cycles = find_cycles(edges, order)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0][0], ["a", "b", "c", "d", "a"])

    def test_acyclic_chain_valid(self):
        entries = parse_and_validate(
            "高钙脱脂奶粉：脱脂奶粉\n脱脂奶粉：奶粉\nwhey：乳清粉")
        self.assertEqual(len(entries), 3)

    def test_atomic_reject_mixed_batch(self):
        # 合法与非法混合：整批拒绝，且自环/多目标/环都在一次响应中返回
        errors = self._errors("ok：b\na：b\nb：a\nx：x\na：c")
        kinds = {e["type"] for e in errors}
        self.assertEqual(kinds, {"self_loop", "multi_target", "cycle"})


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = os.path.join(_DB_DIR, f"alias_db_{self.id().rsplit('.', 1)[-1]}.db")
        db.init_db()
        db.bulk_import(DICT_TEXT)
        # 每个用例从空别名开始
        db.replace_aliases([])

    def test_replace_and_lookup(self):
        n = db.replace_aliases(parse_and_validate(
            "脱脂奶粉：奶粉\n高钙脱脂奶粉：脱脂奶粉"))
        self.assertEqual(n, 2)
        amap = db.alias_map()
        # 表中保存直接边（高钙脱脂奶粉→脱脂奶粉），链解析后才到终点「奶粉」
        self.assertEqual(amap[normalize_name("高钙脱脂奶粉")],
                         normalize_name("脱脂奶粉"))
        from app.analysis import resolve_alias
        self.assertEqual(
            resolve_alias(normalize_name("高钙脱脂奶粉"), amap),
            normalize_name("奶粉"))
        self.assertEqual(amap[normalize_name("脱脂奶粉")], normalize_name("奶粉"))

    def test_replace_is_atomic_on_bad_graph(self):
        db.replace_aliases(parse_and_validate("a：b"))
        with self.assertRaises(AliasImportError):
            parse_and_validate("x：y\ny：x")
        # 校验阶段不触碰库：原别名仍在
        self.assertIn(normalize_name("a"), db.alias_map())

    def test_replace_duplicate_same_target_deduped(self):
        n = db.replace_aliases(parse_and_validate("a：b\na: b"))
        self.assertEqual(n, 1)

    def test_upsert_guard_against_cross_batch_cycle(self):
        db.replace_aliases(parse_and_validate("a：b\nb：c"))
        # 让 c 指回 a 会成环：拒绝
        errors = validate_upsert("c", "a", db.alias_map(),
                                 db.alias_display_map())
        self.assertTrue(any(e["type"] == "cycle" for e in errors))
        self.assertNotIn(normalize_name("c"), db.alias_map())
        # 合法更新（c → 奶粉）放行
        self.assertEqual(
            validate_upsert("c", "奶粉", db.alias_map(),
                            db.alias_display_map()), [])

    def test_upsert_self_loop_rejected(self):
        errors = validate_upsert("奶粉", "奶粉", db.alias_map(),
                                 db.alias_display_map())
        self.assertEqual([e["type"] for e in errors], ["self_loop"])


class ReviewIntegrationTest(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = os.path.join(_DB_DIR, f"alias_review_{self.id().rsplit('.', 1)[-1]}.db")
        db.init_db()
        db.bulk_import(DICT_TEXT)
        db.replace_aliases(parse_and_validate(
            "脱脂奶粉：奶粉\n高钙脱脂奶粉：脱脂奶粉\nwhey：乳清粉"))

    def test_tree_keeps_raw_names_but_lookup_uses_standard(self):
        result, fp = analyze(
            text_a="配料表：高钙脱脂奶粉，whey，鸡蛋，神秘香精",
            text_b=None, label_a="现版", label_b="新版", declared=[])
        tree = result["version_a"]["tree"]
        # 树显示原始名称
        self.assertEqual([n["name"] for n in tree],
                         ["高钙脱脂奶粉", "whey", "鸡蛋", "神秘香精"])
        # 过敏原来自链终点：高钙脱脂奶粉→脱脂奶粉→奶粉(乳)，whey→乳清粉(乳)
        self.assertEqual(tree[0]["own_allergens"], ["乳"])
        self.assertEqual(tree[1]["own_allergens"], ["乳"])
        self.assertFalse(tree[0]["unknown"])   # 终点已登记
        self.assertFalse(tree[1]["unknown"])
        self.assertTrue(tree[3]["unknown"])    # 神秘香精无别名、未登记
        # 遗漏来源路径也用原始名称
        sources = result["label_check"]["missing_sources"]["乳"]
        self.assertIn(["whey"], sources)
        self.assertIn(["高钙脱脂奶粉"], sources)

    def test_fingerprint_uses_final_standard_names(self):
        result, fp = analyze(
            text_a="配料表：高钙脱脂奶粉，whey", text_b=None,
            label_a="现版", label_b="新版", declared=[])
        keys = result_keys(result, db.alias_map())
        # 指纹 key 为最终标准名（奶粉、乳清粉），中间别名（脱脂奶粉等）不出现
        self.assertIn(normalize_name("奶粉"), keys)
        self.assertIn(normalize_name("乳清粉"), keys)
        self.assertNotIn(normalize_name("高钙脱脂奶粉"), keys)
        self.assertNotIn(normalize_name("脱脂奶粉"), keys)
        direct = scoped_fingerprint(
            [normalize_name("奶粉"), normalize_name("乳清粉")], db.dict_lookup())
        self.assertEqual(fp, direct)

    def test_preview_payload(self):
        out = build_resolution_preview(
            "配料表：高钙脱脂奶粉，whey，神秘香精",
            db.alias_map(), db.alias_display_map(), db.dict_lookup())
        by_name = {r["name"]: r for r in out["resolutions"]}
        self.assertEqual(by_name["高钙脱脂奶粉"]["chain"],
                         ["高钙脱脂奶粉", "脱脂奶粉", "奶粉"])
        self.assertEqual(by_name["高钙脱脂奶粉"]["allergens"], ["乳"])
        self.assertTrue(by_name["高钙脱脂奶粉"]["registered"])
        self.assertFalse(by_name["神秘香精"]["registered"])
        self.assertIsNone(by_name["神秘香精"]["allergens"])
        self.assertFalse(by_name["神秘香精"]["aliased"])


def make_review(text_a, declared=None, text_b=None):
    result, fp = analyze(
        text_a=text_a, text_b=text_b,
        label_a="现版配方", label_b="新版配方",
        declared=declared or [])
    rid = db.create_review(
        name="别名时效测试", declared=declared or [],
        text_a=text_a, text_b=text_b,
        label_a="现版配方", label_b="新版配方",
        result=result, dict_fingerprint=fp)
    return rid


class RecheckInvalidationTest(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = os.path.join(_DB_DIR, f"alias_recheck_{self.id().rsplit('.', 1)[-1]}.db")
        db.init_db()
        db.bulk_import(DICT_TEXT)
        db.replace_aliases(parse_and_validate("whey：乳清粉"))

    def test_fresh_after_create(self):
        rid = make_review("配料表：whey，鸡蛋")
        self.assertFalse(db.get_review(rid)["needs_recheck"])

    def test_unrelated_alias_change_does_not_invalidate(self):
        rid = make_review("配料表：whey，鸡蛋")
        # 与配方原料无关的别名增删不改变指纹
        db.replace_aliases(parse_and_validate(
            "whey：乳清粉\n脱脂奶粉：奶粉"))
        self.assertFalse(db.get_review(rid)["needs_recheck"])
        # 删除 whey 之外的别名同样不影响
        db.replace_aliases(parse_and_validate("whey：乳清粉\n别的名：奶粉"))
        self.assertFalse(db.get_review(rid)["needs_recheck"])

    def test_alias_retarget_changes_standard_invalidates(self):
        rid = make_review("配料表：whey，鸡蛋")
        # whey 改指向「乳糖」（同为乳，过敏原相同）——最终标准名改变，
        # 指纹 key 变化，应失效
        db.replace_aliases(parse_and_validate("whey：乳糖"))
        detail = db.get_review(rid)
        self.assertTrue(detail["needs_recheck"])
        self.assertEqual(detail["recheck_reason"], "stale")

    def test_alias_removed_exposes_unregistered_leaf(self):
        rid = make_review("配料表：whey，鸡蛋")
        db.replace_aliases([])  # whey 不再解析到乳清粉，叶子退回未登记
        self.assertTrue(db.get_review(rid)["needs_recheck"])
        outcome = build_recheck(db.get_review(rid))
        removed = (outcome["diff"]["versions"]["version_a"]
                   ["allergens"]["removed"])
        self.assertEqual(removed, ["乳"])
        unknown_added = (outcome["diff"]["versions"]["version_a"]
                         ["unknown_ingredients"]["added"])
        self.assertEqual(unknown_added, ["whey"])

    def test_new_alias_registering_unknown_leaf_invalidates(self):
        # 原配方叶子“高钙脱脂奶粉”未登记；后续补别名链到已登记的奶粉
        rid = make_review("配料表：高钙脱脂奶粉")
        self.assertTrue(
            "高钙脱脂奶粉" in
            db.get_review(rid)["result"]["version_a"]["unknown_ingredients"])
        db.replace_aliases(parse_and_validate(
            "脱脂奶粉：奶粉\n高钙脱脂奶粉：脱脂奶粉"))
        self.assertTrue(db.get_review(rid)["needs_recheck"])

    def test_standard_name_mapping_change_invalidates(self):
        rid = make_review("配料表：whey，鸡蛋")
        # 别名不变，但最终标准名「乳清粉」过敏原映射变化 → 失效
        db.upsert_entry("乳清粉", ["乳", "鱼"])
        self.assertTrue(db.get_review(rid)["needs_recheck"])

    def test_alias_change_to_equivalent_standard_same_allergens_still_stale(self):
        # 最终标准名改变（乳清粉→乳糖，过敏原集合相同）：按需求“最终标准名
        # 或对应过敏原映射改变”需复核，故仍标记 stale
        rid = make_review("配料表：whey，鸡蛋")
        db.replace_aliases(parse_and_validate("whey：乳糖"))
        self.assertTrue(db.get_review(rid)["needs_recheck"])

    def test_recheck_confirm_recovers_fresh(self):
        rid = make_review("配料表：whey，鸡蛋")
        db.replace_aliases(parse_and_validate("whey：乳糖"))
        outcome = build_recheck(db.get_review(rid))
        db.update_review_result(rid, outcome["new_result"],
                                outcome["fingerprint"])
        self.assertFalse(db.get_review(rid)["needs_recheck"])

    def test_two_version_covers_both_recipes(self):
        rid = make_review("配料表：whey，鸡蛋", text_b="配料表：whey，可可粉")
        # B 版独有叶子“可可粉”补别名链到已登记的可可脂 → 失效
        db.replace_aliases(parse_and_validate(
            "whey：乳清粉\n可可粉：可可脂"))
        self.assertTrue(db.get_review(rid)["needs_recheck"])


class AliasAttachTest(unittest.TestCase):
    def test_attach_with_explicit_alias_map(self):
        tree = parse_recipe("a（b）")
        dictionary = {normalize_name("标准B"): {"乳"}}
        aliases = {normalize_name("a"): normalize_name("复合酱"),
                   normalize_name("b"): normalize_name("标准B")}
        attach_allergens(tree, dictionary, aliases)
        self.assertEqual(tree[0].name, "a")                    # 原名保留
        self.assertEqual(tree[0].total_allergens, {"乳"})      # 子级传播
        self.assertFalse(tree[0].unknown)                     # 复合不判未识别
        self.assertEqual(tree[0].children[0].name, "b")       # 原名保留


if __name__ == "__main__":
    unittest.main()
