"""API 请求/响应模型。"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SegmentIn(BaseModel):
    lo: int = Field(..., ge=0, description="区间起点，包含，整数微米")
    hi: int = Field(..., description="区间终点，不包含，整数微米，必须大于 lo")
    count: int = Field(..., ge=0, description="该厚度区间内的片数")

    @model_validator(mode="after")
    def _validate_range(self) -> "SegmentIn":
        if self.hi <= self.lo:
            raise ValueError("hi 必须大于 lo（区间含起点、不含终点）")
        return self


class CountReportIn(BaseModel):
    segments: list[SegmentIn] = Field(
        ...,
        min_length=1,
        description="本次补报的全部厚度区间计数；同一报告内或与已有报告可以重叠。"
        "任一区间与已有计数不相容时，整份报告拒收。",
    )


class ArchiveSheetIn(BaseModel):
    """一张漏填批号的计数表，格式与补报一致。"""

    segments: list[SegmentIn] = Field(
        ...,
        min_length=1,
        description="本张表登记的全部厚度区间计数，格式与补报相同。",
    )


class ArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheets: list[ArchiveSheetIn] = Field(
        ...,
        min_length=1,
        max_length=6,
        description="待归档的计数表，1～6 张；每张表只能归一个候选批。",
    )
    candidate_ids: list[Annotated[int, Field(gt=0)]] = Field(
        ...,
        min_length=2,
        max_length=4,
        description="候选质检号，2～4 个，互不重复。",
    )

    @model_validator(mode="after")
    def _unique_candidates(self) -> "ArchiveRequest":
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("candidate_ids 中的候选质检号必须互不重复")
        return self


class InspectionCreate(BaseModel):
    total_pieces: int = Field(..., ge=0, description="纸样总片数")
    note: str | None = Field(None, max_length=500, description="备注，可选")


class InspectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_pieces: int | None = Field(None, ge=0, description="新的总片数，可选")
    note: str | None = Field(None, max_length=500, description="新的备注；传 null 可清空")

    @model_validator(mode="after")
    def _validate_payload(self) -> "InspectionUpdate":
        if not self.model_fields_set:
            raise ValueError("至少提供 total_pieces 或 note 中的一项")
        return self


class SegmentOut(BaseModel):
    id: int
    lo: int
    hi: int
    count: int


class CountReportOut(BaseModel):
    id: int
    inspection_id: int
    created_at: str
    segments: list[SegmentOut]


class InspectionSummary(BaseModel):
    id: int
    total_pieces: int
    note: str | None
    created_at: str
    report_count: int = Field(..., description="已接收的补报批次数")
    segment_count: int = Field(..., description="已接收的厚度区间条数")


class InspectionDetail(InspectionSummary):
    reports: list[CountReportOut]


class BoundsOut(BaseModel):
    inspection_id: int
    total_pieces: int
    qualified_lo: int
    qualified_hi: int
    min_count: int = Field(..., description="同时满足全部计数时，合格片数可达到的最小值")
    max_count: int = Field(..., description="同时满足全部计数时，合格片数可达到的最大值")


class ArchivedSheetOut(BaseModel):
    sheet_index: int = Field(..., description="计数表在请求中的序号，从 0 开始")
    inspection_id: int = Field(..., description="本表归入的质检号")
    report_id: int | None = Field(
        ...,
        description="本表入库后对应的补报号；未入库的备选方案中为 null",
    )


class ArchiveAssignmentOut(BaseModel):
    """一套完整归属方案：每张表归入哪个候选质检号。"""

    sheets: list[ArchivedSheetOut]


class ArchiveOut(BaseModel):
    status: str = Field(
        ...,
        description=(
            "archived：唯一归属，已整组入库；"
            "multiple：存在多套完整归属，未入库，alternatives 给出两套方案；"
            "no_solution：无解，未入库。"
        ),
    )
    message: str
    sheets: list[ArchivedSheetOut] | None = Field(
        None, description="status=archived 时逐表返回质检号与补报号；其余情况为 null"
    )
    alternatives: list[ArchiveAssignmentOut] | None = Field(
        None, description="status=multiple 时给出两套不同的完整归属方案"
    )
