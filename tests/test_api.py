"""API 集成测试：登记、补报、冲突拒收、查询、原结果仍可查。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client


def make_inspection(client, total_pieces, note=None):
    response = client.post(
        "/inspections", json={"total_pieces": total_pieces, "note": note}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_create_and_get_inspection(client):
    created = make_inspection(client, 100, "批次 A")
    assert created["id"] == 1
    assert created["total_pieces"] == 100
    assert created["note"] == "批次 A"
    assert created["report_count"] == 0
    assert created["segment_count"] == 0

    detail = client.get("/inspections/1")
    assert detail.status_code == 200
    assert detail.json()["reports"] == []

    listed = client.get("/inspections").json()
    assert [item["id"] for item in listed] == [1]

    assert client.get("/inspections/999").status_code == 404


def test_validation_errors(client):
    assert client.post("/inspections", json={"total_pieces": -1}).status_code == 422
    make_inspection(client, 10)

    # hi 必须大于 lo
    response = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 10, "hi": 10, "count": 1}]},
    )
    assert response.status_code == 422

    # 空补报不允许
    assert (
        client.post("/inspections/1/reports", json={"segments": []}).status_code
        == 422
    )

    # 负计数不允许
    response = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 0, "hi": 10, "count": -1}]},
    )
    assert response.status_code == 422


def test_bounds_overlapping_reports(client):
    make_inspection(client, 100)
    first = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 40, "hi": 60, "count": 70}]},
    )
    assert first.status_code == 201
    second = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 50, "hi": 70, "count": 60}]},
    )
    assert second.status_code == 201
    assert second.json()["inspection_id"] == 1
    assert len(second.json()["segments"]) == 1

    bounds = client.get(
        "/inspections/1/bounds", params={"qualified_lo": 50, "qualified_hi": 60}
    )
    assert bounds.status_code == 200
    assert bounds.json() == {
        "inspection_id": 1,
        "total_pieces": 100,
        "qualified_lo": 50,
        "qualified_hi": 60,
        "min_count": 30,
        "max_count": 60,
    }

    # 详情能看到两次补报的全部区间
    detail = client.get("/inspections/1").json()
    assert detail["report_count"] == 2
    assert detail["segment_count"] == 2
    assert [
        (s["lo"], s["hi"], s["count"]) for r in detail["reports"] for s in r["segments"]
    ] == [(40, 60, 70), (50, 70, 60)]


def test_conflicting_report_is_rejected_atomically(client):
    make_inspection(client, 100)
    assert (
        client.post(
            "/inspections/1/reports",
            json={"segments": [{"lo": 30, "hi": 50, "count": 40}]},
        ).status_code
        == 201
    )

    before = client.get("/inspections/1").json()

    # 同一区间报出不同计数 => 与已有计数矛盾，整份拒收
    conflict = client.post(
        "/inspections/1/reports",
        json={
            "segments": [
                {"lo": 0, "hi": 10, "count": 5},  # 这一条本身没问题
                {"lo": 30, "hi": 50, "count": 41},  # 与已有计数冲突
            ]
        },
    )
    assert conflict.status_code == 409
    assert "拒收" in conflict.json()["detail"]

    after = client.get("/inspections/1").json()
    assert after == before  # 原结果完全不变，且半份报告没有被写入
    assert after["report_count"] == 1
    assert len(after["reports"][0]["segments"]) == 1

    # 拒收后仍可正常查询原结果
    bounds = client.get(
        "/inspections/1/bounds", params={"qualified_lo": 30, "qualified_hi": 50}
    )
    assert bounds.status_code == 200
    assert bounds.json()["min_count"] == 40
    assert bounds.json()["max_count"] == 40


def test_conflict_inside_single_report_rejects_whole_report(client):
    make_inspection(client, 10)
    response = client.post(
        "/inspections/1/reports",
        json={
            "segments": [
                {"lo": 0, "hi": 10, "count": 4},
                {"lo": 0, "hi": 10, "count": 5},
            ]
        },
    )
    assert response.status_code == 409
    detail = client.get("/inspections/1").json()
    assert detail["report_count"] == 0
    assert detail["segment_count"] == 0


def test_report_exceeding_total_is_rejected(client):
    make_inspection(client, 5)
    response = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 0, "hi": 10, "count": 6}]},
    )
    assert response.status_code == 409
    assert client.get("/inspections/1").json()["report_count"] == 0


def test_missing_inspection_for_reports(client):
    payload = {"segments": [{"lo": 0, "hi": 10, "count": 1}]}
    assert client.post("/inspections/42/reports", json=payload).status_code == 404
    assert client.get("/inspections/42/reports").status_code == 404
    assert (
        client.get(
            "/inspections/42/bounds", params={"qualified_lo": 0, "qualified_hi": 10}
        ).status_code
        == 404
    )


def test_bounds_validation(client):
    make_inspection(client, 10)
    bad = client.get(
        "/inspections/1/bounds", params={"qualified_lo": 10, "qualified_hi": 10}
    )
    assert bad.status_code == 422
    negative = client.get(
        "/inspections/1/bounds", params={"qualified_lo": -1, "qualified_hi": 10}
    )
    assert negative.status_code == 422


def test_patch_total_conflicting_is_rejected(client):
    make_inspection(client, 100)
    assert (
        client.post(
            "/inspections/1/reports",
            json={"segments": [{"lo": 0, "hi": 50, "count": 60}]},
        ).status_code
        == 201
    )

    conflict = client.patch("/inspections/1", json={"total_pieces": 50})
    assert conflict.status_code == 409
    assert client.get("/inspections/1").json()["total_pieces"] == 100

    ok = client.patch("/inspections/1", json={"note": "已复核"})
    assert ok.status_code == 200
    assert ok.json()["note"] == "已复核"
    assert ok.json()["total_pieces"] == 100


def test_patch_note_to_null(client):
    make_inspection(client, 10, "旧备注")
    response = client.patch("/inspections/1", json={"note": None})
    assert response.status_code == 200
    assert response.json()["note"] is None


def test_docs_available(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
