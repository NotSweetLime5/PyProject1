"""商品型号键：保留完整型号，独立于 jieba 分词。

型号来源必须是商品结构化数据，不从标题或用户问题中猜测。
只统一 Unicode NFKC、大小写和常用空白；保留连字符与版本后缀。
"""
import unicodedata


_SPACE_TABLE = str.maketrans("", "", " \t\n\r\f\v")

# 与 normalize_model_key 保持一致。存储生成列随 model_name 写入自动更新。
# Alembic 迁移保存自己的冻结副本，不导入可变的应用代码。
MODEL_KEY_SQL = r"NULLIF(lower(translate(normalize(model_name, NFKC), E' \t\n\r\f\013', '')), '')"


def normalize_model_key(value: str | None) -> str:
    return unicodedata.normalize("NFKC", value or "").lower().translate(_SPACE_TABLE)
