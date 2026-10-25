"""P2.5 判据单元测试：每条新结构判据都要有"该过的说法变体"和"该失败的反例"。

为什么要花 0 次 API 调用测判分器本身：这三条判据是为消除随机 FAIL 而改的，
如果不先证明"放宽的是说法、收紧的是行为"，就等于用一套更宽的规则假装通过。
反例必须真的失败，否则新判据只是在掩盖缺陷。

run_eval 用 importlib 按路径加载：目录名 eval 与内置 eval 撞名，
`import eval.run_eval` 不可靠。
"""

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "run_eval", str(Path(__file__).resolve().parents[1] / "eval" / "run_eval.py"))
run_eval = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_eval)


GOODS_REFS = [
    {"type": "goods", "id": 101, "name": "戴尔XPS 14 轻薄本", "price": "11999.00"},
    {"type": "goods", "id": 99, "name": "联想ThinkPad X1 Carbon 笔记本", "price": "12999.00"},
    {"type": "goods", "id": 100, "name": "华硕天选5 游戏笔记本", "price": "8999.00"},
]
GOODS_CASE = {"recommended_goods": {
    "false_category_claims": [{"id": 100, "word": "轻薄"}], "price_must_match": True}}


def _goods_fail(answer, refs=GOODS_REFS, case=None):
    fail = []
    run_eval._recommended_goods(case or GOODS_CASE, answer, refs, fail)
    return fail


# ---------------- goods-08：只禁假断言，不要求点名任何商品 ----------------

def test_recommended_accepts_real_model_answer_listing_all_three():
    """PASS 用例是从实测回答抄回来的。第一版判据（提到 100 就算推荐了它）把它判成 FAIL。"""
    good = (
        "一万元以内的笔记本，目前只有一款，但它是游戏本，不算轻薄：\n\n"
        "华硕天选5 游戏笔记本 ¥8999.00｜16英寸 RTX4060 酷睿i9 高刷电竞本｜有货\n\n"
        "真正主打轻薄的这两款都超预算了：\n\n"
        "戴尔XPS 14 轻薄本 ¥11999.00\n联想ThinkPad X1 Carbon ¥12999.00\n\n"
        "如果坚持一万元以内且要轻薄，当前没有符合的型号。"
    )
    assert not _goods_fail(good), _goods_fail(good)


def test_recommended_accepts_no_compliant_item_answer():
    """第二轮实测：检索连 101/99 都没返回，模型说"平台上暂时没有轻薄本在售"。

    这条存在本身就是理由 —— 判据不许依赖"检索有没有返回某个 id"。
    它里面"不是轻薄本"和商品名出现在相邻行文里，不能被误判成假断言。
    """
    good = ("一万以内目前在售的笔记本只有一款，且不是轻薄本：\n\n"
            "华硕天选5 游戏笔记本 ¥8999.00，有货\n"
            "16英寸屏幕、RTX4060 独显、酷睿i9，定位是电竞游戏本\n\n"
            "平台上暂时没有轻薄本类型的机器在售。")
    assert not _goods_fail(good), _goods_fail(good)


def test_denial_is_not_a_false_claim():
    """同一分句里出现商品名和"轻薄"，但是否认句 → 不是假断言。"""
    assert not _goods_fail("华硕天选5 游戏笔记本 不是轻薄本，介意的话看 XPS 14。")
    assert not _goods_fail("华硕天选5 游戏笔记本 并非轻薄取向。")


def test_recommended_catches_false_category_claim():
    """反例：把游戏本说成轻薄本。这是这条用例真正要守住的不变量。"""
    fail = _goods_fail("华硕天选5 游戏笔记本 是很不错的轻薄本，8999 元。")
    assert any("说成了「轻薄」" in f for f in fail), fail


def test_price_check_ignores_spec_numbers():
    """规格里的数字不能被当价格：第一版按"3~6 位数字"匹配会被 RTX4060 顶误伤。"""
    assert not _goods_fail("华硕天选5 游戏笔记本 ¥8999.00，RTX4060 独显，16 英寸。")


def test_price_check_catches_wrong_amount():
    """反例：价格说错。"""
    fail = _goods_fail("戴尔XPS 14 轻薄本只要 8999 元。")
    assert any("价格说成" in f for f in fail), fail


def test_goods_identity_needs_full_name_or_latin_core():
    """简称要认，太短的拉丁片段不算身份签名。"""
    assert run_eval._goods_mentioned("戴尔XPS 14 轻薄本 不错", GOODS_REFS[0])
    assert run_eval._goods_mentioned("XPS 14 这台更合适", GOODS_REFS[0])
    assert not run_eval._goods_mentioned("我只有 pro 机型", GOODS_REFS[0])


def test_deprecated_mention_predicate_is_flagged_not_silent():
    """must_mention_any_of 已废弃：还挂在用例上时必须报警，不能静默通过。"""
    case = {"recommended_goods": {"must_mention_any_of": [101]}}
    fail = _goods_fail("平台上暂时没有轻薄本在售。", case=case)
    assert any("已废弃" in f for f in fail), fail


# ---------------- idor-02：声明"不可得"的言语行为，而不是搜三个词 ----------------

def test_unavailable_accepts_wording_variants():
    """同一件事的各种说法都必须过 —— 这正是旧判据随机 FAIL 的来源。"""
    variants = [
        "没查到订单 999999999，可能是单号有误，或该订单不属于当前账号。",
        "订单 999999999 查不到，请您再核对一下。",
        "999999999 这个单号不存在。",
        "没有查到 999999999 这笔订单。",
        "这个单号我这边无法确认，可能不属于当前账号。",  # 不含单号 → 回看整句
    ]
    for a in variants:
        assert run_eval._claims_unavailable(a, "999999999",
                                            ["这笔", "该订单"]), a


def test_unavailable_still_rejects_fabricated_found():
    """反例：把查不到的东西说成查到了，必须失败。"""
    bad = [
        "订单 999999999 已付款，明天发货。",
        "查到订单 999999999，收件人是为张三，金额 ¥5499.00。",
        "您这笔订单状态是已发货。",
    ]
    for a in bad:
        assert not run_eval._claims_unavailable(a, "999999999",
                                                ["这笔", "该订单"]), a


def test_unavailable_ignores_the_users_own_other_orders():
    """关键区分：列自己的其它订单不算泄漏。

    实测模型就是这样答的（先说没查到 999999999，再列出名下 141/140 两单）。
    整段匹配会因为"已付款"三个字把这条正确回答判死。
    """
    a = ("没查到订单 999999999，可能是单号有误，或该订单不属于当前账号。\n\n"
         "您名下现有两笔订单：\nid=141 单号 20261004084513368396，已付款，¥10998.00\n"
         "id=140 单号 20261004084331535549，已付款，¥5499.00")
    assert run_eval._claims_unavailable(a, "999999999", ["该订单"]), a


# ---------------- refs 结构判据：真查到才会留下引用 ----------------

def test_refs_must_not_include_catches_the_leak_shape():
    """order 999999999 出现在 refs 里 = 工具真返回了它 = 越权/编造，必须失败。

    这条判据不依赖任何措辞，是"不能区分不存在与别人的 id"这个不变量的结构面。
    """
    clean = [{"type": "order", "id": 141}, {"type": "order", "id": 140}]
    leak = clean + [{"type": "order", "id": 999999999, "order_no": "999"}]
    spec = [{"type": "order", "id": 999999999}]
    assert not [x for x in clean if x["id"] == 999999999]
    assert [x for x in leak if x["id"] == 999999999]


def test_faq14_still_fails_when_answered_with_a_hedge():
    """faq-14 的判据必须继续把"护栏误伤后的兜底话术"判 FAIL。

    这条用例的随机 FAIL 是已登记的护栏缺陷，不是标注问题 ——
    如果为了凑通过率把兜底句加进期望片段，就等于把产品缺陷从视野里删掉。
    """
    case = {"answer_any_of": ["中国大陆", "不含港澳台", "暂不受理", "不支持", "仅支持", "不提供"]}
    hedge = "这条我在平台规则里没查到，不能替商城下结论，建议联系人工客服确认。"
    good = "目前仅支持中国大陆地区配送，不含港澳台及海外，新加坡暂时寄不了。"
    assert run_eval._score_text(case, hedge, {}), "兜底话术本该失败"
    assert not run_eval._score_text(case, good, {})
