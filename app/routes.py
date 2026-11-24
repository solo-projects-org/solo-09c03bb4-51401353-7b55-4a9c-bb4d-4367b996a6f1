"""网页路由：持仓上传、公告换报、期末合表、净变动查询、历史版本。"""

from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge

from .csv_utils import CsvError, parse_form_date
from .services import (
    MAX_MERGE_ACCOUNTS,
    canon,
    create_merge_draft,
    get_merge_run,
    get_net_change,
    list_history,
    list_merge_runs,
    replace_announcements,
    update_merge_assignments,
    upload_holding,
    verify_and_apply_merge,
)

bp = Blueprint("main", __name__)


@bp.app_errorhandler(RequestEntityTooLarge)
def too_large(_exc):
    flash("上传文件超过 16MB 限制，请拆分或压缩后再传。", "error")
    return redirect(url_for("main.index"))


def _read_file(field: str) -> tuple[str, bytes]:
    fh = request.files.get(field)
    if fh is None or fh.filename == "":
        raise CsvError(f"未选择文件（字段 {field}）")
    return fh.filename, fh.read()


def _render_index(result=None, rows=None, merge_view=None):
    if rows is None:
        result, rows = get_net_change()
    results, batches, holdings = list_history()
    merge_runs = list_merge_runs()
    return render_template(
        "index.html",
        result=result,
        rows=rows,
        results=results,
        batches=batches,
        holdings=holdings,
        merge_runs=merge_runs,
        merge_view=merge_view,
        max_merge_accounts=MAX_MERGE_ACCOUNTS,
        canon=canon,
    )


@bp.route("/")
def index():
    return _render_index()


def _accounts_from_form(field: str = "accounts") -> list[str]:
    raw = request.form.get(field, "")
    normalized = raw.replace("，", ",").replace("\n", ",")
    return [part.strip() for part in normalized.split(",") if part.strip()]


@bp.route("/holdings/<period>", methods=["POST"])
def upload(period: str):
    if period not in ("begin", "end"):
        abort(404)
    try:
        as_of = parse_form_date(request.form.get("as_of_date", ""), "日期")
        filename, raw = _read_file("file")
        outcome = upload_holding(period, as_of, filename, raw)
        flash(outcome.message, "success")
        if outcome.result_id is not None:
            return redirect(url_for("main.result_detail", result_id=outcome.result_id))
    except CsvError as exc:
        flash(f"持仓上传失败：{exc}。原数据未改动。", "error")
    except Exception as exc:  # pragma: no cover - 兜底
        flash(f"持仓上传发生未知错误：{exc}", "error")
    return redirect(url_for("main.index"))


@bp.route("/announcements", methods=["POST"])
def announcements_replace():
    try:
        filename, raw = _read_file("file")
        message, result_id = replace_announcements(filename, raw)
        flash(message, "success")
        if result_id is not None:
            return redirect(url_for("main.result_detail", result_id=result_id))
    except CsvError as exc:
        flash(f"公告换报失败：{exc}。新表已记为 failed，原生效表继续可用。", "error")
    except Exception as exc:  # pragma: no cover - 兜底
        flash(f"公告换报发生未知错误：{exc}", "error")
    return redirect(url_for("main.index"))


# ---------------------------------------------------------------- 期末合表

def _file_account_assignments() -> dict[int, list[str]]:
    """解析 file_accounts_<i> 多选框：同一文件可勾多个账户（多值）。"""
    labels_by_index: dict[int, list[str]] = {}
    for key, value in request.form.items(multi=True):
        if not key.startswith("file_accounts_"):
            continue
        try:
            idx = int(key.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            continue
        labels_by_index.setdefault(idx, []).append(value.strip())
    return {
        idx: [v for v in values if v] for idx, values in labels_by_index.items()
    }


@bp.route("/merge", methods=["POST"])
def merge_upload():
    """一次上传该报告日的多份期末持仓，登记为待核对草稿并进入核对页。"""
    try:
        report_date = parse_form_date(request.form.get("report_date", ""), "报告日")
        accounts = _accounts_from_form("accounts")
        files = request.files.getlist("files")
        files = [f for f in files if f is not None and f.filename]
        if not files:
            raise CsvError("未选择任何期末持仓 CSV（字段 files）")

        labels_by_index = _file_account_assignments()
        if not labels_by_index and len(files) == 1:
            # 单文件且未单独勾选：默认覆盖全部应报账户（多文件必须在核对页勾选）
            labels_by_index = {0: list(accounts)}

        uploads = []
        for idx, fh in enumerate(files):
            uploads.append((fh.filename, fh.read(), labels_by_index.get(idx, [])))

        run_id = create_merge_draft(report_date, accounts, uploads)
        flash(
            f"期末文件已入库（合表批次 #{run_id}）；请核对每份文件的账户覆盖后执行核对。",
            "success",
        )
        return redirect(url_for("main.merge_detail", run_id=run_id))
    except CsvError as exc:
        flash(f"期末合表草稿失败：{exc}。未写入任何合表数据。", "error")
    except Exception as exc:  # pragma: no cover - 兜底
        flash(f"期末合表草稿发生未知错误：{exc}", "error")
    return redirect(url_for("main.index"))


@bp.route("/merge/<int:run_id>")
def merge_detail(run_id: int):
    try:
        view = get_merge_run(run_id)
    except CsvError:
        abort(404)
    result, rows = get_net_change()
    return _render_index(result=result, rows=rows, merge_view=view)


@bp.route("/merge/<int:run_id>/verify", methods=["POST"])
def merge_verify(run_id: int):
    """执行核对：可先更新每份文件的账户勾选，再核对。"""
    rebase = request.form.get("rebase") == "1"
    try:
        assignments = _file_account_assignments()
        view = get_merge_run(run_id)
        file_ids = [row["id"] for row in view.files]
        # 页面上的勾选框是每份文件的完整覆盖声明：未提交即视为该文件不覆盖
        # 任何账户（清空勾选也以空集为准），这样调整后重试才不会被旧值干扰。
        by_id = {
            file_ids[idx]: assignments.get(idx, [])
            for idx in range(len(file_ids))
        }
        update_merge_assignments(run_id, by_id)

        outcome = verify_and_apply_merge(run_id, rebase=rebase)
        category = "success" if outcome.status == "verified" else "error"
        flash(outcome.message, category)
        # 核对通过后回到合表批次页：显示采用的文件 + 已生效的净变动表
        return redirect(url_for("main.merge_detail", run_id=run_id))
    except CsvError as exc:
        flash(f"期末合表失败：{exc}。已生效数据未改动。", "error")
    except Exception as exc:  # pragma: no cover - 兜底
        flash(f"期末合表发生未知错误：{exc}", "error")
    return redirect(url_for("main.merge_detail", run_id=run_id))


@bp.route("/results/<int:result_id>")
def result_detail(result_id: int):
    result, rows = get_net_change(result_id)
    if result is None:
        abort(404)
    return _render_index(result=result, rows=rows)


@bp.app_template_filter("d")
def decimal_filter(value) -> str:
    return canon(value)


@bp.app_template_filter("fromjson")
def fromjson_filter(value):
    import json

    try:
        return json.loads(value) if value else []
    except (TypeError, ValueError):
        return []
