
def group_turns(rows):
    turns: list[list] = []
    pending: list = []
    for m in rows:
        pending.append(m)
        if m.role == "user":
            turns.append(pending)
            pending = []
    return turns, pending

"""
| 索引  | ID    | Role          | Content |
| 0     | 105   | assistant     | "好的，已为您修改地址。" |
| 1     | 104   | user          | "把地址改成我家。" |
| 2     | 103   | tool_result   | "{物流状态: 运输中}" |
| 3     | 102   | assistant     | "正在查询..." (工具调用) |
| 4     | 101   | user          | "帮我查一下订单ORD-X99的物流" |

初始化 turns 和 pending
-->  pending=[105], turns=[]
根据 if m.role == "user" 判断
-->  turns=[[105, 104]], pending=[]
重复


为什么必须以整轮为单位
1. 模型可能看到助手的回答，但是没有看到用户的请求
2. 模型无法将摘要中的话题与原文衔接
"""

