"""用逐片枚举的独立实现校验上下界算法（仅用于小规模自测）。"""

from __future__ import annotations

from itertools import product


def brute_bounds(
    total: int,
    constraints: list[tuple[int, int, int]],
    q_lo: int,
    q_hi: int,
    max_thickness: int,
) -> tuple[int, int] | None:
    """枚举每片厚度（0..max_thickness-1），返回合格片数的最小/最大值。

    无可行分布时返回 ``None``。
    """
    best_lo = None
    best_hi = None
    for thicknesses in product(range(max_thickness), repeat=total):
        if all(
            sum(1 for t in thicknesses if lo <= t < hi) == count
            for lo, hi, count in constraints
        ):
            qualified = sum(1 for t in thicknesses if q_lo <= t < q_hi)
            best_lo = qualified if best_lo is None else min(best_lo, qualified)
            best_hi = qualified if best_hi is None else max(best_hi, qualified)
    if best_lo is None:
        return None
    return best_lo, best_hi
