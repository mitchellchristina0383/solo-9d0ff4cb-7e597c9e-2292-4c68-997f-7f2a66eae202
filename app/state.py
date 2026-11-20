"""全库副本目录 + 取回作业的统一状态仓库。

设计要点（与值班室约定一致）：

- **一个仓库、一把锁**：登记目录、下架审批、归还、开工、回执、作废都在同一把
  可重入锁内完成“校验 + 写入 + 生成返回快照”。因此这些操作无论怎样交错，
  都等价于某一个串行顺序，``最后一份可读副本`` 的核算与卷的离线状态不会出现
  半更新。
- **目录是副本的唯一来源**：作业开工时从库目录快照待取文件当时在线且未报坏的
  全部副本；之后归还的卷不会补进已开工的作业。
- **作业占用**：待取文件的每份可读（enabled）副本占用所在卷，直到文件取回、
  该副本报坏或作业作废。占用中的卷不得下架。
- **坏副本两处生效**：回执 ``failed`` 时既停用作业内副本，也把库目录中的同一
  副本永久标为不可读（重新登记目录前一直保留）。
- **最后一份可读副本**：下架申请若让某文件在“在线且未报坏”的副本集合变为空，
  整批退回，不改变任何卷的状态。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .planner import MAX_VOLUMES, Copy, Plan, Step, build_plan


# ---------- 错误 ----------

class NoJobError(Exception):
    """尚未创建取回作业。"""


class NoPendingStepError(Exception):
    """当前计划没有可回报的读带步骤（已全部完成或全部无副本可读）。"""


class MismatchError(Exception):
    """回执与当前计划第一步不符。"""

    def __init__(self, expected: Step):
        super().__init__("receipt does not match the first step of the current plan")
        self.expected = expected


class NoLibraryError(Exception):
    """尚未登记全库副本目录。"""


class JobExistsError(Exception):
    """已有进行中的取回作业。"""


class LibraryBusyError(Exception):
    """目录被作业或送检卷占用，不能清除。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class InvalidCatalogError(Exception):
    """目录内容不合法。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class InvalidJobError(Exception):
    """作业入参不合法。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class UnknownFilesError(Exception):
    """作业中包含库目录里没有的文件。"""

    def __init__(self, files: list[str]):
        super().__init__(f"unknown files: {files}")
        self.files = files


class UnknownVolumesError(Exception):
    """申请/归还中出现了库目录里没有的卷。"""

    def __init__(self, volumes: list[int]):
        super().__init__(f"unknown volumes: {volumes}")
        self.volumes = volumes


class DuplicateVolumesError(Exception):
    """同一申请里重复列出卷。"""

    def __init__(self, volumes: list[int]):
        super().__init__(f"duplicate volumes: {volumes}")
        self.volumes = volumes


class VolumeNotOfflineError(Exception):
    """要归还的卷不在送检（离线）状态。"""

    def __init__(self, volume: int):
        super().__init__(f"volume {volume} is online")
        self.volume = volume


class NoRemovalError(Exception):
    """下架申请不存在。"""

    def __init__(self, removal_id: int):
        super().__init__(f"removal {removal_id} not found")
        self.removal_id = removal_id


class VolumeNotInRemovalError(Exception):
    """卷不在指定的下架申请中。"""

    def __init__(self, removal_id: int, volume: int):
        super().__init__(f"volume {volume} not in removal {removal_id}")
        self.removal_id = removal_id
        self.volume = volume


class RemovalConflict(Exception):
    """整批下架申请被退回（未改变任何卷的状态）。"""

    def __init__(
        self,
        affected_files: list[str],
        occupied_volumes: list[int],
        occupied_files: list[str],
        already_offline: list[int],
    ):
        super().__init__("removal request rejected")
        self.affected_files = affected_files
        self.occupied_volumes = occupied_volumes
        self.occupied_files = occupied_files
        self.already_offline = already_offline


# ---------- 内部记录 ----------

@dataclass
class CatalogCopy:
    file: str
    volume: int
    position: int
    readable: bool = True


@dataclass
class RemovalRecord:
    id: int
    volumes: list[int]
    returned: list[int] = field(default_factory=list)

    @property
    def state(self) -> str:
        return "closed" if set(self.volumes) <= set(self.returned) else "open"


@dataclass
class Catalog:
    copies: list[CatalogCopy]
    volumes: list[int]
    offline: dict[int, int] = field(default_factory=dict)  # 卷号 -> 下架申请 id
    removals: list[RemovalRecord] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    event_seq: int = 0

    @property
    def files(self) -> list[str]:
        return sorted({c.file for c in self.copies})

    def add_event(self, action: str, **detail) -> None:
        self.event_seq += 1
        self.events.append({"seq": self.event_seq, "action": action, **detail})


@dataclass
class JobCopy:
    file: str
    volume: int
    position: int
    enabled: bool = True


@dataclass
class ReceiptEvent:
    seq: int
    file: str
    volume: int
    position: int
    result: str  # "ok" | "failed"


@dataclass
class Job:
    id: int
    files: list[str]
    copies: list[JobCopy]
    done: set[str] = field(default_factory=set)
    current_volume: int | None = None  # 当前已挂载的卷（初始为空机）
    head: int = 0  # 带头所在的卷内序号
    mounts_done: int = 0
    rewinds_done: int = 0
    reads_ok: int = 0
    reads_failed: int = 0
    history: list[ReceiptEvent] = field(default_factory=list)

    def enabled_copies(self) -> dict[str, list[Copy]]:
        result: dict[str, list[Copy]] = {}
        for c in self.copies:
            if c.enabled:
                result.setdefault(c.file, []).append(Copy(c.file, c.volume, c.position))
        return result

    def pending(self) -> list[str]:
        return [f for f in self.files if f not in self.done]

    def unretrievable(self) -> list[str]:
        enabled = self.enabled_copies()
        return sorted(f for f in self.pending() if not enabled.get(f))

    def plan(self) -> Plan:
        return build_plan(self.pending(), self.enabled_copies(), self.current_volume, self.head)


# ---------- 只读快照（在锁内生成，调用方可安全序列化） ----------

@dataclass(frozen=True)
class VolumeSnapshot:
    volume: int
    online: bool
    removal_id: int | None


@dataclass(frozen=True)
class CatalogCopySnapshot:
    file: str
    volume: int
    position: int
    readable: bool


@dataclass(frozen=True)
class RemovalSnapshot:
    id: int
    volumes: tuple[int, ...]
    state: str
    returned: tuple[int, ...]


@dataclass(frozen=True)
class LibrarySnapshot:
    files: tuple[str, ...]
    volumes: tuple[VolumeSnapshot, ...]
    copies: tuple[CatalogCopySnapshot, ...]
    removals: tuple[RemovalSnapshot, ...]
    events: tuple[dict, ...]
    occupied_volumes: tuple[int, ...]
    active_job_id: int | None


@dataclass(frozen=True)
class JobStateSnapshot:
    job_id: int
    files: tuple[str, ...]
    current_volume: int | None
    head: int
    pending: tuple[str, ...]
    done: tuple[str, ...]
    unretrievable: tuple[str, ...]
    disabled_copies: tuple[dict, ...]
    occupied_volumes: tuple[int, ...]
    executed: dict[str, int]
    history: tuple[dict, ...]


# ---------- 仓库 ----------

class JobStore:
    """内存仓库：一份全库副本目录 + 至多一个取回作业。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._library: Catalog | None = None
        self._job: Job | None = None
        self._job_seq = 0
        self._removal_seq = 0

    # ----- 内部工具（调用方必须已持锁） -----

    def _require_library(self) -> Catalog:
        if self._library is None:
            raise NoLibraryError
        return self._library

    def _require_job(self) -> Job:
        if self._job is None:
            raise NoJobError
        return self._job

    def _occupancy(self, job: Job | None = None) -> tuple[set[int], dict[int, set[str]]]:
        """返回 (被占用的卷, 卷 -> 占用它的待取文件)。

        只有“仍待取”的文件的“仍可读（enabled）”副本才占用卷；
        文件取回、副本报坏、作业作废都会立即释放。
        """
        job = job if job is not None else self._job
        if job is None:
            return set(), {}
        pending = set(job.pending())
        by_volume: dict[int, set[str]] = {}
        for c in job.copies:
            if c.enabled and c.file in pending:
                by_volume.setdefault(c.volume, set()).add(c.file)
        return set(by_volume), by_volume

    def _library_snapshot(self) -> LibrarySnapshot:
        lib = self._require_library()
        occupied, _ = self._occupancy()
        copies = sorted(lib.copies, key=lambda c: (c.file, c.volume, c.position))
        return LibrarySnapshot(
            files=tuple(lib.files),
            volumes=tuple(
                VolumeSnapshot(
                    volume=v,
                    online=v not in lib.offline,
                    removal_id=lib.offline.get(v),
                )
                for v in lib.volumes
            ),
            copies=tuple(
                CatalogCopySnapshot(c.file, c.volume, c.position, c.readable)
                for c in copies
            ),
            removals=tuple(
                RemovalSnapshot(r.id, tuple(r.volumes), r.state, tuple(r.returned))
                for r in lib.removals
            ),
            events=tuple(dict(e) for e in lib.events),
            occupied_volumes=tuple(sorted(occupied)),
            active_job_id=self._job.id if self._job is not None else None,
        )

    def _job_snapshot(self, job: Job | None = None) -> JobStateSnapshot:
        job = job if job is not None else self._require_job()
        occupied, _ = self._occupancy(job)
        return JobStateSnapshot(
            job_id=job.id,
            files=tuple(job.files),
            current_volume=job.current_volume,
            head=job.head,
            pending=tuple(job.pending()),
            done=tuple(sorted(job.done)),
            unretrievable=tuple(job.unretrievable()),
            disabled_copies=tuple(
                {"file": c.file, "volume": c.volume, "position": c.position}
                for c in job.copies
                if not c.enabled
            ),
            occupied_volumes=tuple(sorted(occupied)),
            executed={
                "mounts": job.mounts_done,
                "rewinds": job.rewinds_done,
                "reads_ok": job.reads_ok,
                "reads_failed": job.reads_failed,
            },
            history=tuple(dict(vars(e)) for e in job.history),
        )

    # ----- 库目录 -----

    def register_library(self, copies: list[CatalogCopy]) -> LibrarySnapshot:
        """登记全库副本目录（至多 12 卷）。

        允许在“无作业、无送检卷”时重新登记（整体替换）；有作业或卷仍送检时
        拒绝，避免目录与作业快照/卷状态脱节。重新登记时，同一 (文件, 卷, 序号)
        的“读坏”标记会保留。
        """
        with self._lock:
            if self._library is not None:
                if self._job is not None:
                    raise LibraryBusyError("取回作业尚未作废（DELETE /job）")
                if self._library.offline:
                    volumes = sorted(self._library.offline)
                    raise LibraryBusyError(f"仍有送检卷未归还: {volumes}")

            seen: set[tuple[str, int, int]] = set()
            for c in copies:
                if not c.file:
                    raise InvalidCatalogError("文件名不能为空")
                key = (c.file, c.volume, c.position)
                if key in seen:
                    raise InvalidCatalogError(
                        f"重复副本: file={c.file} volume={c.volume} position={c.position}"
                    )
                seen.add(key)
            volumes = sorted({c.volume for c in copies})
            if len(volumes) > MAX_VOLUMES:
                raise InvalidCatalogError(
                    f"目录涉及 {len(volumes)} 卷，超过上限 {MAX_VOLUMES} 卷"
                )

            bad = set()
            if self._library is not None:
                bad = {
                    (c.file, c.volume, c.position)
                    for c in self._library.copies
                    if not c.readable
                }
            ordered = sorted(copies, key=lambda c: (c.file, c.volume, c.position))
            for c in ordered:
                if (c.file, c.volume, c.position) in bad:
                    c.readable = False

            if self._library is None:
                lib = Catalog(copies=ordered, volumes=volumes)
            else:
                lib = self._library  # 保留事件流、下架记录与编号（均须已全部归还）
                lib.copies = ordered
                lib.volumes = volumes
                lib.offline = {}
            lib.add_event("register", volumes=volumes, copies=len(ordered))
            self._library = lib
            return self._library_snapshot()

    def get_library(self) -> LibrarySnapshot:
        with self._lock:
            return self._library_snapshot()

    def delete_library(self) -> None:
        """清除目录；有作业或仍有送检卷时拒绝。"""
        with self._lock:
            lib = self._require_library()
            if self._job is not None:
                raise LibraryBusyError("取回作业尚未作废（DELETE /job）")
            if lib.offline:
                volumes = sorted(lib.offline)
                raise LibraryBusyError(f"仍有送检卷未归还: {volumes}")
            self._library = None

    def request_removal(self, volumes: list[int]) -> tuple[RemovalSnapshot, LibrarySnapshot]:
        """申请整批下架。

        通过则所有卷立即离线并返回 (下架记录, 目录快照)；
        任一条件不满足则抛 ``RemovalConflict``，**整批退回、不改变任何状态**。
        """
        with self._lock:
            lib = self._require_library()
            if not volumes:
                raise InvalidCatalogError("下架申请至少包含一卷")
            if len(set(volumes)) != len(volumes):
                duplicated = sorted({v for v in volumes if volumes.count(v) > 1})
                raise DuplicateVolumesError(duplicated)
            unknown = sorted(v for v in volumes if v not in lib.volumes)
            if unknown:
                raise UnknownVolumesError(unknown)

            requested = set(volumes)
            already_offline = sorted(v for v in volumes if v in lib.offline)

            occupied, by_volume = self._occupancy()
            occupied_hit = sorted(requested & occupied)
            occupied_files = sorted(
                {f for v in occupied_hit for f in by_volume.get(v, set())}
            )

            # “可读副本” = 当前在库（在线）且未报坏的副本。送检离线的副本无法在
            # 库内阅读，不算该文件此刻的“可读副本”；读坏的副本已永久标为不可读。
            # 两者都不计入“最后一份”，与开工快照“在线且未报坏”的口径一致。
            readable_by_file: dict[str, set[int]] = {}
            for c in lib.copies:
                if c.readable and c.volume not in lib.offline:
                    readable_by_file.setdefault(c.file, set()).add(c.volume)

            affected = sorted(
                f
                for f, vols in readable_by_file.items()
                if vols and vols <= requested
            )

            if already_offline or occupied_hit or affected:
                raise RemovalConflict(
                    affected_files=affected,
                    occupied_volumes=occupied_hit,
                    occupied_files=occupied_files,
                    already_offline=already_offline,
                )

            self._removal_seq += 1
            record = RemovalRecord(id=self._removal_seq, volumes=sorted(volumes))
            lib.removals.append(record)
            for v in record.volumes:
                lib.offline[v] = record.id
            lib.add_event("remove", volumes=record.volumes, removal_id=record.id)

            # 若当前挂载卷被下架：驱动器已卸带，带头状态一并复位，
            # 避免后续计划把“不在库里的卷”误当成免装带的当前卷。
            if self._job is not None and self._job.current_volume in requested:
                reset_volume = self._job.current_volume
                self._job.current_volume = None
                self._job.head = 0
                lib.add_event(
                    "drive_reset",
                    volume=reset_volume,
                    removal_id=record.id,
                )

            return (
                RemovalSnapshot(
                    record.id, tuple(record.volumes), record.state, tuple(record.returned)
                ),
                self._library_snapshot(),
            )

    def return_volume(self, removal_id: int, volume: int) -> LibrarySnapshot:
        """逐卷归还送检卷（校验与写入在同一把锁内完成）。"""
        with self._lock:
            lib = self._require_library()
            record = next((r for r in lib.removals if r.id == removal_id), None)
            if record is None:
                raise NoRemovalError(removal_id)
            if volume not in record.volumes:
                raise VolumeNotInRemovalError(removal_id, volume)
            return self._return_locked(lib, record, volume)

    def return_any(self, volume: int) -> LibrarySnapshot:
        """按卷号归还（自动找到该卷所属的进行中下架申请）。"""
        with self._lock:
            lib = self._require_library()
            if volume not in lib.volumes:
                raise UnknownVolumesError([volume])
            removal_id = lib.offline.get(volume)
            if removal_id is None:
                raise VolumeNotOfflineError(volume)
            record = next(r for r in lib.removals if r.id == removal_id)
            return self._return_locked(lib, record, volume)

    def _return_locked(
        self, lib: Catalog, record: RemovalRecord, volume: int
    ) -> LibrarySnapshot:
        if lib.offline.get(volume) != record.id:
            raise VolumeNotOfflineError(volume)
        del lib.offline[volume]
        record.returned.append(volume)
        lib.add_event("return", volumes=[volume], removal_id=record.id)
        return self._library_snapshot()

    # ----- 取回作业 -----

    def start_job(self, files: list[str]) -> tuple[JobStateSnapshot, Plan]:
        """开工：从库目录快照待取文件当时在线且未报坏的全部副本。"""
        with self._lock:
            lib = self._require_library()
            if self._job is not None:
                raise JobExistsError

            normalized = [f.strip() for f in files]
            if any(not f for f in normalized):
                raise InvalidJobError("文件名不能为空")
            if len(set(normalized)) != len(normalized):
                raise InvalidJobError("文件清单存在重名")

            known = set(lib.files)
            unknown = sorted(f for f in normalized if f not in known)
            if unknown:
                raise UnknownFilesError(unknown)

            wanted = set(normalized)
            copies = [
                JobCopy(c.file, c.volume, c.position)
                for c in sorted(lib.copies, key=lambda c: (c.file, c.volume, c.position))
                if c.file in wanted and c.readable and c.volume not in lib.offline
            ]

            self._job_seq += 1
            job = Job(id=self._job_seq, files=normalized, copies=copies)
            self._job = job
            return self._job_snapshot(job), job.plan()

    def get_state(self) -> JobStateSnapshot:
        with self._lock:
            return self._job_snapshot()

    def get_plan(self) -> Plan:
        with self._lock:
            return self._require_job().plan()

    def reset_job(self) -> bool:
        """作废当前作业；库目录与坏副本标记保留。"""
        with self._lock:
            existed = self._job is not None
            self._job = None
            return existed

    def apply_receipt(
        self, file: str, volume: int, position: int, result: str
    ) -> tuple[JobStateSnapshot, Plan, Plan]:
        """应用一张回执，返回 (作业快照, 执行前计划, 重排后计划)。"""
        with self._lock:
            job = self._require_job()
            before = job.plan()
            step = before.next_read
            if step is None:
                raise NoPendingStepError
            if (step.file, step.volume, step.position) != (file, volume, position):
                raise MismatchError(step)

            # 该读带步骤之前的装带/回卷动作随回执一并确认
            for s in before.steps:
                if s.action == "read":
                    break
                if s.action == "mount":
                    job.mounts_done += 1
                elif s.action == "rewind":
                    job.rewinds_done += 1

            # 成功或失败都推进带头；跨卷步骤意味着装带已发生
            job.current_volume = volume
            job.head = position

            if result == "ok":
                job.done.add(file)
                job.reads_ok += 1
            else:
                for c in job.copies:
                    if (
                        c.enabled
                        and c.file == file
                        and c.volume == volume
                        and c.position == position
                    ):
                        c.enabled = False  # 停用作业内副本
                        break
                job.reads_failed += 1
                # 读坏的副本在库目录里也永久标为不可读
                lib = self._library
                if lib is not None:
                    for c in lib.copies:
                        if (c.file, c.volume, c.position) == (file, volume, position):
                            c.readable = False

            job.history.append(
                ReceiptEvent(
                    seq=len(job.history) + 1,
                    file=file,
                    volume=volume,
                    position=position,
                    result=result,
                )
            )
            return self._job_snapshot(job), before, job.plan()

    # ----- 仅供测试/进程内复位 -----

    def reset_all(self) -> None:
        with self._lock:
            self._library = None
            self._job = None
            self._job_seq = 0
            self._removal_seq = 0


store = JobStore()
