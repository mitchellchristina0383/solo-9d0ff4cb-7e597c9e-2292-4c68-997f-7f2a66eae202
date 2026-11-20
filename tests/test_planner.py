"""规划器单元测试：最优性、回卷、平局裁决、边界情况。"""
from app.planner import Copy, build_plan


def make_copies(*triples: tuple[str, int, int]) -> dict[str, list[Copy]]:
    result: dict[str, list[Copy]] = {}
    for f, v, p in triples:
        result.setdefault(f, []).append(Copy(f, v, p))
    return result


def reads(plan):
    return [(s.file, s.volume, s.position) for s in plan.steps if s.action == "read"]


def test_demo_initial_plan_two_mounts_no_rewind():
    copies = make_copies(
        ("A", 1, 2), ("A", 2, 5),
        ("B", 1, 6),
        ("C", 2, 1), ("C", 3, 4),
        ("D", 3, 1),
        ("E", 3, 6),
        ("F", 1, 9), ("F", 3, 8),
    )
    plan = build_plan(list("ABCDEF"), copies, current_volume=None, head=0)
    assert plan.cost == 2
    assert plan.mounts == 2 and plan.rewinds == 0
    assert plan.volumes_used == [1, 3]
    # 卷内按序号升序
    assert reads(plan) == [
        ("A", 1, 2), ("B", 1, 6), ("F", 1, 9),
        ("D", 3, 1), ("C", 3, 4), ("E", 3, 6),
    ]
    assert plan.unretrievable == []


def test_rewind_when_needed_position_behind_head():
    copies = make_copies(("X", 1, 3), ("Y", 1, 12))
    plan = build_plan(["X", "Y"], copies, current_volume=1, head=9)
    assert plan.mounts == 0 and plan.rewinds == 1
    actions = [(s.action, s.file, s.position) for s in plan.steps]
    assert actions == [
        ("read", "Y", 12),   # 先读带头之后的
        ("rewind", None, None),
        ("read", "X", 3),    # 回卷后再读带头之前的
    ]


def test_tie_prefers_rewind_over_extra_mount():
    # X 既可回卷在当前卷读，也可换卷读：代价都是 1，平局时选装带更少（即回卷）
    copies = make_copies(("X", 1, 3), ("X", 2, 1))
    plan = build_plan(["X"], copies, current_volume=1, head=9)
    assert plan.cost == 1
    assert plan.mounts == 0 and plan.rewinds == 1
    assert reads(plan) == [("X", 1, 3)]


def test_extra_mount_when_it_strictly_wins():
    # 当前卷上要回卷 2 处？不——每卷至多回卷 1 次；这里验证：
    # 当前卷读要 1 次回卷，换卷要 1 次装带，若另一文件也只能换卷读，则合算
    copies = make_copies(("X", 1, 3), ("X", 2, 1), ("Y", 2, 5))
    plan = build_plan(["X", "Y"], copies, current_volume=1, head=9)
    # 方案一：卷1回卷读X + 装卷2读Y = 2；方案二：装卷2读X、Y = 1
    assert plan.cost == 1
    assert plan.mounts == 1 and plan.rewinds == 0
    assert reads(plan) == [("X", 2, 1), ("Y", 2, 5)]


def test_unretrievable_files_are_listed_and_skipped():
    copies = make_copies(("Y", 1, 1))
    plan = build_plan(["X", "Y"], copies, current_volume=None, head=0)
    assert plan.unretrievable == ["X"]
    assert reads(plan) == [("Y", 1, 1)]


def test_empty_pending_gives_empty_plan():
    plan = build_plan([], {}, current_volume=3, head=5)
    assert plan.steps == [] and plan.cost == 0
    assert plan.next_read is None


def test_twelve_volumes_exact_cover():
    triples = [(f"F{i}", i, 1) for i in range(1, 13)]
    copies = make_copies(*triples)
    plan = build_plan([f"F{i}" for i in range(1, 13)], copies, None, 0)
    assert plan.mounts == 12 and plan.rewinds == 0


def test_single_volume_beats_scattered_copies():
    # 每个文件在 12 卷上都有副本：1 次装带足矣
    triples = [(f, v, p) for f, p in [("A", 1), ("B", 2)] for v in range(1, 13)]
    copies = make_copies(*triples)
    plan = build_plan(["A", "B"], copies, None, 0)
    assert plan.cost == 1
    assert plan.volumes_used == [1]


def test_current_volume_free_mount():
    copies = make_copies(("A", 2, 4), ("B", 2, 7))
    plan = build_plan(["A", "B"], copies, current_volume=2, head=1)
    assert plan.mounts == 0 and plan.rewinds == 0
    assert reads(plan) == [("A", 2, 4), ("B", 2, 7)]
