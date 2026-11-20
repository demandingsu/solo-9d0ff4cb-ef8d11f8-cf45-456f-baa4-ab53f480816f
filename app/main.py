"""FastAPI 应用：纸样厚度计数核对。

* ``POST /inspections``                    登记一批纸样的总片数
* ``GET  /inspections``                    列出全部登记
* ``GET  /inspections/{id}``               查看登记及已接收的分次补报
* ``PATCH /inspections/{id}``              更正总片数/备注（与已有计数冲突则拒收）
* ``POST /inspections/{id}/reports``       分次提交厚度区间计数（整份原子接收或拒收）
* ``GET  /inspections/{id}/reports``       查看补报历史
* ``POST /archives``                       漏填批号计数表的整组归档试算与入库
* ``GET  /inspections/{id}/bounds``        给定合格区间，查询合格片数最少/最多
* ``GET  /health``                         健康检查

所有区间均为整数微米，含起点不含终点，即 ``[lo, hi)``，且 ``hi > lo``。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlite3 import Connection

from . import db
from .archiving import (
    ArchiveBasisChanged,
    CandidateMissing,
    commit_archiving,
    plan_archiving,
)
from .schemas import (
    ArchiveAssignmentOut,
    ArchiveOut,
    ArchiveRequest,
    ArchivedSheetOut,
    BoundsOut,
    CountReportIn,
    CountReportOut,
    InspectionCreate,
    InspectionDetail,
    InspectionSummary,
    InspectionUpdate,
)
from .solver import InconsistentCounts, check_consistent, compute_bounds

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "./data/inspection.db")


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or DEFAULT_DB_PATH

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init(path)
        yield

    api = FastAPI(
        title="纸样厚度计数核对",
        description=(
            "登记总片数，分次补报互可重叠的厚度区间计数；"
            "按全部计数的交集计算合格片数的可达到上下界，不逐片展开。"
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    def get_conn():
        connection = db.connect(path)
        try:
            yield connection
        finally:
            connection.close()

    def _require_inspection(connection: Connection, inspection_id: int):
        row = db.get_inspection(connection, inspection_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"登记 {inspection_id} 不存在",
            )
        return row

    def _summary(row) -> InspectionSummary:
        return InspectionSummary(
            id=row["id"],
            total_pieces=row["total_pieces"],
            note=row["note"],
            created_at=row["created_at"],
            report_count=row["report_count"],
            segment_count=row["segment_count"],
        )

    @api.get("/health", tags=["运维"], summary="健康检查")
    def health() -> dict:
        return {"status": "ok"}

    @api.post(
        "/inspections",
        response_model=InspectionSummary,
        status_code=status.HTTP_201_CREATED,
        tags=["登记"],
        summary="登记一批纸样的总片数",
    )
    def create_inspection(
        payload: InspectionCreate, connection: Connection = Depends(get_conn)
    ) -> InspectionSummary:
        inspection_id = db.create_inspection(
            connection, payload.total_pieces, payload.note
        )
        row = _require_inspection(connection, inspection_id)
        return _summary(row)

    @api.get(
        "/inspections",
        response_model=list[InspectionSummary],
        tags=["登记"],
        summary="列出全部登记",
    )
    def list_inspections(
        connection: Connection = Depends(get_conn),
    ) -> list[InspectionSummary]:
        return [_summary(row) for row in db.list_inspections(connection)]

    @api.get(
        "/inspections/{inspection_id}",
        response_model=InspectionDetail,
        tags=["登记"],
        summary="查看登记及已接收的补报",
    )
    def get_inspection(
        inspection_id: int, connection: Connection = Depends(get_conn)
    ) -> InspectionDetail:
        row = _require_inspection(connection, inspection_id)
        summary = _summary(row)
        reports = [CountReportOut(**r) for r in db.get_reports(connection, inspection_id)]
        return InspectionDetail(**summary.model_dump(), reports=reports)

    @api.patch(
        "/inspections/{inspection_id}",
        response_model=InspectionSummary,
        tags=["登记"],
        summary="更正总片数或备注",
        description=(
            "若新的总片数与已接收的计数不相容，整次更正被拒收，原登记保持不变。"
        ),
    )
    def update_inspection(
        inspection_id: int,
        payload: InspectionUpdate,
        connection: Connection = Depends(get_conn),
    ) -> InspectionSummary:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _require_inspection(connection, inspection_id)
            new_total = (
                payload.total_pieces
                if "total_pieces" in payload.model_fields_set
                else int(row["total_pieces"])
            )
            new_note = payload.note if "note" in payload.model_fields_set else row["note"]
            if "total_pieces" in payload.model_fields_set:
                constraints = db.load_constraints(connection, inspection_id)
                try:
                    check_consistent(new_total, constraints)
                except InconsistentCounts:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"新总片数 {new_total} 与已接收的计数不相容，"
                            "本次更正已拒收，原登记保持不变"
                        ),
                    )
            db.update_inspection(connection, inspection_id, new_total, new_note)
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        row = _require_inspection(connection, inspection_id)
        return _summary(row)

    @api.post(
        "/inspections/{inspection_id}/reports",
        response_model=CountReportOut,
        status_code=status.HTTP_201_CREATED,
        tags=["补报"],
        summary="提交一批厚度区间计数",
        description=(
            "区间可以重叠。整份报告要么全部接收，要么在与已有计数"
            "（含本报告内其他区间）不相容时整份拒收；拒收不改变已有结果。"
        ),
    )
    def submit_report(
        inspection_id: int,
        payload: CountReportIn,
        connection: Connection = Depends(get_conn),
    ) -> CountReportOut:
        segments = [(s.lo, s.hi, s.count) for s in payload.segments]

        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _require_inspection(connection, inspection_id)
            existing = db.load_constraints(connection, inspection_id)
            try:
                check_consistent(int(row["total_pieces"]), existing + segments)
            except InconsistentCounts as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "补报与已有计数（或本报告内部）不相容，整份拒收；"
                        "已有结果保持不变"
                    ),
                ) from exc
            report_id = db.insert_report(connection, inspection_id, segments)
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise

        reports = db.get_reports(connection, inspection_id)
        for report in reports:
            if report["id"] == report_id:
                return CountReportOut(**report)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="补报写入后未能读回",
        )

    @api.get(
        "/inspections/{inspection_id}/reports",
        response_model=list[CountReportOut],
        tags=["补报"],
        summary="查看补报历史",
    )
    def list_reports(
        inspection_id: int, connection: Connection = Depends(get_conn)
    ) -> list[CountReportOut]:
        _require_inspection(connection, inspection_id)
        return [CountReportOut(**r) for r in db.get_reports(connection, inspection_id)]

    @api.post(
        "/archives",
        response_model=ArchiveOut,
        responses={
            201: {"model": ArchiveOut, "description": "唯一归属，整组已入库"},
            200: {"model": ArchiveOut, "description": "无解或多解，均未入库"},
            404: {"description": "候选质检号不存在"},
            409: {"description": "写入时候选批依据已变化，整组退回"},
        },
        tags=["归档"],
        summary="漏填批号计数表的整组归档试算与入库",
        description=(
            "接收 1～6 张计数表（区间计数格式与补报相同）和 2～4 个候选质检号。"
            "每张表只能归一个候选批，同批可收多张；同批多表先合并，再与该批"
            "总片数和已收补报核对。整组只有一种相容归属时才一次入库，按表返回"
            "质检号与补报号；无解返回 no_solution，多解返回两套不同的完整归属"
            "方案（alternatives），两种情况都不入库。试算为只读、不加写锁，"
            "期间补报与更正总片数可继续；写入时重新核对所有候选批，任一批的"
            "总片数或已收计数与试算依据不符、或写入失败，整组退回（409）。"
        ),
    )
    def archive_sheets(
        payload: ArchiveRequest, connection: Connection = Depends(get_conn)
    ) -> JSONResponse:
        sheets = [
            [(s.lo, s.hi, s.count) for s in sheet.segments] for sheet in payload.sheets
        ]
        candidate_ids = list(payload.candidate_ids)

        try:
            plan = plan_archiving(connection, sheets, candidate_ids)
        except CandidateMissing as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"候选质检号 {exc.inspection_id} 不存在，未做任何写入",
            ) from exc

        def _assignment_out(assignment) -> ArchiveAssignmentOut:
            return ArchiveAssignmentOut(
                sheets=[
                    ArchivedSheetOut(
                        sheet_index=position,
                        inspection_id=candidate_ids[candidate_index],
                        # 备选方案未入库，没有补报号。
                        report_id=None,
                    )
                    for position, candidate_index in enumerate(assignment)
                ]
            )

        feasible = plan.feasible_assignments
        if not feasible:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=ArchiveOut(
                    status="no_solution",
                    message=(
                        "无解：候选批中不存在能同时容纳全部计数表的完整归属，"
                        "整组未入库"
                    ),
                ).model_dump(),
            )
        if len(feasible) > 1:
            # 枚举按字典序返回，前两套天然不同。
            alternatives = [_assignment_out(feasible[0]), _assignment_out(feasible[1])]
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=ArchiveOut(
                    status="multiple",
                    message=(
                        f"存在 {len(feasible)} 套相容归属，未入库；"
                        "以下给出两套不同的完整方案，请交班员查表确认"
                    ),
                    alternatives=alternatives,
                ).model_dump(),
            )

        assignment = feasible[0]
        try:
            report_ids = commit_archiving(
                connection, sheets, candidate_ids, plan.snapshots, assignment
            )
        except ArchiveBasisChanged as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"试算期间候选批 {exc.inspection_id} 的总片数或已收计数发生变化，"
                    "与试算依据不符，整组退回、未入库；请重新试算"
                ),
            ) from exc

        archived = [
            ArchivedSheetOut(
                sheet_index=position,
                inspection_id=candidate_ids[candidate_index],
                report_id=report_id,
            )
            for position, (candidate_index, report_id) in enumerate(
                zip(assignment, report_ids)
            )
        ]
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=ArchiveOut(
                status="archived",
                message="唯一归属，整组已按表入库，每份计数表对应一个补报号",
                sheets=archived,
            ).model_dump(),
        )

    @api.get(
        "/inspections/{inspection_id}/bounds",
        response_model=BoundsOut,
        tags=["查询"],
        summary="查询合格片数的最少/最多可达到值",
        description=(
            "合格区间为 [qualified_lo, qualified_hi)，整数微米，含起点不含终点。"
            "返回值是同时满足全部已接收计数时，合格片数可同时达到的最小值和最大值；"
            "不逐片展开，按端点划分的整数规划精确求解。"
        ),
    )
    def get_bounds(
        inspection_id: int,
        qualified_lo: int = Query(..., ge=0, description="合格区间起点，包含"),
        qualified_hi: int = Query(..., description="合格区间终点，不包含，须大于起点"),
        connection: Connection = Depends(get_conn),
    ) -> BoundsOut:
        if qualified_hi <= qualified_lo:
            raise HTTPException(
                status_code=422,
                detail="qualified_hi 必须大于 qualified_lo（区间含起点、不含终点）",
            )
        row = _require_inspection(connection, inspection_id)
        constraints = db.load_constraints(connection, inspection_id)
        try:
            min_count, max_count = compute_bounds(
                int(row["total_pieces"]), constraints, qualified_lo, qualified_hi
            )
        except InconsistentCounts as exc:
            # 正常写入路径会拒绝不相容数据；此处防御数据库被外部改写。
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="已存计数互相矛盾，无法计算；请检查数据",
            ) from exc
        return BoundsOut(
            inspection_id=inspection_id,
            total_pieces=int(row["total_pieces"]),
            qualified_lo=qualified_lo,
            qualified_hi=qualified_hi,
            min_count=min_count,
            max_count=max_count,
        )

    return api


app = create_app()
