"""偏好字段的确定性准入规则。写入与读取使用同一规则，覆盖旧数据。

这是保守的字段校验，不将词面检查视为通用的提示词注入检测器。
"""
import re
import unicodedata


class UnsafePreference(ValueError):
    def __init__(self, message: str, *, instruction: bool = False):
        super().__init__(message)
        self.instruction = instruction


_CONTROL = re.compile(
    r"忽略|无视|绕过|跳过|覆盖.{0,8}(指令|规则)|系统(提示|指令)|开发者|管理员|"
    r"最高优先|权限|所有(用户|人|订单)|别的用户|其他用户|直接(确认|退款|批准)|"
    r"一定能退|保证退款|无条件退款|不(用|要|必).{0,8}(工具|查询|验证|核实)|"
    r"(遇到|以后|无论|始终|永远).{0,20}(确认|退款|批准|承诺|执行)|"
    r"(https?://|<[/!]?[a-z]|```)|"
    r"\b(ignore|override|bypass|system|developer|admin|instruction|prompt|execute)\b|"
    r"\b(always|never).{0,30}\b(refund|approve|verify|tool|check)\b",
    re.I,
)
_BRAND_VALUE = re.compile(r"[\w\s./&+()·、-]{1,64}", re.UNICODE)
_STYLE_ATOM = (
    r"简洁|简短|详细|详细一点|简洁一点|简短一点|通俗|专业|耐心|直接|客观|"
    r"中文|英文|中英双语|分点|分点说明|分步骤|分步骤说明|先结论后解释|"
    r"少用术语|多举例|举例说明|条理清晰|口语化|正式|少用表情|不用表情"
)
_STYLE_VALUE = re.compile(rf"(?:{_STYLE_ATOM})(?:[、，, /+；;和并且]*(?:{_STYLE_ATOM}))*")


def validate_preference(key: str, value: str) -> str:
    if key not in {"brand_preference", "usual_use", "answer_style"}:
        raise UnsafePreference("仅支持品牌偏好、常用用途和回答风格")
    # Cf 包括零宽字符、双向控制字符；先检查，避免规范化后悄悄吞掉控制信息。
    if any(unicodedata.category(c).startswith("C") for c in value):
        raise UnsafePreference("偏好须为单行文字，不含隐藏控制字符")
    cleaned = unicodedata.normalize("NFKC", value).strip()
    if not 1 <= len(cleaned) <= 120:
        raise UnsafePreference("偏好长度应为 1 到 120 字")
    if _CONTROL.search(cleaned):
        raise UnsafePreference("偏好仅用于个性化描述，请移除操作指令、权限声明或业务承诺", instruction=True)
    if key == "answer_style" and not _STYLE_VALUE.fullmatch(cleaned):
        raise UnsafePreference("回答风格请使用简洁、详细、通俗、分点说明等表达方式")
    if key == "brand_preference" and not _BRAND_VALUE.fullmatch(cleaned):
        raise UnsafePreference("品牌偏好请填写品牌名称或品牌列表，最多 64 字")
    return cleaned
