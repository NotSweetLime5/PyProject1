"""评测/标定残留清理：默认只看不动，删除范围必须能证明归属。

为什么需要它：本项目 **pytest、dev 服务、eval runner 共用同一个数据库**
（`DATABASE_URL` 只有一条，`APP_ENV` 只决定 SQL 是否 echo）。
每跑一轮 61 条评测就多出约 60 个会话 / 120 条消息，跑 12 轮之后
`zhangsan_demo`（eval runner 的 DEMO_USER）名下就堆了 2767 条消息 —— 全是评测残留。

两条纪律，都是这个脚本存在的理由：

1. **只删能证明归属的东西。** 默认口径是"某次运行自己记录下来的 sid 列表"
   （`eval/results.json` 每条用例都存了 session_id），
   那是这批数据唯一的身份证。第二口径才是"这几个评测专用账号名下的会话"。
   任何"按时间/按条数清理老会话"的做法我都不要 —— 游客老会话本来就没法证明是谁的，
   P1 就为这事定过没有弱兼容。
2. **默认 dry-run。** 删除是不可逆的，而这份数据同时是若干评测结论的出处
   （`eval/p25_runs/`、`eval/p3a_runs/` 里的明细引用的就是这些 id）。
   看清楚要删什么，再加 `--yes`。

用法：
    python eval/cleanup_residual.py                      # 只看：最近一轮 results.json 将删什么
    python eval/cleanup_residual.py --yes                # 真删这批
    python eval/cleanup_residual.py --users zhangsan_demo eval_probe_other eval_probe_victim
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import engine  # noqa: E402

REPORT = """
select count(distinct m.session_id) sessions, count(*) msgs,
       coalesce(sum(length(m.content)),0) chars,
       min(m.created_at) oldest, max(m.created_at) newest,
       count(distinct case when cs.summary is not null then m.session_id end) with_summary
  from chat_messages m
  left join chat_sessions cs on cs.session_id = m.session_id
 where m.session_id = any(:ids) and m.deleted_at is null
"""

# 这批 sid 里有多少是"有主会话"：只有能落到某个用户名下才算证明得了归属
OWNERS = """
select coalesce(u.username, '(游客/无主)') who, count(distinct m.session_id) sessions, count(*) msgs
  from chat_messages m
  left join chat_sessions cs on cs.session_id = m.session_id
  left join users u on u.id = cs.user_id
 where m.session_id = any(:ids) and m.deleted_at is null
 group by 1 order by 2 desc
"""

SIDS_FROM_USERS = """
select session_id from chat_sessions cs
  join users u on u.id = cs.user_id
 where u.username = any(:names)
"""

DELETE_MSGS = "delete from chat_messages where session_id = any(:ids)"
DELETE_SESS = "delete from chat_sessions where session_id = any(:ids)"


def sids_from_results(path: Path) -> list[str]:
    """从 runner 的明细里取本轮用过的 sid。

    多轮用例的同一个会话会在几条记录里重复出现，所以去重；
    另外 runner 自己也发过一些校验用的坏请求（edge-*），那些不在结果里，本来就不该删。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for rec in data:
        for r in rec.get("recs") or []:
            sid = ((r.get("done") or {}).get("session_id")) or r.get("session_id")
            if sid and isinstance(sid, str) and sid not in out:
                out.append(sid)
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-results", default="eval/results.json",
                    help="runner 明细文件；用它记录的 sid 作为删除范围")
    ap.add_argument("--users", nargs="*", default=None,
                    help="改用'这几个用户名下的会话'作为范围（评测专用账号）")
    ap.add_argument("--yes", action="store_true", help="真的删；不给就是 dry-run")
    args = ap.parse_args()

    async with engine.begin() as c:
        if args.users:
            ids = [r[0] for r in (await c.execute(
                text(SIDS_FROM_USERS), {"names": args.users})).fetchall()]
            scope = f"用户名下（{', '.join(args.users)}）"
        else:
            ids = sids_from_results(Path(args.from_results))
            scope = args.from_results
        if not ids:
            print(f"没有匹配到任何会话（范围来源：{scope}）。什么都不做。")
            return

        rep = (await c.execute(text(REPORT), {"ids": ids})).fetchone()
        print(f"范围来源：{scope}\n命中会话 {len(ids)} 个；实际有存活消息的：")
        print(f"  会话 {rep[0]} / 消息 {rep[1]} / 正文 {rep[2]} 字 / 时间 {rep[3]} ~ {rep[4]}"
              f" / 其中已生成摘要 {rep[5]} 个")
        print("归属分布（这就是'能不能证明是你的数据'的答案）：")
        for who, sess, msgs in (await c.execute(text(OWNERS), {"ids": ids})).fetchall():
            print(f"  {who:24s} 会话 {sess:4d} 消息 {msgs:5d}")
        orphan = len(ids) - rep[0]
        if orphan > 0:
            print(f"  另有 {orphan} 个 sid 一条存活消息都没有（签发后没聊过，或被软删）")

        if not args.yes:
            print("\ndry-run：没有删任何东西。确认上面这些就是你想要的范围之后，加 --yes 再跑。")
            return

        d1 = (await c.execute(text(DELETE_MSGS), {"ids": ids})).rowcount
        d2 = (await c.execute(text(DELETE_SESS), {"ids": ids})).rowcount
        print(f"\n已删除：chat_messages {d1} 行，chat_sessions {d2} 行（只限上面列出的范围）。")
        print("注意：users / goods / faqs 这些种子数据一条都没动；评测的 fixture 账号也留着。")
    await engine.dispose()


asyncio.run(main())
