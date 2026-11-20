"""缺批号计数表的归档试算与入库。

班组交班时发现若干张纸厚计数表漏填批号，只知道它们来自哪几个候选质检批。
本模块把 1~6 张表（每张沿用补报的区间计数格式）归入 2~4 个候选批：

* 每张表只能归一个候选批，同一候选批可以收多张表；
* 分到同一候选批的多张表先合并，再与该批总片数和已收补报一起做相容性核对；
* 枚举全部归属（最多 4^6 = 4096 种），相容的归属按字典序返回；
* 整组只有一种相容归属时才由调用方入库；无解或多解都不写数据。

试算（:func:`plan_archiving`）只开延迟读事务，在 WAL 下不阻塞其他连接继续
补报或更正总片数。入库（:func:`commit_archiving`）开 ``BEGIN IMMEDIATE`` 写
事务，先逐个核对**所有**候选批的总片数与已收计数是否仍与试算快照一致，任一
不符或写入失败则整组回滚。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Sequence

from . import db
from .solver import InconsistentCounts, check_consistent

# 一张表 = 一组 [lo, hi) 区间计数，与补报格式一致。
Sheet = list[tuple[int, int, int]]


class CandidateMissing(Exception):
    """候选质检号不存在。"""

    def __init__(self, inspection_id: int):
        super().__init__(f"候选质检号 {inspection_id} 不存在")
        self.inspection_id = inspection_id


class ArchiveBasisChanged(Exception):
    """写入时发现候选批的总片数或已收计数与试算依据不符。"""

    def __init__(self, inspection_id: int):
        super().__init__(
            f"候选批 {inspection_id} 的总片数或已收计数在试算后发生变化"
        )
        self.inspection_id = inspection_id


@dataclass(frozen=True)
class CandidateSnapshot:
    """试算时刻一个候选批的核对依据：总片数 + 全部已收区间计数（规范序）。"""

    inspection_id: int
    total_pieces: int
    constraints: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True)
class ArchivePlan:
    """一次试算的结果：候选批快照与全部相容归属（候选项下标序列，字典序）。"""

    snapshots: tuple[CandidateSnapshot, ...]
    feasible_assignments: tuple[tuple[int, ...], ...]


def snapshot_candidate(
    connection, inspection_id: int
) -> CandidateSnapshot | None:
    """读取一个候选批的试算依据；批不存在时返回 ``None``。"""
    row = db.get_inspection(connection, inspection_id)
    if row is None:
        return None
    # 排序后作为规范形式，写入复核时可直接做整体相等比较。
    constraints = tuple(sorted(db.load_constraints(connection, inspection_id)))
    return CandidateSnapshot(
        inspection_id=inspection_id,
        total_pieces=int(row["total_pieces"]),
        constraints=constraints,
    )


def _group_is_feasible(
    snapshot: CandidateSnapshot, sheets: Sequence[Sheet], positions: frozenset[int]
) -> bool:
    """分到同一候选批的若干张表合并后是否与该批依据相容。"""
    merged = list(snapshot.constraints)
    for position in positions:
        merged.extend(sheets[position])
    try:
        check_consistent(snapshot.total_pieces, merged)
    except InconsistentCounts:
        return False
    return True


def enumerate_feasible(
    snapshots: Sequence[CandidateSnapshot], sheets: Sequence[Sheet]
) -> list[tuple[int, ...]]:
    """枚举全部相容归属。

    归属用长度等于表数的下标序列表示，每个位置取值 ``0..len(snapshots)-1``，
    即该表归入候选列表中的第几个质检号。按字典序（表序由前到后、候选序由小
    到大）返回，便于多解时固定取前两套。同一候选批 + 同一组表的相容性结果
    做记忆化，避免重复建图。
    """
    candidate_count = len(snapshots)
    memo: dict[tuple[int, frozenset[int]], bool] = {}
    feasible: list[tuple[int, ...]] = []

    for assignment in product(range(candidate_count), repeat=len(sheets)):
        groups: dict[int, list[int]] = {}
        for position, candidate_index in enumerate(assignment):
            groups.setdefault(candidate_index, []).append(position)

        ok = True
        for candidate_index, positions in groups.items():
            key = (candidate_index, frozenset(positions))
            cached = memo.get(key)
            if cached is None:
                cached = _group_is_feasible(
                    snapshots[candidate_index], sheets, key[1]
                )
                memo[key] = cached
            if not cached:
                ok = False
                break
        if ok:
            feasible.append(assignment)
    return feasible


def plan_archiving(
    connection, sheets: Sequence[Sheet], candidate_ids: Sequence[int]
) -> ArchivePlan:
    """只读试算：在一个延迟读事务里取一致快照并枚举相容归属。

    延迟事务只执行 SELECT，在 WAL 模式下不获取写锁，试算期间其他连接可以
    正常补报或更正总片数。事务结束（回滚即释放快照）后不带任何锁。
    """
    connection.execute("BEGIN")
    try:
        snapshots: list[CandidateSnapshot] = []
        for inspection_id in candidate_ids:
            snapshot = snapshot_candidate(connection, inspection_id)
            if snapshot is None:
                raise CandidateMissing(inspection_id)
            snapshots.append(snapshot)
        feasible = enumerate_feasible(tuple(snapshots), sheets)
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
    return ArchivePlan(
        snapshots=tuple(snapshots),
        feasible_assignments=tuple(feasible),
    )


def commit_archiving(
    connection,
    sheets: Sequence[Sheet],
    candidate_ids: Sequence[int],
    snapshots: Sequence[CandidateSnapshot],
    assignment: Sequence[int],
) -> list[int]:
    """按唯一归属入库：每张表写成对应候选批的一份补报。

    先开 ``BEGIN IMMEDIATE`` 串行化写入，再逐个核对**所有**候选批（含本次
    没有分到表的候选批）的总片数与已收计数是否仍等于试算快照；任一不符或
    后续写入失败，整组回滚并抛出 :class:`ArchiveBasisChanged`。
    返回与表同序的新建补报号列表。
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        for expected in snapshots:
            current = snapshot_candidate(connection, expected.inspection_id)
            if current is None or current != expected:
                raise ArchiveBasisChanged(expected.inspection_id)

        report_ids: list[int] = []
        for sheet, candidate_index in zip(sheets, assignment):
            inspection_id = candidate_ids[candidate_index]
            report_ids.append(db.insert_report(connection, inspection_id, sheet))
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return report_ids
