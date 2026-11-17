"""居务会议与议事闭环。

流程：建会 → 加议题 → 发出通知 → 记录决议/签到 → 定稿并转任务 → 居务公开。
内部纪要与公开稿分列保存；发布接口从不读取 minutes_md，避免把讨论过程、
个人发言或敏感个案带到居民端。
"""
import hashlib
from datetime import datetime

from flask import Blueprint, request

from app.agents.security import detect_public_personal_detail, detect_sensitive
from app.api import fail, ok
from app.auth import current_user, perm_required
from app.extensions import db
from app.models import AuditLog, Meeting, MeetingItem, Notice, Task, User

bp = Blueprint("meetings", __name__)

KINDS = ("居务会", "两委联席会", "居民代表会议", "议事协商")
DT_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d")


def _parse_dt(value):
    raw = (value or "").strip()
    for fmt in DT_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _actor():
    u = current_user()
    return u.name if u else "system"


def _scoped():
    q = Meeting.query
    u = current_user()
    if not u:
        return q.filter(Meeting.id == -1)
    if u.role in ("street", "admin"):
        return q
    if not u.org:
        return q.filter(Meeting.id == -1)
    return q.filter(Meeting.org == u.org)


def _load(mid: int):
    m = db.session.get(Meeting, mid)
    if not m:
        return None, (fail("会议不存在"), 404)
    u = current_user()
    if not u or (u.role not in ("street", "admin") and u.org != m.org):
        return None, (fail("会议不存在"), 404)
    return m, None


def _item_row(i: MeetingItem):
    return {
        "id": i.id, "title": i.title, "detail": i.detail or "",
        "decision": i.decision or "", "assignee_role": i.assignee_role or "",
        "assignee_org": i.assignee_org or "",
        "deadline": i.deadline.strftime("%Y-%m-%d %H:%M") if i.deadline else "",
        "is_public": bool(i.is_public), "task_id": i.task_id,
    }


def _row(m: Meeting, detail=False):
    data = {
        "id": m.id, "org": m.org, "kind": m.kind, "title": m.title,
        "held_at": m.held_at.strftime("%Y-%m-%d %H:%M") if m.held_at else "",
        "location": m.location or "", "convener": m.convener or "",
        "status": m.status or "draft", "expected_count": m.expected_count or 0,
        "present_count": m.present_count or 0,
        "public_summary": m.public_summary or "",
        "published_notice_id": m.published_notice_id,
        "creator": m.creator or "",
        "created_at": m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else "",
        "notified_at": m.notified_at.strftime("%Y-%m-%d %H:%M") if m.notified_at else "",
        "finalized_at": m.finalized_at.strftime("%Y-%m-%d %H:%M") if m.finalized_at else "",
        "agenda_count": len(m.items),
        "decision_count": sum(1 for x in m.items if (x.decision or "").strip()),
        "task_count": sum(1 for x in m.items if x.task_id),
    }
    if detail:
        data["minutes_md"] = m.minutes_md or ""
        data["items"] = [_item_row(x) for x in sorted(m.items, key=lambda x: x.id)]
    return data


def _known_org(role: str, org: str) -> bool:
    if role not in ("community", "property") or not org:
        return False
    return User.query.filter_by(role=role, org=org, status="active").first() is not None


@bp.get("")
@perm_required("meeting.view")
def list_meetings():
    rows = _scoped().order_by(Meeting.held_at.desc(), Meeting.id.desc()).limit(200).all()
    counts = {s: _scoped().filter(Meeting.status == s).count()
              for s in ("draft", "notified", "finalized")}
    counts["total"] = sum(counts.values())
    return ok({"items": [_row(m) for m in rows], "counts": counts})


@bp.post("")
@perm_required("meeting.manage")
def create_meeting():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    kind = (data.get("kind") or "").strip()
    held_at = _parse_dt(data.get("held_at"))
    if not title or len(title) > 128:
        return fail("会议标题不能为空且不能超过 128 字"), 400
    if kind not in KINDS:
        return fail("会议类型不合法"), 400
    if not held_at:
        return fail("会议时间格式应为 2026-11-12 14:30"), 400
    u = current_user()
    org = (u.org if u and u.role == "community" else (data.get("org") or (u.org if u else ""))).strip()
    if not org:
        return fail("缺少会议所属社区"), 400
    if not _known_org("community", org):
        return fail("所属社区不存在或没有在职社区账号"), 400
    m = Meeting(org=org[:64], kind=kind, title=title,
                held_at=held_at, location=(data.get("location") or "").strip()[:128],
                convener=(data.get("convener") or (u.name if u else "")).strip()[:32],
                creator=_actor())
    db.session.add(m)
    db.session.flush()
    db.session.add(AuditLog(actor=_actor(), agent="meeting", action="create",
                            output_digest=f"新建{kind}《{title}》（{org}）"[:250]))
    db.session.commit()
    return ok(_row(m, detail=True))


@bp.get("/<int:mid>")
@perm_required("meeting.view")
def detail(mid: int):
    m, err = _load(mid)
    return err if err else ok(_row(m, detail=True))


@bp.post("/<int:mid>/items")
@perm_required("meeting.manage")
def add_item(mid: int):
    m, err = _load(mid)
    if err:
        return err
    if m.status == "finalized":
        return fail("会议已定稿，不能再增删议题"), 409
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title or len(title) > 128:
        return fail("议题标题不能为空且不能超过 128 字"), 400
    item = MeetingItem(meeting_id=m.id, title=title,
                       detail=(data.get("detail") or "").strip()[:2000])
    db.session.add(item)
    db.session.add(AuditLog(actor=_actor(), agent="meeting", action="agenda_add",
                            output_digest=f"《{m.title}》新增议题「{title}」"[:250]))
    db.session.commit()
    return ok(_item_row(item))


@bp.post("/<int:mid>/notify")
@perm_required("meeting.manage")
def notify(mid: int):
    m, err = _load(mid)
    if err:
        return err
    if m.status == "finalized":
        return fail("会议已定稿"), 409
    if not m.items:
        return fail("至少添加一个议题后再发通知"), 400
    if m.status == "notified":
        return ok({"msg": "已发出过通知", "meeting": _row(m, detail=True)})
    m.status = "notified"
    m.notified_at = datetime.now()
    db.session.add(AuditLog(actor=_actor(), agent="meeting", action="notify",
                            output_digest=f"发出会议通知《{m.title}》，议题 {len(m.items)} 项"))
    db.session.commit()
    return ok({"msg": "已发出通知", "meeting": _row(m, detail=True)})


@bp.post("/<int:mid>/items/<int:iid>/decision")
@perm_required("meeting.manage")
def decide_item(mid: int, iid: int):
    m, err = _load(mid)
    if err:
        return err
    if m.status == "finalized":
        return fail("会议已定稿，决议已冻结"), 409
    item = db.session.get(MeetingItem, iid)
    if not item or item.meeting_id != m.id:
        return fail("议题不存在"), 404
    data = request.get_json(silent=True) or {}
    decision = (data.get("decision") or "").strip()
    if len(decision) < 5 or len(decision) > 2000:
        return fail("决议内容应为 5～2000 字"), 400
    role = (data.get("assignee_role") or "").strip()
    org = (data.get("assignee_org") or "").strip()
    deadline = _parse_dt(data.get("deadline")) if data.get("deadline") else None
    if role or org or deadline:
        if role not in ("community", "property"):
            return fail("承办角色只能是 community 或 property"), 400
        if not _known_org(role, org):
            return fail("承办单位不存在或没有在职账号"), 400
        if not deadline or deadline <= datetime.now():
            return fail("转督办任务时必须填写晚于当前时间的截止时间"), 400
    item.decision = decision
    item.assignee_role = role
    item.assignee_org = org
    item.deadline = deadline
    item.is_public = bool(data.get("is_public"))
    db.session.add(AuditLog(actor=_actor(), agent="meeting", action="decision",
                            output_digest=f"《{m.title}》议题「{item.title}」形成决议"[:250]))
    db.session.commit()
    return ok(_item_row(item))


@bp.post("/<int:mid>/finalize")
@perm_required("meeting.manage")
def finalize(mid: int):
    m, err = _load(mid)
    if err:
        return err
    if m.status == "finalized":
        return ok({"msg": "会议已定稿", "meeting": _row(m, detail=True)})
    if m.status != "notified":
        return fail("请先发出会议通知，再登记签到和定稿"), 409
    data = request.get_json(silent=True) or {}
    try:
        expected = int(data.get("expected_count") or 0)
        present = int(data.get("present_count") or 0)
    except (TypeError, ValueError):
        return fail("应到、实到人数必须是整数"), 400
    if expected < 0 or present < 0 or (expected and present > expected):
        return fail("签到人数不合法"), 400
    if m.kind == "居民代表会议" and (expected <= 0 or present * 2 <= expected):
        return fail("居民代表会议实到人数必须超过应到人数的一半，未达到法定人数"), 409
    minutes = (data.get("minutes_md") or "").strip()
    if len(minutes) < 20:
        return fail("内部会议记录至少 20 字"), 400
    decided = [x for x in m.items if (x.decision or "").strip()]
    if not decided:
        return fail("至少有一项议题形成决议后才能定稿"), 400

    now = datetime.now()
    created_tasks = []
    for item in decided:
        if not item.assignee_role or item.task_id:
            continue
        if not item.deadline or item.deadline <= now:
            return fail(f"议题「{item.title}」的督办截止时间已过，请先更新决议"), 409
        task = Task(title=("[会议决议] " + item.title)[:100],
                    assignee_role=item.assignee_role, assignee_org=item.assignee_org,
                    detail=(item.decision or "")[:500], deadline=item.deadline,
                    creator=_actor())
        db.session.add(task)
        db.session.flush()
        item.task_id = task.id
        created_tasks.append(task.id)

    m.expected_count = expected
    m.present_count = present
    m.minutes_md = minutes
    m.public_summary = (data.get("public_summary") or "").strip()[:4000]
    m.status = "finalized"
    m.finalized_at = now
    db.session.add(AuditLog(
        actor=_actor(), agent="meeting", action="finalize",
        output_digest=(f"会议《{m.title}》定稿：实到 {present}/{expected}，"
                       f"决议 {len(decided)} 项，转任务 {len(created_tasks)} 项")[:250]))
    db.session.commit()
    return ok({"msg": "会议已定稿", "created_task_ids": created_tasks,
               "meeting": _row(m, detail=True)})


@bp.post("/<int:mid>/publish")
@perm_required("meeting.manage")
def publish(mid: int):
    m, err = _load(mid)
    if err:
        return err
    if m.status != "finalized":
        return fail("会议定稿后才能发布居务公开"), 409
    if m.published_notice_id:
        return ok({"msg": "已发布过", "notice_id": m.published_notice_id})
    public_items = [x for x in m.items if x.is_public and (x.decision or "").strip()]
    summary = (m.public_summary or "").strip()
    if len(summary) < 10 and not public_items:
        return fail("请填写至少 10 字的公开摘要，或将至少一项决议标记为可公开"), 400
    public_text = summary + "\n" + "\n".join(x.decision or "" for x in public_items)
    privacy = detect_public_personal_detail(public_text)
    if detect_sensitive(public_text) or privacy:
        kinds = "、".join(privacy) if privacy else "身份证号或个人手机号"
        return fail(f"公开内容含{kinds}，请先脱敏或改写为群体性表述"), 400

    lines = [
        f"## 会议基本情况\n\n{m.org}于{m.held_at:%Y年%m月%d日}召开{m.kind}《{m.title}》。",
    ]
    if summary:
        lines.append("## 会议情况\n\n" + summary)
    if public_items:
        decisions = "\n".join(f"{idx}. **{x.title}**：{x.decision}"
                                for idx, x in enumerate(public_items, 1))
        lines.append("## 公开决议\n\n" + decisions)
    lines.append("## 说明\n\n本公开稿仅载明经确认可公开的事项，内部讨论记录和个人信息不予公开。")
    body = "\n\n".join(lines)
    notice = Notice(
        title=("居务公开：" + m.title)[:160], issuer=m.org,
        issued_at=datetime.now().date(), category="居务公开", level="社区",
        scope="all", body_md=body, summary=(summary or public_items[0].decision)[:120],
        status="published", published_at=datetime.now(), kind="original",
        direction="inner", secret_level="公开", recv_status="",
        source_file=f"meeting:{m.id}",
        source_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        created_by=_actor(),
    )
    db.session.add(notice)
    db.session.flush()
    m.published_notice_id = notice.id
    db.session.add(AuditLog(actor=_actor(), agent="meeting", action="publish",
                            output_digest=f"会议《{m.title}》发布居务公开 #{notice.id}"))
    db.session.commit()
    return ok({"msg": "居务公开已发布", "notice_id": notice.id})
