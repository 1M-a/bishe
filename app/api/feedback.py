"""居委人工纠错与模型迭代接口。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from flask import Blueprint, request
from sqlalchemy import func

from app.api import fail, ok
from app.auth import current_user, perm_required
from app.agents.security import detect_sensitive
from app.extensions import db
from app.models import AuditLog, FdeRecord, ModelFeedback, WorkItem

bp = Blueprint("feedback", __name__)
ISSUES = {"classify", "route", "split", "noise"}
LINES = {"物业", "综治", "民政", "卫健", "街道"}


def _visible_item(item: WorkItem, user) -> bool:
    if not item or not user:
        return False
    if user.role == "admin":
        return True
    if user.role != "community":
        return False
    fde = db.session.get(FdeRecord, item.fde_id) if item.fde_id else None
    creator = (fde.creator if fde else "") or ""
    # 居民公开入口产生的单据允许属地社区复核；内部首录只允许原录入人所在社区。
    return creator.startswith("居民") or creator == (user.name or "")


@bp.get("")
@perm_required("feedback.view")
def list_feedback():
    user = current_user()
    if not user or user.role not in ("community", "admin"):
        return fail("模型纠错只对社区和管理员开放"), 403
    days = request.args.get("days", "30")
    try:
        days = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        days = 30
    since = datetime.now() - timedelta(days=days)
    q = ModelFeedback.query.filter(ModelFeedback.created_at >= since)
    if user.role == "community":
        q = q.filter(ModelFeedback.actor_org == (user.org or ""))
    rows = q.order_by(ModelFeedback.id.desc()).limit(200).all()
    group_q = (db.session.query(
                    ModelFeedback.issue_type, ModelFeedback.trigger,
                    ModelFeedback.before_json, ModelFeedback.after_json,
                    func.count(ModelFeedback.id).label("support"),
                    func.sum(ModelFeedback.applied_count).label("applications"))
               .filter(ModelFeedback.status == "approved",
                       ModelFeedback.trigger != "",
                       ModelFeedback.created_at >= since))
    if user.role == "community":
        group_q = group_q.filter(ModelFeedback.actor_org == (user.org or ""))
    grouped = (group_q.group_by(
        ModelFeedback.issue_type, ModelFeedback.trigger,
        ModelFeedback.before_json, ModelFeedback.after_json).all())
    grouped_map = {
        (issue, trigger, before, after): (int(support), int(applications or 0))
        for issue, trigger, before, after, support, applications in grouped
    }
    return ok({
        "items": [{
            "id": r.id, "work_item_id": r.work_item_id,
            "issue_type": r.issue_type, "trigger": r.trigger,
            "before": json.loads(r.before_json or "{}"),
            "after": json.loads(r.after_json or "{}"),
            "reason": r.reason, "actor": r.actor,
            "support": grouped_map.get(
                (r.issue_type, r.trigger, r.before_json, r.after_json), (1, 0))[0],
            "applied_count": r.applied_count or 0,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
        } for r in rows],
        "rule_candidates": [{"issue_type": issue, "trigger": trigger,
                             "support": int(support), "ready": int(support) >= 3}
                            for issue, trigger, _before, _after, support, _apps in grouped],
        "counts": {
            "samples": len(rows),
            "rules_ready": sum(1 for *_key, support, _apps in grouped
                               if int(support) >= 3),
            "applications": sum(int(apps or 0) for *_key, _support, apps in grouped),
        },
    })


@bp.get("/candidates")
@perm_required("feedback.view")
def candidates():
    user = current_user()
    if not user or user.role not in ("community", "admin"):
        return fail("模型纠错只对社区和管理员开放"), 403
    rows = WorkItem.query.order_by(WorkItem.id.desc()).limit(200).all()
    rows = [x for x in rows if _visible_item(x, user)]
    return ok({"items": [{
        "id": x.id, "fde_id": x.fde_id, "subject": x.subject or "",
        "type": x.type or "",
        "target_lines": [v for v in (x.target_lines or "").split(",") if v],
        "created_at": x.created_at.strftime("%Y-%m-%d %H:%M") if x.created_at else "",
    } for x in rows]})


@bp.post("")
@perm_required("feedback.manage")
def create_feedback():
    user = current_user()
    if not user or user.role not in ("community", "admin"):
        return fail("模型纠错只对社区和管理员开放"), 403
    data = request.get_json(silent=True) or {}
    try:
        work_item_id = int(data.get("work_item_id"))
    except (TypeError, ValueError):
        return fail("缺少有效的业务项"), 400
    item = db.session.get(WorkItem, work_item_id)
    if not _visible_item(item, user):
        return fail("无权复核该业务项"), 403
    issue_type = (data.get("issue_type") or "classify").strip()
    if issue_type not in ISSUES:
        return fail("纠错类型不合法"), 400
    trigger = (data.get("trigger") or "").strip()[:32]
    if len(trigger) < 2:
        return fail("请填写至少 2 个字的触发词，便于后续复用"), 400
    fde = db.session.get(FdeRecord, item.fde_id) if item.fde_id else None
    if not fde or trigger not in (fde.raw_text or ""):
        return fail("触发词必须出现在该事项原文中"), 400
    corrected_type = (data.get("corrected_type") or "").strip()
    corrected_lines = data.get("corrected_lines") or []
    if corrected_type and corrected_type not in ("报修", "人口变动", "走访", "调解", "咨询", "隐患排查", "其他"):
        return fail("修正后的事项类型不合法"), 400
    if isinstance(corrected_lines, str):
        corrected_lines = [x.strip() for x in corrected_lines.replace("，", ",").split(",") if x.strip()]
    if any(x not in LINES for x in corrected_lines):
        return fail("修正后的业务线不合法"), 400
    before = {"type": item.type or "", "target_lines": [x for x in (item.target_lines or "").split(",") if x]}
    after = {"type": corrected_type or before["type"],
             "target_lines": list(dict.fromkeys(corrected_lines or before["target_lines"]))}
    reason = (data.get("reason") or "").strip()[:512]
    if len(reason) < 4:
        return fail("请填写纠错原因"), 400
    if detect_sensitive(trigger + " " + reason):
        return fail("触发词和原因不得填写手机号或身份证号"), 400
    row = ModelFeedback(
        work_item_id=item.id, issue_type=issue_type, trigger=trigger,
        before_json=json.dumps(before, ensure_ascii=False, sort_keys=True),
        after_json=json.dumps(after, ensure_ascii=False, sort_keys=True),
        reason=reason, actor=(user.name or "")[:32],
        actor_org=(user.org or "")[:64], status="approved")
    duplicate = ModelFeedback.query.filter_by(
        work_item_id=item.id, issue_type=issue_type, trigger=trigger,
        before_json=row.before_json, after_json=row.after_json,
        status="approved").first()
    if duplicate:
        return fail("这条业务项已经提交过相同纠错，不能重复计数"), 409
    db.session.add(row)
    db.session.add(AuditLog(
        actor=user.name, agent="feedback", action="create",
        input_digest=(f"work_item={item.id};trigger_hash="
                      f"{hashlib.sha256(trigger.encode('utf-8')).hexdigest()[:16]}"),
        output_digest=f"issue={issue_type};{before} -> {after}"[:250]))
    db.session.commit()
    support = (ModelFeedback.query.filter_by(
        issue_type=issue_type, trigger=trigger,
        before_json=row.before_json, after_json=row.after_json,
        status="approved").count())
    return ok({"id": row.id, "support": support,
               "rule_ready": support >= 3,
               "msg": ("已形成可复用规则候选" if support >= 3
                       else f"已记录第 {support} 次确认，累计 3 次后才自动应用")})
