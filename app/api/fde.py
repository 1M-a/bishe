"""FDE 首录接口"""
import csv
import hashlib
import io
import json
import os
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from flask import Blueprint, current_app, request, send_file
from sqlalchemy import func

from app.api import ok, fail
from app.agents.orchestrator import Orchestrator
from app.auth import current_user, perm_required
from app.extensions import db
from app.models import AuditLog, CaptureDraft, FdeRecord, WorkItem
# 业务项进度的判据统一在 app/itemstate.py：只看工单会漏掉"只写台账"的
# 民政/综治线，出现"居民端已办结、首录台仍待派发"的自相矛盾。
from app.itemstate import STAGE_STAFF, stage as item_stage

bp = Blueprint("fde", __name__)
orch = Orchestrator()
CAPTURE_FIELDS = ("source_community", "speaker", "sent_at", "category",
                 "tags", "pain", "suggestion", "owner", "status",
                 "item_type", "target_lines")
CAPTURE_CSV_HEADERS = ("来源小区", "发言人", "发言时间", "一级分类", "二级标签",
                       "痛点摘要", "居民建议", "责任主体", "当前状态")
CAPTURE_CSV_KEYS = ("source_community", "speaker", "sent_at", "category",
                    "tags", "pain", "suggestion", "owner", "status")

# 业务线顺序与拆解枚举一致（app/agents/parser.py:LINES）。
# 图表顺序写死在这里而不是"按数量降序"：同一业务线在今天和周报截图里
# 停在同一个位置，看图的人不用每次重新找。
LINE_ORDER = ("物业", "综治", "民政", "卫健", "街道")

# 「节省工时」的两个基准（分钟/次）。放在后端而不是模板里：
# 口径要改（比如访谈拿到真实均值）时只改这一处，页面文案与答辩口径同时生效。
MIN_PER_ITEM = 6      # 手工登记一条业务项并分派：填表、喊人、记本子
MIN_PER_REUSE = 10    # 复用历史档案免一次重复采集：翻档、打电话核对

# 业务项进度的判据统一在 app/itemstate.py。
def _by_day(model, start, end):
    """按自然日计数：{ '2026-10-19': 4, ... }

    走服务端 GROUP BY 而不是把行拉回来数——页面上的曲线和明细表用的是
    同一批 created_at，统计与明细不会各算各的。
    created_at 为 NULL 的老数据落不进任何一天（SQL 里 NULL >= x 是 NULL），
    这类行本来也没有"发生了什么时间"，计入某天反而是编数据。
    """
    rows = (db.session.query(func.date(model.created_at), func.count(model.id))
            .filter(model.created_at >= start, model.created_at < end)
            .group_by(func.date(model.created_at)).all())
    return {str(d): int(c) for d, c in rows if d}


def _today_rows(today_start):
    """今日录入的业务项；今天还没录过就退回最近几条，并如实告诉前端。

    返回 (要显示的行, 是否回退, 状态分布)。
    状态分布算的是**今日全部**事项，不是这 8 行：卡片上写"今日 N 项"，
    下面的分布却只数了屏上的 8 条，两处对不上，看一眼就露馅。
    """
    from app.models import Ledger, WorkItem, WorkOrder

    all_items = (WorkItem.query.filter(WorkItem.created_at >= today_start)
                 .order_by(WorkItem.id.desc()).all())
    fallback = False
    if not all_items:
        all_items = WorkItem.query.order_by(WorkItem.id.desc()).limit(6).all()
        fallback = True
    show = all_items[:8]

    # 工单/台账一次性批量取，别在循环里逐条查（8 条就是 8 次往返）。
    # 一条业务项理论上可能有多张工单（转派不产生新单，但历史数据存在），
    # 所以存成列表，判状态时看**全部**单子而不是最后一张。
    ids = [i.id for i in all_items]
    orders, filed = {}, set()
    if ids:
        for o in WorkOrder.query.filter(WorkOrder.work_item_id.in_(ids)).all():
            orders.setdefault(o.work_item_id, []).append(o)
        filed = {wid for (wid,) in db.session.query(Ledger.work_item_id)
                 .filter(Ledger.work_item_id.in_(ids)).distinct().all()}

    summary = {"待派发": 0, "已派单": 0, "办理中": 0, "已完成": 0}
    rows = []
    for it in all_items:
        st = item_stage(it, orders=orders.get(it.id, []), filed=(it.id in filed))
        status, badge = STAGE_STAFF[st]
        summary[status] = summary.get(status, 0) + 1
        if len(rows) < len(show):
            rows.append({
                "id": it.id,
                "subject": it.subject or "",
                "line": (it.target_lines or "").replace(",", "/"),
                "status": status,
                "badge": badge,
                "created_at": it.created_at.strftime("%m-%d %H:%M") if it.created_at else "",
            })
    return rows, fallback, summary, len(all_items)


@bp.post("")
@perm_required("fde.create")
def submit():
    data = request.get_json(silent=True) or {}
    raw = data.get("raw_text")
    # 类型必须先判：前端传数字/对象时 raw_text.strip() 会抛 AttributeError，
    # 被全局处理器兜成一个 500——用户看到"服务内部错误"，实际只是参数传错。
    if not isinstance(raw, str):
        if raw is None:
            return fail("请填写首录内容")
        return fail("首录内容必须是文本")
    raw = raw.strip()
    if not raw:
        return fail("raw_text 不能为空")
    if len(raw) < 5:
        return fail("内容太短（至少 5 个字），请把时间、地点、诉求说清楚")
    if len(raw) > 2000:
        return fail("首录内容过长（≤2000 字），请分条录入或精简后再提交")
    source = (data.get("source") or "text").strip()
    if source not in ("text", "voice", "image"):
        # 来源只用于统计与展示，但枚举外的值会让报表口径多出一类"幽灵来源"
        return fail("来源只能是 text / voice / image")
    # 记录人一律取会话身份，不信任前端传值（防止冒名录入）
    u = current_user()
    result = orch.handle_fde(raw, source=source, creator=u.name if u else "")
    return ok(result)


def _capture_scope(draft_id: str):
    """截图草稿仅对管理员及同一社区组织内的社区人员开放。"""
    u = current_user()
    if not u or u.role not in ("community", "admin"):
        return None
    q = CaptureDraft.query.filter_by(id=draft_id)
    if u.role == "community":
        if u.org:
            q = q.filter(CaptureDraft.org == u.org)
        else:
            q = q.filter(CaptureDraft.creator_id == u.id)
    return q.first()


def _capture_audit(action: str, draft: CaptureDraft, detail: str = ""):
    u = current_user()
    db.session.add(AuditLog(
        actor=(u.name if u else "system")[:32], agent="capture", action=action,
        input_digest=f"draft={draft.id} sha256={draft.source_sha256[:16]}",
        output_digest=(detail or draft.status)[:250],
    ))


def _capture_rows(draft: CaptureDraft) -> list[dict]:
    try:
        data = json.loads(draft.structured_json or "[]")
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _capture_payload(draft: CaptureDraft) -> dict:
    return {
        "id": draft.id, "status": draft.status,
        "source_sha256": draft.source_sha256, "source_size": draft.source_size,
        "source_mime": draft.source_mime, "ocr_text": draft.ocr_text or "",
        "rows": _capture_rows(draft), "analysis_source": draft.analysis_source or "",
        "fde_id": draft.confirmed_fde_id,
        "created_at": draft.created_at.strftime("%Y-%m-%d %H:%M") if draft.created_at else "",
    }


def _analyze_capture(draft: CaptureDraft, text: str):
    from app.capture import analyze, MAX_OCR_CHARS

    if not isinstance(text, str) or len(text) > MAX_OCR_CHARS:
        raise ValueError("OCR 原文最多 12000 字")
    draft.ocr_text = text.strip()
    rows, source = analyze(draft.ocr_text, draft.community_hint)
    suggested = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    draft.suggested_json = suggested
    draft.structured_json = suggested
    draft.expected_count = len(rows)
    draft.analysis_source = source
    draft.status = "pending" if rows else "needs_input"
    draft.updated_at = datetime.now()
    return rows, source


@bp.get("/capture/drafts")
@perm_required("fde.view")
def capture_drafts():
    u = current_user()
    if not u or u.role not in ("community", "admin"):
        return fail("截图草稿仅对居委会开放"), 403
    q = CaptureDraft.query
    if u.role == "community":
        if u.org:
            q = q.filter(CaptureDraft.org == u.org)
        else:
            q = q.filter(CaptureDraft.creator_id == u.id)
    rows = (q.filter(CaptureDraft.status.in_(("pending", "needs_input", "processing")))
            .order_by(CaptureDraft.updated_at.desc()).limit(20).all())
    return ok({"items": [{"id": d.id, "status": d.status,
                            "created_at": d.created_at.strftime("%m-%d %H:%M")
                            if d.created_at else "",
                            "source_sha256": d.source_sha256[:12]} for d in rows]})


@bp.post("/capture/preview")
@perm_required("fde.create")
def capture_preview():
    import uuid
    from app.capture import CaptureError, extract_text
    from app.evidence import save_photos

    u = current_user()
    if not u or u.role not in ("community", "admin"):
        return fail("截图收录仅对居委会开放"), 403
    upload = request.files.get("image")
    if not upload or not upload.filename:
        return fail("请选择一张聊天截图"), 400
    # 通过现有证据保存器按文件头识别格式、随机命名并计算 SHA-256。
    saved, errors = save_photos([upload], current_app.config["EVIDENCE_DIR"])
    if errors or not saved:
        return fail("；".join(errors) or "截图保存失败"), 400
    file_info = saved[0]
    draft = CaptureDraft(
        id=uuid.uuid4().hex, creator_id=u.id, creator=u.name or u.username,
        org=(u.org or "")[:64],
        community_hint=(request.form.get("community") or u.org or "").strip()[:64],
        source_file=file_info["stored"], source_sha256=file_info["sha256"],
        source_mime=file_info["mime"], source_size=file_info["size"],
        status="needs_input",
    )
    db.session.add(draft)
    db.session.flush()
    ocr_error = ""
    try:
        path = Path(current_app.config["EVIDENCE_DIR"]) / file_info["stored"]
        text, detected_mime = extract_text(path.read_bytes())
        if detected_mime != file_info["mime"]:
            raise CaptureError("图片格式校验不一致")
        rows, source = _analyze_capture(draft, text)
        ocr_error = ("" if text else "未识别到文字，可在原文框粘贴或手工补录")
        if source == "rules" and text:
            ocr_error = "当前为规则整理结果，请逐项核对并补充缺失信息"
        _capture_audit("capture_preview", draft,
                       f"OCR 字符 {len(text)}，候选 {len(rows)}，来源 {source or 'none'}")
    except Exception as exc:
        # 原图作为受控证据保留；文本可由工作人员补入并重新分析。
        draft.status = "needs_input"
        draft.updated_at = datetime.now()
        ocr_error = (str(exc)[:160] if isinstance(exc, CaptureError)
                     else "本机 OCR 暂不可用，请在识别原文中粘贴文字后重新分析")
        _capture_audit("capture_ocr_unavailable", draft,
                       f"OCR 失败 {type(exc).__name__}")
    db.session.commit()
    payload = _capture_payload(draft)
    payload["notice"] = ocr_error
    return ok(payload)


@bp.get("/capture/<draft_id>")
@perm_required("fde.view")
def capture_detail(draft_id):
    draft = _capture_scope(draft_id)
    if not draft:
        return fail("草稿不存在或无权查看"), 404
    if draft.status == "processing":
        existing = FdeRecord.query.filter_by(capture_draft_id=draft.id).first()
        if existing:
            count = WorkItem.query.filter_by(fde_id=existing.id).count()
            if draft.expected_count > 0 and count == draft.expected_count:
                draft.status = "confirmed"
                draft.confirmed_fde_id = existing.id
                draft.confirmed_at = draft.confirmed_at or datetime.now()
                draft.updated_at = datetime.now()
                _capture_audit("capture_confirm_recovered", draft,
                               f"fde={existing.id} items={count}")
                db.session.commit()
    _capture_audit("capture_view", draft, "查看截图识别草稿")
    db.session.commit()
    return ok(_capture_payload(draft))


@bp.get("/capture/<draft_id>/image")
@perm_required("fde.view")
def capture_image(draft_id):
    draft = _capture_scope(draft_id)
    if not draft:
        return fail("草稿不存在或无权查看"), 404
    if os.path.basename(draft.source_file or "") != draft.source_file:
        return fail("证据文件记录无效"), 404
    path = Path(current_app.config["EVIDENCE_DIR"]) / draft.source_file
    if not path.is_file():
        return fail("原截图已不可用"), 404
    _capture_audit("capture_image_view", draft, "查看原始截图")
    db.session.commit()
    response = send_file(path, mimetype=draft.source_mime, as_attachment=False,
                         conditional=False)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.post("/capture/<draft_id>/analyze")
@perm_required("fde.create")
def capture_analyze_draft(draft_id):
    draft = _capture_scope(draft_id)
    if not draft:
        return fail("草稿不存在或无权操作"), 404
    if draft.status not in ("needs_input", "pending"):
        return fail("当前草稿状态不可重新分析"), 409
    data = request.get_json(silent=True) or {}
    text = data.get("ocr_text", draft.ocr_text or "")
    if not isinstance(text, str) or len(text.strip()) < 5:
        return fail("请补充至少 5 个字的识别原文"), 400
    try:
        rows, source = _analyze_capture(draft, text)
    except ValueError as exc:
        return fail(str(exc)), 400
    _capture_audit("capture_analyze", draft,
                   f"候选 {len(rows)}，来源 {source or 'none'}")
    db.session.commit()
    payload = _capture_payload(draft)
    payload["notice"] = ("当前为规则整理结果，请逐项核对并补充缺失信息"
                             if rows and source == "rules" else
                             "" if rows else "未形成候选，请继续修订原文")
    return ok(payload)


def _validate_capture_rows(value):
    from app.capture import ITEM_TYPES, LINES

    if not isinstance(value, list) or not 1 <= len(value) <= 10:
        raise ValueError("请确认 1 至 10 条结构化记录")
    rows = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("识别结果格式无效")
        row = {}
        for key in CAPTURE_FIELDS:
            item = raw.get(key)
            if key in ("tags", "target_lines"):
                if isinstance(item, str):
                    item = [v.strip() for v in item.replace("，", ",").replace("/", ",").split(",")]
                if not isinstance(item, list):
                    item = []
                limit = 8 if key == "tags" else 5
                item = list(dict.fromkeys(str(v).strip()[:40] for v in item
                                          if isinstance(v, (str, int)) and str(v).strip()))[:limit]
                if key == "target_lines":
                    item = [v for v in item if v in LINES]
                    if not item:
                        raise ValueError("每条记录至少选择一个分发业务线")
                row[key] = item
            else:
                if not isinstance(item, str):
                    item = str(item) if item is not None else ""
                limits = {
                    "source_community": 64, "speaker": 64, "sent_at": 64,
                    "category": 40, "pain": 500, "suggestion": 500,
                    "owner": 64, "status": 32, "item_type": 16,
                }
                row[key] = item.strip()[:limits[key]]
        if row["item_type"] not in ITEM_TYPES:
            raise ValueError("业务类型不在允许范围")
        if len(row["pain"]) < 5:
            raise ValueError("痛点摘要至少填写 5 个字")
        if not row["source_community"]:
            raise ValueError("请填写来源小区")
        rows.append(row)
    return rows


@bp.post("/capture/<draft_id>/confirm")
@perm_required("fde.create")
def capture_confirm(draft_id):
    draft = _capture_scope(draft_id)
    if not draft:
        return fail("草稿不存在或无权操作"), 404
    if draft.status == "confirmed":
        return ok({"fde_id": draft.confirmed_fde_id, "idempotent": True})
    existing = FdeRecord.query.filter_by(capture_draft_id=draft.id).first()
    if existing:
        count = WorkItem.query.filter_by(fde_id=existing.id).count()
        if draft.expected_count > 0 and count == draft.expected_count:
            draft.status = "confirmed"
            draft.confirmed_fde_id = existing.id
            draft.confirmed_at = draft.confirmed_at or datetime.now()
            draft.updated_at = datetime.now()
            _capture_audit("capture_confirm_recovered", draft,
                           f"fde={existing.id} items={count}")
            db.session.commit()
            return ok({"fde_id": existing.id, "item_count": count,
                       "idempotent": True})
        return fail("正式记录已部分写入，为避免重复分发已锁定草稿，请联系管理员核查"), 409
    if draft.status != "pending":
        return fail("请先完成识别并检查候选结果"), 409
    data = request.get_json(silent=True) or {}
    try:
        rows = _validate_capture_rows(data.get("rows"))
    except ValueError as exc:
        return fail(str(exc)), 400

    # 原文只由确认后的摘要字段拼装；截图 OCR 全文不会流入正式 FDE。
    approved = []
    statements = []
    for row in rows:
        speaker = row["speaker"] or "未注明"
        statements.append(
            f"{row['source_community']}，{speaker}（{row['sent_at'] or '时间未注明'}）反映："
            f"{row['pain']}；建议：{row['suggestion'] or '未明确'}；责任主体：{row['owner']}")
        fields = {
            "来源小区": row["source_community"], "发言人": row["speaker"],
            "发言时间": row["sent_at"], "一级分类": row["category"],
            "二级标签": "；".join(row["tags"]), "痛点摘要": row["pain"],
            "居民建议": row["suggestion"], "责任主体": row["owner"],
            "当前状态": row["status"],
        }
        approved.append({"type": row["item_type"], "subject": row["pain"][:40],
                         "fields": fields, "target_lines": row["target_lines"]})
    raw_text = "\n".join(statements)
    if len(raw_text) > 12000:
        return fail("确认内容过长，请精简摘要后再入库"), 400

    # 原子领取草稿，双击或并发请求只有一个能进入正式主链。
    suggestion = draft.suggested_json or draft.structured_json or "[]"
    suggestion_hash = hashlib.sha256(suggestion.encode("utf-8")).hexdigest()[:16]
    approved_json = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    approved_hash = hashlib.sha256(approved_json.encode("utf-8")).hexdigest()[:16]
    claimed = (CaptureDraft.query.filter_by(id=draft.id, status="pending")
               .update({"status": "processing",
                        "structured_json": approved_json,
                        "expected_count": len(rows), "updated_at": datetime.now()},
                       synchronize_session=False))
    if claimed:
        _capture_audit("capture_confirm_review", draft,
                       f"suggestion={suggestion_hash} approved={approved_hash} "
                       f"edited={suggestion_hash != approved_hash} items={len(rows)}")
    db.session.commit()
    if not claimed:
        return fail("草稿正在处理或状态已变化，请刷新后查看"), 409

    existing = FdeRecord.query.filter_by(capture_draft_id=draft.id).first()
    if existing:
        count = WorkItem.query.filter_by(fde_id=existing.id).count()
        if count != len(rows):
            return fail("该草稿已创建部分业务记录，已停止重试以避免重复分发，请联系管理员核查"), 409
        draft.status, draft.confirmed_fde_id = "confirmed", existing.id
        draft.confirmed_at = datetime.now()
        db.session.commit()
        return ok({"fde_id": existing.id, "idempotent": True})

    u = current_user()
    try:
        result = orch.handle_fde(
            raw_text=raw_text, source="image", creator=u.name if u else "",
            approved_items=approved, capture_draft_id=draft.id)
    except Exception as exc:
        db.session.rollback()
        draft = db.session.get(CaptureDraft, draft_id)
        existing = FdeRecord.query.filter_by(capture_draft_id=draft_id).first()
        if not existing:
            draft.status = "pending"
            draft.updated_at = datetime.now()
        _capture_audit("capture_confirm_failed", draft, type(exc).__name__)
        db.session.commit()
        return fail("確認入庫失敗；草稿已保留，請檢查狀態後重試" if not existing
                    else "业务链已部分写入，为避免重复派发已锁定草稿，请联系管理员核查"), 500

    draft = db.session.get(CaptureDraft, draft_id)
    draft.status = "confirmed"
    draft.confirmed_fde_id = result["fde_id"]
    draft.confirmed_at = datetime.now()
    draft.updated_at = datetime.now()
    _capture_audit("capture_confirm", draft,
                   f"fde={draft.confirmed_fde_id} items={len(rows)}")
    db.session.commit()
    return ok({"fde_id": draft.confirmed_fde_id, "item_count": len(rows),
               "order_count": len(result.get("orders", [])),
               "ledger_count": len(result.get("ledgers", [])),
               "idempotent": False})


@bp.post("/capture/<draft_id>/reject")
@perm_required("fde.create")
def capture_reject(draft_id):
    draft = _capture_scope(draft_id)
    if not draft:
        return fail("草稿不存在或无权操作"), 404
    if draft.status not in ("pending", "needs_input"):
        return fail("当前草稿状态不可作废"), 409
    draft.status = "rejected"
    draft.rejected_at = datetime.now()
    draft.updated_at = datetime.now()
    _capture_audit("capture_reject", draft, "居委会作废识别草稿")
    db.session.commit()
    return ok({"status": draft.status})


@bp.get("/capture/<draft_id>/csv")
@perm_required("fde.view")
def capture_csv(draft_id):
    draft = _capture_scope(draft_id)
    if not draft:
        return fail("草稿不存在或无权导出"), 404
    if draft.status != "confirmed":
        return fail("只有已确认入库的记录可以导出"), 409
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(CAPTURE_CSV_HEADERS)
    for row in _capture_rows(draft):
        values = ["；".join(row.get(k, [])) if k == "tags" and isinstance(row.get(k), list)
                  else row.get(k, "") for k in CAPTURE_CSV_KEYS]
        # 防止把居民输入当 Excel 公式执行。
        values = ["'" + value if isinstance(value, str) and
                  value.lstrip("\ufeff\t\r\n ").startswith(("=", "+", "-", "@"))
                  else value for value in values]
        writer.writerow(values)
    data = buf.getvalue().encode("utf-8-sig")
    _capture_audit("capture_csv_export", draft,
                   f"导出 {len(_capture_rows(draft))} 行 CSV")
    db.session.commit()
    return send_file(io.BytesIO(data), mimetype="text/csv; charset=utf-8",
                     as_attachment=True, download_name=f"capture-{draft.id[:8]}.csv")


def _secure_count():
    """已按二级/三级管控的字段数（field_security 里 level>=2）。

    **必须 join work_item**。2026-10-23 的整体检查查出 15 条孤儿密级：
    一次手工清理（按依赖顺序删 order→ledger→item→fde）漏掉了 field_security，
    剩下的行仍然被 `level >= 2` 数进来，首页"敏感字段管控 46 个"里混着
    15 个已经不存在的业务项 —— 数字虚高，而且拿明细去对永远对不上。
    孤儿由 `scripts/fix_orphans.py` 清理；这里加 join 是为了即使再出现，
    卡片也不会骗人。
    """
    from app.models import FieldSecurity, WorkItem
    # 别写成 `db.session.query(func.count(X)).join(...).count()`：
    # 那样外层 count() 会把这个"已经 SELECT count(*) 的子查询"整体再数一遍行数，
    # 内层没有 GROUP BY ⇒ 永远返回一行 ⇒ 结果恒等于 1（有记录时）或 0。
    # 线上首页真的显示过"敏感字段管控 1 个"，而库里是 43 个。
    return (FieldSecurity.query
            .join(WorkItem, WorkItem.id == FieldSecurity.work_item_id)
            .filter(FieldSecurity.level >= 2).count())


@bp.get("/stats")
@perm_required("fde.view")
def stats():
    from app.models import AuditLog, FdeRecord, WorkItem
    return ok({
        "fde_count": FdeRecord.query.count(),
        "item_count": WorkItem.query.count(),
        "audit_count": AuditLog.query.count(),
        # 已按二级/三级管控的字段数（field_security 表，可查可审计）
        "secure_count": _secure_count(),
    })


@bp.get("/dashboard")
@perm_required("fde.view")
def dashboard():
    """事项登记首页数据：今日事项、督办提醒及可复核统计。

    为什么单开一个接口而不是模板里塞常量：这三块原本是写死的演示数字
    （12/9/15/5/5、18.5h、五条固定任务），录了十条新事项图表不动一下，
    评委只要问一句"这个 15 是今天的吗"就答不上来。现在全部现查：
      · 柱状图 = 本周 work_item.target_lines 拆分计数
      · 折线   = 本周每天 拆解/派单/台账 三张表的创建数
      · 今日任务 = 今日 work_item + 其工单的流转状态
    一条事项可同时派往多条业务线（target_lines 存 "综治,物业"），
    所以柱状图合计**会大于**事项数——前端 hint 里写明，不让两处对不上。
    """
    from app.agents.supervise import SuperviseAgent
    from app.models import FdeRecord, Ledger, WorkItem, WorkOrder, WorkOrderLog

    now = datetime.now()
    today = now.date()
    monday = today - timedelta(days=today.weekday())      # 本周一
    week_start = datetime.combine(monday, dtime.min)
    week_end = datetime.combine(today + timedelta(days=1), dtime.min)  # 明天零点，不含

    # —— 统计卡 ——
    items_week = WorkItem.query.filter(WorkItem.created_at >= week_start,
                                       WorkItem.created_at < week_end).count()
    reused_week = Ledger.query.filter(Ledger.reused.is_(True),
                                      Ledger.created_at >= week_start,
                                      Ledger.created_at < week_end).count()
    saved_min = items_week * MIN_PER_ITEM + reused_week * MIN_PER_REUSE

    # —— 业务线分布（本周）——
    # 只取这一列：拆多值字段必须逐行分，但没必要把正文/JSON 一起捞出来
    rows = (db.session.query(WorkItem.target_lines)
            .filter(WorkItem.created_at >= week_start,
                    WorkItem.created_at < week_end).all())
    counts = {k: 0 for k in LINE_ORDER}
    for (raw,) in rows:
        for name in str(raw or "").replace("，", ",").split(","):
            name = name.strip()
            if name in counts:            # 枚举外的历史脏值不计数，也不占位
                counts[name] += 1

    # —— 处理量趋势（本周一 ~ 今天）——
    # 只到今天就停：把周四~周日画成 0 会让曲线末端"看起来在暴跌"
    n_days = today.weekday() + 1
    days = [monday + timedelta(days=i) for i in range(n_days)]
    d_fde = _by_day(WorkItem, week_start, week_end)
    d_ord = _by_day(WorkOrder, week_start, week_end)
    d_led = _by_day(Ledger, week_start, week_end)
    keys = [d.isoformat() for d in days]

    today_rows, fallback, by_status, today_count = _today_rows(
        datetime.combine(today, dtime.min))

    return ok({
        "week": {
            "start": monday.isoformat(),
            "end": today.isoformat(),
            "label": "本周",
        },
        "stats": {
            "fde_count": FdeRecord.query.count(),
            "item_count": WorkItem.query.count(),
            "secure_count": _secure_count(),
            "item_week": items_week,
            "reused_week": reused_week,
            "saved_minutes": saved_min,
            "saved_hours": round(saved_min / 60, 1),
            # 口径随数字一起下发：页面悬浮提示与报表/答辩讲的是同一句话
            "saved_basis": (f"本周拆解 {items_week} 个业务项 × {MIN_PER_ITEM} 分钟"
                            f" + 复用历史档案 {reused_week} 条 × {MIN_PER_REUSE} 分钟"),
        },
        "lines": {"names": list(LINE_ORDER),
                  "counts": [counts[k] for k in LINE_ORDER]},
        "trend": {
            "labels": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][:n_days],
            "dates": keys,
            "parsed": [d_fde.get(k, 0) for k in keys],
            "dispatched": [d_ord.get(k, 0) for k in keys],
            "ledger": [d_led.get(k, 0) for k in keys],
        },
        "today": {
            "fallback": fallback,
            "date": today.isoformat(),
            "count": today_count,
            "by_status": by_status,
            "rows": today_rows,
        },
        "alerts": {
            "overdue": WorkOrder.query.filter(
                SuperviseAgent.overdue_condition(WorkOrder)).count(),
            # 只提醒“被退回后仍在重办”的单，已经重新办结的不继续占首页红点。
            "returned": (db.session.query(func.count(func.distinct(WorkOrder.id)))
                         .join(WorkOrderLog, WorkOrderLog.order_id == WorkOrder.id)
                         .filter(WorkOrderLog.action == "reject",
                                 WorkOrder.status != "done").scalar() or 0),
        },
    })
