from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "dev"
    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embedding_dim: int = 1024

    # 未付款订单多久自动关闭。测试时可以在 .env 里写 ORDER_PAY_TIMEOUT_MINUTES=1
    order_pay_timeout_minutes: int = 30
    order_cleanup_interval_seconds: int = 60
    # 跑 e2e 或单测时关掉，否则后台任务会和断言抢同一批订单
    scheduler_enabled: bool = True

    # 按 IP 的固定窗口限流。bcrypt 单次 200ms，不限流就等于把 CPU 交给攻击者支配
    login_rate_limit: int = 10
    register_rate_limit: int = 5
    rate_limit_window_seconds: int = 60

    # FAQ 语义召回下限。实测值，不是估的：15 条相关问题 top1 落在 0.627~0.885，
    # 8 条无关问题落在 0.295~0.545，可分区间 (0.5451, 0.6271)，取中点。
    # 关键一条：'不存在的商品xyz' 相似度 0.5451，阈值低于 0.55 它就会污染 prompt。
    faq_similarity_threshold: float = 0.59
    # 低于这个值的命中仍然进 prompt（阈值证明过不可分：实测真命中最低 0.596，
    # 而表外误召是 0.606/0.612，抬地板只会先杀掉真命中），但要显式标成"相关度较低"，
    # 免得模型把一条勉强擦边的 FAQ 当成权威依据。
    faq_low_confidence: float = 0.65

    # 历史窗口。P2 之前用"最近 8 轮 = 16 条"定义，实测证明那是错的定义方式：
    # 标定集里最长一条会话 64 条 / 2557 字，被 16 条砍到只剩 640 字，而折算成 token
    # 只丢了 121~1115 tok —— 1M 的 context window 连 0.1% 都没用上就丢了信息。
    #
    # 6000 来自 `eval/context_calibration.jsonl` 的实测（边际 0.572~0.606 token/字、
    # 固定前缀 1286 tok、首 token 延迟到 20k 字都基本不动），
    # **不是**从模型 context window 反推出来的 —— 天花板有几万字级别，它约束不了任何东西，
    # 真正约束预算的是成本、延迟和长上下文里的注意力衰减。
    #
    # 软预算，判据落在轮边界上：开始下一轮之前没超预算就继续收整轮，允许最后一轮顶破
    # 再停；最近一轮更是无条件整轮保留。反过来要求"整轮装得下才收"会被轮次原子性反噬
    # —— 两轮各 3000 字、预算 6000 时只收 3060 字，实测把一条 3/3 稳定的用例打成 0/3。
    # 单条消息大到要限请求体积，那是独立的入口边界，不在这里偷偷解决。
    chat_history_char_budget: int = 6000
    # 防御性保险丝，不是窗口定义。实测每条消息的结构开销 ≈4.21 token，128 条也就 538 tok，
    # 所以在正常流量下它很少是第一个触发的 —— 但**不能说它永远轮不到**：
    # 全是超短消息的会话里它会先于字符预算生效，这是允许的行为，不是缺陷。
    # 先按 128 上，之后用 chat.obs 日志里 stop_reason 的真实命中率决定要不要调 256。
    chat_history_max_messages: int = 128
    # keyset 分块读取的块大小。只是"每次捞多少行"，不定义窗口，也不定义预算。
    chat_history_fetch_block: int = 64
    # 异常保险：单个会话最多扫这么多条就停，防脏数据/超长会话把读路径拖死。
    # 这不是窗口定义，正常情况下不该被触发；触发了说明数据有问题。
    chat_history_max_scanned: int = 4000
    chat_rate_limit: int = 20
    # 归属校验。P1 走完 shadow 观察 + 老数据回填后默认强制：
    # 归属没过一律 410，不再放行（放行分支的历史行为由测试用显式 record_only 钉住）。
    # 留这个开关是为了灰度和排障 —— 置 false 会重新打开"猜到 sid 就能读别人的上下文、
    # 还能往别人的会话里写消息"这个洞，不是用来长期跑的。
    # 翻之前必须先跑 c3f2sessfill01 回填老会话归属，否则老会话一律 410。
    chat_ownership_enforced: bool = True
    # 评测用：把护栏的 raw/before 一起回传。生产关闭 —— before 含脱敏前文本
    chat_guard_echo_raw: bool = False

    # --- P3A 会话滚动摘要 ---
    # 摘要只处理"已经被读窗口挤出去"的那段，所以这里刻意**没有**独立的触发字数：
    # 保留区由读窗口决定，旧前缀才可摘要。判据和读侧同源，
    # 改预算时两边一起动，不会出现"窗口收紧了但摘要还在按旧阈值跑"的漂移。
    chat_summary_enabled: bool = True
    chat_summary_interval_seconds: int = 300
    # 灰度开关（这一条）和"每个会话许不许摘要"（chat_sessions.summary_eligible）是两层，别混：
    # 前者是全局 kill switch / 独立回滚路径，后者是数据层的资格，只由真实消息写入授予。
    # 回填 1300 多个历史会话的尾部指针不影响摘要资格，所以两者都不需要为它临时关掉。
    # 摘要正文的硬上限（模型写超就截断）。摘要**不计入** chat_history_char_budget，
    # 它是叠加在最旧那一端的额外上下文 —— 反过来"先给摘要预留、剩下的才给原文"会造出
    # 一段两边都不管的空白：保留区按扣过的预算算、读侧却按整个预算取。
    # 叠加的代价有界：最多多出 target 字（800 字按旧标定约 480 token）。
    # 截断不是卫生问题，是这个上界能成立的前提。
    chat_summary_target_chars: int = 800
    # 回滞：至少被挤出这么多字才起一次 LLM 调用。没有它的话，摘要之后尾部正好贴着
    # 预算上沿，用户再多说一句就会把上一轮挤出去、立刻又要摘要一次 —— 等于每次回答多付一次调用。
    chat_summary_min_fold_chars: int = 1500
    # 单次折叠软上限，最后一轮允许超限，不能拆开问答；余下部分下次继续。
    chat_summary_fold_max_chars: int = 12000
    # 一轮 job 最多处理几个会话。
    chat_summary_batch: int = 10
    # 请求发现历史缺口时立即补摘要；异常巨型历史分批推进，超过本次上限提示重试。
    chat_summary_request_max_passes: int = 8

    # 前端跨域白名单。用 * 配 allow_credentials 是无效组合（浏览器会拒），
    # 所以必须是明确的 origin 列表。
    # 5174 也在里面：5173 是 Vite 默认端口，但本机另一个项目已经占了它，
    # 换端口时不必再回来改配置
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ]


@lru_cache    # 第一次调用执行函数体并缓存返回值；后续所有调用直接返回缓存对象
def get_settings() -> Settings:
    return Settings()
