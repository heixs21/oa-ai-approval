"""打印待办列表的完整原始返回，用于和网页上的待办条数对比。

用法（在项目根目录执行）：
    python tools/dump_todo.py

重点看每条的这几个字段：
    currentNodeName  流程当前停在哪个节点（是不是在等你处理）
    creatorName      发起人（如果都是自己，说明是"我发起的"而非"待我审批"）
    isprocessed      是否已处理
    isbereject       是否被退回
    agenttype        代理类型
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as C
from oa import oa

PATH = "/api/workflow/paService/getDoingWorkflowRequestList"

KEYS = (
    "requestId", "requestName", "workflowId", "workflowName",
    "currentNodeId", "currentNodeName", "currentnodetype",
    "creatorId", "creatorName",
    "isprocessed", "isbereject", "isremark",
    "agenttype", "agentorbyagentid", "lastOperateTime",
)


def main():
    print("=" * 78)
    print("OA:", C.OA_BASE, " userid:", C.OA_USERID, " 白名单:", C.WORKFLOW_WHITELIST)
    print("=" * 78)

    data = oa.api(PATH, {"pageNo": 1, "pageSize": 100})

    print("\n【原始返回（前 6000 字符）】")
    print(json.dumps(data, ensure_ascii=False, indent=2)[:6000])

    todos = oa.get_doing_list()
    print("\n" + "=" * 78)
    print(f"【解析出 {len(todos)} 条】")
    for i, t in enumerate(todos, 1):
        print(f"\n--- 第 {i} 条 ---")
        for k in KEYS:
            if k in t:
                print(f"    {k:20} = {t.get(k)!r}")

    # 列出每条有哪些字段，方便看差异
    if todos:
        keysets = [set(t.keys()) for t in todos]
        common = set.intersection(*keysets)
        extra = set.union(*keysets) - common
        print("\n" + "=" * 78)
        print("所有条目共有的字段数:", len(common))
        print("只在部分条目出现的字段:", sorted(extra) or "无")

    print("\n" + "=" * 78)
    print("对比方法：把上面每条的 requestId 和网页待办里的流程编号/标题对一下，")
    print("看多出来的那几条是停在谁那里、状态是什么。")
    print("=" * 78)


if __name__ == "__main__":
    main()
