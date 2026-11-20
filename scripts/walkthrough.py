#!/usr/bin/env python3
"""值班流程走查：登记目录 → 整批下架 → 开工取回 → 逐卷归还。

与 /docs 页面调用的是同一套 API，覆盖三类关键交错：

1. 下架审批会被“拿走最后一份可读副本”整批退回；
2. 作业中占用卷的下架申请被退回，列出受影响文件与占用卷；
3. 读坏副本同步在库目录中标为不可读，并释放其卷占用；
4. 送检卷可逐卷归还；归还的副本不会被补进已开工作业（需重新开工才纳入）。

用法: python3 scripts/walkthrough.py [base_url]
"""
from __future__ import annotations

import json
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

CATALOG = {
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
    ]
}

# 剧本：这两步读带失败，其余成功
SCRIPTED_FAILURES = {("A", 1, 2), ("A", 2, 5)}


def detail(resp: httpx.Response):
    try:
        return resp.json().get("detail", resp.text)
    except ValueError:
        return resp.text


def show_plan(plan: dict) -> None:
    print(
        f"  计划: 装带 {plan['mounts']} 次 + 回卷 {plan['rewinds']} 次 "
        f"= 总代价 {plan['cost']}"
    )
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
        f"待取={state['pending']} 已取={state['done']} 占用卷={state['occupied_volumes']}"
    )
    print(
        f"        累计: 装带{ex['mounts']} 回卷{ex['rewinds']} "
        f"成功{ex['reads_ok']} 失败{ex['reads_failed']} "
        f"停用副本={state['disabled_copies']}"
    )


def show_library(lib: dict) -> None:
    parts = []
    for v in lib["volumes"]:
        if v["online"]:
            parts.append(f"卷{v['volume']}=在线")
        else:
            parts.append(f"卷{v['volume']}=送检(申请{v['removal_id']})")
    print(f"  库目录: {'  '.join(parts)}  占用卷={lib['occupied_volumes']}")
    bad = [c for c in lib["copies"] if not c["readable"]]
    if bad:
        print(f"  不可读副本: {[(c['file'], c['volume'], c['position']) for c in bad]}")


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=10)

    print(f"== 0. 复位，登记全库副本目录 (POST {BASE}/library)")
    client.delete("/job")
    client.delete("/library")
    r = client.post("/library", json=CATALOG)
    r.raise_for_status()
    show_library(r.json())

    print("\n== 1. 申请整批下架 {2,3}：C 的两份副本都在其中 → 整批退回")
    r = client.post("/removals", json={"volumes": [2, 3]})
    print(f"  HTTP {r.status_code}: {json.dumps(detail(r), ensure_ascii=False)}")
    show_library(client.get("/library").json())

    print("\n== 2. 申请下架 {1,2}：A、B 会失去最后一份在线可读副本 → 整批退回")
    r = client.post("/removals", json={"volumes": [1, 2]})
    print(f"  HTTP {r.status_code}: {json.dumps(detail(r), ensure_ascii=False)}")

    print("\n== 3. 作业中申请下架卷1：被待取文件占用 → 退回")
    r = client.post("/jobs", json={"files": list("ABCDEF")})
    r.raise_for_status()
    job = r.json()
    show_plan(job["plan"])
    show_library(client.get("/library").json())
    r = client.post("/removals", json={"volumes": [1]})
    print(f"  HTTP {r.status_code}: {json.dumps(detail(r), ensure_ascii=False)}")

    print("\n== 4. 逐张回报读带结果")
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
            f"\n-- 第 {step} 步: 文件{nxt['file']} 卷{nxt['volume']} "
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
        show_library(client.get("/library").json())

    final = client.get("/state").json()
    print("\n== 取回流程结束 ==")
    print(f"  已取回: {final['done']}")
    print(f"  无副本可读: {final['unretrievable']}")
    print(f"  累计执行: {final['executed']}")

    print("\n== 5. 作业已无占用；送检卷2，再按卷号归还")
    client.delete("/job")
    r = client.post("/removals", json={"volumes": [2]})
    r.raise_for_status()
    removal = r.json()["removal"]
    print(f"  下架申请 {removal['id']} 已生效: {removal['volumes']} → 立即离线")
    show_library(client.get("/library").json())
    # 送检期间卷2 的副本不能被新作业纳入：C 只能用卷3 的副本
    r = client.post("/jobs", json={"files": ["C"]})
    r.raise_for_status()
    body = r.json()
    print(
        "  送检期间开工取 C：纳入卷 "
        f"{sorted({s['volume'] for s in body['plan']['steps'] if s['action'] == 'read'})}"
    )
    client.delete("/job")
    r = client.post("/returns", json={"volume": 2})
    r.raise_for_status()
    print(f"  归还卷2: 申请状态 = {r.json()['removals'][-1]['state']}")
    show_library(r.json())

    print("\n== 6. 重新开工：无副本可读的文件仍单列")
    r = client.post("/jobs", json={"files": list("ABCDEF")})
    r.raise_for_status()
    body = r.json()
    show_state(body["state"])
    show_plan(body["plan"])
    client.delete("/job")
    return 0


if __name__ == "__main__":
    sys.exit(main())
