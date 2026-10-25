import random
import uuid

from conftest import make_client, run

from app.core.redis import redis_client


def _u(tag: str) -> str:
    return f"tx_{tag}_{uuid.uuid4().hex[:6]}"


def _phone() -> str:
    return "13" + "".join(random.choice("0123456789") for _ in range(9))


async def _register_and_login(c, username=None, password="Passw0rd!x"):
    name = username or _u("u")
    r = await c.post(
        "/api/v1/auth/register",
        json={"username": name, "password": password, "phone": _phone()},
    )
    assert r.status_code == 200, r.text
    # 注册也吃限流额度，登录前清一次，免得把"限流 701"误读成"密码错 401"
    async for k in redis_client.scan_iter("rl:*"):
        await redis_client.delete(k)
    r = await c.post("/api/v1/auth/login", json={"username": name, "password": password})
    assert r.status_code == 200, r.text
    return name, r.json()["data"]


def test_register_returns_safe_envelope():
    async def main():
        async with make_client() as c:
            name = _u("reg")
            r = await c.post(
                "/api/v1/auth/register",
                json={"username": name, "password": "Passw0rd!x", "phone": _phone(), "nick_name": "小张"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert set(body) == {"code", "msg", "data"}, body
            data = body["data"]
            # 白名单模型：password_hash 和 phone 从来没被写进 UserOut，
            # 所以以后给 users 加敏感列也不会默认泄漏
            assert "password_hash" not in data, data
            assert "phone" not in data, data
            assert data["username"] == name and data["is_active"] is True
    run(main())


def test_validation_rules():
    async def main():
        base = {"username": _u("v"), "password": "Passw0rd!x", "phone": _phone()}
        cases = [
            ({**base, "phone": "12345678901"}, "手机号段不合法"),
            ({**base, "password": "short1"}, "密码过短"),
            ({**base, "username": "bad name"}, "用户名含空格"),
            ({**base, "password": "密" * 25}, "超 bcrypt 72 字节上限"),
        ]
        async with make_client() as c:
            for payload, why in cases:
                r = await c.post("/api/v1/auth/register", json=payload)
                assert r.status_code == 400, f"{why} 应 400，实得 {r.status_code}: {r.text[:150]}"
                assert r.json()["code"] == 400
    run(main())


def test_duplicate_username_conflicts():
    async def main():
        name = _u("dup")
        async with make_client() as c:
            payload = {"username": name, "password": "Passw0rd!x", "phone": _phone()}
            assert (await c.post("/api/v1/auth/register", json=payload)).status_code == 200
            r = await c.post("/api/v1/auth/register", json=payload)
            assert r.status_code == 409, r.text
    run(main())


def test_login_failures_are_indistinguishable():
    """用户不存在和密码错误必须连消息带耗时都无法区分。"""
    async def main():
        name = _u("timing")
        async with make_client() as c:
            await c.post(
                "/api/v1/auth/register",
                json={"username": name, "password": "Passw0rd!x", "phone": _phone()},
            )
            wrong = await c.post("/api/v1/auth/login", json={"username": name, "password": "WrongPass1!"})
            ghost = await c.post("/api/v1/auth/login", json={"username": _u("no"), "password": "WrongPass1!"})
            assert wrong.status_code == ghost.status_code == 401
            assert wrong.json()["msg"] == ghost.json()["msg"], (wrong.text, ghost.text)

            async def median_ms(payload, n=3):
                ts = []
                for _ in range(n):
                    import time
                    t0 = time.perf_counter()
                    await c.post("/api/v1/auth/login", json=payload)
                    ts.append((time.perf_counter() - t0) * 1000)
                return sorted(ts)[n // 2]

            a = await median_ms({"username": name, "password": "WrongPass1!"})
            b = await median_ms({"username": _u("no"), "password": "WrongPass1!"})
            # 没有 dummy verify 时这个差值约等于一次 bcrypt 的耗时（实测 200ms）
            assert abs(a - b) < 60, f"存在 {a:.0f}ms vs 不存在 {b:.0f}ms，时序泄露了用户存在性"
    run(main())


def test_token_guards_current_user():
    async def main():
        async with make_client() as c:
            name, data = await _register_and_login(c)
            tok = data["access_token"]
            r = await c.get("/api/v1/users/me", headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 200 and r.json()["data"]["username"] == name

            r = await c.get("/api/v1/users/me")
            assert r.status_code == 401 and r.json()["msg"] == "请先登录", r.text

            r = await c.get("/api/v1/users/me", headers={"Authorization": "Bearer garbage"})
            assert r.status_code == 401, r.text

            r = await c.get("/api/v1/users/me", headers={"Authorization": f"Basic {tok}"})
            assert r.status_code == 401, r.text
    run(main())


def test_login_rate_limited_by_ip():
    """限流由这一个测试负责验证。

    conftest 会话级把额度放宽了，所以这里必须自己调窄再复原 ——
    否则它会永远测不到东西（断言"第 11 次返回 701"会静默通过不了）。
    """
    from app.core.config import get_settings

    async def main():
        s = get_settings()
        saved = s.login_rate_limit
        s.login_rate_limit = 10
        try:
            async with make_client() as c:
                codes = [
                    (await c.post("/api/v1/auth/login", json={"username": _u("x"), "password": "Passw0rd!x"})).status_code
                    for _ in range(13)
                ]
                assert set(codes[:10]) == {401}, codes
                assert 701 in codes, codes
                # 限流响应也要走统一契约，否则前端要为它写特例
                r = await c.post("/api/v1/auth/login", json={"username": _u("x"), "password": "Passw0rd!x"})
                assert r.status_code == 701 and r.json()["code"] == 701, r.text
        finally:
            s.login_rate_limit = saved
            async for k in redis_client.scan_iter("rl:*"):
                await redis_client.delete(k)
    run(main())
