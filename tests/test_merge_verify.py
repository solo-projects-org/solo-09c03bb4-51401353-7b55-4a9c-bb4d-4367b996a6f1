"""期末合表核对（纯计算）测试：凑不齐、股数不一致、多解但一致、不枚举组合。"""

from decimal import Decimal as D

from app import merge_verify
from app.merge_verify import MergeInput, verify_merge


def f(name, rows, accounts):
    return MergeInput(
        name,
        [(code, D(str(shares))) for code, shares in rows],
        frozenset(accounts),
    )


def test_unique_combination_passes():
    files = [
        f("acc1.csv", [("X", 100), ("Y", 5)], ["A1"]),
        f("acc2.csv", [("X", 50)], ["A2"]),
    ]
    out = verify_merge(files, ["A1", "A2"])
    assert out.status == "ok"
    assert out.holdings == [("X", D("150")), ("Y", D("5"))]
    assert out.chosen == [0, 1]


def test_missing_account_is_incomplete_with_reason():
    files = [f("acc1.csv", [("X", 100)], ["A1"])]
    out = verify_merge(files, ["A1", "A2"])
    assert out.status == "incomplete"
    assert "A2" in out.message
    assert "没有任何文件" in out.message or "没有被任何文件" in out.message


def test_overlapping_only_files_are_incomplete():
    # 每份文件都同时覆盖 A、C 或 B、C，任何组合都会让 C 出现两次
    files = [
        f("ac.csv", [("X", 1)], ["A", "C"]),
        f("bc.csv", [("X", 1)], ["B", "C"]),
    ]
    out = verify_merge(files, ["A", "B", "C"])
    assert out.status == "incomplete"
    assert "恰好出现一次" in out.message


def test_zero_share_row_is_treated_as_missing():
    # 同一代码在 A 文件为 0 股、在 B 文件为 100 股：两者组合的合计应一致
    files = [
        f("ab.csv", [("X", 100)], ["A", "B"]),
        f("a.csv", [("X", 0), ("Y", 5)], ["A"]),
        f("b.csv", [("X", 100), ("Y", 5)], ["B"]),
    ]
    out = verify_merge(files, ["A", "B"])
    assert out.status == "mismatch"
    assert any(d["code"] == "Y" for d in out.differences)


def test_multiple_combinations_consistent_passes():
    # 两种选法：ab 文件 vs a+b 文件，逐证券合计完全一致
    files = [
        f("ab.csv", [("X", 100), ("Y", 5)], ["A", "B"]),
        f("a.csv", [("X", 100), ("Y", 5)], ["A"]),
        f("b.csv", [("X", 0)], ["B"]),
    ]
    out = verify_merge(files, ["A", "B"])
    assert out.status == "ok"
    assert out.holdings == [("X", D("100")), ("Y", D("5"))]
    assert "所有可行组合" in out.message


def test_mismatch_reports_two_file_sets_and_a_difference():
    files = [
        f("ab.csv", [("X", 100)], ["A", "B"]),
        f("a.csv", [("X", 50), ("Z", 1)], ["A"]),
        f("b.csv", [("X", 50), ("Z", 1)], ["B"]),
    ]
    out = verify_merge(files, ["A", "B"])
    assert out.status == "mismatch"
    assert out.witness_a["files"] != out.witness_b["files"]
    # 两组文件都能恰好覆盖全部账户
    assert out.witness_a and out.witness_b
    diff_codes = {d["code"] for d in out.differences}
    assert "Z" in diff_codes
    assert out.differences[0]["shares_a"] != out.differences[0]["shares_b"]


def test_twelve_accounts_many_files_scales_without_enumerating(monkeypatch):
    accounts = [f"A{i:02d}" for i in range(12)]
    # 12 个单账户文件 + 一个覆盖全部账户的等价文件：可行组合 2 种，合计相同
    files = [
        f(f"f{i}.csv", [("X", i + 1)], [accounts[i]]) for i in range(12)
    ]
    files.append(f("all.csv", [("X", sum(range(1, 13)))], accounts))

    # 禁止出现“枚举完整组合”的行为：任何对 2^n 状态之外的笛卡尔积计数都会被拦截
    calls = {"n": 0}
    real_count = merge_verify._count_cover_solutions

    def counting(files_, accounts_, limit=2):
        calls["n"] += 1
        return real_count(files_, accounts_, limit=limit)

    monkeypatch.setattr(merge_verify, "_count_cover_solutions", counting)
    out = verify_merge(files, accounts)
    assert out.status == "ok"
    # 只统计了一次可行选法数量；一致性判定走线性代数/子集 DP，
    # 没有把 2^12 个文件组合逐一展开
    assert calls["n"] == 1


def test_fractional_shares_kept_exact():
    files = [
        f("a.csv", [("X", "0.125")], ["A"]),
        f("b.csv", [("X", "0.25")], ["B"]),
    ]
    out = verify_merge(files, ["A", "B"])
    assert out.status == "ok"
    assert dict(out.holdings)["X"] == D("0.375")


def test_no_accounts_rejected():
    out = verify_merge([f("a.csv", [("X", 1)], ["A"])], [])
    assert out.status == "incomplete"
