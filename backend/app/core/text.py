"""
中文分词。PG 的 'simple' 全文配置不认识中文，所以分词在 Python 侧做，
把结果存成空格连接的字符串写进 goods.search_text，让生成列去算 tsvector。


PG 默认解析器不提供这里需要的中文词语切分；Python 的 jieba 提前识别词语边界。
PG 仍会解析分词后的 search_text，自动运行 to_tsvector('simple', search_text)，生成 search_vector。
在search_vector 建 GIN 索引，用于快速查询。
用户搜 【手机】，构造查询： to_tsquery('simple', '手机')
PG 去 search_vector 字段中查询，命中这条商品，返回结果


分词逻辑写在 Python 应用代码里。只要商品文本更新，Python 必须重新分词，更新 search_text。
"""

import re

import jieba

# 词典用于辅助标题全文召回，不承担完整型号匹配。
# 完整型号由独立 model_key 精确检索，后续不需要逐个往这里添加新型号。
_CUSTOM_WORDS: tuple[str, ...] = (
    "Mate60Pro",
    "Mate60",
    "Mate70",
    "iPhone15Pro",
    "iPhone15",
    "小米14",
    "红米K70",
    "AirPods",
    "FreeBuds",
    "骁龙",
    "麒麟",
)

# \u4e00-\u9fff 匹配中文字符, 其余（标点、emoji、全角符号）压成分词边界
_TOKEN_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]+")

_loaded = False


def _ensure_dict() -> None:
    global _loaded
    """
    懒加载
    如果已经加载过（_loaded=True），直接 return，不再重复加载词典
    """
    if _loaded:
        return
    for word in _CUSTOM_WORDS:
        # word是自定义词典中的每个词，freq是词频，
        # 告诉jieba，这个词是个完整的词汇；词频越大，jiaba认为是一个完整的词汇
        jieba.add_word(word, freq=10000)
    _loaded = True
    # _loaded = True 在 for 循环内 ：只加完第一个单词，就谎称词典全部就绪，别的线程看到标记就跳过加载，导致部分自定义词条缺失


def segment(text: str) -> str:
    """
    调用_ensure_dict()，保证自定义型号词典已经加载进 jieba
    
    
    if not text : 输入是空字符串、None，直接返回空，不继续处理，防止报错
    
    pieces : 创建空列表，用来存放最后所有分词结果
    
    _TOKEN_RE 是之前定义的正则表达式
    
    .findall(text) 扫描整个输入字符串 text，把所有能匹配上正则规则的片段，收集起来，返回一个列表；
    匹配不上的直接丢弃，当作分隔符
    
    jieba.cut_for_search(chunk) 
    对每个匹配上的片段，用 jieba 分词，返回一个列表；例如 “长江，大桥” 会被分词为 ['长江','，','大桥']
    t.strip()：去掉词前后空格、换行
    
    cut_for_search 是搜索引擎专用分词模式，设计目标：长词拆成小词，同时保留原来完整长词
    
    
    extend ：把可迭代对象里面每一项，逐个追加进 pieces 列表
    return " ".join(pieces) ： 把列表 pieces 里面所有词语，用空格连起来拼成一整个字符串，然后返回这个字符串
    """
    
    _ensure_dict()
    if not text:
        return ""
    pieces: list[str] = []
    for chunk in _TOKEN_RE.findall(text):
        pieces.extend(t for t in jieba.cut_for_search(chunk) if t.strip())
    return " ".join(pieces)


def segment_query(text: str) -> str:
    """
    seen 字典用来去重
    jieba.cut(chunk) ： 精确模式分词，如输入智能手机 → 只分出 ['智能手机']，不会拆成「智能、手机、智能手机」
    seen.setdefault(token.lower(), None)
    如果这个词不在 seen 字典，就把它加入字典；
    如果已经存在，啥也不做 → 天然去重
    
    return " & ".join(seen) ： 把字典里所有的关键词，用 `&` 拼接起来，返回这个字符串
    """
    _ensure_dict()
    seen: dict[str, None] = {}
    for chunk in _TOKEN_RE.findall(text or ""):
        for token in jieba.cut(chunk):
            token = token.strip()
            if token:
                seen.setdefault(token.lower(), None)
    return " & ".join(seen)
