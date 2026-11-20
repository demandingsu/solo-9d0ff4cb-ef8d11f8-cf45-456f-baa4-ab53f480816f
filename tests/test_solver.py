"""算法层的单元测试：重叠区间、冲突检测、与逐片枚举对拍。"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.solver import InconsistentCounts, compute_bounds  # noqa: E402
from tests.brute import brute_bounds  # noqa: E402


def test_overlapping_intervals_intersect():
    # 100 片。A: [40,60) 有 70 片；B: [50,70) 有 60 片。
    # 交集 [50,60) 至少 70 + 60 - 100 = 30 片，至多 60 片。
    assert compute_bounds(100, [(40, 60, 70), (50, 70, 60)], 50, 60) == (30, 60)
    # 并集 [40,70) 的片数 = 70 + 60 - 交集，范围 70..100。
    assert compute_bounds(100, [(40, 60, 70), (50, 70, 60)], 40, 70) == (70, 100)


def test_no_constraints_returns_trivial_bounds():
    assert compute_bounds(50, [], 10, 20) == (0, 50)
    assert compute_bounds(50, [], 0, 10) == (0, 50)


def test_exact_partition_is_tight():
    constraints = [(0, 50, 20), (50, 100, 30)]
    assert compute_bounds(50, constraints, 0, 50) == (20, 20)
    assert compute_bounds(50, constraints, 50, 100) == (30, 30)


def test_qualified_interval_outside_reported_range():
    # 已报计数都在 [200,300)，剩余 40 片可落在任意非负厚度；
    # 查询 [0,100) 时这 40 片都可以放进去，因此是 0..40。
    constraints = [(200, 300, 60)]
    assert compute_bounds(100, constraints, 0, 100) == (0, 40)
    # 查询 [300,400) 同样可容纳全部剩余片数。
    assert compute_bounds(100, constraints, 300, 400) == (0, 40)
    # 若总片数恰好等于已报计数，则区间外的片数只能为 0。
    assert compute_bounds(60, constraints, 0, 100) == (0, 0)


def test_conflict_same_range_different_counts():
    with pytest.raises(InconsistentCounts):
        compute_bounds(100, [(10, 20, 5), (10, 20, 6)], 0, 100)


def test_conflict_exceeds_total():
    with pytest.raises(InconsistentCounts):
        compute_bounds(10, [(0, 50, 11)], 0, 100)


def test_conflict_nested_ranges():
    # [0,100) 只有 3 片，子区间 [20,30) 却有 4 片，不可能。
    with pytest.raises(InconsistentCounts):
        compute_bounds(10, [(0, 100, 3), (20, 30, 4)], 0, 100)


def test_conflict_disjoint_sum_exceeds_total():
    with pytest.raises(InconsistentCounts):
        compute_bounds(5, [(0, 10, 3), (10, 20, 3)], 0, 100)


def test_touching_intervals_are_not_overlapping():
    # [0,10) 与 [10,20) 互不重叠，各自计数相加即为 [0,20)。
    assert compute_bounds(20, [(0, 10, 7), (10, 20, 8)], 0, 20) == (15, 15)


def test_zero_counts():
    assert compute_bounds(10, [(0, 10, 0)], 0, 10) == (0, 0)


def test_bounds_respect_integrality_with_unit_gap():
    # 原子区间长度为 1 时也必须给出整数解。
    # 总 3 片；[0,1) 1 片，[0,3) 2 片 => [1,2) 与 [2,3) 合计 1 片。
    assert compute_bounds(3, [(0, 1, 1), (0, 3, 2)], 1, 2) == (0, 1)


@pytest.mark.parametrize("seed", range(40))
def test_matches_brute_force(seed: int):
    rng = random.Random(seed)
    total = rng.randint(0, 5)
    max_thickness = 5
    raw = []
    for _ in range(rng.randint(0, 4)):
        lo = rng.randint(0, max_thickness - 1)
        hi = rng.randint(lo + 1, max_thickness)
        raw.append((lo, hi, rng.randint(0, total)))
    q_lo = rng.randint(0, max_thickness - 1)
    q_hi = rng.randint(q_lo + 1, max_thickness)

    # 逐片枚举只能枚举有限厚度域；用 [0, max_thickness) 恰好容纳全部片数
    # 这一条约束把解空间限制在同一有限域内，两边模型才等价。
    bounded = raw + [(0, max_thickness, total)]

    expected = brute_bounds(total, raw, q_lo, q_hi, max_thickness)
    if expected is None:
        with pytest.raises(InconsistentCounts):
            compute_bounds(total, bounded, q_lo, q_hi)
    else:
        assert compute_bounds(total, bounded, q_lo, q_hi) == expected


def test_large_counts_do_not_expand_pieces():
    # 总片数很大也应在端点规模内完成；这里主要防止误用逐片枚举。
    constraints = [
        (0, 1_000_000, 5_000_000),
        (500_000, 1_500_000, 4_000_000),
        (1_000_000, 2_000_000, 5_000_000),
    ]
    min_count, max_count = compute_bounds(
        10_000_000, constraints, 500_000, 1_000_000
    )
    assert 0 <= min_count <= max_count <= 10_000_000
