"""期末合表核对与合并（纯计算，无数据库依赖）。

输入是多份已解析的期末持仓，每份附带用户勾选的应报账户。
每份文件只能整表采用；每个应报账户在选定组合里必须恰好出现一次。

三种结论：
- incomplete：没有任何一种选法能恰好覆盖每个应报账户，给出原因；
- mismatch：存在多种可行组合且逐证券股数合计不同，给出两组文件与差异；
- ok：所有可行组合的逐证券合计股数完全一致，合并结果唯一。

一致性的证明不枚举组合：
1. 应报账户数 n <= 12。把文件对账户的覆盖写成 F×n 的 0/1 矩阵 B，
   任一可行选法 x 都满足 B^T x = 1。若某证券的股数向量 v 落在 B 的列空间
   （即账户覆盖列向量张成的空间）里，则对任意可行 x 都有 v·x 为常数——
   这是线性代数恒等式，不需要列出组合（见 _in_row_space）。
2. 对不在列空间里的证券，用账户子集 DP 求该证券合计的最小值与最大值
   （状态数 2^n，与文件组合数量无关）。两者相等即证明该证券在所有可行
   组合下恒定；不等则用极值的两组选法作为差异见证。
3. “凑不齐”同样由账户子集 DP（2^n 个状态）判定，并可给出手工调整提示。

因此核对的时间上界是 O(证券数 · 2^n · 文件数)，n <= 12 时完全可控，
且从不枚举、也不存储完整的文件组合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Optional, Sequence

ZERO = Decimal(0)


@dataclass(frozen=True)
class MergeInput:
    """一份待合表的期末持仓。"""

    file_key: str  # 展示用标识（原始文件名）
    rows: Sequence[tuple[str, Decimal]]  # 已去重、股数非负
    accounts: frozenset[str]  # 该文件覆盖的应报账户


@dataclass
class MergeOutcome:
    status: str  # ok / incomplete / mismatch
    message: str
    holdings: list[tuple[str, Decimal]] = field(default_factory=list)
    chosen: list[int] = field(default_factory=list)  # ok 时采用的文件下标
    witness_a: Optional[dict] = None
    witness_b: Optional[dict] = None
    differences: list[dict] = field(default_factory=list)


# --------------------------------------------------------------- 账户覆盖 DP

def _coverage_bits(accounts: frozenset[str], account_index: dict[str, int]) -> int:
    bits = 0
    for acc in accounts:
        bits |= 1 << account_index[acc]
    return bits


def _format_accounts(indices: int, account_order: Sequence[str]) -> str:
    return "、".join(
        account_order[i] for i in range(len(account_order)) if indices & (1 << i)
    )


def solve_cover(
    files: Sequence[MergeInput], accounts: Sequence[str]
) -> tuple[str, Optional[str], list[int]]:
    """求「每个应报账户恰好被覆盖一次」的文件集合。

    返回 (status, reason, chosen_indices)。ok 时 chosen_indices 是一组可行解。
    状态数是账户子集数 2^n，不随文件数量或组合数量增长。
    """
    n = len(accounts)
    idx = {acc: i for i, acc in enumerate(accounts)}
    full = (1 << n) - 1
    bits_list = [_coverage_bits(f.accounts, idx) for f in files]

    covering: list[list[int]] = [[] for _ in range(n)]
    for fi, fbits in enumerate(bits_list):
        for ai in range(n):
            if fbits & (1 << ai):
                covering[ai].append(fi)

    empty_accounts = [a for a, c in zip(accounts, covering) if not c]
    if empty_accounts:
        return (
            "incomplete",
            f"应报账户 {', '.join(empty_accounts)} 没有被任何文件勾选覆盖，无法凑齐",
            [],
        )

    reach = [False] * (1 << n)
    parent: list[Optional[int]] = [None] * (1 << n)
    parent_file: list[Optional[int]] = [None] * (1 << n)
    reach[0] = True
    for mask in range(1 << n):
        if not reach[mask]:
            continue
        for fi, fbits in enumerate(bits_list):
            if not fbits or (fbits & mask):
                continue  # 已覆盖的账户不能再被覆盖
            new_mask = mask | fbits
            if not reach[new_mask]:
                reach[new_mask] = True
                parent[new_mask] = mask
                parent_file[new_mask] = fi

    if not reach[full]:
        # 诊断：先看是否“允许重叠”就能覆盖全部账户
        reach_any = [False] * (1 << n)
        reach_any[0] = True
        for mask in range(1 << n):
            if not reach_any[mask]:
                continue
            for fbits in bits_list:
                if fbits:
                    reach_any[mask | fbits] = True
        if not reach_any[full]:  # pragma: no cover - 上面已拦截空账户
            missing = [
                accounts[ai]
                for ai in range(n)
                if not any(bits_list[fi] & (1 << ai) for fi in covering[ai])
            ]
            return (
                "incomplete",
                f"应报账户 {', '.join(missing)} 没有任何文件覆盖，无法凑齐",
                [],
            )

        best = 0
        for mask in range(1 << n):
            if reach_any[mask] and mask.bit_count() > best.bit_count():
                best = mask
        missing = [accounts[ai] for ai in range(n) if not (best & (1 << ai))]
        if not missing:
            # 所有账户都能被覆盖，但没有任何选法让它们各出现一次：
            # 说明覆盖结构必然重叠。给出重叠账户的候选文件，便于人工调整。
            hints = []
            for acc in accounts:
                ai = idx[acc]
                candidates = [
                    f"{files[fi].file_key}（同时覆盖 "
                    f"{_format_accounts(bits_list[fi] & ~(1 << ai), accounts) or '无'}）"
                    for fi in covering[ai]
                ]
                hints.append(f"账户 {acc} 的候选文件：{'、'.join(candidates)}")
            return (
                "incomplete",
                "无法让每个应报账户恰好出现一次：所有账户都有文件覆盖，"
                "但任何组合都会出现账户重叠或缺口。"
                + "；".join(hints)
                + "。请调整每份文件勾选的账户后重试",
                [],
            )

        hints = []
        for acc in missing:
            ai = idx[acc]
            candidates = [
                f"{files[fi].file_key}（同时覆盖 "
                f"{_format_accounts(bits_list[fi] & ~(1 << ai), accounts) or '无'}）"
                for fi in covering[ai]
            ]
            hints.append(f"账户 {acc} 只能由 {'、'.join(candidates)} 提供")
        return (
            "incomplete",
            "无法让每个应报账户恰好出现一次："
            f"最多只能同时满足 {best.bit_count()} / {n} 个应报账户。"
            + "；".join(hints)
            + "。请补充文件或调整每份文件的勾选账户后重试",
            [],
        )

    chosen: list[int] = []
    mask = full
    while mask:
        prev = parent[mask]
        fi = parent_file[mask]
        if prev is None or fi is None:  # pragma: no cover - DP 不变量
            break
        chosen.append(fi)
        mask = prev
    chosen.reverse()
    return "ok", None, chosen


def _count_cover_solutions(
    files: Sequence[MergeInput], accounts: Sequence[str], limit: int = 2
) -> int:
    """统计可行选法数量（封顶 limit）。

    0/1 背包式 DP：逐个文件、掩码从大到小更新，保证每份文件最多用一次，
    统计的是文件集合（与顺序无关）。状态数 2^n，不枚举组合。
    """
    n = len(accounts)
    idx = {acc: i for i, acc in enumerate(accounts)}
    full = (1 << n) - 1
    counts = [0] * (1 << n)
    counts[0] = 1
    for f in files:
        fbits = _coverage_bits(f.accounts, idx)
        if not fbits:
            continue
        for mask in range(full, -1, -1):
            c = counts[mask]
            if c == 0 or (mask & fbits):
                continue
            nm = mask | fbits
            counts[nm] = min(limit, counts[nm] + c)
    return counts[full]


# --------------------------------------------------------------- 精确线性代数

def _reduce(
    row: list[Fraction], basis: list[tuple[int, list[Fraction]]]
) -> list[Fraction]:
    """按主元消去。基向量已全简化，消去顺序不影响结果。"""
    for pivot, b in basis:
        if row[pivot] != 0:
            factor = row[pivot]
            row = [x - factor * y for x, y in zip(row, b)]
    return row


def _row_space_basis(
    rows: Sequence[Sequence[Fraction]],
) -> list[tuple[int, list[Fraction]]]:
    """返回行空间的一组全简化基（按主元升序），用于判定向量是否在行空间中。"""
    basis: list[tuple[int, list[Fraction]]] = []
    for raw in rows:
        row = _reduce([Fraction(x) for x in raw], basis)
        pivot = next((i for i, x in enumerate(row) if x != 0), None)
        if pivot is None:
            continue
        inv = Fraction(1, 1) / row[pivot]
        row = [x * inv for x in row]
        updated: list[tuple[int, list[Fraction]]] = []
        for p, b in basis:
            if b[pivot] != 0:
                factor = b[pivot]
                b = [x - factor * y for x, y in zip(b, row)]
            updated.append((p, b))
        updated.append((pivot, row))
        updated.sort(key=lambda item: item[0])
        basis = updated
    return basis


def _in_row_space(
    vec: Sequence[Fraction], basis: list[tuple[int, list[Fraction]]]
) -> bool:
    reduced = _reduce([Fraction(x) for x in vec], basis)
    return all(x == 0 for x in reduced)


# --------------------------------------------------------------- 组合与见证

def _combo_holdings(
    files: Sequence[MergeInput],
    accounts: Sequence[str],
    chosen: Sequence[int],
) -> dict[str, Decimal]:
    """合并一组文件，并校验每账户恰好一次；缺行自动按零处理（不写入合计）。"""
    idx = {acc: i for i, acc in enumerate(accounts)}
    cover = [0] * len(accounts)
    for fi in chosen:
        fbits = _coverage_bits(files[fi].accounts, idx)
        for ai in range(len(accounts)):
            if fbits & (1 << ai):
                cover[ai] += 1
    if any(c != 1 for c in cover):
        raise ValueError("选中的文件未让每个应报账户恰好出现一次")
    total: dict[str, Decimal] = {}
    for fi in chosen:
        for code, shares in files[fi].rows:
            total[code] = total.get(code, ZERO) + shares
    return total


def _difference_rows(
    a: dict[str, Decimal], b: dict[str, Decimal]
) -> list[dict]:
    diffs = []
    for code in sorted(set(a) | set(b)):
        av = a.get(code, ZERO)
        bv = b.get(code, ZERO)
        if av != bv:
            diffs.append({"code": code, "shares_a": av, "shares_b": bv})
    return diffs


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f") if d != 0 else "0"


def _extremal_cover(
    bits_list: Sequence[int],
    n: int,
    values: Sequence[Decimal],
    want_min: bool,
) -> tuple[Optional[Decimal], Optional[list[int]]]:
    """单证券合计的极值（账户子集 DP，状态数 2^n），返回极值与一组选法。

    “每账户恰好出现一次”的约束由掩码互斥保证；每个账户子集只保留一个
    当前最优值，状态与文件组合数量无关，因此本函数不枚举任何完整组合。
    """
    full = (1 << n) - 1
    best: list[Optional[Decimal]] = [None] * (1 << n)
    best[0] = ZERO
    chosen: list[Optional[int]] = [None] * (1 << n)
    for mask in range(1 << n):
        cur = best[mask]
        if cur is None:
            continue
        for fi, fbits in enumerate(bits_list):
            if not fbits or (fbits & mask):
                continue
            nm = mask | fbits
            cand = cur + values[fi]
            if best[nm] is None or (cand < best[nm] if want_min else cand > best[nm]):
                best[nm] = cand
                chosen[nm] = fi
    if best[full] is None:  # pragma: no cover - solve_cover 已保证可行
        return None, None
    path: list[int] = []
    mask = full
    while mask:
        fi = chosen[mask]
        if fi is None:  # pragma: no cover
            return None, None
        path.append(fi)
        mask ^= bits_list[fi]
    path.reverse()
    return best[full], path


def _find_witness(
    files: Sequence[MergeInput],
    accounts: Sequence[str],
    base_chosen: Sequence[int],
    value_maps: Sequence[dict[str, Decimal]],
    bits_list: Sequence[int],
) -> Optional[
    tuple[
        tuple[list[int], dict[str, Decimal]],
        tuple[list[int], dict[str, Decimal]],
    ]
]:
    """寻找两组可行组合，使至少一只证券合计不同；不存在则返回 None。

    一致性证明分两层，均不枚举完整组合：
    1. 线性代数：若某证券在各文件中的股数向量落在账户覆盖矩阵的列空间，
       则它在任意可行组合下的合计都是同一常数（一次判定）。
    2. 对列空间外的证券，用账户子集 DP（状态数 2^n，n <= 12）求其合计的
       最小值和最大值。两者相等即证明该证券在所有可行组合下恒定；
       不等则取极值两组组合作为差异见证。
    因此“所有可行组合一致”的结论最多来自 2^n 个账户子集状态，
    与文件组合数量无关。
    """
    n = len(accounts)

    # 账户列向量：第 a 列 = 哪些文件覆盖账户 a；其行空间 = B 的列空间
    account_rows = [
        [Fraction(1 if fbits & (1 << ai) else 0) for fbits in bits_list]
        for ai in range(n)
    ]
    basis = _row_space_basis(account_rows)

    base_total_map = _combo_holdings(files, accounts, base_chosen)

    all_codes = sorted({c for m in value_maps for c in m})
    row_space_cache: dict[tuple[Decimal, ...], bool] = {}
    extremal_cache: dict[tuple[Decimal, ...], tuple] = {}
    for code in all_codes:
        values = tuple(m.get(code, ZERO) for m in value_maps)
        if all(v == 0 for v in values):
            continue

        # 同一股数向量在多只证券上重复出现时（如缺行按零），复用判定结果
        cached = row_space_cache.get(values)
        if cached is None:
            cached = _in_row_space([Fraction(v) for v in values], basis)
            row_space_cache[values] = cached
        if cached:
            continue  # 列空间内：任意可行组合合计相同

        result = extremal_cache.get(values)
        if result is None:
            lo_choice = _extremal_cover(bits_list, n, list(values), want_min=True)
            hi_choice = _extremal_cover(bits_list, n, list(values), want_min=False)
            result = (lo_choice, hi_choice)
            extremal_cache[values] = result
        (lo, choice_lo), (hi, choice_hi) = result
        if lo is None or hi is None:  # pragma: no cover - solve_cover 已保证可行
            continue
        if lo == hi:
            continue  # 该证券在所有可行组合下恒定
        # 选一个与基准组合确实存在差异的极值组合作为见证
        map_lo = _combo_holdings(files, accounts, choice_lo)
        map_hi = _combo_holdings(files, accounts, choice_hi)
        if _difference_rows(base_total_map, map_lo):
            return (list(base_chosen), base_total_map), (choice_lo, map_lo)
        if _difference_rows(base_total_map, map_hi):
            return (list(base_chosen), base_total_map), (choice_hi, map_hi)
        # 两个极值都与基准相同却 lo != hi：理论不可达，保守返回极值对
        return (choice_lo, map_lo), (choice_hi, map_hi)
    return None


# --------------------------------------------------------------- 核对入口

def verify_merge(
    files: Sequence[MergeInput], accounts: Sequence[str]
) -> MergeOutcome:
    """核对一组期末文件能否合表，并在可行时给出合并持仓。"""
    if not accounts:
        return MergeOutcome("incomplete", "未填写任何应报账户，无法核对")

    status, reason, chosen = solve_cover(files, accounts)
    if status != "ok":
        return MergeOutcome("incomplete", reason or "无法凑齐")

    base_map = _combo_holdings(files, accounts, chosen)
    base_rows = sorted(base_map.items())

    solution_count = _count_cover_solutions(files, accounts, limit=2)
    if solution_count <= 1:
        return MergeOutcome(
            "ok",
            f"核对通过：{len(files)} 份文件、{len(accounts)} 个应报账户，"
            f"存在唯一可行组合，合并持仓 {len(base_rows)} 个证券",
            holdings=base_rows,
            chosen=list(chosen),
        )

    idx = {acc: i for i, acc in enumerate(accounts)}
    bits_list = [_coverage_bits(f.accounts, idx) for f in files]
    value_maps = [{code: shares for code, shares in f.rows} for f in files]

    witness = _find_witness(files, accounts, chosen, value_maps, bits_list)
    if witness is None:
        return MergeOutcome(
            "ok",
            f"核对通过：{len(files)} 份文件、{len(accounts)} 个应报账户，"
            f"所有可行组合的逐证券股数完全一致，合并持仓 {len(base_rows)} 个证券",
            holdings=base_rows,
            chosen=list(chosen),
        )

    (choice_a, map_a), (choice_b, map_b) = witness
    differences = _difference_rows(map_a, map_b)
    files_a = [files[i].file_key for i in choice_a]
    files_b = [files[i].file_key for i in choice_b]
    detail = "；".join(
        f"{d['code']}：组合A {_fmt(d['shares_a'])} 股 vs 组合B {_fmt(d['shares_b'])} 股"
        for d in differences[:5]
    )
    return MergeOutcome(
        "mismatch",
        "核对失败：存在多种可行组合，且逐证券股数不一致。"
        f"组合A 采用文件：{', '.join(files_a)}；"
        f"组合B 采用文件：{', '.join(files_b)}。"
        f"至少一项差异：{detail}",
        witness_a={"files": files_a, "holdings": map_a},
        witness_b={"files": files_b, "holdings": map_b},
        differences=differences,
    )
