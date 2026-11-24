"""期末合表网页端到端测试：多文件上传、勾选账户、核对、失败提示、采用文件展示。"""

import io

import pytest

from app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "merge_web.db"),
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        }
    )
    return app.test_client()


BEGIN = "证券代码,股数\n000001,100\n000002,200\n000003,50\n".encode()
ANN = "生效日,旧代码,新代码,新旧股数比\n2026-01-15,000001,000001X,3\n".encode()
ACC1 = "证券代码,股数\n000001X,200\n000002,200\n".encode()
ACC2 = "证券代码,股数\n000001X,100\n000003,50\n".encode()


def _upload_holding(client, period, date, data):
    return client.post(
        f"/holdings/{period}",
        data={"as_of_date": date, "file": (io.BytesIO(data), "x.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def _prepare(client):
    _upload_holding(client, "begin", "2026-01-01", BEGIN)
    client.post(
        "/announcements",
        data={"file": (io.BytesIO(ANN), "ann.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def _merge(client, accounts, files, labels):
    data = {
        "report_date": "2026-01-31",
        "accounts": accounts,
        "files": [(io.BytesIO(raw), name) for name, raw in files],
    }
    data.update(labels)
    return client.post(
        "/merge", data=data, content_type="multipart/form-data", follow_redirects=True
    )


def test_merge_page_renders_form(client):
    resp = client.get("/")
    text = resp.data.decode()
    assert "期末合表" in text
    assert "最多 12 个" in text


def test_merge_upload_verify_and_show_adopted_files(client):
    _prepare(client)
    resp = _merge(
        client,
        "ACC1,ACC2",
        [("acc1.csv", ACC1), ("acc2.csv", ACC2)],
        {"file_accounts_0": ["ACC1"], "file_accounts_1": ["ACC2"]},
    )
    assert resp.status_code == 200
    text = resp.data.decode()
    assert "合表批次" in text
    assert "/merge/1/verify" in text

    resp = client.post(
        "/merge/1/verify",
        data={"file_accounts_0": ["ACC1"], "file_accounts_1": ["ACC2"]},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    text = resp.data.decode()
    assert resp.status_code == 200
    assert "核对通过" in text
    assert "已采用" in text
    assert "acc1.csv" in text and "acc2.csv" in text
    # 净变动表：000001X 折算 300、实报 300、净变动 0
    assert "000001X" in text
    assert "合表核对通过" in text


def test_incomplete_merge_shows_reason_and_can_be_fixed(client):
    _prepare(client)
    _merge(
        client,
        "ACC1,ACC2",
        [("acc1.csv", ACC1), ("acc2.csv", ACC2)],
        {"file_accounts_0": ["ACC1"], "file_accounts_1": ["ACC2"]},
    )
    # 清掉第二份文件的勾选：ACC2 没人覆盖 -> 凑不齐
    resp = client.post(
        "/merge/1/verify",
        data={"file_accounts_0": ["ACC1"]},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    text = resp.data.decode()
    assert "无法凑齐" in text
    assert "ACC2" in text

    # 调整勾选后重试可通过
    resp = client.post(
        "/merge/1/verify",
        data={"file_accounts_0": ["ACC1"], "file_accounts_1": ["ACC2"]},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "核对通过" in resp.data.decode()


def test_failed_merge_keeps_previous_result_viewable(client):
    _prepare(client)
    # 先由原入口上传期末持仓并生成结果
    _upload_holding(client, "end", "2026-01-31", ACC1)
    before = client.get("/").data.decode()
    assert "000003" in before  # 原期末文件没有 000003，折算侧仍有 50

    resp = _merge(
        client,
        "ACC1,ACC2",
        [("acc1.csv", ACC1)],
        {"file_accounts_0": ["ACC1"]},
    )
    assert "合表批次" in resp.data.decode()
    resp = client.post(
        "/merge/1/verify",
        data={"file_accounts_0": ["ACC1"]},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    text = resp.data.decode()
    assert "无法凑齐" in text
    # 原结果仍可查（历史结果批次可打开）
    assert "/results/" in text
    detail = client.get("/results/1")
    assert detail.status_code == 200


def test_merge_draft_rejects_too_many_accounts(client):
    _prepare(client)
    accounts = ",".join(f"A{i}" for i in range(13))
    resp = _merge(
        client, accounts, [("acc1.csv", ACC1)], {"file_accounts_0": ["A0"]}
    )
    text = resp.data.decode()
    assert "期末合表草稿失败" in text
    assert "最多 12 个" in text


def test_each_file_gets_its_own_checkbox_group(client):
    """回归：核对页每份文件的勾选框必须按文件下标命名（file_accounts_0/1/2），
    内层账户循环不能覆盖外层文件下标。"""
    _prepare(client)
    third = "证券代码,股数\n000003,10\n".encode()
    resp = _merge(
        client,
        "ACC1,ACC2,ACC3",
        [("acc1.csv", ACC1), ("acc2.csv", ACC2), ("acc3.csv", third)],
        {
            "file_accounts_0": ["ACC1"],
            "file_accounts_1": ["ACC2"],
            "file_accounts_2": ["ACC3"],
        },
    )
    text = resp.data.decode()
    for idx in (0, 1, 2):
        assert f'name="file_accounts_{idx}"' in text
    # 页面回显的勾选与提交一致
    assert text.count(" checked>") == 3

    # 用页面回显的表单结构重新提交也能通过核对
    resp = client.post(
        "/merge/1/verify",
        data={
            "file_accounts_0": ["ACC1"],
            "file_accounts_1": ["ACC2"],
            "file_accounts_2": ["ACC3"],
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "核对通过" in resp.data.decode()


def test_retry_required_when_new_end_file_arrives(client):
    _prepare(client)
    _merge(
        client,
        "ACC1",
        [("acc1.csv", ACC1)],
        {"file_accounts_0": ["ACC1"]},
    )
    # 核对前，原入口先上传一份新的期末表
    _upload_holding(client, "end", "2026-01-31", ACC2)
    resp = client.post(
        "/merge/1/verify",
        data={"file_accounts_0": ["ACC1"]},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    text = resp.data.decode()
    assert "重试" in text
    assert "retry_required" in text


def test_merge_upload_requires_file(client):
    _prepare(client)
    resp = client.post(
        "/merge",
        data={"report_date": "2026-01-31", "accounts": "ACC1"},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "未选择任何期末持仓" in resp.data.decode()
