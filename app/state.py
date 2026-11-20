"""作业状态：文件清单、副本目录、带头位置，以及回执的应用。

约定：
- 只有“当前计划第一步（第一个 read 动作）”的回执才会被接受并推进进度；
  其余回执一律拒绝（MismatchError），不更新任何状态。
- 无论读带成功还是失败，带头都推进到该步骤的卷内序号；
  若该步骤在另一卷上，说明装带已发生，当前卷随之切换。
- 失败的副本立即停用，不再参与后续规划。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .planner import Copy, Plan, Step, build_plan


class NoJobError(Exception):
    """尚未创建取回作业。"""


class NoPendingStepError(Exception):
    """当前计划没有可回报的读带步骤（已全部完成或全部无副本可读）。"""


class MismatchError(Exception):
    """回执与当前计划第一步不符。"""

    def __init__(self, expected: Step):
        super().__init__("receipt does not match the first step of the current plan")
        self.expected = expected


@dataclass
class CopyRecord:
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
    files: list[str]
    copies: list[CopyRecord]
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


class JobStore:
    """内存作业仓库：同一时刻只处理一个取回作业。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: Job | None = None

    def create(self, files: list[str], copies: list[CopyRecord]) -> Job:
        with self._lock:
            self._job = Job(files=files, copies=copies)
            return self._job

    def get(self) -> Job:
        with self._lock:
            if self._job is None:
                raise NoJobError
            return self._job

    def reset(self) -> bool:
        with self._lock:
            existed = self._job is not None
            self._job = None
            return existed

    def apply_receipt(self, file: str, volume: int, position: int, result: str) -> tuple[Job, Plan, Plan]:
        """应用一张回执，返回 (作业, 执行前计划, 重排后计划)。"""
        with self._lock:
            if self._job is None:
                raise NoJobError
            job = self._job
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
                    if c.file == file and c.volume == volume and c.position == position:
                        c.enabled = False  # 停用出错的副本
                        break
                job.reads_failed += 1

            job.history.append(
                ReceiptEvent(
                    seq=len(job.history) + 1,
                    file=file,
                    volume=volume,
                    position=position,
                    result=result,
                )
            )
            return job, before, job.plan()


store = JobStore()
