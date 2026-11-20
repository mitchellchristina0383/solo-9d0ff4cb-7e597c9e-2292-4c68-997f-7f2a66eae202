"""API 集成测试：库目录登记、整批下架审批、逐卷归还、取回作业及交错场景。"""
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.state import store

client = TestClient(app)

# 演示目录：A、F 各两份；B 仅卷1；D、E 仅卷3 → 只有卷2 可单独安全下架
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


@pytest.fixture(autouse=True)
def reset_state():
    store.reset_all()
    yield
    store.reset_all()


def register(copies=None):
    body = {"copies": copies} if copies is not None else CATALOG
    resp = client.post("/library", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def start_job(files=("A", "B", "C", "D", "E", "F")):
    resp = client.post("/jobs", json={"files": list(files)})
    assert resp.status_code == 201, resp.text
    return resp.json()


def report(file, volume, position, result):
    return client.post(
        "/receipts",
        json={"file": file, "volume": volume, "position": position, "result": result},
    )


def request_removal(*volumes):
    return client.post("/removals", json={"volumes": list(volumes)})


def return_volume(removal_id, volume):
    return client.post(f"/removals/{removal_id}/returns", json={"volume": volume})


def volume_map(library):
    return {v["volume"]: v for v in library["volumes"]}


# ---------- 库目录 ----------

def test_register_and_read_library():
    lib = register()
    assert lib["files"] == list("ABCDEF")
    assert [v["volume"] for v in lib["volumes"]] == [1, 2, 3]
    assert all(v["online"] and v["removal_id"] is None for v in lib["volumes"])
    assert all(c["readable"] for c in lib["copies"])
    assert lib["removals"] == [] and lib["occupied_volumes"] == []
    assert client.get("/library").json() == lib


def test_library_registration_validation():
    assert client.post("/library", json={"copies": []}).status_code == 422
    # 重复副本
    resp = client.post("/library", json={"copies": [
        {"file": "A", "volume": 1, "position": 1},
        {"file": "A", "volume": 1, "position": 1},
    ]})
    assert resp.status_code == 422
    # 超过 12 卷
    resp = client.post("/library", json={"copies": [
        {"file": "A", "volume": v, "position": 1} for v in range(1, 14)
    ]})
    assert resp.status_code == 422
    # 已登记后可在无作业时重新登记（整体替换）
    register()
    assert client.post("/library", json=CATALOG).status_code == 201
    # 有作业时不能重登/清除
    start_job()
    assert client.post("/library", json=CATALOG).status_code == 409
    assert client.delete("/library").status_code == 409
    client.delete("/job")
    assert client.delete("/library").status_code == 204
    assert client.get("/library").status_code == 404


def test_re_register_keeps_bad_copy_marks():
    register()
    start_job(["A"])
    assert report("A", 1, 2, "failed").status_code == 200
    client.delete("/job")
    lib = client.post("/library", json=CATALOG).json()
    assert {"file": "A", "volume": 1, "position": 2, "readable": False} in lib["copies"]
    # 送检卷未归还时也不能重新登记
    register([
        {"file": "A", "volume": 1, "position": 1},
        {"file": "A", "volume": 2, "position": 1},
    ])
    assert request_removal(1).status_code == 201
    assert client.post("/library", json={"copies": [
        {"file": "A", "volume": 1, "position": 1},
        {"file": "A", "volume": 2, "position": 1},
    ]}).status_code == 409


# ---------- 下架审批 ----------

def test_removal_goes_offline_immediately_and_is_listed():
    register()
    resp = request_removal(2)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["removal"] == {"id": 1, "volumes": [2], "state": "open", "returned": []}
    vols = volume_map(body["library"])
    assert vols[2]["online"] is False and vols[2]["removal_id"] == 1
    assert vols[1]["online"] is True and vols[3]["online"] is True


def test_removal_rejected_when_it_takes_last_readable_copy():
    register()
    # {2,3} 一起下架：C 的两份副本都在其中，D/E 只在卷3 → 受影响；整批退回
    resp = request_removal(2, 3)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["affected_files"] == ["C", "D", "E"]
    assert detail["occupied_volumes"] == []
    lib = client.get("/library").json()
    assert all(v["online"] for v in lib["volumes"])
    assert lib["removals"] == []
    # 卷2 单独下架是安全的（A 还有卷1、C 还有卷3）
    assert request_removal(2).status_code == 201


def test_removal_rejected_when_volume_occupied():
    register()
    start_job()
    # 作业中每个待取文件的可读副本都占卷
    resp = request_removal(1)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["occupied_volumes"] == [1]
    assert detail["occupied_files"] == ["A", "B", "F"]
    # 同时卷1 上还有 B 的最后一份副本
    assert detail["affected_files"] == ["B"]
    lib = client.get("/library").json()
    assert all(v["online"] for v in lib["volumes"])
    assert lib["occupied_volumes"] == [1, 2, 3]


def test_removal_whole_batch_rejected_leaves_safe_volume_online():
    register()
    # {1,2}：A 的两份都在其中，B 仅卷1 → 受影响；卷2 本身安全也必须留在线上
    resp = request_removal(1, 2)
    assert resp.status_code == 409
    assert resp.json()["detail"]["affected_files"] == ["A", "B"]
    assert all(v["online"] for v in client.get("/library").json()["volumes"])


def test_removal_counts_only_online_readable_copies():
    register([
        {"file": "A", "volume": 1, "position": 1},
        {"file": "A", "volume": 2, "position": 1},
        {"file": "B", "volume": 3, "position": 1},
    ])
    # 卷1 可安全下架（A 还有卷2）
    assert request_removal(1).status_code == 201
    # 卷1 已送检离线，其副本不能在库内阅读；再下架卷2 会拿走 A 最后一份
    # “在线且未报坏”的副本 → 整批退回
    resp = request_removal(2)
    assert resp.status_code == 409
    assert resp.json()["detail"]["affected_files"] == ["A"]
    # 归还卷1 后 A 又有了可读副本，此时下架卷2 安全（A 只剩卷1 的副本）
    assert return_volume(1, 1).status_code == 200
    assert request_removal(2).status_code == 201
    body = start_job(["A", "B"])
    # A 只能用卷1；B 只能用卷3
    assert {s["volume"] for s in body["plan"]["steps"] if s["action"] == "read"} == {1, 3}
    assert body["state"]["unretrievable"] == []


def test_removal_validation():
    register()
    assert request_removal(9).status_code == 422
    assert request_removal(1, 1).status_code == 422
    assert request_removal().status_code == 422


def test_return_is_per_volume_and_tracked():
    register([
        {"file": "A", "volume": 1, "position": 1},
        {"file": "A", "volume": 2, "position": 1},
        {"file": "A", "volume": 3, "position": 1},
    ])
    body = request_removal(1, 2).json()
    assert body["removal"]["id"] == 1
    lib = return_volume(1, 1).json()
    assert {v["volume"]: v["online"] for v in lib["volumes"]} == {
        1: True, 2: False, 3: True,
    }
    assert lib["removals"][0] == {
        "id": 1, "volumes": [1, 2], "state": "open", "returned": [1],
    }
    lib = return_volume(1, 2).json()
    assert lib["removals"][0]["state"] == "closed"
    assert all(v["online"] for v in lib["volumes"])
    # 重复归还 → 409；卷不在该申请 → 422；申请不存在 → 404
    assert return_volume(1, 1).status_code == 409
    assert return_volume(1, 3).status_code == 422
    assert return_volume(99, 3).status_code == 404


# ---------- 作业与目录的衔接 ----------

def test_start_job_snapshots_online_readable_copies_only():
    register([
        {"file": "A", "volume": 1, "position": 2},
        {"file": "A", "volume": 2, "position": 5},
        {"file": "A", "volume": 3, "position": 9},
        {"file": "B", "volume": 1, "position": 6},
    ])
    # 1) 先把 A@卷1:2 读坏
    start_job(["A"])
    assert report("A", 1, 2, "failed").status_code == 200
    client.delete("/job")
    # 2) 卷2 送检离线（A 还有卷3，安全）
    assert request_removal(2).status_code == 201

    body = start_job(["A"])
    # 开工快照：卷1:2 不可读、卷2 离线，只纳入卷3:9
    assert body["plan"]["next_step"] == {
        "seq": 2, "action": "read", "volume": 3, "file": "A", "position": 9,
    }
    assert body["state"]["occupied_volumes"] == [3]
    lib = client.get("/library").json()
    assert [c for c in lib["copies"] if c["file"] == "A"] == [
        {"file": "A", "volume": 1, "position": 2, "readable": False},
        {"file": "A", "volume": 2, "position": 5, "readable": True},
        {"file": "A", "volume": 3, "position": 9, "readable": True},
    ]


def test_unretrievable_when_only_copies_bad_or_offline():
    register([
        {"file": "A", "volume": 1, "position": 2},
        {"file": "A", "volume": 2, "position": 5},
        {"file": "A", "volume": 3, "position": 7},
    ])
    # 开工前：卷1 副本先读坏、卷2 送检离线（A 仍有卷3，故下架卷2 安全）
    start_job(["A"])
    assert report("A", 1, 2, "failed").status_code == 200
    client.delete("/job")
    assert request_removal(2).status_code == 201
    # 开工快照只剩卷3；读坏它 → A 无副本可读
    body = start_job(["A"])
    assert body["state"]["occupied_volumes"] == [3]
    assert report("A", 3, 7, "failed").status_code == 200
    state = client.get("/state").json()
    assert state["unretrievable"] == ["A"]
    assert client.get("/plan").json()["steps"] == []
    assert client.get("/plan").json()["next_step"] is None
    # 坏副本在库目录中仍标为不可读；卷2 的副本仍可读（只是离线）
    lib = client.get("/library").json()
    readable = {(c["file"], c["volume"]): c["readable"] for c in lib["copies"]}
    assert readable[("A", 1)] is False and readable[("A", 2)] is True
    assert readable[("A", 3)] is False


def test_returned_volume_not_retroactively_added_to_running_job():
    register()
    assert request_removal(2).status_code == 201
    body = start_job(["C"])
    # C 只有卷3 在线可用（卷2 离线）
    assert body["plan"]["next_step"]["volume"] == 3
    assert return_volume(1, 2).status_code == 200
    # 归还后当前作业仍不纳入卷2 的副本
    assert client.get("/state").json()["files"] == ["C"]
    assert client.get("/plan").json()["next_step"]["volume"] == 3
    # 作废后重新开工才会纳入卷2（卷号更小，平局时胜出）
    client.delete("/job")
    body = start_job(["C"])
    assert body["plan"]["volumes_used"] == [2]


def test_job_validation():
    assert client.post("/jobs", json={"files": ["A"]}).status_code == 404  # 无目录
    register()
    assert client.post("/jobs", json={"files": []}).status_code == 422
    assert client.post("/jobs", json={"files": ["A", "A"]}).status_code == 422
    assert client.post("/jobs", json={"files": ["Z"]}).status_code == 422
    start_job()
    assert client.post("/jobs", json={"files": ["A"]}).status_code == 409


def test_receipt_must_match_first_step():
    register()
    start_job()
    resp = report("B", 1, 6, "ok")
    assert resp.status_code == 409
    assert resp.json()["detail"]["expected"] == {
        "file": "A", "volume": 1, "position": 2,
    }
    state = client.get("/state").json()
    assert state["pending"] == list("ABCDEF")
    assert state["current_volume"] is None


# ---------- 读坏副本与交错的审批/归还 ----------

def test_failed_read_marks_copy_unreadable_and_stops_occupying():
    register([
        {"file": "A", "volume": 1, "position": 2},
        {"file": "A", "volume": 2, "position": 5},
        {"file": "A", "volume": 3, "position": 7},
    ])
    start_job(["A"])
    body = report("A", 1, 2, "failed").json()
    assert body["state"]["disabled_copies"] == [
        {"file": "A", "volume": 1, "position": 2}
    ]
    lib = client.get("/library").json()
    assert {"file": "A", "volume": 1, "position": 2, "readable": False} in lib["copies"]
    # 坏副本不再占用卷1；A 剩余两份可读副本占用卷2、卷3
    assert lib["occupied_volumes"] == [2, 3]
    assert request_removal(1).status_code == 201  # 卷1 已无待取副本占用
    # A 仍待取，卷2 被占用 → 不能下架
    assert request_removal(2).status_code == 409
    assert request_removal(2).json()["detail"]["occupied_volumes"] == [2]
    # 取回 A 后卷2/卷3 释放；A 仍有卷3 可读副本，卷2 下架安全
    assert report("A", 2, 5, "ok").status_code == 200
    assert client.get("/library").json()["occupied_volumes"] == []
    assert request_removal(2).status_code == 201


def test_bad_copy_does_not_count_as_last_readable_copy():
    register()
    start_job(["C"])
    # 计划先用卷2 的副本（卷号小）；读坏它
    body = report("C", 2, 1, "failed").json()
    assert body["state"]["current_volume"] == 2
    # C 最后一份在线可读副本在卷3；卷2 上已无 C 的可读副本，不构成“最后一份”
    assert request_removal(2).status_code == 201
    # 取回 C（用卷3 的副本）
    nxt = client.get("/plan").json()["next_step"]
    assert (nxt["file"], nxt["volume"], nxt["position"]) == ("C", 3, 4)
    assert report("C", 3, 4, "ok").status_code == 200


def test_removal_of_current_volume_resets_drive_state():
    register([
        {"file": "A", "volume": 1, "position": 2},
        {"file": "A", "volume": 2, "position": 5},
        {"file": "B", "volume": 2, "position": 6},
    ])
    start_job(["A"])
    assert report("A", 1, 2, "ok").status_code == 200
    state = client.get("/state").json()
    assert state["current_volume"] == 1 and state["done"] == ["A"]
    # A 已取回，卷1 不再被占用；下架当前挂载卷
    assert request_removal(1).status_code == 201
    state = client.get("/state").json()
    assert state["current_volume"] is None and state["head"] == 0
    lib = client.get("/library").json()
    assert lib["events"][-1] == {
        "seq": lib["events"][-1]["seq"],
        "action": "drive_reset",
        "volume": 1,
        "removal_id": 1,
    }


def test_job_void_releases_occupancy():
    register([
        {"file": "A", "volume": 1, "position": 1},
        {"file": "A", "volume": 2, "position": 1},
    ])
    start_job(["A"])
    assert request_removal(1).status_code == 409
    client.delete("/job")
    assert request_removal(1).status_code == 201


def test_return_during_job_does_not_occupy_back():
    """归还卷的副本不会补进已开工作业，因此该卷可再次下架。"""
    register()
    assert request_removal(2).status_code == 201
    start_job(["A", "B"])  # A、B 都只剩卷1 的副本
    assert client.get("/state").json()["occupied_volumes"] == [1]
    assert return_volume(1, 2).status_code == 200
    assert client.get("/state").json()["occupied_volumes"] == [1]
    assert request_removal(2).status_code == 201


def test_interleaved_removal_and_job_start_are_atomic():
    """并发审批/开工：结果只能落在“下架成功→作业不含该卷副本”或
    “作业先开工→占用该卷→下架退回”两种串行结果之一。"""
    errors = []
    for _ in range(20):
        store.reset_all()
        client.post("/library", json={"copies": [
            {"file": "A", "volume": 1, "position": 1},
            {"file": "A", "volume": 2, "position": 1},
        ]})
        barrier = threading.Barrier(2)

        def do_remove():
            barrier.wait()
            request_removal(1)

        def do_start():
            barrier.wait()
            client.post("/jobs", json={"files": ["A"]})

        t1 = threading.Thread(target=do_remove)
        t2 = threading.Thread(target=do_start)
        t1.start(); t2.start(); t1.join(); t2.join()

        lib = client.get("/library").json()
        vol1 = volume_map(lib)[1]
        state = client.get("/state").json()
        if not vol1["online"]:
            if 1 in state["occupied_volumes"]:
                errors.append("下架成功的卷仍被作业占用")
            # 作业只能看到卷2 的副本
            assert state["occupied_volumes"] == [2]
        else:
            if 1 not in state["occupied_volumes"]:
                errors.append("作业先开工却未占用卷1")
    assert errors == []


def test_interleaved_return_and_removal_keep_last_copy():
    """并发归还/重复下架后，任何文件至少保留一份在线可读副本。"""
    for _ in range(20):
        store.reset_all()
        client.post("/library", json={"copies": [
            {"file": "A", "volume": 1, "position": 1},
            {"file": "A", "volume": 2, "position": 1},
            {"file": "B", "volume": 2, "position": 2},
        ]})
        assert request_removal(1).status_code == 201  # A 仍有卷2；B 在卷2
        barrier = threading.Barrier(2)

        def do_return():
            barrier.wait()
            return_volume(1, 1)

        def do_remove():
            barrier.wait()
            request_removal(1)  # 若归还先完成则重新下架成功；否则 409

        t1 = threading.Thread(target=do_return)
        t2 = threading.Thread(target=do_remove)
        t1.start(); t2.start(); t1.join(); t2.join()

        lib = client.get("/library").json()
        online_readable = {
            (c["file"], c["volume"])
            for c in lib["copies"]
            if c["readable"] and volume_map(lib)[c["volume"]]["online"]
        }
        for f in {c["file"] for c in lib["copies"]}:
            assert any(name == f for name, _ in online_readable), f
        # 下架申请记录与卷状态保持一致：在线卷不属于任何 open 申请
        for v in lib["volumes"]:
            if v["online"]:
                assert v["removal_id"] is None


def test_interleaved_receipt_failure_and_removal_are_atomic():
    """并发：读坏回执与“下架该卷”交错。

    两者在同一把锁内完成，因此结果只能是：
    - 下架先成功（A 在卷3 仍可读）→ 作业里不再有卷2 的可用副本；
    - 回执先成功 → 卷2 上 A 的副本已不可读、不再占用，可被下架；
    - 都发生但顺序相反 → 下架被占用退回，随后回执把副本标坏。
    任何顺序下：卷2 离线 ⇒ 它的 A 副本在作业中已停用。
    """
    errors = []
    for _ in range(20):
        store.reset_all()
        client.post("/library", json={"copies": [
            {"file": "A", "volume": 2, "position": 3},
            {"file": "A", "volume": 3, "position": 4},
        ]})
        client.post("/jobs", json={"files": ["A"]})
        barrier = threading.Barrier(2)

        def do_fail():
            barrier.wait()
            report("A", 2, 3, "failed")

        def do_remove():
            barrier.wait()
            request_removal(2)

        t1 = threading.Thread(target=do_fail)
        t2 = threading.Thread(target=do_remove)
        t1.start(); t2.start(); t1.join(); t2.join()

        lib = client.get("/library").json()
        state = client.get("/state").json()
        vol2 = volume_map(lib)[2]
        disabled = {(c["file"], c["volume"]) for c in state["disabled_copies"]}
        if not vol2["online"]:
            if ("A", 2) not in disabled:
                errors.append("卷2 已下架但作业中副本仍可用")
            if 2 in state["occupied_volumes"]:
                errors.append("卷2 已下架但被作业占用")
        # A 在卷3 的副本仍可读，且一直待取/可完成
        assert client.get("/plan").json()["next_step"]["volume"] == 3
    assert errors == []


def test_return_by_volume_without_removal_id():
    register([
        {"file": "A", "volume": 1, "position": 1},
        {"file": "A", "volume": 2, "position": 1},
        {"file": "A", "volume": 3, "position": 1},
    ])
    assert request_removal(1, 2).status_code == 201
    lib = client.post("/returns", json={"volume": 2}).json()
    assert {v["volume"]: v["online"] for v in lib["volumes"]} == {
        1: False, 2: True, 3: True,
    }
    assert lib["removals"][0]["returned"] == [2]
    # 未在送检的卷 → 409；未知卷 → 422
    assert client.post("/returns", json={"volume": 3}).status_code == 409
    assert client.post("/returns", json={"volume": 9}).status_code == 422
    assert client.post("/returns", json={"volume": 1}).json()["removals"][0]["state"] == "closed"
