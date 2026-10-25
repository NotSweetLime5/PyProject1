# 后端镜像。
# 用 -slim 而不是 alpine：asyncpg / bcrypt 这类带 C 扩展的包在 musl 上
# 经常要现场编译且容易出 ABI 问题，debian slim 有预编译 wheel。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# 只在这一层装依赖：requirements.txt 不变时这一层的缓存就不会失效，
# 改代码不会导致重装全部依赖
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# 非 root 运行。容器里以 root 跑应用意味着一旦逃逸就是节点权限
RUN useradd -r -u 10001 appuser && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8000

# 健康检查打 /health，它会真的探一次 PG 和 Redis，
# 所以"容器健康"等于"依赖可用"，而不只是"进程还在"
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
