"""FastAPI 应用：纸样厚度计数核对。

* ``POST /inspections``                    登记一批纸样的总片数
* ``GET  /inspections``                    列出全部登记
* ``GET  /inspections/{id}``               查看登记及已接收的分次补报
* ``PATCH /inspections/{id}``              更正总片数/备注（与已有计数冲突则拒收）
* ``POST /inspections/{id}/reports``       分次提交厚度区间计数（整份原子接收或拒收）
* ``GET  /inspections/{id}/reports``       查看补报历史
* ``GET  /inspections/{id}/bounds``        给定合格区间，查询合格片数最少/最多
* ``GET  /health``                         健康检查

所有区间均为整数微米，含起点不含终点，即 ``[lo, hi)``，且 ``hi > lo``。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlite3 import Connection

from . import db
from .schemas import (
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
