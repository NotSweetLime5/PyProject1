"""SiliconFlow embedding 客户端。OpenAI 兼容协议，所以直接用 openai SDK。"""

import asyncio

from openai import APITimeoutError, AsyncOpenAI

from app.core.config import get_settings

settings = get_settings()


# 异步 http 客户端，用来请求向量服务
# 懒加载，第一次调用时才创建，防止浪费资源
_client: AsyncOpenAI | None = None

# 批量提交文本，一次最多 32 个
BATCH_SIZE = 32



# 懒加载获取硅基流动的异步 OpenAI 兼容客户端。
# 全局只创建 1 个实例，第一次调用才初始化，后面每次直接复用这个客户端
def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.siliconflow_api_key,
            base_url=settings.siliconflow_base_url,
            timeout=60.0,
            max_retries=3,  
        )
    return _client

# 处理小批次，接收小批文本，返回对应的向量二维列表
async def _embed_chunk(chunk: list[str]) -> list[list[float]]:
    resp = await get_client().embeddings.create(
        model=settings.embedding_model, input=chunk
    )
    """
    sorted(resp.data, key=lambda i: i.index)
    其中 resp 是api返回的响应对象，
    resp.data：是一个列表，里面每一个 item 是一条 embedding 结果对象
    每个item有 .index（输入文本在请求数组里的下标），.embedding（文本对应的向量数组）
    key=lambda i: i.index：按 index 重排，确保与输入同序
    """
    
    return [item.embedding for item in sorted(resp.data, key=lambda i: i.index)]

# 接收一长串文本列表，循环逐个批次调用 _embed_chunk 处理，返回所有向量
async def embed_many(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    out: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        out.extend(await _embed_chunk(texts[i : i + BATCH_SIZE]))
    return out

# 接收一个文本，返回对应的向量
async def embed_one(text: str) -> list[float]:
    vectors = await embed_many([text])
    return vectors[0]


async def embed_many_concurrent(
    texts: list[str], concurrency: int = 4
) -> list[list[float]]:
    """
    concurrency: int = 4：并发数，默认 4 个
    sem = asyncio.Semaphore(concurrency) ： 有 10 个批次任务，最多同时 4 个在跑；某个任务跑完，下一个才能进去
    
    
    
    """
    if not texts:
        return []
    sem = asyncio.Semaphore(concurrency)
    chunks = [texts[i : i + BATCH_SIZE] for i in range(0, len(texts), BATCH_SIZE)]

    async def run(chunk: list[str]) -> list[list[float]]:
        async with sem:
            try:
                return await _embed_chunk(chunk)
            except APITimeoutError:
                return await _embed_chunk(chunk)  # 补一次，再失败就抛

    batches = await asyncio.gather(*(run(c) for c in chunks))
    return [vec for batch in batches for vec in batch]
