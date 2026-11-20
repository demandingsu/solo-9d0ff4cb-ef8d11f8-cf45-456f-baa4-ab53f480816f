"""归档 API 测试：唯一归属入库、多解、无解，以及试算期间候选批更正的退回。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.archiving import plan_archiving  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "archive.db"))
    with TestClient(app) as test_client:
        yield test_client


def make_inspection(client, total_pieces, note=None):
    response = client.post(
        "/inspections", json={"total_pieces": total_pieces, "note": note}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def sheet(*segments):
    return {"segments": [{"lo": lo, "hi": hi, "count": c} for lo, hi, c in segments]}


def test_archive_validation(client):
    make_inspection(client, 10)
    make_inspection(client, 10)

    # 候选质检号不足 2 个
    response = client.post(
        "/archives",
        json={"sheets": [sheet((0, 10, 3))], "candidate_ids": [1]},
    )
    assert response.status_code == 422

    # 候选超过 4 个
    response = client.post(
        "/archives",
        json={"sheets": [sheet((0, 10, 3))], "candidate_ids": [1, 2, 3, 4, 5]},
    )
    assert response.status_code == 422

    # 表数为 0
    response = client.post(
        "/archives", json={"sheets": [], "candidate_ids": [1, 2]}
    )
    assert response.status_code == 422

    # 表数超过 6
    response = client.post(
        "/archives",
        json={
            "sheets": [sheet((0, 10, 1)) for _ in range(7)],
            "candidate_ids": [1, 2],
        },
    )
    assert response.status_code == 422

    # 候选质检号重复
    response = client.post(
        "/archives",
        json={"sheets": [sheet((0, 10, 3))], "candidate_ids": [1, 1]},
    )
    assert response.status_code == 422

    # 区间非法（hi <= lo）同样被拒
    response = client.post(
        "/archives",
        json={"sheets": [sheet((5, 5, 1))], "candidate_ids": [1, 2]},
    )
    assert response.status_code == 422

    # 非正质检号被拒
    response = client.post(
        "/archives",
        json={"sheets": [sheet((0, 10, 3))], "candidate_ids": [0, 1]},
    )
    assert response.status_code == 422

    # 多余字段被拒
    response = client.post(
        "/archives",
        json={
            "sheets": [sheet((0, 10, 3))],
            "candidate_ids": [1, 2],
            "extra": 1,
        },
    )
    assert response.status_code == 422


def test_archive_candidate_missing_returns_404(client):
    make_inspection(client, 10)
    response = client.post(
        "/archives",
        json={"sheets": [sheet((0, 10, 3))], "candidate_ids": [1, 999]},
    )
    assert response.status_code == 404
    assert "999" in response.json()["detail"]
    # 未做任何写入
    assert client.get("/inspections/1").json()["report_count"] == 0


def test_archive_unique_assignment_writes_reports(client):
    # 批 1 总 10 片，已收补报 [0,10)=6；批 2 总 10 片，已收 [0,10)=4。
    b1 = make_inspection(client, 10)
    assert client.post(f"/inspections/{b1}/reports", json=sheet((0, 10, 6))).status_code == 201
    b2 = make_inspection(client, 10)
    assert client.post(f"/inspections/{b2}/reports", json=sheet((0, 10, 4))).status_code == 201

    # 新表 [0,10)=4：批 1 已收 6，再收 4 恰好 10，相容；批 2 已收 4，再收 4 => 8 也相容！
    # 因此改为 [0,10)=7：批 1（6+7=13>10）不行；批 2（4+7=11>10）也不行。
    # 用非重叠区间构造唯一归属：表 [10,20)=3。
    # 批 1：[0,10)=6 与 [10,20)=3 合计 9<=10 相容；批 2：4+3=7<=10 也相容 => 多解。
    # 让批 2 容量被占满：批 2 已收 [0,10)=4，再补 [10,20)=6 使其总片数 10 用满。
    assert client.post(f"/inspections/{b2}/reports", json=sheet((10, 20, 6))).status_code == 201
    # 此时批 2 已固定 10 片分布，任何额外计数都不相容；批 1 仍可收 [10,20)=3。
    response = client.post(
        "/archives",
        json={"sheets": [sheet((10, 20, 3))], "candidate_ids": [b1, b2]},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "archived"
    assert len(body["sheets"]) == 1
    archived = body["sheets"][0]
    assert archived["sheet_index"] == 0
    assert archived["inspection_id"] == b1
    # 补报号为全局自增，与批 1 详情中最新补报一致。
    latest_report = client.get(f"/inspections/{b1}").json()["reports"][-1]
    assert archived["report_id"] == latest_report["id"]
    assert body["alternatives"] is None

    detail = client.get(f"/inspections/{b1}").json()
    assert detail["report_count"] == 2
    assert detail["segment_count"] == 2
    # 落选批保持不变
    other = client.get(f"/inspections/{b2}").json()
    assert other["report_count"] == 2


def test_archive_multiple_sheets_merged_into_same_batch(client):
    b1 = make_inspection(client, 20)
    # 批 2 总片数 2：两张表（[0,10)=5、[10,20)=3）单张都进不了批 2；
    # 两张表只能都归批 1（合并后共 8<=20，相容），归属唯一。
    b2 = make_inspection(client, 2)

    response = client.post(
        "/archives",
        json={
            "sheets": [sheet((0, 10, 5)), sheet((10, 20, 3))],
            "candidate_ids": [b1, b2],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "archived"
    assert [item["inspection_id"] for item in body["sheets"]] == [b1, b1]
    report_ids = [item["report_id"] for item in body["sheets"]]
    assert len(set(report_ids)) == 2  # 两张表对应两个不同补报号

    detail = client.get(f"/inspections/{b1}").json()
    assert detail["report_count"] == 2
    assert detail["segment_count"] == 2
    assert client.get(f"/inspections/{b2}").json()["report_count"] == 0


def test_archive_multiple_solutions_returns_two_alternatives(client):
    # 两个完全相同的空批（各 10 片），一张 [0,10)=3 的表归哪批都相容。
    b1 = make_inspection(client, 10)
    b2 = make_inspection(client, 10)
    response = client.post(
        "/archives",
        json={"sheets": [sheet((0, 10, 3))], "candidate_ids": [b1, b2]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "multiple"
    assert body["sheets"] is None
    assert len(body["alternatives"]) == 2
    first, second = body["alternatives"]
    first_target = first["sheets"][0]["inspection_id"]
    second_target = second["sheets"][0]["inspection_id"]
    assert {first_target, second_target} == {b1, b2}
    # 未入库方案没有补报号
    assert first["sheets"][0]["report_id"] is None

    # 多解不入库
    assert client.get(f"/inspections/{b1}").json()["report_count"] == 0
    assert client.get(f"/inspections/{b2}").json()["report_count"] == 0


def test_archive_individual_sheets_archivable_but_group_no_solution(client):
    """自测场景一：逐张都能归档，但整组无解。"""
    # 两个空批各 10 片。
    c1 = make_inspection(client, 10)
    c2 = make_inspection(client, 10)

    # 逐张试算：单张 [0,10)=6 归哪个空批都相容（多解即证明每张表都有处可去）。
    single = client.post(
        "/archives",
        json={"sheets": [sheet((0, 10, 6))], "candidate_ids": [c1, c2]},
    )
    assert single.status_code == 200
    assert single.json()["status"] == "multiple"
    assert len(single.json()["alternatives"]) == 2

    # 占用批 c2：已收 [0,10)=8，只剩 2 片余量。
    assert (
        client.post(f"/inspections/{c2}/reports", json=sheet((0, 10, 8))).status_code
        == 201
    )
    # 两张表 A=[0,10)=6、B=[0,10)=5：
    #   * c2 余量只有 2，两张表都进不了 c2；
    #   * 两张表只能都去 c1，但同区间两个不同计数（6 与 5）合并不相容 => 整组无解。
    group = client.post(
        "/archives",
        json={
            "sheets": [sheet((0, 10, 6)), sheet((0, 10, 5))],
            "candidate_ids": [c1, c2],
        },
    )
    assert group.status_code == 200
    body = group.json()
    assert body["status"] == "no_solution"
    assert body["alternatives"] is None
    assert body["sheets"] is None
    assert "无解" in body["message"]

    # 无解不入库：c1 仍为空，c2 只有先前的一份补报
    assert client.get(f"/inspections/{c1}").json()["report_count"] == 0
    assert client.get(f"/inspections/{c2}").json()["report_count"] == 1


def test_archive_no_solution_group_too_large_for_every_batch(client):
    """更直白的整组无解：每批余量都装不下任何一张表。"""
    b1 = make_inspection(client, 10)
    b2 = make_inspection(client, 10)
    for batch_id in (b1, b2):
        assert (
            client.post(
                f"/inspections/{batch_id}/reports", json=sheet((0, 10, 9))
            ).status_code
            == 201
        )
    # 每张表 [0,10)=5，任一批 9+5>10，一张都收不了。
    response = client.post(
        "/archives",
        json={
            "sheets": [sheet((0, 10, 5)), sheet((0, 10, 5))],
            "candidate_ids": [b1, b2],
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "no_solution"
    for batch_id in (b1, b2):
        assert client.get(f"/inspections/{batch_id}").json()["report_count"] == 1


def test_archive_trial_then_correction_aborts_write(tmp_path, monkeypatch):
    """自测场景二：试算期间候选批更正总片数，写入核对失败，整组退回。"""
    db_file = tmp_path / "race.db"
    app = create_app(str(db_file))
    with TestClient(app) as client:
        b1 = make_inspection(client, 10)
        b2 = make_inspection(client, 10)
        # 让唯一归属成立：批 2 用满容量，表只能归批 1。
        assert (
            client.post(f"/inspections/{b2}/reports", json=sheet((0, 10, 10))).status_code
            == 201
        )
        payload = {"sheets": [sheet((0, 10, 3))], "candidate_ids": [b1, b2]}

        from app import archiving

        original_commit = archiving.commit_archiving

        def commit_with_correction(connection, sheets, candidate_ids, snapshots, assignment):
            # 在试算结束后、正式写入前，用另一连接更正批 1 的总片数 10 -> 2。
            writer = db.connect(str(db_file))
            try:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(
                    "UPDATE inspections SET total_pieces = ? WHERE id = ?",
                    (2, b1),
                )
                writer.execute("COMMIT")
            finally:
                writer.close()
            return original_commit(
                connection, sheets, candidate_ids, snapshots, assignment
            )

        monkeypatch.setattr("app.main.commit_archiving", commit_with_correction)

        response = client.post("/archives", json=payload)
        assert response.status_code == 409
        assert str(b1) in response.json()["detail"]
        assert "退回" in response.json()["detail"]

        # 整组未入库
        assert client.get(f"/inspections/{b1}").json()["report_count"] == 0
        assert client.get(f"/inspections/{b2}").json()["report_count"] == 1
        # 更正本身生效
        assert client.get(f"/inspections/{b1}").json()["total_pieces"] == 2


def test_archive_trial_then_report_added_aborts_write(tmp_path, monkeypatch):
    """试算后、写入前，另一连接给候选批补报，已收计数变化，同样整组退回。"""
    db_file = tmp_path / "race2.db"
    app = create_app(str(db_file))
    with TestClient(app) as client:
        b1 = make_inspection(client, 10)
        b2 = make_inspection(client, 10)
        # 唯一归属：批 2 用满，表只能归批 1。
        assert (
            client.post(f"/inspections/{b2}/reports", json=sheet((0, 10, 10))).status_code
            == 201
        )

        from app import archiving

        original_commit = archiving.commit_archiving

        def commit_with_new_report(connection, sheets, candidate_ids, snapshots, assignment):
            writer = db.connect(str(db_file))
            try:
                writer.execute("BEGIN IMMEDIATE")
                db.insert_report(writer, b1, [(0, 10, 9)])
                writer.execute("COMMIT")
            finally:
                writer.close()
            return original_commit(
                connection, sheets, candidate_ids, snapshots, assignment
            )

        monkeypatch.setattr("app.main.commit_archiving", commit_with_new_report)

        response = client.post(
            "/archives",
            json={"sheets": [sheet((0, 10, 3))], "candidate_ids": [b1, b2]},
        )
        assert response.status_code == 409
        # 归档退回；已收计数里只有竞争者写入的那一份
        assert client.get(f"/inspections/{b1}").json()["report_count"] == 1
        assert client.get(f"/inspections/{b2}").json()["report_count"] == 1


def test_archive_trial_uses_read_only_snapshot(tmp_path):
    """试算本身只读、不开写锁：试算结束后其他连接的写入不被阻塞。"""
    db_file = tmp_path / "readonly.db"
    app = create_app(str(db_file))
    with TestClient(app) as client:
        b1 = make_inspection(client, 10)
        b2 = make_inspection(client, 10)

    conn = db.connect(str(db_file))
    try:
        plan = plan_archiving(conn, [[(0, 10, 3)]], [b1, b2])
        # 试算连接已结束事务、不持写锁；另一连接可以立即取得写锁并写入。
        writer = db.connect(str(db_file))
        try:
            writer.execute("BEGIN IMMEDIATE")
            report_id = db.insert_report(writer, b1, [(0, 10, 2)])
            writer.execute("COMMIT")
            assert report_id >= 1
        finally:
            writer.close()
        assert len(plan.feasible_assignments) == 2
    finally:
        conn.close()


def test_archive_three_sheets_unique_grouping(client):
    # 批 1：10 片空批；批 2：10 片但已用满（[0,10)=10），任何表都进不了。
    b1 = make_inspection(client, 10)
    b2 = make_inspection(client, 10)
    assert client.post(f"/inspections/{b2}/reports", json=sheet((0, 10, 10))).status_code == 201

    # 三张互不重叠的表，合计 9 片，只能全部归批 1（唯一归属）。
    response = client.post(
        "/archives",
        json={
            "sheets": [
                sheet((0, 10, 4)),
                sheet((10, 20, 3)),
                sheet((20, 30, 2)),
            ],
            "candidate_ids": [b1, b2],
        },
    )
    assert response.status_code == 201
    assert [item["inspection_id"] for item in response.json()["sheets"]] == [b1, b1, b1]
    report_ids = [item["report_id"] for item in response.json()["sheets"]]
    assert len(set(report_ids)) == 3  # 三张表对应三个不同补报号
    detail = client.get(f"/inspections/{b1}").json()
    assert detail["report_count"] == 3
    assert detail["segment_count"] == 3
    assert [r["id"] for r in detail["reports"]] == report_ids


def test_archive_merged_sheets_internal_conflict_no_solution(client):
    # 批 1 空批（10 片）；批 2 已用满，收不了任何表。
    b1 = make_inspection(client, 10)
    b2 = make_inspection(client, 10)
    assert client.post(f"/inspections/{b2}/reports", json=sheet((0, 10, 10))).status_code == 201

    # 两张表同区间 [0,10) 各报 5 与 4，合并即同区间两个不同计数 => 不相容；
    # 又没有别的批可去 => 无解，且不入库。
    response = client.post(
        "/archives",
        json={
            "sheets": [sheet((0, 10, 5)), sheet((0, 10, 4))],
            "candidate_ids": [b1, b2],
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "no_solution"
    assert client.get(f"/inspections/{b1}").json()["report_count"] == 0


def test_archive_full_group_rollback_on_write_failure(tmp_path, monkeypatch):
    """写入中途失败时整组回滚，不留下半组补报。"""
    db_file = tmp_path / "rollback.db"
    app = create_app(str(db_file))
    # 关闭 TestClient 的异常上抛，验证服务端真正返回 500 且事务回滚。
    with TestClient(app, raise_server_exceptions=False) as client:
        b1 = make_inspection(client, 10)
        b2 = make_inspection(client, 10)
        assert (
            client.post(f"/inspections/{b2}/reports", json=sheet((0, 10, 10))).status_code
            == 201
        )

        call_count = {"n": 0}
        original_insert = db.insert_report

        def flaky_insert(connection, inspection_id, segments):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("模拟第二张表写入失败")
            return original_insert(connection, inspection_id, segments)

        monkeypatch.setattr("app.db.insert_report", flaky_insert)

        response = client.post(
            "/archives",
            json={
                "sheets": [sheet((0, 10, 2)), sheet((10, 20, 2))],
                "candidate_ids": [b1, b2],
            },
        )
        assert response.status_code == 500
        # 第一张表的写入随事务回滚，不留任何数据
        assert client.get(f"/inspections/{b1}").json()["report_count"] == 0
        assert client.get(f"/inspections/{b1}").json()["segment_count"] == 0
