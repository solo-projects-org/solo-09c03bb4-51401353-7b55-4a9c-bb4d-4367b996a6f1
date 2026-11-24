"""业务服务层：上传、整表换报、增量重算、期末合表、净变动查询。"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .csv_utils import CsvError, parse_announcements, parse_holdings
from .db import get_db
from .engine import Announcement, Lot, simulate, trace_paths
from .merge_verify import MergeInput, verify_merge


# ---------------------------------------------------------------- 小工具

def canon(d: Decimal) -> str:
    """Decimal -> 规范字符串（去尾零，中间不取整仅在运算阶段保证）。"""
    return format(d.normalize(), "f") if d != 0 else "0"


def _save_upload(raw: bytes, prefix: str) -> str:
    from flask import current_app

    directory = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(directory, exist_ok=True)
    name = f"{prefix}_{uuid.uuid4().hex}.csv"
    path = os.path.join(directory, name)
    with open(path, "wb") as fh:
        fh.write(raw)
    return name


def _load_active_announcements(conn) -> list[Announcement]:
    batch = conn.execute(
        "SELECT id FROM announcement_batch WHERE status='active' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if batch is None:
        return []
    rows = conn.execute(
        "SELECT effective_date, old_code, new_code, ratio "
        "FROM announcement WHERE batch_id=? "
        "ORDER BY effective_date, old_code",
        (batch["id"],),
    ).fetchall()
    return [
        Announcement(
            effective_date=r["effective_date"],
            old_code=r["old_code"],
            new_code=r["new_code"],
            ratio=Decimal(r["ratio"]),
        )
        for r in rows
    ]


def _current_holding(conn, period: str):
    return conn.execute(
        "SELECT * FROM holding_file WHERE period=? AND is_current=1 "
        "ORDER BY id DESC LIMIT 1",
        (period,),
    ).fetchone()


def _holding_rows(conn, file_id: int) -> list[tuple[str, Decimal]]:
    rows = conn.execute(
        "SELECT code, shares FROM holding_row WHERE file_id=? ORDER BY code",
        (file_id,),
    ).fetchall()
    return [(r["code"], Decimal(r["shares"])) for r in rows]


def _latest_result(conn):
    return conn.execute(
        "SELECT * FROM result_batch ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _format_path(path_json: str) -> str:
    steps = json.loads(path_json)
    if not steps:
        return "无折算"
    parts = []
    for eff, old, new, ratio in steps:
        parts.append(f"{eff}: {old} → {new}（{ratio}:1）")
    return "；".join(parts)


# ---------------------------------------------------------------- 重算

def _persist_result(
    conn,
    lots: list[Lot],
    ann_batch_id: int,
    begin_file_id: int,
    end_file_id: Optional[int],
    begin_date: str,
    end_date: str,
    note: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO result_batch "
        "(announcement_batch_id, begin_file_id, end_file_id, begin_date, end_date, trigger_note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ann_batch_id, begin_file_id, end_file_id, begin_date, end_date, note),
    )
    result_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO result_lot "
        "(result_batch_id, origin_code, end_code, adjusted_shares, path_json) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (
                result_id,
                lot.origin_code,
                lot.current_code,
                canon(lot.shares),
                json.dumps(
                    [(d, o, n, canon(r)) for d, o, n, r in lot.path],
                    ensure_ascii=False,
                ),
            )
            for lot in lots
        ],
    )
    return result_id


def _recompute_full(
    conn,
    announcements: list[Announcement],
    begin_file_id: int,
    end_file_id: Optional[int],
    begin_date: str,
    end_date: Optional[str],
    note: str,
) -> int:
    opening = _holding_rows(conn, begin_file_id)
    lots = simulate(opening, announcements, begin_date, end_date)
    active_batch = conn.execute(
        "SELECT id FROM announcement_batch WHERE status='active' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _persist_result(
        conn,
        lots,
        active_batch["id"],
        begin_file_id,
        end_file_id,
        begin_date,
        end_date,
        note,
    )


# ---------------------------------------------------------------- 持仓上传

@dataclass
class UploadResult:
    message: str
    result_id: Optional[int] = None


def upload_holding(period: str, as_of_date: str, filename: str, raw: bytes) -> UploadResult:
    """上传某一期完整持仓；同周期再次上传按整表替换处理。"""
    if period not in ("begin", "end"):
        raise CsvError("period 只能是 begin 或 end")
    rows = parse_holdings(raw)

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        stored_name = _save_upload(raw, f"holding_{period}")
        cur = conn.execute(
            "INSERT INTO holding_file (period, as_of_date, filename, row_count) "
            "VALUES (?, ?, ?, ?)",
            (period, as_of_date, stored_name, len(rows)),
        )
        file_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO holding_row (file_id, code, shares) VALUES (?, ?, ?)",
            [(file_id, r.code, canon(r.shares)) for r in rows],
        )
        conn.execute(
            "UPDATE holding_file SET is_current=0 WHERE period=? AND id<>?",
            (period, file_id),
        )

        begin_file = _current_holding(conn, "begin")
        end_file = _current_holding(conn, "end")
        result_id = None
        if begin_file is not None:
            # 期末未上传时窗口上不封顶，期末日在结果批次中记空
            end_date = end_file["as_of_date"] if end_file else None
            if end_date is not None and end_date < begin_file["as_of_date"]:
                raise CsvError(
                    f"期末日 {end_date} 早于期初日 {begin_file['as_of_date']}，无法折算"
                )
            result_id = _recompute_full(
                conn,
                _load_active_announcements(conn),
                begin_file["id"],
                end_file["id"] if end_file else None,
                begin_file["as_of_date"],
                end_date,
                f"上传{ '期初' if period == 'begin' else '期末' }持仓触发全量重算",
            )
        conn.commit()
        return UploadResult(
            message=f"{period} 持仓已保存（{len(rows)} 行，来源文件 {filename}）",
            result_id=result_id,
        )
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------- 公告整表换报

def replace_announcements(filename: str, raw: bytes) -> tuple[str, Optional[int]]:
    """整表换报公告。

    流程：
    1. 先在事务外解析/校验新表，失败则只登记 failed 批次，原 active 表不动；
    2. 与现表完全相同则跳过，不产生新版本；
    3. 代码级轻量模拟找出“可能受改动影响”的期初持仓；
    4. 事务内建新批次、仅对受影响来源重算，其余来源沿用上一结果批次的行；
    5. 切换 active/superseded。任一步失败原表仍可查。
    """
    conn = get_db()

    # 1) 解析校验（不落库失败之外的数据）
    try:
        new_anns = parse_announcements(raw)
    except CsvError as exc:
        _record_failed_batch(conn, filename, exc)
        raise

    old_anns = _load_active_announcements(conn)

    def _sig(anns: list[Announcement]) -> set[tuple[str, str, str, str]]:
        return {
            (a.effective_date, a.old_code, a.new_code, canon(a.ratio))
            for a in anns
        }

    old_sig = _sig(old_anns)
    new_sig = _sig(new_anns)
    if old_sig == new_sig:
        return ("新公告表与当前生效表完全一致，未产生新版本。", None)

    begin_file = _current_holding(conn, "begin")
    if begin_file is None:
        # 期初持仓尚未上传：只换公告表，等持仓上传时再折算
        try:
            conn.execute("BEGIN IMMEDIATE")
            new_id = _insert_announcement_batch(conn, new_anns, filename, "期初持仓未上传，先换表")
            conn.execute(
                "UPDATE announcement_batch SET status='superseded', "
                "superseded_at=datetime('now') WHERE status='active' AND id<>?",
                (new_id,),
            )
            conn.commit()
            return (f"公告已换报（{len(new_anns)} 行）；期初持仓上传后自动折算。", None)
        except Exception:
            conn.rollback()
            raise

    end_file = _current_holding(conn, "end")
    end_date = end_file["as_of_date"] if end_file else None
    begin_date = begin_file["as_of_date"]
    if end_date is not None and end_date < begin_date:
        _record_failed_batch(
            conn, filename, CsvError(f"期末日 {end_date} 早于期初日 {begin_date}")
        )
        raise CsvError(f"期末日 {end_date} 早于期初日 {begin_date}，无法折算")

    opening = _holding_rows(conn, begin_file["id"])

    # 上一结果若基于不同的期初文件，所有来源都必须重算，不能复用旧行
    prev_result = _latest_result(conn)
    begin_changed = (
        prev_result is not None
        and prev_result["begin_file_id"] != begin_file["id"]
    )

    # 2) 代码级轻量模拟，定位受影响的期初来源
    new_paths = trace_paths(
        [code for code, _ in opening], new_anns, begin_date, end_date
    )
    old_paths = trace_paths(
        [code for code, _ in opening], old_anns, begin_date, end_date
    )
    changed_keys = old_sig.symmetric_difference(new_sig)
    if begin_changed:
        affected = {code for code, _ in opening}
    else:
        affected = {
            origin
            for origin in new_paths.keys() | old_paths.keys()
            if _path_touches(new_paths.get(origin, []), changed_keys)
            or _path_touches(old_paths.get(origin, []), changed_keys)
        }

    # 3) 只对受影响来源做完整股数模拟
    affected_opening = [(c, s) for c, s in opening if c in affected]
    fresh_lots = simulate(affected_opening, new_anns, begin_date, end_date)

    # 4) 落库：新批次 + 增量结果批次
    try:
        conn.execute("BEGIN IMMEDIATE")
        stored_name = _save_upload(raw, "announcement")
        new_batch_id = _insert_announcement_batch(
            conn, new_anns, filename, "公告整表换报", stored_name=stored_name
        )

        cur = conn.execute(
            "INSERT INTO result_batch "
            "(announcement_batch_id, begin_file_id, end_file_id, begin_date, end_date, trigger_note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                new_batch_id,
                begin_file["id"],
                end_file["id"] if end_file else None,
                begin_date,
                end_date,
                f"公告换报：仅重算 {len(affected)} / {len(opening)} 条期初持仓",
            ),
        )
        new_result_id = cur.lastrowid

        # 未受影响的来源：沿用上一结果批次的折算行
        # 仅当上一结果基于同一份期初文件时才可复用，否则全部重算
        reused = 0
        if prev_result is not None and not begin_changed:
            old_lots = conn.execute(
                "SELECT origin_code, end_code, adjusted_shares, path_json "
                "FROM result_lot WHERE result_batch_id=?",
                (prev_result["id"],),
            ).fetchall()
            reusable = [r for r in old_lots if r["origin_code"] not in affected]
            conn.executemany(
                "INSERT INTO result_lot "
                "(result_batch_id, origin_code, end_code, adjusted_shares, path_json) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        new_result_id,
                        r["origin_code"],
                        r["end_code"],
                        r["adjusted_shares"],
                        r["path_json"],
                    )
                    for r in reusable
                ],
            )
            reused = len(reusable)

        conn.executemany(
            "INSERT INTO result_lot "
            "(result_batch_id, origin_code, end_code, adjusted_shares, path_json) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    new_result_id,
                    lot.origin_code,
                    lot.current_code,
                    canon(lot.shares),
                    json.dumps(
                        [(d, o, n, canon(r)) for d, o, n, r in lot.path],
                        ensure_ascii=False,
                    ),
                )
                for lot in fresh_lots
            ],
        )

        conn.execute(
            "UPDATE announcement_batch SET status='superseded', "
            "superseded_at=datetime('now') WHERE status='active' AND id<>?",
            (new_batch_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    msg = (
        f"公告整表换报成功（新表 {len(new_anns)} 行）；"
        f"受影响期初持仓 {len(affected)} 条已重算，{reused} 条沿用原折算结果。"
    )
    return msg, new_result_id


def _path_touches(
    path: list[tuple[str, str, str]], changed_keys: set[tuple]
) -> bool:
    # changed_keys 的元素是 (日期, 旧代码, 新代码, 比例)，路径只含前三段
    changed_triples = {(d, o, n) for (d, o, n, _r) in changed_keys}
    return any(step in changed_triples for step in path)



def _insert_announcement_batch(
    conn,
    anns: list[Announcement],
    filename: str,
    note: str,
    stored_name: Optional[str] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO announcement_batch (status, filename, row_count, note) "
        "VALUES ('active', ?, ?, ?)",
        (stored_name or filename, len(anns), note),
    )
    batch_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO announcement (batch_id, effective_date, old_code, new_code, ratio) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (batch_id, a.effective_date, a.old_code, a.new_code, canon(a.ratio))
            for a in anns
        ],
    )
    return batch_id


def _record_failed_batch(conn, filename: str, exc: Exception) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO announcement_batch (status, filename, row_count, note) "
            "VALUES ('failed', ?, 0, ?)",
            (filename, f"换报失败，原表继续生效：{exc}"),
        )
        conn.commit()
    except Exception:
        conn.rollback()


# ---------------------------------------------------------------- 期末合表

MAX_MERGE_ACCOUNTS = 12


@dataclass
class MergeResult:
    status: str  # awaiting_accounts / verified / retry_required / failed
    message: str
    run_id: int

    @property
    def ok(self) -> bool:
        return self.status == "verified"


def create_merge_draft(
    report_date: str,
    accounts: list[str],
    uploads: list[tuple[str, bytes, list[str]]],
) -> int:
    """入库一批待核对的期末文件（尚未生效），返回 merge_run.id。

    uploads 每项为 (原始文件名, 文件字节, 该文件勾选的应报账户)。
    文件先解析校验；任何一份解析失败则整批判失败（不产生草稿）。
    """
    accounts = _normalize_accounts(accounts)
    if not accounts:
        raise CsvError("至少填写一个应报账户")
    if len(accounts) > MAX_MERGE_ACCOUNTS:
        raise CsvError(f"应报账户最多 {MAX_MERGE_ACCOUNTS} 个，当前 {len(accounts)} 个")
    if not uploads:
        raise CsvError("至少上传一份期末持仓 CSV")

    parsed: list[tuple[str, list[str], list[tuple[str, Decimal]], str]] = []
    for filename, raw, file_accounts in uploads:
        labels = _normalize_accounts(file_accounts)
        unknown = [a for a in labels if a not in accounts]
        if unknown:
            raise CsvError(
                f"文件 {filename} 勾选了未填写的应报账户：{', '.join(unknown)}"
            )
        rows = parse_holdings(raw)
        stored = _save_upload(raw, "merge_end")
        parsed.append((filename, labels, [(r.code, r.shares) for r in rows], stored))

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current_end = _current_holding(conn, "end")
        cur = conn.execute(
            "INSERT INTO merge_run "
            "(report_date, accounts_json, status, message, snapshot_end_file_id) "
            "VALUES (?, ?, 'awaiting_accounts', ?, ?)",
            (
                report_date,
                json.dumps(accounts, ensure_ascii=False),
                "已入库待填写应报账户覆盖并核对",
                current_end["id"] if current_end is not None else 0,
            ),
        )
        run_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO merge_file "
            "(merge_run_id, filename, stored_name, row_count, rows_json, accounts_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    filename,
                    stored,
                    len(rows),
                    json.dumps(
                        [[c, canon(s)] for c, s in rows], ensure_ascii=False
                    ),
                    json.dumps(labels, ensure_ascii=False),
                )
                for filename, labels, rows, stored in parsed
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return run_id


def _normalize_accounts(accounts: Optional[list[str]]) -> list[str]:
    """去空白、去重并保持首次出现顺序。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in accounts or []:
        acc = (raw or "").strip()
        if not acc or acc in seen:
            continue
        seen.add(acc)
        out.append(acc)
    return out


@dataclass
class MergeRunView:
    run: object
    files: list[object]
    accounts: list[str]
    details: Optional[dict]


def _load_merge_run(conn, run_id: int) -> MergeRunView:
    run = conn.execute("SELECT * FROM merge_run WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise CsvError(f"合表批次 #{run_id} 不存在")
    files = conn.execute(
        "SELECT * FROM merge_file WHERE merge_run_id=? ORDER BY id", (run_id,)
    ).fetchall()
    accounts = json.loads(run["accounts_json"])
    details = json.loads(run["details_json"]) if run["details_json"] else None
    return MergeRunView(run=run, files=files, accounts=accounts, details=details)


def verify_and_apply_merge(run_id: int, rebase: bool = False) -> MergeResult:
    """核对合表草稿；通过则把合并后的期末持仓与净变动结果一起生效。

    - 核对计算在数据库事务之外进行，核对期间原入口仍可正常上传期末持仓；
    - 核对开始与结束各取一次「当前期末表」快照：期间若有新的期末表先入库，
      本次合表标记为 retry_required，不产生任何生效数据；
    - 草稿入库后、核对开始前若也有新的期末表先入库，同样提示重试；
      rebase=True 表示用户已确认以当前期末表为基线重新核对（显式重试）；
    - 失败或重试都不覆盖任何已生效的期末持仓或结果，旧结果仍可查。
    """
    conn = get_db()
    view = _load_merge_run(conn, run_id)
    run = view.run
    if run["status"] == "verified":
        return MergeResult("verified", "该合表批次已生效，无需重复核对", run_id)

    accounts = view.accounts
    inputs = [
        MergeInput(
            file_key=row["filename"],
            rows=[(c, Decimal(s)) for c, s in json.loads(row["rows_json"])],
            accounts=frozenset(json.loads(row["accounts_json"])),
        )
        for row in view.files
    ]

    current_end = _current_holding(conn, "end")
    current_end_id = current_end["id"] if current_end is not None else 0
    if current_end_id != run["snapshot_end_file_id"] and not rebase:
        conn.execute(
            "UPDATE merge_run SET status='retry_required', message=? WHERE id=?",
            ("合表草稿入库后有新的期末表先入库，本次合表未生效，请重试", run_id),
        )
        conn.commit()
        return MergeResult(
            "retry_required",
            "合表草稿入库后有新的期末表先入库，本次合表未生效，请重试",
            run_id,
        )

    # 本次核对开始快照（显式重试时以当前期末表为基线）
    conn.execute(
        "UPDATE merge_run SET status='awaiting_accounts', snapshot_end_file_id=?, "
        "message=? WHERE id=?",
        (current_end_id, "核对中", run_id),
    )
    conn.commit()
    snapshot_end_id = current_end_id

    # 纯计算核对，不占用数据库写锁，期间原入口可继续上传期末持仓
    try:
        outcome = verify_merge(inputs, accounts)
    except Exception as exc:  # pragma: no cover - 纯计算不应抛错，兜底不动已生效数据
        message = f"核对计算出错，本次合表未生效：{exc}"
        conn.execute(
            "UPDATE merge_run SET status='failed', message=? WHERE id=?",
            (message, run_id),
        )
        conn.commit()
        return MergeResult("failed", message, run_id)

    try:
        conn.execute("BEGIN IMMEDIATE")
        current_end = _current_holding(conn, "end")
        current_end_id = current_end["id"] if current_end is not None else 0
        if current_end_id != snapshot_end_id:
            conn.execute(
                "UPDATE merge_run SET status='retry_required', message=? WHERE id=?",
                (
                    "核对期间有新的期末持仓表先入库，本次合表未生效，请重新核对",
                    run_id,
                ),
            )
            conn.commit()
            return MergeResult(
                "retry_required",
                "核对期间有新的期末持仓表先入库，本次合表未生效，请重新上传并核对",
                run_id,
            )

        if outcome.status == "ok":
            begin_file = _current_holding(conn, "begin")
            if begin_file is None:
                message = "期初持仓尚未上传，无法计算净变动；请先上传期初持仓再合表"
                conn.execute(
                    "UPDATE merge_run SET status='failed', message=? WHERE id=?",
                    (message, run_id),
                )
                conn.commit()
                return MergeResult("failed", message, run_id)
            if run["report_date"] < begin_file["as_of_date"]:
                message = (
                    f"报告日 {run['report_date']} 早于期初日 "
                    f"{begin_file['as_of_date']}，无法折算"
                )
                conn.execute(
                    "UPDATE merge_run SET status='failed', message=? WHERE id=?",
                    (message, run_id),
                )
                conn.commit()
                return MergeResult("failed", message, run_id)

            adopted = [view.files[i] for i in outcome.chosen]
            merge_names = "、".join(row["filename"] for row in adopted)

            # 合并期末持仓：以整表替换方式写入 holding_file（period=end）
            holdings = outcome.holdings
            file_cur = conn.execute(
                "INSERT INTO holding_file "
                "(period, as_of_date, filename, row_count, source, merge_run_id) "
                "VALUES ('end', ?, ?, ?, 'merge', ?)",
                (
                    run["report_date"],
                    f"合表采用：{merge_names}",
                    len(holdings),
                    run_id,
                ),
            )
            end_file_id = file_cur.lastrowid
            conn.executemany(
                "INSERT INTO holding_row (file_id, code, shares) VALUES (?, ?, ?)",
                [(end_file_id, code, canon(shares)) for code, shares in holdings],
            )
            conn.execute(
                "UPDATE holding_file SET is_current=0 WHERE period='end' AND id<>?",
                (end_file_id,),
            )

            # 期末持仓与净变动表在同一事务内一起生效
            result_id = _recompute_full(
                conn,
                _load_active_announcements(conn),
                begin_file["id"],
                end_file_id,
                begin_file["as_of_date"],
                run["report_date"],
                "期末合表核对通过，合并期末持仓并重算净变动",
            )
            conn.execute(
                "UPDATE merge_run SET status='verified', message=?, "
                "end_file_id=?, result_batch_id=?, details_json=? WHERE id=?",
                (
                    outcome.message,
                    end_file_id,
                    result_id,
                    json.dumps(_witness_payload(outcome), ensure_ascii=False),
                    run_id,
                ),
            )
            adopted_ids = {row["id"] for row in adopted}
            conn.execute(
                "UPDATE merge_file SET adopted=0 WHERE merge_run_id=?", (run_id,)
            )
            conn.executemany(
                "UPDATE merge_file SET adopted=1 WHERE id=?",
                [(fid,) for fid in adopted_ids],
            )
            conn.commit()
            return MergeResult("verified", outcome.message, run_id)

        # 核对未通过：只记录本次失败，不动任何已生效数据
        conn.execute(
            "UPDATE merge_run SET status='failed', message=?, details_json=? WHERE id=?",
            (
                outcome.message,
                json.dumps(_witness_payload(outcome), ensure_ascii=False),
                run_id,
            ),
        )
        conn.commit()
        return MergeResult("failed", outcome.message, run_id)
    except Exception:
        conn.rollback()
        raise


def update_merge_assignments(run_id: int, assignments: dict[int, list[str]]) -> None:
    """更新合表批次中每份文件勾选的应报账户（供核对失败后调整重试）。"""
    conn = get_db()
    run = conn.execute("SELECT * FROM merge_run WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise CsvError(f"合表批次 #{run_id} 不存在")
    accounts = set(json.loads(run["accounts_json"]))
    files = conn.execute(
        "SELECT id FROM merge_file WHERE merge_run_id=?", (run_id,)
    ).fetchall()
    known_ids = {row["id"] for row in files}
    try:
        conn.execute("BEGIN IMMEDIATE")
        for file_id, labels in assignments.items():
            if file_id not in known_ids:
                raise CsvError(f"合表文件 #{file_id} 不属于该批次")
            cleaned = _normalize_accounts(labels)
            unknown = [a for a in cleaned if a not in accounts]
            if unknown:
                raise CsvError(
                    f"文件勾选了未填写的应报账户：{', '.join(unknown)}"
                )
            conn.execute(
                "UPDATE merge_file SET accounts_json=? WHERE id=?",
                (json.dumps(cleaned, ensure_ascii=False), file_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _witness_payload(outcome) -> dict:
    payload: dict = {"status": outcome.status}
    if outcome.differences:
        payload["differences"] = [
            {
                "code": d["code"],
                "shares_a": canon(d["shares_a"]),
                "shares_b": canon(d["shares_b"]),
            }
            for d in outcome.differences
        ]
    if outcome.witness_a:
        payload["witness_a"] = {
            "files": outcome.witness_a["files"],
            "holdings": {
                code: canon(shares)
                for code, shares in outcome.witness_a["holdings"].items()
            },
        }
    if outcome.witness_b:
        payload["witness_b"] = {
            "files": outcome.witness_b["files"],
            "holdings": {
                code: canon(shares)
                for code, shares in outcome.witness_b["holdings"].items()
            },
        }
    return payload


def list_merge_runs():
    conn = get_db()
    return conn.execute(
        "SELECT mr.*, "
        "(SELECT COUNT(*) FROM merge_file mf WHERE mf.merge_run_id=mr.id) AS file_count, "
        "(SELECT COUNT(*) FROM merge_file mf WHERE mf.merge_run_id=mr.id AND mf.adopted=1) "
        "AS adopted_count "
        "FROM merge_run mr ORDER BY mr.id DESC"
    ).fetchall()


def get_merge_run(run_id: int) -> MergeRunView:
    return _load_merge_run(get_db(), run_id)


# ---------------------------------------------------------------- 查询

@dataclass
class NetChangeRow:
    end_code: str
    sources: str
    adjusted_shares: Decimal
    reported_shares: Decimal
    net_change: Decimal
    source_details: list[dict]


def get_net_change(result_id: Optional[int] = None) -> tuple[Optional[object], list[NetChangeRow]]:
    conn = get_db()
    result = conn.execute(
        "SELECT rb.*, ab.filename AS ann_filename, "
        "hf.source AS end_source, hf.filename AS end_filename "
        "FROM result_batch rb JOIN announcement_batch ab ON ab.id=rb.announcement_batch_id "
        "LEFT JOIN holding_file hf ON hf.id=rb.end_file_id "
        + ("WHERE rb.id=?" if result_id is not None else "")
        + " ORDER BY rb.id DESC LIMIT 1",
        (result_id,) if result_id is not None else (),
    ).fetchone()
    if result is None:
        return None, []

    lots = conn.execute(
        "SELECT * FROM result_lot WHERE result_batch_id=? ORDER BY origin_code",
        (result["id"],),
    ).fetchall()

    reported: dict[str, Decimal] = {}
    if result["end_file_id"] is not None:
        for r in conn.execute(
            "SELECT code, shares FROM holding_row WHERE file_id=?",
            (result["end_file_id"],),
        ).fetchall():
            reported[r["code"]] = Decimal(r["shares"])
    # 按期末代码聚合折算来源
    grouped: dict[str, list[object]] = {}
    for lot in lots:
        grouped.setdefault(lot["end_code"], []).append(lot)

    codes = sorted(set(grouped.keys()) | set(reported.keys()))
    rows: list[NetChangeRow] = []
    for code in codes:
        lots_here = grouped.get(code, [])
        adjusted = sum(
            (Decimal(l["adjusted_shares"]) for l in lots_here), Decimal(0)
        )
        reported_value = reported.get(code, Decimal(0))  # 缺项按零
        details = [
            {
                "origin_code": l["origin_code"],
                "shares": Decimal(l["adjusted_shares"]),
                "path": _format_path(l["path_json"]),
            }
            for l in lots_here
        ]
        sources = "、".join(
            f"{d['origin_code']}（{canon(d['shares'])} 股）" for d in details
        ) or "—"
        rows.append(
            NetChangeRow(
                end_code=code,
                sources=sources,
                adjusted_shares=adjusted,
                reported_shares=reported_value,
                net_change=reported_value - adjusted,
                source_details=details,
            )
        )
    return result, rows


def list_history():
    conn = get_db()
    results = conn.execute(
        "SELECT rb.*, ab.status AS ann_status, ab.filename AS ann_filename "
        "FROM result_batch rb JOIN announcement_batch ab ON ab.id=rb.announcement_batch_id "
        "ORDER BY rb.id DESC"
    ).fetchall()
    batches = conn.execute(
        "SELECT * FROM announcement_batch ORDER BY id DESC"
    ).fetchall()
    holdings = conn.execute(
        "SELECT * FROM holding_file ORDER BY id DESC"
    ).fetchall()
    return results, batches, holdings
