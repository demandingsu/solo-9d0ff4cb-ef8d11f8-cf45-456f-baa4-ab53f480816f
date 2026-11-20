"""并发补报测试：串行化事务保证不丢失不相容检查。"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402


def test_concurrent_conflicting_reports_only_one_wins(tmp_path):
    app = create_app(str(tmp_path / "concurrent.db"))
    with TestClient(app) as client:
        assert client.post("/inspections", json={"total_pieces": 10}).status_code == 201

        def submit(count: int):
            return client.post(
                "/inspections/1/reports",
                json={"segments": [{"lo": 0, "hi": 10, "count": count}]},
            ).status_code

        # 5 与 6 互相矛盾，只能有一个成功。
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(pool.map(submit, [5, 6]))

        assert statuses == [201, 409]
        detail = client.get("/inspections/1").json()
        assert detail["report_count"] == 1
        assert detail["segment_count"] == 1


def test_concurrent_compatible_reports_all_accepted(tmp_path):
    app = create_app(str(tmp_path / "concurrent2.db"))
    with TestClient(app) as client:
        assert client.post("/inspections", json={"total_pieces": 10}).status_code == 201

        def submit(pair):
            lo, hi = pair
            return client.post(
                "/inspections/1/reports",
                json={"segments": [{"lo": lo, "hi": hi, "count": 2}]},
            ).status_code

        pairs = [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10)]
        with ThreadPoolExecutor(max_workers=5) as pool:
            statuses = list(pool.map(submit, pairs))

        assert statuses == [201] * 5
        detail = client.get("/inspections/1").json()
        assert detail["report_count"] == 5
        bounds = client.get(
            "/inspections/1/bounds", params={"qualified_lo": 0, "qualified_hi": 10}
        ).json()
        assert bounds["min_count"] == 10
        assert bounds["max_count"] == 10
