"""API 集成测试：完整取回流程，含坏副本停用与重排。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

DEMO_JOB = {
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


@pytest.fixture(autouse=True)
def reset_job():
    client.delete("/job")
    yield
    client.delete("/job")


def create_demo_job():
    resp = client.post("/jobs", json=DEMO_JOB)
    assert resp.status_code == 201, resp.text
    return resp.json()


def report(file, volume, position, result):
    return client.post(
        "/receipts",
        json={"file": file, "volume": volume, "position": position, "result": result},
    )


def test_initial_plan_is_optimal():
    body = create_demo_job()
    plan = body["plan"]
    assert plan["cost"] == 2
    assert plan["mounts"] == 2 and plan["rewinds"] == 0
    assert plan["next_step"] == {
        "seq": 2, "action": "read", "volume": 1, "file": "A", "position": 2,
    }
    state = body["state"]
    assert state["current_volume"] is None and state["head"] == 0
    assert state["pending"] == list("ABCDEF")


def test_receipt_must_match_first_step():
    create_demo_job()
    # 不按计划第一步回报（第一步是 A@1:2，这里报 B）→ 409，进度不动
    resp = report("B", 1, 6, "ok")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["expected"] == {"file": "A", "volume": 1, "position": 2}
    state = client.get("/state").json()
    assert state["pending"] == list("ABCDEF")
    assert state["current_volume"] is None


def test_failed_copy_disabled_and_replanned():
    create_demo_job()
    # 第一步 A@卷1:2 读失败：带头推进到卷1@2，副本停用，A 还有卷2 的副本
    resp = report("A", 1, 2, "failed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"]["copy_disabled"] is True
    state = body["state"]
    assert state["current_volume"] == 1 and state["head"] == 2
    assert state["disabled_copies"] == [{"file": "A", "volume": 1, "position": 2}]
    assert state["pending"] == list("ABCDEF")  # A 未取到，仍在待取
    assert state["unretrievable"] == []
    plan = body["plan"]
    # 重排：A 只能用卷2 的副本；卷1 是当前卷免装带
    assert plan["cost"] == 2 and plan["mounts"] == 2 and plan["rewinds"] == 0
    assert plan["next_step"]["file"] == "B"


def test_full_retrieval_flow_with_two_failures():
    create_demo_job()

    # 1) A@1:2 失败 → 停用，重排
    plan = report("A", 1, 2, "failed").json()["plan"]
    # 2) B@1:6 成功（当前卷继续读，不装带）
    body = report("B", 1, 6, "ok").json()
    assert body["state"]["head"] == 6
    assert body["state"]["done"] == ["B"]
    # 3) 下一步在卷2：C@2:1 成功（装带计入累计：卷1、卷2 各一次）
    body = report("C", 2, 1, "ok").json()
    assert body["state"]["current_volume"] == 2 and body["state"]["head"] == 1
    assert body["state"]["executed"]["mounts"] == 2
    # 4) A@2:5 失败 → A 副本耗尽，进入无副本可读清单
    body = report("A", 2, 5, "failed").json()
    assert body["state"]["unretrievable"] == ["A"]
    assert body["state"]["pending"] == ["A", "D", "E", "F"]
    plan = body["plan"]
    assert plan["unretrievable"] == ["A"]
    assert plan["mounts"] == 1 and plan["rewinds"] == 0  # 只剩卷3 要装
    # 5) 依次取回 D、E、F（卷3 升序）
    for f, p in [("D", 1), ("E", 6), ("F", 8)]:
        body = report(f, 3, p, "ok").json()
    state = body["state"]
    assert state["done"] == ["B", "C", "D", "E", "F"]
    assert state["unretrievable"] == ["A"]
    assert state["executed"] == {"mounts": 3, "rewinds": 0, "reads_ok": 5, "reads_failed": 2}
    # 计划清空：没有可回报的步骤
    assert body["plan"]["steps"] == []
    assert body["plan"]["next_step"] is None
    # 再回报 → 409
    assert report("F", 3, 8, "ok").status_code == 409


def test_live_flow_never_needs_rewind():
    """不变式：当前卷上不存在“带头之下的可用副本”。

    计划按序号升序调度，且失败副本立即停用，所以带头不可能越过
    仍处于可用状态的副本 —— 实际流程中重排永远不需要回卷。
    （回卷分支只在理论平局裁决中出现，见 tests/test_planner.py。）
    """
    create_demo_job()
    plan = report("A", 1, 2, "failed").json()["plan"]
    assert plan["rewinds"] == 0
    plan = report("B", 1, 6, "ok").json()["plan"]
    assert plan["rewinds"] == 0
    plan = report("C", 2, 1, "ok").json()["plan"]
    assert plan["rewinds"] == 0
    plan = report("A", 2, 5, "failed").json()["plan"]
    assert plan["rewinds"] == 0


def test_validation_errors():
    # 超过 12 卷
    resp = client.post("/jobs", json={
        "files": ["A"],
        "copies": [{"file": "A", "volume": v, "position": 1} for v in range(1, 14)],
    })
    assert resp.status_code == 422
    # 未知文件
    resp = client.post("/jobs", json={
        "files": ["A"],
        "copies": [{"file": "B", "volume": 1, "position": 1}],
    })
    assert resp.status_code == 422
    # 重复副本
    resp = client.post("/jobs", json={
        "files": ["A"],
        "copies": [
            {"file": "A", "volume": 1, "position": 1},
            {"file": "A", "volume": 1, "position": 1},
        ],
    })
    assert resp.status_code == 422
    # 非法序号
    resp = client.post("/jobs", json={
        "files": ["A"],
        "copies": [{"file": "A", "volume": 1, "position": 0}],
    })
    assert resp.status_code == 422


def test_endpoints_require_job():
    assert client.get("/state").status_code == 404
    assert client.get("/plan").status_code == 404
    assert report("A", 1, 1, "ok").status_code == 404
    assert client.delete("/job").status_code == 204
