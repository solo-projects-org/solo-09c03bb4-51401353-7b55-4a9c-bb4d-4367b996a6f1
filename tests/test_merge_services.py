"""期末合表服务层测试：入库、核对生效、失败可查、新表先入库需重试、采用文件。"""

import pytest

from app import create_app
from app.csv_utils import CsvError
from app.db import get_db
from app.services import (
    create_merge_draft,
    get_merge_run,
    get_net_change,
    list_merge_runs,
    replace_announcements,
    upload_holding,
    verify_and_apply_merge,
)


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "merge.db"),
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        }
    )
    yield app


def csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


BEGIN = csv_bytes(
    "证券代码,股数\n"
    "000001,100\n"
    "000002,200\n"
    "000003,50\n"
)
ANN = csv_bytes("生效日,旧代码,新代码,新旧股数比\n2026-01-15,000001,000001X,3\n")
ACC1 = csv_bytes("证券代码,股数\n000001X,200\n000002,200\n")
ACC2 = csv_bytes("证券代码,股数\n000001X,100\n000003,50\n")
ACC1_ALT = csv_bytes("证券代码,股数\n000001X,300\n000002,100\n")


def _setup_begin(app):
    upload_holding("begin", "2026-01-01", "begin.csv", BEGIN)
    replace_announcements("ann.csv", ANN)


def _net(rows):
    return {r.end_code: (r.adjusted_shares, r.reported_shares, r.net_change) for r in rows}


def test_merge_draft_requires_accounts_and_files(app):
    with app.app_context():
        with pytest.raises(CsvError):
            create_merge_draft("2026-01-31", [], [("a.csv", ACC1, ["ACC1"])])
        with pytest.raises(CsvError):
            create_merge_draft("2026-01-31", ["ACC1"], [])
        # 超过 12 个应报账户
        too_many = [f"A{i}" for i in range(13)]
        with pytest.raises(CsvError):
            create_merge_draft(
                "2026-01-31", too_many, [("a.csv", ACC1, [too_many[0]])]
            )


def test_merge_verifies_and_applies_end_holding_with_result(app):
    with app.app_context():
        _setup_begin(app)
        run_id = create_merge_draft(
            "2026-01-31",
            ["ACC1", "ACC2"],
            [("acc1.csv", ACC1, ["ACC1"]), ("acc2.csv", ACC2, ["ACC2"])],
        )
        outcome = verify_and_apply_merge(run_id)
        assert outcome.status == "verified"
        assert outcome.ok

        view = get_merge_run(run_id)
        assert view.run["status"] == "verified"
        assert view.run["result_batch_id"] is not None
        assert view.run["end_file_id"] is not None
        # 页面采用的恰是勾选为覆盖的两份文件
        assert [row["adopted"] for row in view.files] == [1, 1]

        result, rows = get_net_change()
        m = _net(rows)
        # 000001 折算 300 = 实报 200 + 100
        assert m["000001X"] == (300, 300, 0)
        assert m["000002"] == (200, 200, 0)
        assert m["000003"] == (50, 50, 0)

        end = get_db().execute(
            "SELECT * FROM holding_file WHERE id=?", (view.run["end_file_id"],)
        ).fetchone()
        assert end["source"] == "merge"
        assert end["is_current"] == 1
        assert "acc1.csv" in end["filename"] and "acc2.csv" in end["filename"]


def test_merge_incomplete_failure_keeps_previous_result(app):
    with app.app_context():
        _setup_begin(app)
        # 先有一份原入口上传的期末持仓并生成结果
        upload_holding("end", "2026-01-31", "old_end.csv", ACC1)
        old_result, old_rows = get_net_change()
        old_map = _net(old_rows)

        # 合表无法凑齐（ACC2 没被任何文件覆盖）
        run_id = create_merge_draft(
            "2026-01-31", ["ACC1", "ACC2"], [("acc1.csv", ACC1, ["ACC1"])]
        )
        outcome = verify_and_apply_merge(run_id)
        assert outcome.status == "failed"
        assert "ACC2" in outcome.message

        # 原结果与原期末持仓完全不动，仍可查询
        result, rows = get_net_change()
        assert result["id"] == old_result["id"]
        assert _net(rows) == old_map
        end = get_db().execute(
            "SELECT * FROM holding_file WHERE period='end' AND is_current=1"
        ).fetchone()
        assert end["source"] == "upload"

        view = get_merge_run(run_id)
        assert view.run["status"] == "failed"


def test_merge_mismatch_reports_two_file_sets_and_difference(app):
    with app.app_context():
        _setup_begin(app)
        # 组合1：ACC1_ALT 单文件覆盖 ACC1、ACC2；组合2：ACC1+ACC2 两个文件
        # 两者覆盖都合法，但股数不同 -> 必须失败并给出两组文件与差异
        combined = csv_bytes(
            "证券代码,股数\n"
            "000001X,250\n"
            "000002,200\n"
            "000003,60\n"
        )
        run_id = create_merge_draft(
            "2026-01-31",
            ["ACC1", "ACC2"],
            [
                ("combined.csv", combined, ["ACC1", "ACC2"]),
                ("acc1.csv", ACC1, ["ACC1"]),
                ("acc2.csv", ACC2, ["ACC2"]),
            ],
        )
        outcome = verify_and_apply_merge(run_id)
        assert outcome.status == "failed"
        assert "逐证券股数不一致" in outcome.message

        view = get_merge_run(run_id)
        details = view.details
        assert details["status"] == "mismatch"
        assert details["witness_a"]["files"] != details["witness_b"]["files"]
        assert details["differences"]
        first = details["differences"][0]
        assert first["shares_a"] != first["shares_b"]

        # 失败不影响已生效数据（此时期末仍未上传任何表）
        result, rows = get_net_change()
        assert result is not None
        assert get_db().execute(
            "SELECT COUNT(*) c FROM holding_file WHERE period='end' AND is_current=1"
        ).fetchone()["c"] == 0


def test_new_end_file_during_verification_requires_retry(app):
    with app.app_context():
        _setup_begin(app)
        # 草稿入库时没有生效的期末持仓
        run_id = create_merge_draft(
            "2026-01-31", ["ACC1"], [("acc1.csv", ACC1, ["ACC1"])]
        )
        # 核对前，原入口先上传了新的期末表
        upload_holding("end", "2026-01-31", "new_end.csv", ACC1_ALT)

        outcome = verify_and_apply_merge(run_id)
        assert outcome.status == "retry_required"
        assert "重试" in outcome.message
        view = get_merge_run(run_id)
        assert view.run["status"] == "retry_required"
        # 新期末表未被合表覆盖
        end = get_db().execute(
            "SELECT * FROM holding_file WHERE period='end' AND is_current=1"
        ).fetchone()
        assert end["source"] == "upload"

        # 显式重试：以当前期末表为基线可正常生效
        outcome2 = verify_and_apply_merge(run_id, rebase=True)
        assert outcome2.status == "verified"
        result, rows = get_net_change()
        assert result["end_file_id"] == get_merge_run(run_id).run["end_file_id"]


def test_merge_after_announcement_replace_uses_active_table(app):
    with app.app_context():
        _setup_begin(app)
        run_id = create_merge_draft(
            "2026-01-31", ["ACC1"], [("acc1.csv", ACC1, ["ACC1"])]
        )
        assert verify_and_apply_merge(run_id).status == "verified"

        # 换公告后，合表产生的净变动可基于新公告再次计算（原入口换报链路兼容）
        ann2 = csv_bytes("生效日,旧代码,新代码,新旧股数比\n2026-01-15,000001,000001X,2\n")
        replace_announcements("ann2.csv", ann2)
        result, rows = get_net_change()
        m = _net(rows)
        assert m["000001X"][0] == 200  # 100 * 2


def test_merge_run_listing_and_duplicate_verify(app):
    with app.app_context():
        _setup_begin(app)
        run_id = create_merge_draft(
            "2026-01-31", ["ACC1"], [("acc1.csv", ACC1, ["ACC1"])]
        )
        assert verify_and_apply_merge(run_id).status == "verified"
        # 重复核对不再产生新结果批次
        before = get_db().execute("SELECT COUNT(*) c FROM result_batch").fetchone()["c"]
        again = verify_and_apply_merge(run_id)
        assert again.status == "verified"
        after = get_db().execute("SELECT COUNT(*) c FROM result_batch").fetchone()["c"]
        assert before == after

        runs = list_merge_runs()
        assert runs and runs[0]["id"] == run_id


def test_merge_report_date_before_begin_fails_cleanly(app):
    with app.app_context():
        _setup_begin(app)
        run_id = create_merge_draft(
            "2025-12-31", ["ACC1"], [("acc1.csv", ACC1, ["ACC1"])]
        )
        outcome = verify_and_apply_merge(run_id)
        assert outcome.status == "failed"
        assert "早于期初日" in outcome.message
        # 未生效期末持仓
        assert get_db().execute(
            "SELECT COUNT(*) c FROM holding_file WHERE period='end'"
        ).fetchone()["c"] == 0
