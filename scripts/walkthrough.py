#!/usr/bin/env python3
"""取回流程走查：对运行中的服务完整走一遍取回流程（与 /docs 页面调用的是同一套 API）。

场景：6 个待取文件、9 个副本、3 卷。
- 首次计划应为 2 次装带、0 次回卷的最优解；
- 故意回报两张坏副本（A 在卷1、卷2 的副本先后读失败），
  验证：带头推进、副本停用、按新位置重排、A 最终进入“无副本可读”清单；
- 演示一次“不按计划第一步回报”被拒（409）。

用法: python3 scripts/walkthrough.py [base_url]
"""
from __future__ import annotations

import json
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

JOB = {
    "files": ["A", "B", "C", "D", "E", "F"],
    "copies": [
        {"file": "A", "volume": 1, "position": 2},
        {"file": "A", "volume": 2, "position": 5},
        {"file": "B", "volume": 1, "position": 6},
        {"file": "C", "volume": 2, "position": 1},
        {"file": "C", "volume": 3, "position": 4},
        {"file": "D", "volume": 3, "position": 1},
        {"file": "E", "volume": 3, "position": 6},
        {"file": "F", "volume": 1, "position": 9},
        {"file": "F", "volume": 3, "position": 8},
    ],
}

# 剧本：这两步读带失败，其余成功
SCRIPTED_FAILURES = {("A", 1, 2), ("A", 2, 5)}


def show_plan(plan: dict) -> None:
    print(f"  计划: 装带 {plan['mounts']} 次 + 回卷 {plan['rewinds']} 次 = 总代价 {plan['cost']}")
    for s in plan["steps"]:
        if s["action"] == "mount":
            print(f"    {s['seq']:>2}. 装带  卷{s['volume']}")
        elif s["action"] == "rewind":
            print(f"    {s['seq']:>2}. 回卷  卷{s['volume']}")
        else:
            print(f"    {s['seq']:>2}. 读带  文件{s['file']}  卷{s['volume']} 序号{s['position']}")
    if plan["unretrievable"]:
        print(f"  !! 已无副本可读: {plan['unretrievable']}")


def show_state(state: dict) -> None:
    ex = state["executed"]
    print(
        f"  状态: 当前卷={state['current_volume']} 带头={state['head']} "
        f"待取={state['pending']} 已取={state['done']}"
    )
    print(
        f"        累计: 装带{ex['mounts']} 回卷{ex['rewinds']} "
        f"成功{ex['reads_ok']} 失败{ex['reads_failed']} "
        f"停用副本={state['disabled_copies']}"
    )


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=10)

    print(f"== 0. 复位并创建作业 (POST {BASE}/jobs)")
    client.delete("/job")
    r = client.post("/jobs", json=JOB)
    r.raise_for_status()
    body = r.json()
    show_plan(body["plan"])

    print("\n== 1. 演示乱序回报被拒 (POST /receipts, 非计划第一步)")
    r = client.post(
        "/receipts", json={"file": "B", "volume": 1, "position": 6, "result": "ok"}
    )
    print(f"  HTTP {r.status_code}: {json.dumps(r.json()['detail'], ensure_ascii=False)}")

    step = 0
    while True:
        plan = client.get("/plan").json()
        nxt = plan["next_step"]
        if nxt is None:
            break
        step += 1
        key = (nxt["file"], nxt["volume"], nxt["position"])
        result = "failed" if key in SCRIPTED_FAILURES else "ok"
        print(
            f"\n== 回报第 {step} 步: 文件{nxt['file']} 卷{nxt['volume']} "
            f"序号{nxt['position']} -> {result}"
        )
        r = client.post(
            "/receipts",
            json={
                "file": nxt["file"],
                "volume": nxt["volume"],
                "position": nxt["position"],
                "result": result,
            },
        )
        r.raise_for_status()
        body = r.json()
        show_state(body["state"])
        show_plan(body["plan"])

    final = client.get("/state").json()
    print("\n== 取回流程结束 ==")
    print(f"  已取回: {final['done']}")
    print(f"  无副本可读: {final['unretrievable']}")
    print(f"  累计执行: {final['executed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
