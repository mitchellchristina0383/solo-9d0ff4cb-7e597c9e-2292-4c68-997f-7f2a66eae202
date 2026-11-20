"""请求/响应模型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .planner import MAX_VOLUMES


class CopyIn(BaseModel):
    file: str = Field(min_length=1, description="待取文件名")
    volume: int = Field(ge=1, description="卷号")
    position: int = Field(ge=1, description="卷内序号")

    model_config = {"json_schema_extra": {"examples": [{"file": "A", "volume": 1, "position": 2}]}}


class JobIn(BaseModel):
    files: list[str] = Field(min_length=1, description="待取文件清单")
    copies: list[CopyIn] = Field(description=f"副本目录（不同卷号至多 {MAX_VOLUMES} 个）")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
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
            ]
        }
    }


class ReceiptIn(BaseModel):
    """值班员回报：必须对应当前计划的第一个读带步骤。"""

    file: str = Field(min_length=1)
    volume: int = Field(ge=1)
    position: int = Field(ge=1)
    result: Literal["ok", "failed"] = Field(description="ok=读取成功；failed=读带失败（带头照样推进，副本停用）")

    model_config = {
        "json_schema_extra": {
            "examples": [{"file": "A", "volume": 1, "position": 2, "result": "ok"}]
        }
    }


class StepOut(BaseModel):
    seq: int
    action: str = Field(description="mount=装带 / rewind=回卷 / read=读带")
    volume: int | None = None
    file: str | None = None
    position: int | None = None


class PlanOut(BaseModel):
    volumes_used: list[int]
    mounts: int = Field(description="本计划装带次数")
    rewinds: int = Field(description="本计划回卷次数")
    cost: int = Field(description="装带 + 回卷总次数（已最小化）")
    next_step: StepOut | None = Field(description="下一个要回报的读带步骤")
    steps: list[StepOut]
    unretrievable: list[str] = Field(description="已无任何可用副本的待取文件")


class StateOut(BaseModel):
    current_volume: int | None = Field(description="当前已挂载的卷号（空机为 null）")
    head: int = Field(description="带头当前卷内序号")
    pending: list[str]
    done: list[str]
    unretrievable: list[str]
    disabled_copies: list[CopyIn]
    executed: dict[str, int] = Field(description="已确认的装带/回卷/成功/失败次数")
    history: list[dict]


class JobOut(BaseModel):
    state: StateOut
    plan: PlanOut


class ReceiptOut(BaseModel):
    applied: dict
    state: StateOut
    plan: PlanOut = Field(description="按带头新位置重排后的计划")
