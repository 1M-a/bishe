"""居民端页面（登录门禁由 RESIDENT_AUTH 开关控制）

居民端刻意**不复用内部端的侧边栏布局**：内部端是"一屏多卡"的工位视角，
居民多在手机上打开，需要的是大按钮、短路径、一次只做一件事。
两套布局通过页头/页脚的入口互相跳转：
    内部登录页 → 「我是居民」
    居民端页头 → 「工作人员入口」

RESIDENT_AUTH=0（演示版）：免注册、免登录，凭手机号 + 6 位查询码办事。
RESIDENT_AUTH=1（调整/试用版）：邀请码注册后登录访问，未登录重定向到内部登录页。
"""
from flask import (Blueprint, abort, current_app, redirect, render_template,
                   request, session, url_for)

from app.auth import current_user
from app.extensions import db
from app.models import Notice, ResidentTicket
from app.notice.ingest import visible_query
from app.notice.render import render as render_md
from app.portal_seed import grouped_contacts

bp = Blueprint("portalviews", __name__)


def _auth_gate():
    """登录门禁：RESIDENT_AUTH=1 时未登录重定向到登录页；=0 免注册照旧。"""
    if current_app.config.get("RESIDENT_AUTH") and not current_user():
        return redirect(url_for("views.login"))
    return None


def _recent_ticket():
    # RESIDENT_AUTH=1：session 只属于演示模式。登录居民的"最近进度"按
    # 账号绑定的手机号查本人诉求，不依赖本机会话（同机换账号不串）。
    if current_app.config.get("RESIDENT_AUTH"):
        u = current_user()
        if not u or not u.phone:
            return None, None
        ticket = (ResidentTicket.query
                  .filter_by(phone=u.phone)
                  .order_by(ResidentTicket.id.desc()).first())
        if ticket:
            from app.api.portal import _ticket_payload
            return ticket, _ticket_payload(ticket)
        return None, None
    for tid in session.get("portal_tickets", []):
        ticket = db.session.get(ResidentTicket, tid)
        if ticket:
            from app.api.portal import _ticket_payload
            return ticket, _ticket_payload(ticket)
    return None, None


@bp.get("/portal")
def home():
    resp = _auth_gate()
    if resp:
        return resp
    recent_ticket, recent_view = _recent_ticket()
    # 首页滚动条：只取"已发布 + 预警"的公告（visible_query 已过滤 published/scope/direction）。
    # 判定只看 is_alert，不用标题关键词；不叠加 pinned —— pinned 只影响列表排序与置顶标签，
    # 置顶≠预警，二者正交，pinned=1 的公告不自动进滚动条。
    alerts = (visible_query("resident")
              .filter(Notice.is_alert.is_(True))
              .order_by(Notice.published_at.desc())
              .all())
    # 首页通知公告卡：只取已发布公告（visible_query 保证）前 5 条，
    # 排序与列表页完全一致（置顶在前）——避免"列表页有、首页没有"的口径分叉。
    latest_notices = (visible_query("resident")
                      .order_by(Notice.pinned.desc(), Notice.pinned_at.desc(),
                                Notice.published_at.desc()).limit(5).all())
    return render_template("portal/home.html", recent_ticket=recent_ticket,
                           recent_view=recent_view, alerts=alerts,
                           latest_notices=latest_notices)


@bp.get("/portal/report")
def report():
    resp = _auth_gate()
    if resp:
        return resp
    return render_template("portal/report.html")


@bp.get("/portal/done/<int:tid>")
def done(tid: int):
    resp = _auth_gate()
    if resp:
        return resp
    t = db.session.get(ResidentTicket, tid)
    if not t:
        return redirect(url_for("portalviews.home"))
    if current_app.config.get("RESIDENT_AUTH"):
        # 登录模式：只允许查看本人诉求（会话留痕只属于演示模式）
        u = current_user()
        if not u or t.phone != (u.phone or "").strip():
            return redirect(url_for("portalviews.home"))
    elif tid not in session.get("portal_tickets", []):
        return redirect(url_for("portalviews.home"))
    from app.api.portal import _ticket_payload
    return render_template("portal/done.html", ticket=t, view=_ticket_payload(t))


@bp.get("/portal/query")
def query():
    resp = _auth_gate()
    if resp:
        return resp
    recent_ticket, recent_view = _recent_ticket()
    return render_template("portal/query.html", recent_ticket=recent_ticket,
                           recent_view=recent_view)


@bp.get("/portal/ticket/<int:tid>")
def ticket(tid: int):
    resp = _auth_gate()
    if resp:
        return resp
    t = db.session.get(ResidentTicket, tid)
    if not t:
        abort(404)
    if current_app.config.get("RESIDENT_AUTH"):
        # 登录模式：只允许查看本人诉求（会话留痕只属于演示模式）
        u = current_user()
        if not u or t.phone != (u.phone or "").strip():
            abort(404)
    elif tid not in session.get("portal_tickets", []):
        abort(404)
    from app.api.portal import _ticket_payload
    return render_template("portal/ticket.html", ticket=t, view=_ticket_payload(t))


@bp.get("/portal/notices")
def notices():
    resp = _auth_gate()
    if resp:
        return resp
    # 排序只看 pinned / pinned_at：置顶的排最前（后置顶的在上），其余按发布时间倒序。
    # is_alert 与排序、标签完全正交 —— 一条预警公告如果没置顶，在列表里就按普通时间排、
    # 也不带"置顶"标签。
    items = (visible_query("resident")
             .order_by(Notice.pinned.desc(), Notice.pinned_at.desc(),
                       Notice.published_at.desc()).limit(100).all())
    return render_template("portal/notices.html", notices=items)


@bp.get("/portal/notice/<int:nid>")
def notice(nid: int):
    """公告详情。mode=view 网页阅读 / print 打印版 / official 发布版"""
    resp = _auth_gate()
    if resp:
        return resp
    n = db.session.get(Notice, nid)
    # 详情页按 id 直取，必须自己再判一次范围：
    # 光有列表过滤挡不住手工拼 URL 枚举 id（居民端没有登录态，更不能依赖前端）。
    # 用 `can_view` 而不是手写 status/scope 两个条件——列表侧 `visible_query`
    # 还额外排除了发文（`_exclude_docs`），手写版漏了那一条，
    # 会出现"列表里没有、拼 id 能打开"的不同源问题。
    from app.notice.ingest import can_view
    if not n or not can_view(n, "resident"):
        abort(404)
    mode = request.args.get("mode", "view")
    if mode not in ("view", "print", "official"):
        mode = "view"
    body = render_md(n.body_md)
    if mode == "view":
        return render_template("portal/notice.html", notice=n, body=body)
    from datetime import datetime
    return render_template("portal/notice_doc.html", notice=n, body=body, mode=mode,
                           now=datetime.now().strftime("%Y-%m-%d %H:%M"))


@bp.get("/portal/contacts")
def contacts():
    resp = _auth_gate()
    if resp:
        return resp
    return render_template("portal/contacts.html", groups=grouped_contacts())