"""FastAPI 入口：全库副本目录 + 磁带机取回模拟。

对应值班室流程：
1. 送检前在 ``POST /library`` 登记全库副本目录（至多 12 卷）；
2. ``POST /removals`` 按卷申请整批下架，通过后卷立即离线；
3. ``POST /removals/{id}/returns`` 逐卷归还；
4. ``POST /jobs`` 开工取回文件，值班员逐步回报读带结果。

所有会改状态的操作都经 ``JobStore`` 的同一把锁串行执行（审批/归还/开工/回执
交错时保持一致），本层只做 HTTP 映射。
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import RedirectResponse

from .planner import MAX_VOLUMES, Plan, Step
from .schemas import (
    CatalogCopyIn,
    JobIn,
    JobOut,
    LibraryIn,
    LibraryOut,
    PlanOut,
    ReceiptIn,
    ReceiptOut,
    RemovalIn,
    RemovalOut,
    RemovalResultOut,
    ReturnIn,
    StateOut,
    StepOut,
)
from .state import (
    CatalogCopy,
    DuplicateVolumesError,
    InvalidCatalogError,
    InvalidJobError,
    JobExistsError,
    JobStateSnapshot,
    LibraryBusyError,
    LibrarySnapshot,
    MismatchError,
    NoJobError,
    NoLibraryError,
    NoPendingStepError,
    NoRemovalError,
    RemovalConflict,
    RemovalSnapshot,
    UnknownFilesError,
    UnknownVolumesError,
    VolumeNotInRemovalError,
    VolumeNotOfflineError,
    store,
)

app = FastAPI(
    title="磁带机取回模拟器",
    description=(
        f"登记全库副本目录（至多 {MAX_VOLUMES} 卷），按卷申请整批下架并逐卷归还；"
        "开工时从目录快照待取文件当时在线且未报坏的全部副本。"
        "作业中待取文件的可读副本占用所在卷，占用卷或最后一份可读副本受影响的"
        "下架申请整批退回。读坏的副本在目录中也标为不可读。"
        "读取顺序按装带 + 回卷总次数最少给出；只有当前计划第一步的回执会推进进度。"
    ),
    version="2.0.0",
)


# ---------- 序列化（入参已在锁内转为快照，这里不再触碰可变状态） ----------

def _step_out(step: Step) -> StepOut:
    return StepOut(
        seq=step.seq,
        action=step.action,
        volume=step.volume,
        file=step.file,
        position=step.position,
    )


def _plan_out(plan: Plan) -> PlanOut:
    nxt = plan.next_read
    return PlanOut(
        volumes_used=plan.volumes_used,
        mounts=plan.mounts,
        rewinds=plan.rewinds,
        cost=plan.cost,
        next_step=_step_out(nxt) if nxt else None,
        steps=[_step_out(s) for s in plan.steps],
        unretrievable=plan.unretrievable,
    )


def _state_out(job: JobStateSnapshot) -> StateOut:
    return StateOut(
        job_id=job.job_id,
        files=list(job.files),
        current_volume=job.current_volume,
        head=job.head,
        pending=list(job.pending),
        done=list(job.done),
        unretrievable=list(job.unretrievable),
        disabled_copies=[CatalogCopyIn(**c) for c in job.disabled_copies],
        occupied_volumes=list(job.occupied_volumes),
        executed=dict(job.executed),
        history=[dict(e) for e in job.history],
    )


def _removal_out(r: RemovalSnapshot) -> RemovalOut:
    return RemovalOut(
        id=r.id, volumes=list(r.volumes), state=r.state, returned=list(r.returned)
    )


def _library_out(lib: LibrarySnapshot) -> LibraryOut:
    return LibraryOut(
        files=list(lib.files),
        volumes=[
            {"volume": v.volume, "online": v.online, "removal_id": v.removal_id}
            for v in lib.volumes
        ],
        copies=[
            {
                "file": c.file,
                "volume": c.volume,
                "position": c.position,
                "readable": c.readable,
            }
            for c in lib.copies
        ],
        removals=[_removal_out(r) for r in lib.removals],
        events=[dict(e) for e in lib.events],
        occupied_volumes=list(lib.occupied_volumes),
        active_job_id=lib.active_job_id,
    )


def _library_or_404() -> LibrarySnapshot:
    try:
        return store.get_library()
    except NoLibraryError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "尚未登记全库副本目录，请先 POST /library"
        )


def _job_or_404() -> JobStateSnapshot:
    try:
        return store.get_state()
    except NoJobError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "尚未创建取回作业，请先 POST /jobs")


def _catalog_copy(c: CatalogCopyIn) -> CatalogCopy:
    return CatalogCopy(file=c.file.strip(), volume=c.volume, position=c.position)


# ---------- 库目录 ----------

@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.post(
    "/library",
    status_code=status.HTTP_201_CREATED,
    response_model=LibraryOut,
    summary="登记全库副本目录（无作业、无送检卷时可重新登记替换）",
    responses={409: {"description": "仍有作业或送检卷"}, 422: {"description": "目录不合法"}},
)
def register_library(body: LibraryIn) -> LibraryOut:
    if not body.copies:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "副本目录不能为空")
    try:
        lib = store.register_library([_catalog_copy(c) for c in body.copies])
    except LibraryBusyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.reason)
    except InvalidCatalogError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.reason)
    return _library_out(lib)


@app.get("/library", response_model=LibraryOut, summary="查看全库副本目录（卷状态、可读性、下架记录）")
def get_library() -> LibraryOut:
    return _library_out(_library_or_404())


@app.delete(
    "/library",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="清除全库副本目录（需无作业、无送检卷）",
    responses={409: {"description": "仍有作业或送检卷"}},
)
def delete_library() -> Response:
    try:
        store.delete_library()
    except NoLibraryError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "尚未登记全库副本目录")
    except LibraryBusyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- 下架与归还 ----------

@app.post(
    "/removals",
    status_code=status.HTTP_201_CREATED,
    response_model=RemovalResultOut,
    summary="按卷申请整批下架；会拿走最后一份可读副本或含占用卷时整批退回",
    responses={
        404: {"description": "尚未登记目录"},
        409: {"description": "整批退回（受影响文件、占用卷见 detail）"},
        422: {"description": "卷号不在目录中 / 重复 / 空申请"},
    },
)
def request_removal(body: RemovalIn) -> RemovalResultOut:
    try:
        removal, lib = store.request_removal(body.volumes)
    except NoLibraryError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "尚未登记全库副本目录，请先 POST /library"
        )
    except (DuplicateVolumesError, UnknownVolumesError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    except RemovalConflict as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "error": "整批下架申请已退回，卷状态未改变",
                "affected_files": exc.affected_files,
                "occupied_volumes": exc.occupied_volumes,
                "occupied_files": exc.occupied_files,
                "already_offline_volumes": exc.already_offline,
            },
        )
    return RemovalResultOut(removal=_removal_out(removal), library=_library_out(lib))


@app.post(
    "/removals/{removal_id}/returns",
    response_model=LibraryOut,
    summary="按申请归还一卷送检磁带",
    responses={
        404: {"description": "目录或下架申请不存在"},
        409: {"description": "该卷当前不在送检状态"},
        422: {"description": "卷号不在目录或不在该申请中"},
    },
)
def return_volume(removal_id: int, body: ReturnIn) -> LibraryOut:
    try:
        lib = store.return_volume(removal_id, body.volume)
    except NoLibraryError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "尚未登记全库副本目录")
    except NoRemovalError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"下架申请 {removal_id} 不存在")
    except VolumeNotInRemovalError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"卷 {body.volume} 不在下架申请 {removal_id} 中",
        )
    except VolumeNotOfflineError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"卷 {body.volume} 当前不在送检（离线）状态"
        )
    except UnknownVolumesError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    return _library_out(lib)


@app.post(
    "/returns",
    response_model=LibraryOut,
    summary="按卷号归还送检磁带（自动归属其下架申请，可逐卷、可乱序）",
    responses={
        404: {"description": "尚未登记目录"},
        409: {"description": "该卷当前不在送检状态"},
        422: {"description": "卷号不在目录中"},
    },
)
def return_any(body: ReturnIn) -> LibraryOut:
    try:
        lib = store.return_any(body.volume)
    except NoLibraryError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "尚未登记全库副本目录")
    except VolumeNotOfflineError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"卷 {body.volume} 当前不在送检（离线）状态"
        )
    except UnknownVolumesError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    return _library_out(lib)


# ---------- 作业 ----------

@app.post(
    "/jobs",
    status_code=status.HTTP_201_CREATED,
    response_model=JobOut,
    summary="开工：从库目录快照待取文件当时在线且未报坏的全部副本，返回最优计划",
    responses={
        404: {"description": "尚未登记目录"},
        409: {"description": "已有进行中的作业"},
        422: {"description": "清单为空/重名，或含目录中不存在的文件"},
    },
)
def create_job(body: JobIn) -> JobOut:
    try:
        state, plan = store.start_job(body.files)
    except NoLibraryError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "尚未登记全库副本目录，请先 POST /library"
        )
    except JobExistsError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "已有进行中的取回作业；请先完成或 DELETE /job 作废"
        )
    except UnknownFilesError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"以下文件不在全库副本目录中: {exc.files}",
        )
    except InvalidJobError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.reason)
    return JobOut(state=_state_out(state), plan=_plan_out(plan))


@app.get("/state", response_model=StateOut, summary="查看当前作业状态（带头位置、待取/已取/无副本文件、占用卷）")
def get_state() -> StateOut:
    return _state_out(_job_or_404())


@app.get("/plan", response_model=PlanOut, summary="按当前带头位置重算最优读取计划")
def get_plan() -> PlanOut:
    try:
        return _plan_out(store.get_plan())
    except NoJobError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "尚未创建取回作业，请先 POST /jobs")


@app.post(
    "/receipts",
    response_model=ReceiptOut,
    summary="回报当前计划第一步的读带结果，并按带头新位置重排剩余文件",
    responses={
        404: {"description": "尚未创建作业"},
        409: {"description": "回执与当前计划第一步不符，或没有待回报的步骤"},
    },
)
def post_receipt(body: ReceiptIn) -> ReceiptOut:
    try:
        state, _before, after = store.apply_receipt(
            body.file, body.volume, body.position, body.result
        )
    except NoJobError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "尚未创建取回作业，请先 POST /jobs")
    except NoPendingStepError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "当前计划没有待回报的读带步骤（已全部完成或无副本可读）"
        )
    except MismatchError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "error": "回执与当前计划第一步不符，进度未更新",
                "expected": {
                    "file": exc.expected.file,
                    "volume": exc.expected.volume,
                    "position": exc.expected.position,
                },
            },
        )
    return ReceiptOut(
        applied={
            "file": body.file,
            "volume": body.volume,
            "position": body.position,
            "result": body.result,
            "copy_disabled": body.result == "failed",
        },
        state=_state_out(state),
        plan=_plan_out(after),
    )


@app.delete(
    "/job",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="作废当前作业（释放全部卷占用；库目录与坏副本标记保留）",
)
def delete_job() -> Response:
    store.reset_job()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
