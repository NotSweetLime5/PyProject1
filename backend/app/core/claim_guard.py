"""回答侧护栏：拦住"从查不到推出平台不支持"，以及政策语境下无依据的肯定断言。

为什么不在检索侧做：连续三次实验（抬阈值、词面覆盖门、rerank 卡分）都在
"这条资料答不答得上这个问题"上失败，因为那本身是语义判断 —— 正确答案常要靠
上位词推理（「能寄到新加坡吗」的证据写的是「海外」），错误答案反而字面擦边。
分数阈值在这两类上重叠，实测间隔 -0.0653。

所以改判"说出来的话"：
  1. 否定政策断言（不支持 / 不提供 / 不允许 …）且宾语在证据里查无此事 → 改写；
  2. 仅在 FAQ 政策语境（policy_mode）下，平台主语的肯定政策断言，若宾语里的专属词
     在证据中查无此事 → 改写。
第 1 条的关键是：证据里写了"不含港澳台及海外"，那么"不支持寄到海外"是有依据的真话，
绝不能改写。第 2 条刻意更窄：必须同时满足政策词、平台主语（或句首断言）、非商品主语、
专属宾语；"这款手机支持 NFC 支付"、"支持在线支付"这类句子不会被碰。
"""

from dataclasses import dataclass, field
import re

_NEG_CLAIM = re.compile(r"(暂?不支持|不提供|不提供|不允许|没有开通|未开通)")
# 认知管辖域：这一句是在陈述"我不知道"，而不是在替平台下结论。
# 这是本模块唯一的判据来源 —— 护栏和评测 runner 都调 assertion_about，
# 两边各写一份正则迟早会漂移成两个标准。
# 间接疑问标记：句子在转述一个待确认的问题，不是在下结论。
# 这一类比短语表管用 ——「确认所购商品**是否**提供上门安装」不含任何"查不到"字样，
# 但它同样不是断言。漏了这类标记是评测第九次误判的直接原因。
_QUESTION_FRAME = re.compile(r"(是否|能否|可否|有没有|是不是)")
_EPISTEMIC = re.compile(
    r"(没查到|没有查到|没写明|没有写明|未写明|查不到|无法确认|不能确认|不确定|不知道|"
    r"没有相关|没收录|建议联系人工|请联系人工|以人工|没有对应)"
)
_SENT_END = "。！？!?;\n"
_MAX_OBJECT = 12

# 肯定断言护栏只在 Agent 真的查过 FAQ（policy_mode）后生效，避免把商品描述当政策。
_POS_CLAIM = re.compile(r"(支持|提供|可以用|能用|开通|已有)")
_POLICY_TERMS = re.compile(
    r"(支付|付款|货到|配送|快递|发货|运费|包邮|退货|退款|换货|发票|保修|维修|安装|售后|"
    r"优惠|满减|折扣|积分|会员|分期|花呗|信用卡|地址|签收|验货|物流|时效)"
)
_PLATFORM_SUBJECT = re.compile(r"(本商城|本平台|平台|商城|本店|我们|官方|本公司)")
_PRODUCT_SUBJECT = re.compile(
    r"(这款|那款|该商品|此商品|型号|手机|电脑|耳机|笔记本|平板|电视|冰箱|洗衣机|空调|相机|手表)"
)
_NEG_BEFORE = re.compile(r"(不|没|未|无|别|非|拒|莫|无法|不能|不可|不会|并未|暂不|尚未|没有)$")
_GENERIC_PREFIXES = ("在线", "移动", "电子", "网上", "网银", "线下")
_GENERIC_SUFFIXES = ("支付", "付款", "服务", "方式")
_GENERIC_PHRASES = {p + s for p in _GENERIC_PREFIXES for s in _GENERIC_SUFFIXES} | {
    "支付方式", "售后服务", "上门服务", "免费服务", "平台服务", "商品服务",
}
_GENERIC_TOKENS = {
    "支付", "付款", "服务", "方式", "商品", "平台", "订单", "在线", "上门", "免费",
    "电子", "移动", "可以", "支持", "提供", "已有", "开通",
}
_PART_SPLIT = re.compile(r"(?:以及|和|与|及|或|、|\s)+")
_COMMA_SPLIT = re.compile(r"[，,；;]")
_MAX_POS_OBJECT = 24


def _clauses(text: str) -> list[str]:
    parts = [p for p in re.split(rf"(?<=[{_SENT_END}])", text) if p.strip()]
    return parts


def _tokens(text: str) -> set[str]:
    """取 2 字以上的片段做重叠判断。

    不用分词器：这里只要求"宾语和证据有共同字样"，宁可宽不可严 ——
    判宽了是漏改写（回到今天的现状），判严了会把真话改掉（新增故障）。
    """
    out = set()
    for ch in re.split(r"[\s，、；：,.:;!！?？()（）\"'“”]+", text):
        if len(ch) >= 2:
            out.add(ch)
        for i in range(len(ch) - 1):
            out.add(ch[i:i + 2])
    return out


def assertion_about(text: str, subject: str) -> str:
    """就某个主题词，这句话在做什么：positive / negative / hedged / absent。

    positive = "本商城支持货到付款"（无依据时是编造能力）
    negative = "本商城不支持货到付款"（无依据时是编造政策）
    hedged   = "没有写明是否支持货到付款"（陈述自己不知道 —— 这才是表外问题的正确行为）

    判序很重要：先认 hedge 再认正负。否则「不支持」里那个"不"会被当成违规，
    把正确兜底一起杀掉 —— 这正是上一版覆盖门只测一头就翻车的地方。
    """
    if not subject:
        return "absent"
    found = set()
    for clause in _clauses(text):
        if subject not in clause:
            continue
        if _EPISTEMIC.search(clause) or _QUESTION_FRAME.search(clause):
            found.add("hedged")
        elif _NEG_CLAIM.search(clause):
            found.add("negative")
        elif re.search(r"(支持|提供|可以用|能用|开通|已有)", clause):
            found.add("positive")
        else:
            found.add("mention")
    if not found:
        return "absent"
    # 一句话里既有多处，取最严重的那个：编造政策 > 编造能力 > 兜底 > 只是提及
    for kind in ("negative", "positive", "hedged", "mention"):
        if kind in found:
            return kind
    return "absent"


def unsupported_negation(sentence: str, evidence: list[str] | str) -> bool:
    """这句话是否在断言"平台不做什么"，而依据里查无此事。"""
    ev_text = " ".join(evidence) if isinstance(evidence, list) else (evidence or "")
    m = _NEG_CLAIM.search(sentence)
    if not m:
        return False
    obj = sentence[m.end(): m.end() + _MAX_OBJECT]
    obj = re.split(rf"[{_SENT_END}，、；：,]", obj)[0].strip()
    if not obj:
        # "本商城不支持"这种没宾语的，无法核对依据，按可疑处理
        return True
    if not ev_text:
        return True
    ev_tokens = _tokens(ev_text)
    # 宾语整体、或宾语里任意 2 字片段出现在证据中，都算"有依据"
    return not (obj in ev_text or (_tokens(obj) & ev_tokens))


def _comma_clauses(text: str) -> list[str]:
    return [c for c in _COMMA_SPLIT.split(text) if c.strip()]


def _specific_tokens(part: str) -> set[str]:
    return {t for t in _tokens(part) if t not in _GENERIC_TOKENS}


def _split_object_parts(obj: str) -> list[str]:
    return [p.strip() for p in _PART_SPLIT.split(obj) if p.strip()]


def _preceded_by_negation(text: str, pos: int) -> bool:
    return bool(_NEG_BEFORE.search(text[max(0, pos - 4):pos]))


def _is_platform_claim(clause: str, verb_pos: int) -> bool:
    prefix = clause[:verb_pos].strip()
    if not prefix:
        return True
    if _PRODUCT_SUBJECT.search(prefix):
        return False
    return bool(_PLATFORM_SUBJECT.search(prefix))


def _extract_positive_object(clause: str, start: int) -> str:
    obj = clause[start:start + _MAX_POS_OBJECT]
    return re.split(rf"[{_SENT_END}，,：:]", obj)[0].strip()


def _positive_object_supported(obj: str, ev_text: str) -> bool:
    """宾语里的每个专属部分都要在证据中找到依据。

    只共享"支付/服务"这类通用词不算依据，否则"支持信用卡支付"会因为证据里有
    "微信支付"而蒙混过关。反过来，证据明确否定或只是存疑时，也不能支持肯定断言。
    """
    specific = []
    for part in _split_object_parts(obj):
        if part in _GENERIC_PHRASES:
            continue
        st = _specific_tokens(part)
        if st:
            specific.append((part, st))
    if not specific:
        return True
    if not ev_text:
        return False
    ev_tokens = _tokens(ev_text)
    for part, st in specific:
        if part in ev_text:
            kind = assertion_about(ev_text, part)
            if kind in ("negative", "hedged"):
                return False
            if kind == "positive":
                continue
        if not (st & ev_tokens):
            return False
    return True


def unsupported_positive_claim(
    sentence: str, evidence: list[str] | str, *, policy_mode: bool = False
) -> bool:
    """政策语境下，平台是否做出了证据无法支持的肯定断言。"""
    if not policy_mode:
        return False
    ev_text = " ".join(evidence) if isinstance(evidence, list) else (evidence or "")
    for sent in _clauses(sentence):
        for clause in _comma_clauses(sent):
            if not _POLICY_TERMS.search(clause):
                continue
            for m in _POS_CLAIM.finditer(clause):
                if _preceded_by_negation(clause, m.start()):
                    continue
                if not _is_platform_claim(clause, m.start()):
                    continue
                obj = _extract_positive_object(clause, m.end())
                if not obj:
                    continue
                if assertion_about(clause, obj) != "positive":
                    continue
                if not _positive_object_supported(obj, ev_text):
                    return True
    return False


def hedge(sentence: str) -> str:
    """替换成不含政策断言的说法。"""
    return "这条我在平台规则里没查到，不能替商城下结论，建议联系人工客服确认。"


@dataclass
class Intervention:
    kind: str          # masked | hedged | memory_ack_removed
    before: str
    after: str


@dataclass
class GuardDecision:
    """一次回答的全部干预记录。raw 与 final 由同一个对象派生，不存在第二套判断。"""

    interventions: list[Intervention] = field(default_factory=list)

    @property
    def triggered(self) -> bool:
        return bool(self.interventions)

    @property
    def kinds(self) -> list[str]:
        return sorted({i.kind for i in self.interventions})

    def to_dict(self) -> dict:
        return {
            "triggered": self.triggered,
            "kinds": self.kinds,
            "count": len(self.interventions),
            # before 含脱敏前文本，默认不外发，只在 eval 诊断开关下带上
            "detail": [],
        }


def inspect_sentence(
    sentence: str, evidence: list[str] | str, mask, *, policy_mode: bool = False
) -> tuple[str, GuardDecision]:
    """看一句已经完整的句子，返回（应当发出去的句子, 决策）。

    判序固定：先 mask，再在无依据的政策断言上改写。两个分支都只写进 decision，
    由 apply 负责替换 —— 这样 raw/final 不可能分叉。
    """
    d = GuardDecision()
    masked = mask(sentence)
    if masked != sentence:
        d.interventions.append(Intervention("masked", sentence, masked))
    if unsupported_negation(masked, evidence) or unsupported_positive_claim(
        masked, evidence, policy_mode=policy_mode
    ):
        d.interventions.append(Intervention("hedged", masked, hedge(masked)))
        return hedge(masked), d
    return masked, d


def apply_decision(sentence: str, decision: GuardDecision) -> str:
    """把决策落成文本。没有第三种判断路径。"""
    return decision.interventions[-1].after if decision.interventions else sentence


def _memory_write_claim(sentence: str) -> bool:
    # 在已有后端回执的轮次，生成正文不再重复确认写入；普通业务事实仍由原护栏处理。
    if re.search(r"记住|记下了|记好了|记妥", sentence):
        return True
    state = re.search(r"保存|删除|清除|更新|修改|记录", sentence)
    return bool(state and (
        re.search(r"偏好|回答风格|常用用途|品牌偏好|这项设置|这条设置", sentence)
        or re.match(r"\s*(?:我)?(?:已|已经|未|没有|不会|不能)", sentence)
    ))


class SentenceGuard:
    """按句缓冲的回答护栏。

    逐 token 检查会漏 —— "不" 和 "支持" 可能分在两块里。所以扣到句号才放行整句，
    代价是每句末尾晚出现几十毫秒，换来"发出去的文本 == 落库的文本"。
    raw（模型原文）在这里累积，只用于评测与日志；判分与真实输出共用同一个 decision。
    policy_mode 由调用方在 Agent 真的调用 search_faq 后打开，肯定断言护栏才生效。
    """

    def __init__(self, evidence: list[str], mask, policy_mode: bool = False,
                 *, memory_receipt: bool = False) -> None:
        # 传进来的是那个"活"列表本身：证据是工具调用结束时才陆续追加的，
        # 构造时快照会永远拿到空列表。
        self.evidence = evidence
        self.mask = mask
        self.policy_mode = policy_mode
        self.memory_receipt = memory_receipt
        self._buf = ""
        self.sent = ""
        self.raw = ""
        self.decision = GuardDecision()
        self.hits = 0

    def push(self, text: str) -> str:
        if not text:
            return ""
        self._buf += text
        ready, rest = [], self._buf
        while True:
            idx = min((rest.find(c) for c in _SENT_END if c in rest), default=-1)
            if idx == -1:
                break
            sentence, rest = rest[: idx + 1], rest[idx + 1:]
            ready.append(sentence)
        self._buf = rest
        if not ready:
            return ""
        return "".join(self._emit(s) for s in ready)

    def flush(self) -> str:
        out = self._emit(self._buf) if self._buf else ""
        self._buf = ""
        return out

    def _emit(self, sentence: str) -> str:
        self.raw += sentence
        if self.memory_receipt and _memory_write_claim(sentence):
            out = ""
            d = GuardDecision([Intervention("memory_ack_removed", sentence, "")])
        else:
            out, d = inspect_sentence(sentence, self.evidence, self.mask, policy_mode=self.policy_mode)
        self.decision.interventions.extend(d.interventions)
        self.hits = len(self.decision.interventions)
        self.sent += out
        return out
