"""读取顺序规划器：在“装带次数 + 回卷次数”最少的约束下给出读取顺序。

代价模型（与值班室约定一致）：
- 装带：挂载一卷计 1 次；当前已挂载的卷继续读不计装带。
- 装带后带头在卷首（序号 0 之前），只能向序号增大的方向读。
- 回卷：在当前卷上需要读取比带头位置更小的序号时，必须回卷 1 次，
  回卷后带头回到卷首，再按序号升序读。
- 同一卷内按序号升序读取不需要回卷；因此每卷至多回卷 1 次，
  只有“当前已挂载卷”可能被迫回卷（其余卷从卷首开始升序读即可）。

算法：卷数最多 12，枚举所有非空卷子集（至多 4095 个），
对每个能覆盖全部待取文件的子集计算代价，取代价最小者。
平局时依次按：装带更少、用卷更少、子集位掩码更小（保证确定性）。
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_VOLUMES = 12


@dataclass(frozen=True)
class Copy:
    """一个可用副本：文件 file 位于卷 volume 的卷内序号 position。"""

    file: str
    volume: int
    position: int


@dataclass(frozen=True)
class Step:
    """计划中的一个动作：mount（装带）/ rewind（回卷）/ read（读带）。"""

    seq: int
    action: str
    volume: int | None = None
    file: str | None = None
    position: int | None = None


@dataclass
class Plan:
    steps: list[Step]
    volumes_used: list[int]
    mounts: int
    rewinds: int
    unretrievable: list[str]  # 已无任何可用副本的待取文件

    @property
    def cost(self) -> int:
        return self.mounts + self.rewinds

    @property
    def next_read(self) -> Step | None:
        """计划中的第一个读带步骤（值班员下一步要执行并回报的步骤）。"""
        for step in self.steps:
            if step.action == "read":
                return step
        return None


def _choose_volumes(
    targets: list[str],
    copies: dict[str, list[Copy]],
    current_volume: int | None,
    head: int,
    volumes: list[int],
) -> int:
    """枚举卷子集，返回最优子集的位掩码。"""
    n = len(volumes)
    best_key: tuple[int, int, int, int] | None = None
    best_mask = 0
    for mask in range(1, 1 << n):
        selected = {volumes[i] for i in range(n) if mask & (1 << i)}
        # 覆盖检查：每个待取文件在选定卷中至少有一个可用副本
        if not all(any(c.volume in selected for c in copies[f]) for f in targets):
            continue
        mounts = len(selected) - (1 if current_volume in selected else 0)
        rewinds = 0
        if current_volume is not None and current_volume in selected:
            # 仅当某文件在选定卷中的副本全部落在当前卷且全部低于带头时，
            # 才被迫回卷（否则总可以把它安排到别的卷或带头之后的副本上）
            for f in targets:
                in_sel = [c for c in copies[f] if c.volume in selected]
                if all(
                    c.volume == current_volume and c.position < head for c in in_sel
                ):
                    rewinds = 1
                    break
        key = (mounts + rewinds, mounts, len(selected), mask)
        if best_key is None or key < best_key:
            best_key = key
            best_mask = mask
    return best_mask


def _assign(
    file: str,
    copies: dict[str, list[Copy]],
    selected: set[int],
    current_volume: int | None,
    head: int,
) -> Copy:
    """为文件在选定卷中挑一个副本（实现上面计算出的回卷数）。"""
    candidates = [c for c in copies[file] if c.volume in selected]
    # 优先放到非当前卷（从卷首升序读，绝无回卷）
    off_current = [c for c in candidates if c.volume != current_volume]
    if off_current:
        return min(off_current, key=lambda c: (c.volume, c.position))
    # 只能放当前卷：优先带头之后（含当前位置）的副本，避免回卷
    after_head = [c for c in candidates if c.position >= head]
    if after_head:
        return min(after_head, key=lambda c: c.position)
    return min(candidates, key=lambda c: c.position)


def build_plan(
    pending: list[str],
    copies: dict[str, list[Copy]],
    current_volume: int | None,
    head: int,
) -> Plan:
    """按当前状态为剩余待取文件生成最优读取计划。

    pending: 待取文件名列表。
    copies: 每个文件当前可用（未停用）的副本列表。
    current_volume: 当前已挂载的卷号（未挂载为 None）。
    head: 带头当前所在的卷内序号。
    """
    unretrievable = sorted(f for f in pending if not copies.get(f))
    targets = [f for f in pending if copies.get(f)]
    if not targets:
        return Plan([], [], 0, 0, unretrievable)

    volumes = sorted({c.volume for f in targets for c in copies[f]})
    mask = _choose_volumes(targets, copies, current_volume, head, volumes)
    selected = {volumes[i] for i in range(len(volumes)) if mask & (1 << i)}

    by_volume: dict[int, list[Copy]] = {}
    for f in targets:
        chosen = _assign(f, copies, selected, current_volume, head)
        by_volume.setdefault(chosen.volume, []).append(chosen)

    # 当前卷最先读（避免卸下再装），其余卷按卷号升序
    ordered = sorted(by_volume, key=lambda v: (v != current_volume, v))

    steps: list[Step] = []
    seq = 1

    def emit(action: str, **kw) -> None:
        nonlocal seq
        steps.append(Step(seq=seq, action=action, **kw))
        seq += 1

    for volume in ordered:
        items = sorted(by_volume[volume], key=lambda c: (c.position, c.file))
        if volume == current_volume:
            after = [c for c in items if c.position >= head]
            before = [c for c in items if c.position < head]
            for c in after:
                emit("read", volume=volume, file=c.file, position=c.position)
            if before:
                emit("rewind", volume=volume)
                for c in before:
                    emit("read", volume=volume, file=c.file, position=c.position)
        else:
            emit("mount", volume=volume)
            for c in items:
                emit("read", volume=volume, file=c.file, position=c.position)

    mounts = sum(1 for s in steps if s.action == "mount")
    rewinds = sum(1 for s in steps if s.action == "rewind")
    return Plan(steps, ordered, mounts, rewinds, unretrievable)
