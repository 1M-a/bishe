"""居民端页面（免登录）

居民端刻意**不复用内部端的侧边栏布局**：内部端是"一屏多卡"的工位视角，
居民多在手机上打开，需要的是大按钮、短路径、一次只做一件事。
两套布局通过页头/页脚的入口互相跳转：
    内部登录页 → 「我是居民」
    居民端页头 → 「工作人员入口」
"""
from flask import (Blueprint, abort, redirect, render_template, request, session,
                   url_for)

from app.extensions import db
from app.models import Notice, ResidentTicket
from app.notice.ingest import visible_query
from app.notice.render import render as render_md
from app.portal_seed import grouped_contacts

bp = Blueprint("portalviews", __name__)


def _recent_ticket():
    for tid in session.get("portal_tickets", []):
        ticket = db.session.get(ResidentTicket, tid)
        if ticket:
            from app.api.portal import _ticket_payload
            return ticket, _ticket_payload(ticket)
    return None, None


@bp.get("/portal")
def home():
    recent_ticket, recent_view = _recent_ticket()
    return render_template("portal/home.html", recent_ticket=recent_ticket,
                           recent_view=recent_view)


@bp.get("/portal/report")
def report():
    return render_template("portal/report.html")


@bp.get("/portal/done/<int:tid>")
def done(tid: int):
    t = db.session.get(ResidentTicket, tid)
    if not t or tid not in session.get("portal_tickets", []):
        return redirect(url_for("portalviews.home"))
    from app.api.portal import _ticket_payload
    return render_template("portal/done.html", ticket=t, view=_ticket_payload(t))


@bp.get("/portal/query")
def query():
    recent_ticket, recent_view = _recent_ticket()
    return render_template("portal/query.html", recent_ticket=recent_ticket,
                           recent_view=recent_view)


@bp.get("/portal/ticket/<int:tid>")
def ticket(tid: int):
    t = db.session.get(ResidentTicket, tid)
    if not t or tid not in session.get("portal_tickets", []):
        abort(404)
    from app.api.portal import _ticket_payload
    return render_template("portal/ticket.html", ticket=t, view=_ticket_payload(t))


@bp.get("/portal/notices")
def notices():
    items = (visible_query("resident")
             .order_by(Notice.pinned.desc(), Notice.published_at.desc()).limit(100).all())
    return render_template("portal/notices.html", notices=items)


@bp.get("/portal/notice/<int:nid>")
def notice(nid: int):
    """公告详情。mode=view 网页阅读 / print 打印版 / official 发布版"""
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
    return render_template("portal/contacts.html", groups=grouped_contacts())
