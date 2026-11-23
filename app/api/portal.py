"""居民端接口：填报 / 查进度 / 看公告 / 找三级管理者

与内部接口的三点不同：
1. **没有登录态**。居民凭「手机号 + 6 位查询码」双因子查自己的单子，
   不建账号、不存密码，符合最小必要。
2. **返回字段收窄**。只给业务类型、承办单位、状态、时限、处理反馈；
   不返回 fields_json、不返回内部 id 链路、手机号一律打码。
3. **必须防刷**。这是公网公开入口，接口按 IP 做滑动窗口限流，
   否则一条脚本就能把首录表和模型队列打满。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from datetime import datetime, timedelta

from flask import Blueprint, current_app, request, session

from app.api import fail, ok
from app.auth import ROLE_NAMES, current_user
from app import arbitration, evidence
from app.extensions import db
from app.itemstate import STAGE_RESIDENT, stage as item_stage, sync_ticket_status
from app.models import (AuditLog, EvidenceChain, FdeRecord, Ledger, Notice,
                        ResidentTicket, WorkItem, WorkOrder)
from app.ratelimit import allow as _rate_ok
from app.intake import content_hash, find_duplicate, normalize_text

bp = Blueprint("portal", __name__)

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

# 业务线 → 居民看得懂的承办单位
#
# 物业名不再写死在代码里：从 Config.ORG_NAMES["property_short"] 读取，
# 演示版默认"幸福里物业"，部署到别区时由 ORG_PROPERTY_SHORT 环境变量覆盖，
# 页面文案与派单单位（app/agents/dispatch.py）共用同一处配置，保证口径一致。
def _line_to_org() -> dict:
    cfg = current_app.config.get("ORG_NAMES", {})
    return {
        "物业": cfg.get("property_short", "幸福里物业"),
        "综治": "社区综治",
        "民政": "社区民政",
        "卫健": "社区卫生",
        "街道": cfg.get("street", "街道办事处"),
    }

# 查询码锁定：连错 5 次锁 30 分钟
#
# 6 位数字码只有 10^6 种，光靠"每分钟 20 次"的限流挡不住——那是 35 天跑完，
# 但攻击者只盯一个手机号、每天跑满 20 次，一个月内仍有相当概率撞对，
# 而撞对一次的后果是**读到别人家的姓名、门牌、报修内容**。
# 所以必须有"错够次数就整段时间别再试"的机制。
#
# 计数落在审计表而不是内存字典上：内存随重启清零，攻击者只要等到
# 服务重启（部署更新、平台迁移）就能把计数洗掉，锁定形同虚设。
QUERY_FAIL_MAX = 5
QUERY_FAIL_WINDOW = 30 * 60          # 秒

# 公网填报的硬闸：同时只允许 4 个匿名请求进入模型/落库链路。
# 维度限流挡不住"换手机号 + 换 IP"的脚本，而 threaded=True 每请求一个线程，
# 没有这个闸，几百个请求会各自排队 40 秒，把 2C2G 机器的线程和内存吃满。
# 拿不到槽位立即 429，不排队——居民端宁可快速失败，也不占着连接干等。
_REPORT_SLOTS = threading.BoundedSemaphore(4)
# 匿名请求的模型排队上限：社区内部可以等 40 秒，居民提交不值得等那么久，
# 8 秒拿不到模型就直接走规则兜底，链路仍然完整。
REPORT_QUEUE_TIMEOUT = 8.0


def _request_data():
    if request.is_json:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else None
    return request.form


def _string_field(data, key, limit=None):
    value = data.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(key)
    value = value.strip()
    return value[:limit] if limit else value


def _phone_key(phone: str) -> str:
    """审计里的稳定键：手机号哈希，不落明文

    不用打码后的手机号当键：`138****8000` 这种三前一后一共 7 位，
    两个不同号码撞在一起就会"别人的失败把我锁了"。
    """
    return "portal:" + hashlib.sha256(phone.encode("utf-8")).hexdigest()[:16]


def _query_locked(phone: str) -> bool:
    cutoff = datetime.now() - timedelta(seconds=QUERY_FAIL_WINDOW)
    n = (AuditLog.query
         .filter(AuditLog.actor == _phone_key(phone),
                 AuditLog.action == "query_fail",
                 AuditLog.created_at >= cutoff)
         .count())
    return n >= QUERY_FAIL_MAX


def _note_query_fail(phone: str):
    db.session.add(AuditLog(actor=_phone_key(phone), agent="portal",
                            action="query_fail",
                            input_digest="查询码不匹配 · " + mask_phone(phone)))
    db.session.commit()


def _client_ip() -> str:
    # 只读 remote_addr：XFF 是客户端可伪造的，拿它做限流键等于没限。
    # 反代部署时由 ProxyFix 把可信的那一段写进 remote_addr（见 config.TRUST_PROXY）。
    return request.remote_addr or "unknown"


def mask_phone(p: str) -> str:
    return p[:3] + "****" + p[-4:] if p and len(p) == 11 else ""


def _gen_code() -> str:
    for _ in range(30):
        code = f"{secrets.randbelow(10 ** 6):06d}"
        if not ResidentTicket.query.filter_by(code=code).first():
            return code
    raise RuntimeError("查询码生成失败")


def _remember(ticket_id: int):
    """把单号记进本机会话，方便居民回来直接看（换设备仍可用手机号+码查询）"""
    ids = [i for i in session.get("portal_tickets", []) if i != ticket_id]
    ids.insert(0, ticket_id)
    session["portal_tickets"] = ids[:10]


def _append_ticket_photos(ticket: ResidentTicket, files: list, actor: str):
    """安全追加居民图片；文件名随机化，内容哈希进入证据链。

    room 只数照片（type != audio）：语音不占照片配额，反之追加语音也不
    会因为已传照片被拒。
    """
    current = [a for a in evidence.load_attachments(ticket.attachments)
               if a.get("type") != "audio"]
    audio_items = [a for a in evidence.load_attachments(ticket.attachments)
                   if a.get("type") == "audio"]
    room = max(0, evidence.MAX_PHOTOS - len(current))
    errors = []
    if len(files) > room:
        errors.append(f"最多 {evidence.MAX_PHOTOS} 张，当前还可添加 {room} 张")
    saved = []
    if room:
        saved, rejected = evidence.save_photos(
            files[:room], current_app.config["EVIDENCE_DIR"])
        errors.extend(rejected)
    if saved:
        ticket.attachments = json.dumps(audio_items + current + saved,
                                        ensure_ascii=False)
        for photo in saved:
            evidence.append("ticket", ticket.id, "attach", {
                "name": photo["name"], "sha256": photo["sha256"], "size": photo["size"],
            }, actor=actor)
    return saved, errors


def _append_ticket_audio(ticket: ResidentTicket, files: list, actor: str):
    """安全追加居民语音；同样走哈希进链，落盘 data/evidence/audio/。

    语音上限 1 条/次（前端每个提交只产生一个 blob）。重新录=覆盖：
    新语音顶掉旧语音条目（老文件留盘但不再出现在单据里，演示口径简明）。
    """
    photo_items = [a for a in evidence.load_attachments(ticket.attachments)
                   if a.get("type") != "audio"]
    saved, errors = evidence.save_audio(
        files[:1], current_app.config["EVIDENCE_DIR"])
    if saved:
        ticket.attachments = json.dumps(
            photo_items + saved, ensure_ascii=False)
        for audio in saved:
            evidence.append("ticket", ticket.id, "attach", {
                "name": audio["name"], "sha256": audio["sha256"],
                "size": audio["size"], "mime": audio["mime"], "type": "audio",
            }, actor=actor)
    return saved, errors


def _batch_load(tickets: list[ResidentTicket]):
    """一次取齐多张单据的事项/工单/台账。

    早先 `_ticket_payload` 每张票、每个事项各查一轮：一页 10 张票、每张拆 3 项，
    就是 70 次查询。查询集中在写路径之后做一次批量取，渲染层只组装。
    """
    fde_ids = [t.fde_id for t in tickets if t.fde_id]
    items_by_fde: dict[int, list] = {}
    if fde_ids:
        for it in (WorkItem.query.filter(WorkItem.fde_id.in_(fde_ids))
                   .order_by(WorkItem.id.asc()).all()):
            items_by_fde.setdefault(it.fde_id, []).append(it)
    item_ids = [it.id for items in items_by_fde.values() for it in items]
    orders_map: dict[int, list] = {}
    filed_ids: set[int] = set()
    if item_ids:
        for o in WorkOrder.query.filter(WorkOrder.work_item_id.in_(item_ids)).all():
            orders_map.setdefault(o.work_item_id, []).append(o)
        filed_ids = {wid for (wid,) in db.session.query(Ledger.work_item_id)
                     .filter(Ledger.work_item_id.in_(item_ids)).distinct().all()}
    return items_by_fde, orders_map, filed_ids


def _payload(t: ResidentTicket, items: list, orders_map: dict, filed_ids: set) -> dict:
    """组装居民可见的进度视图。**只读**：渲染函数不写库、不 commit。

    状态判据已收口到 `app/itemstate.py`——居民端与首录台必须用同一套，
    否则同一个事项会出现"居民看到已办结、社区看到待派发"。

    居民只关心"办没办完"：已办结 / 处理中 / 已受理（已派单与未接单都算已受理）。

    `filed`（已进台账）必须算办结：民政/综治这些线**只写台账、不产生工单**，
    早先判据写成 `all(s == "done")`，这些事项永远到不了"已办结"——
    居民申请低保复核，社区早就归档了，居民端还显示"已受理"，
    等于告诉居民"没人管"。这不是显示问题，是状态语义错了。

    状态回写只发生在写路径（建单、居民表态、工单流转），见 itemstate.sync_ticket_status。
    """
    from app.agents.supervise import SuperviseAgent

    rows = []

    for it in items:
        orders = orders_map.get(it.id, [])
        lines = [l for l in (it.target_lines or "").split(",") if l]

        state = STAGE_RESIDENT[item_stage(it, orders=orders, filed=it.id in filed_ids)]

        orgs = [_line_to_org().get(l, l) for l in lines]
        sla = max([o.sla_hours for o in orders], default=None)
        feedbacks = [o.feedback for o in orders if o.feedback]
        # 居民表态是**按事项**看的：一个诉求拆成"修灯"和"低保复核"两项，
        # 灯修好了不代表低保也办了。所以把每项的确认状态各自带出去，
        # 而不是给整张诉求一个"已确认"——那个粒度会掩盖掉没办的那一项。
        cs = [o.confirm_status or "" for o in orders]
        if cs and all(c == "ok" for c in cs):
            confirm = "ok"
        elif "disputed" in cs:
            confirm = "disputed"
        else:
            confirm = ""
        # 居民红灯与督办告警必须使用同一 SLA 兜底口径（None/0 按 24h）。
        overdue = any(SuperviseAgent.is_overdue(o) for o in orders)
        light = ("red" if confirm == "disputed" or overdue else
                 "green" if state == "已办结" else "yellow")
        rows.append({
            "type": it.type,
            "subject": it.subject,
            "orgs": orgs or ["社区服务站"],
            "state": state,
            "traffic_light": light,
            "sla_hours": sla,
            "feedback": feedbacks[0] if feedbacks else "",
            "created_at": (orders[0].created_at.strftime("%m-%d %H:%M") if orders else
                           it.created_at.strftime("%m-%d %H:%M")),
            "confirm_status": confirm,
            "confirm_text": {"ok": "已确认解决", "disputed": "已反馈不服"}.get(confirm, ""),
            # 只有办结的事项才能表态。判据看工单状态而不是整单，理由同上。
            "can_confirm": any(o.status == "done" for o in orders),
            "verdict_text": next((o.verdict_basis for o in orders if o.verdict), ""),
        })

    if rows and all(r["state"] == "已办结" for r in rows):
        overall = "done"
    elif any(r["state"] != "已受理" for r in rows):
        # 有一项推进过（处理中/已办结）但没全完 → 整单算处理中。
        # 早先只在"有 doing"时才算，于是"一项已办结、一项还没开始"会显示"已受理"，
        # 把已经推进的那项也一起说成没动。
        overall = "doing"
    else:
        overall = "open"

    if any(r["traffic_light"] == "red" for r in rows):
        traffic_light = "red"
    elif rows and all(r["traffic_light"] == "green" for r in rows):
        traffic_light = "green"
    else:
        traffic_light = "yellow"

    # 整单层面的表态口径：全部事项都确认才算"已确认"。
    # 有一项不服就算"有争议"——宁可让居委会多看一条，也不能把
    # 居民的不满淹没在"大部分已确认"里。
    all_cs = [r["confirm_status"] for r in rows]
    if rows and all(c == "ok" for c in all_cs):
        confirm_overall = "ok"
    elif "disputed" in all_cs:
        confirm_overall = "disputed"
    else:
        confirm_overall = ""

    return {
        "id": t.id,
        "code": t.code,
        "name": t.name,
        "phone": mask_phone(t.phone),
        "address": t.address,
        "created_at": t.created_at.strftime("%Y-%m-%d %H:%M"),
        "status": overall,
        "status_text": {"open": "已受理", "doing": "处理中", "done": "已办结"}[overall],
        "traffic_light": traffic_light,
        "traffic_text": {"red": "需关注", "yellow": "处理中", "green": "已办结"}[traffic_light],
        "items": rows,
        "line_summary": t.line_summary,
        "confirm_status": confirm_overall,
        "can_confirm": any(r["can_confirm"] for r in rows),
        # 满意度：评分只在这张单确认解决后才有；没评是 None，
        # 默认 0 会让前端把"没评"画成"0 星"或误判已评分。
        "rating": t.rating,
        "rating_comment": t.rating_comment or "",
        # 附件只回文件名与大小，**不回存盘路径**：那个路径一旦透出，
        # 就等于给出"猜下一个文件名"的线索（虽然是 uuid，但没有必要给）。
        "attachments": [{"name": a.get("name", ""), "size": a.get("size", 0)}
                        for a in evidence.load_attachments(t.attachments)],
        # 争议类型由服务端下发，居民端不再写死一份中文表
        "dispute_types": arbitration.DISPUTE_TYPES,
    }


def _ticket_payload(t: ResidentTicket) -> dict:
    items_by_fde, orders_map, filed_ids = _batch_load([t])
    return _payload(t, items_by_fde.get(t.fde_id, []), orders_map, filed_ids)


def _ticket_payload_many(tickets: list[ResidentTicket]) -> list[dict]:
    items_by_fde, orders_map, filed_ids = _batch_load(tickets)
    return [_payload(t, items_by_fde.get(t.fde_id, []), orders_map, filed_ids)
            for t in tickets]


# ------------------------------------------------------------------ 居民填报

@bp.post("/report")
def report():
    data = _request_data()
    if data is None:
        return fail("请求格式不正确，请使用表单或 JSON 对象", 400), 400
    try:
        phone = _string_field(data, "phone")
        name = _string_field(data, "name", 16)
        address = _string_field(data, "address", 64)
        text = _string_field(data, "text")
    except TypeError:
        return fail("手机号、称呼、位置和情况必须是文字", 400), 400

    # 登录门禁（RESIDENT_AUTH=1）：未登录直接 401；phone 以注册账号为准，
    # 不接收自由填写的 phone（演示模式保持"手机号+查询码"双因子）。
    auth_mode = bool(current_app.config.get("RESIDENT_AUTH"))
    u = current_user() if auth_mode else None
    if auth_mode and not u:
        return fail("请先登录后再提交", 401), 401
    if u:
        phone = (u.phone or "").strip()

    # 三通道平级：文字 / 照片 / 语音可任意组合，只有全空才拒绝。
    photos = request.files.getlist("photos") if request.files else []
    audio_files = request.files.getlist("audio") if request.files else []
    has_media = bool(photos) or bool(audio_files)

    # 语音时长兜底：webm/m4a 头解析不出时长，靠前端随表单上报的
    # audio_seconds 复核（居民端是 MediaRecorder 自己的走秒，可信即可用），
    # wav 能从头解析就用头解析值压掉上报值，防伪造。
    audio_seconds = 0.0
    if audio_files:
        parsed = evidence.audio_files_to_seconds(audio_files)
        if parsed:
            audio_seconds = float(parsed)
        else:
            try:
                audio_seconds = float(_string_field(data, "audio_seconds") or 0)
            except (TypeError, ValueError):
                audio_seconds = 0.0
        if audio_seconds > evidence.MAX_AUDIO_SECONDS:
            return fail("语音不能超过 5 分钟，请重录后提交", 400), 400

    # 三档限流：手机号是真实约束（一条诉求对应一个人），IP 是兜底，
    # 全局闸防"换手机号 + 换 IP"的脚本。全部在入参校验之前，
    # 否则伪造一堆非法手机号就能无限试探。
    if not _rate_ok("report:global", 30, 60):
        return fail("系统当前提交繁忙，请稍后再试", 429), 429
    if not _rate_ok(f"report:phone:{phone}", 5, 60):
        return fail("提交太频繁，请稍后再试（同一手机号每分钟最多 5 次）", 429), 429
    if not _rate_ok(f"report:ip:{_client_ip()}", 30, 60):
        return fail("提交太频繁，请稍后再试", 429), 429
    # 语音独立限流：录音文件大、又可能撞模型，与文字提交分开统计，
    # 避免"频繁试录音"把同一入口的文字配额也吃光。
    if audio_files and not _rate_ok(f"voice:{phone}", 5, 60):
        return fail("语音提交太频繁，请稍后再试", 429), 429

    if not PHONE_RE.match(phone):
        return fail("请填写 11 位手机号，用于接收处理结果", 400), 400

    # 文字校验：有照片或语音时文字可为空（三通道平级），否则最低 5 字。
    if not has_media and len(text) < 5:
        return fail("请把情况说清楚一些（至少 5 个字）", 400), 400
    if len(text) > 1500:
        return fail("内容过长，请控制在 1500 字以内", 400), 400

    # 居民输入先过确定性质量层，再进入模型：模型只处理清洗后的文本，
    # 去重也基于同一份规范化文本，避免“您好！！！灯坏了”和“灯坏了”各建一单。
    normalized, quality_flags = normalize_text(text)
    if not has_media and len(normalized) < 5:
        return fail("有效内容太少，请补充时间、地点或具体问题", 400), 400
    if has_media and not normalized:
        # 纯媒体反馈（照片/语音，无文字）：给下游拆解一个可读的占位描述，
        # 让业务项能正常立项；真实内容以附件为准（语音不进模型，方案 C）。
        if audio_files and not photos:
            normalized = "（语音反馈，详情见附件）"
        elif photos and not audio_files:
            normalized = "（照片反馈，详情见附件）"
        else:
            normalized = "（语音+照片反馈，详情见附件）"
        if "媒体占位" not in quality_flags:
            quality_flags.append("媒体占位")

    # 纯媒体上报不参与文字去重（占位文本没有可去重的语义），
    # 有文字才走 find_duplicate，避免两个不同照片事项被占位文本误合并。
    previous = None
    duplicate_kind = duplicate_score = ""
    if "媒体占位" not in quality_flags:
        previous, duplicate_kind, duplicate_score = find_duplicate(phone, normalized)
    if previous:
        if not auth_mode:
            _remember(previous.id)
        actor = f"居民(尾号{phone[-4:]})"
        saved, photo_errors = _append_ticket_photos(
            previous, photos, actor)
        saved_audio, audio_errors = _append_ticket_audio(
            previous, audio_files, actor)
        extra = f"；追加图片 {len(saved)} 张"
        if audio_files:
            extra += (f"；追加语音 {len(saved_audio)} 条"
                      + (f"（{audio_errors[0]}）" if audio_errors else ""))
        db.session.add(AuditLog(
            actor=actor, agent="portal", action="dedupe",
            input_digest=f"content={content_hash(normalized)[:16]}",
            output_digest=(f"命中诉求#{previous.id}，{duplicate_kind}，"
                           f"相似度 {duplicate_score:.4f}{extra}")[:250]))
        db.session.commit()
        if saved_audio:
            db.session.add(AuditLog(actor=actor, agent="portal",
                                    action="voice_upload",
                                    output_digest=f"诉求#{previous.id} 追加语音 {len(saved_audio)} 条"))
            db.session.commit()
        return ok({
            "ticket_id": previous.id, "code": previous.code,
            "deduplicated": True, "duplicate_of": previous.id,
            "duplicate_kind": duplicate_kind,
            "duplicate_score": duplicate_score,
            "quality_flags": quality_flags,
            "photos": len(saved), "photo_errors": photo_errors,
            "audio": len(saved_audio), "audio_errors": audio_errors,
            "msg": "已找到相同诉求，继续使用原查询码",
        })

    # 居民身份不进 user 表，creator 只留可识别的痕迹（姓 + 手机尾号）
    label = f"居民{name[:1] + '某' if name else ''}(尾号{phone[-4:]})"
    from app.agents.orchestrator import Orchestrator
    # 拿不到公网并发槽位立即拒绝，不进模型队列；拿到才允许调用。
    if not _REPORT_SLOTS.acquire(blocking=False):
        return fail("系统当前提交繁忙，请稍后再试", 429), 429
    try:
        result = Orchestrator().handle_fde(
            normalized, source="portal", creator=label,
            queue_timeout=REPORT_QUEUE_TIMEOUT)
    except Exception:                                   # noqa: BLE001
        # 不把异常类名回给公网；细节交给全局错误记录器与审计
        return fail("提交失败，请稍后重试或电话联系社区", 500), 500
    finally:
        _REPORT_SLOTS.release()

    lines = []
    for it in result["items"]:
        for l in it["target_lines"]:
            if l not in lines:
                lines.append(l)

    ticket = ResidentTicket(
        code=_gen_code(), phone=phone, name=name, address=address,
        raw_text=normalized, normalized_text=normalized,
        content_hash=content_hash(normalized),
        quality_status="clean",
        quality_flags=json.dumps(quality_flags, ensure_ascii=False),
        fde_id=result["fde_id"],
        item_count=len(result["items"]),
        line_summary="/".join(lines),
        status="open",
    )
    db.session.add(ticket)
    # 先 flush 拿 id：链记录的 target_id 要用，而它不允许为空。
    db.session.flush()
    # 居民端状态只在写路径同步一次：有的业务项只写台账、已经算办结，
    # 建单时就应当是"已办结"而不是先写 open 等下一次 GET 来校正。
    sync_ticket_status(result["fde_id"])

    # 照片与语音：分别存盘、每种算哈希进链。
    # 落盘失败不阻断提交——居民描述已经录进去了，因为"声没传上"让整单退回
    # 等于把最要紧的信息也一起丢了。错误放在返回值里如实告诉居民。
    saved, photo_errors = evidence.save_photos(
        photos, current_app.config["EVIDENCE_DIR"])
    saved_audio, audio_errors = evidence.save_audio(
        audio_files, current_app.config["EVIDENCE_DIR"])
    merged_attach = list(saved) + list(saved_audio)
    if merged_attach:
        ticket.attachments = json.dumps(merged_attach, ensure_ascii=False)

    evidence.append("ticket", ticket.id, "submit", {
        "code": ticket.code,
        "text": evidence.mask_payload(text),
        "item_count": ticket.item_count,
        "line_summary": ticket.line_summary,
        "photos": len(saved),
        "audio": len(saved_audio),
    }, actor=label)
    for ph in saved:
        evidence.append("ticket", ticket.id, "attach", {
            "name": ph["name"], "sha256": ph["sha256"], "size": ph["size"],
        }, actor=label)
    for au in saved_audio:
        evidence.append("ticket", ticket.id, "attach", {
            "name": au["name"], "sha256": au["sha256"],
            "size": au["size"], "mime": au["mime"], "type": "audio",
        }, actor=label)

    db.session.commit()
    if not auth_mode:
        _remember(ticket.id)
    db.session.add(AuditLog(actor=label, agent="portal", action="submit",
                            input_digest="", output_digest=f"居民提交，拆出 {len(result['items'])} 项"))
    if saved_audio:
        db.session.add(AuditLog(actor=label, agent="portal",
                                action="voice_upload",
                                output_digest=f"居民上传语音 {len(saved_audio)} 条"))
    db.session.commit()

    return ok({
        "ticket_id": ticket.id,
        "code": ticket.code,
        "item_count": len(result["items"]),
        "order_count": len(result["orders"]),
        "ledger_count": len(result["ledgers"]),
        "line_summary": ticket.line_summary,
        "photos": len(saved),
        "photo_errors": photo_errors,
        "audio": len(saved_audio),
        "audio_errors": audio_errors,
        "deduplicated": False,
        "quality_flags": quality_flags,
        "items": [{"type": it["type"], "subject": it["subject"],
                   "orgs": [_line_to_org().get(l, l) for l in it["target_lines"]]}
                  for it in result["items"]],
        "llm_used": result["llm_used"],
    })


# ------------------------------------------------------------------ 进度查询

@bp.get("/ticket")
def query_ticket():
    auth_mode = bool(current_app.config.get("RESIDENT_AUTH"))
    u = current_user() if auth_mode else None
    if auth_mode and not u:
        return fail("请先登录", 401), 401
    # 登录模式：手机号以注册账号为准，不接收自由填写的 phone
    if u:
        phone = (u.phone or "").strip()
        code = (request.args.get("code") or "").strip()
    else:
        phone = (request.args.get("phone") or "").strip()
        code = (request.args.get("code") or "").strip()
    # 限流必须落在**手机号**维度上：查询码只有 6 位，真正的攻击是"知道手机号后枚举查询码"，
    # 而 IP 在反向代理后面既不稳定、又可被 X-Forwarded-For 伪造，按 IP 限等于没限。
    if not _rate_ok(f"query:phone:{phone}", 20, 60):
        return fail("查询太频繁，请稍后再试（同一手机号每分钟最多 20 次）", 429), 429
    if not _rate_ok(f"query:ip:{_client_ip()}", 120, 60):
        return fail("查询太频繁，请稍后再试", 429), 429
    if not PHONE_RE.match(phone) or not re.match(r"^\d{6}$", code):
        return fail("请填写手机号与 6 位查询码", 400), 400
    # 锁定检查放在格式校验之后：格式都不对的请求不该写审计、也不该占额度
    if _query_locked(phone):
        return fail("该手机号查询码输错次数过多，已锁定 30 分钟，请稍后再试", 429), 429
    t = ResidentTicket.query.filter_by(phone=phone, code=code).first()
    if not t:
        _note_query_fail(phone)
        return fail("没有查到对应记录，请核对手机号与查询码", 404), 404
    if not auth_mode:
        _remember(t.id)
    return ok(_ticket_payload(t))


@bp.get("/ticket/<int:tid>")
def ticket_detail(tid: int):
    auth_mode = bool(current_app.config.get("RESIDENT_AUTH"))
    u = current_user() if auth_mode else None
    if auth_mode and not u:
        return fail("请先登录", 401), 401
    t = db.session.get(ResidentTicket, tid)
    if not t:
        return fail("没有查到对应记录", 404), 404
    if auth_mode:
        # 登录模式：按当前用户过滤本人诉求（uid ↔ ResidentTicket.phone），
        # 只依赖账号绑定的手机号，不依赖本机会话（同机换账号不串）。
        if t.phone != (u.phone or "").strip():
            return fail("无权查看该记录", 403), 403
    elif tid not in session.get("portal_tickets", []):
        # 演示模式走本机会话留痕，防止遍历 id 拿到别人的信息
        return fail("无权查看该记录", 403), 403
    return ok(_ticket_payload(t))


@bp.post("/ticket/<int:tid>/confirm")
def confirm_ticket(tid: int):
    """居民表态：解决了 / 没解决

    这是"三方博弈"里居民唯一的**反向通道**。没有它，物业点办结就是终局，
    居委会也无从知道哪件事其实没办成——所有"反复投诉"在系统里都无处显形，
    只能表现为居民又提交一遍，然后被当成新单重新派出去。
    """
    t = db.session.get(ResidentTicket, tid)
    if not t:
        return fail("无权操作该记录", 403), 403
    auth_mode = bool(current_app.config.get("RESIDENT_AUTH"))
    u = current_user() if auth_mode else None
    if auth_mode and not u:
        return fail("请先登录", 401), 401
    if auth_mode:
        # 登录模式：只允许评价本人诉求（按账号绑定的手机号过滤）
        if t.phone != (u.phone or "").strip():
            return fail("无权操作该记录，这是其他居民提交的诉求"), 403
    elif tid not in session.get("portal_tickets", []):
        return fail("无权操作该记录，请先用手机号和查询码查询"), 403
    if not _rate_ok(f"confirm:{tid}", 10, 60):
        return fail("操作太频繁，请稍后再试"), 429

    d = _request_data()
    if d is None:
        return fail("请求格式不正确，请使用表单或 JSON 对象", 400), 400
    # 布尔值别直接 bool("false")——字符串 "false" 是真值，会把"没解决"判成"已解决"。
    raw = d.get("ok")
    if raw is not None and not isinstance(raw, (bool, int, str)):
        return fail("确认结果格式不正确", 400), 400
    try:
        note = _string_field(d, "note", 512)
        dispute_type = _string_field(d, "dispute_type")
        rating_comment = _string_field(d, "rating_comment", 512)
    except TypeError:
        return fail("说明和争议类型必须是文字", 400), 400
    # 星级只能 1–5 的整数。字符串"5"合法，5.5、0、6 一律拒绝——
    # 半星/十分制会污染报表均分的统一口径（1–5 星是标准）。
    # int(3.5) 会坍缩成 3，肉眼看着合法、均值却被悄悄改小，
    # 所以先保证值是整数（float 且非整 或 字符串带小数点都拒）。
    raw_rating = d.get("rating")
    rating = None
    if raw_rating not in (None, ""):
        if isinstance(raw_rating, bool):
            return fail("评分必须是 1–5 星的整数", 400), 400
        if isinstance(raw_rating, float) and not raw_rating.is_integer():
            return fail("评分必须是 1–5 星的整数", 400), 400
        if isinstance(raw_rating, str) and "." in raw_rating:
            return fail("评分必须是 1–5 星的整数", 400), 400
        try:
            rating = int(raw_rating)
        except (TypeError, ValueError):
            return fail("评分必须是 1–5 星的整数", 400), 400
        if not 1 <= rating <= 5:
            return fail("评分必须是 1–5 星的整数", 400), 400
    do_ok = raw is True or str(raw).lower() in ("1", "true", "yes", "ok")
    okc, msg, n = arbitration.resident_confirm(
        t, do_ok, note, dispute_type)
    if not okc:
        return fail(msg, 400), 400
    # 满意度只在"确认解决"时收：没解决时收集 1 星，
    # 等于把争议当评分发了，还占了街道报表的评价分母。
    if do_ok and rating is not None:
        t.rating = rating
        t.rating_comment = rating_comment or ""
    sync_ticket_status(t.fde_id)
    db.session.commit()
    return ok({"affected": n, "msg": msg, "view": _ticket_payload(t)})


@bp.post("/ticket/<int:tid>/attach")
def attach_photo(tid: int):
    """补传照片。居民常常先打字说情况、过一会儿才想起拍图。"""
    t = db.session.get(ResidentTicket, tid)
    if not t:
        return fail("无权操作该记录"), 403
    auth_mode = bool(current_app.config.get("RESIDENT_AUTH"))
    u = current_user() if auth_mode else None
    if auth_mode and not u:
        return fail("请先登录", 401), 401
    if auth_mode:
        if t.phone != (u.phone or "").strip():
            return fail("无权操作该记录，这是其他居民提交的诉求"), 403
    elif tid not in session.get("portal_tickets", []):
        return fail("无权操作该记录"), 403
    if not _rate_ok(f"attach:{tid}", 6, 300):
        return fail("上传太频繁，请稍后再试"), 429

    cur = evidence.load_attachments(t.attachments)
    room = evidence.MAX_PHOTOS - len(cur)
    if room <= 0:
        return fail(f"最多 {evidence.MAX_PHOTOS} 张，已上传 {len(cur)} 张"), 400

    saved, errors = evidence.save_photos(
        request.files.getlist("photos") if request.files else [],
        current_app.config["EVIDENCE_DIR"])
    if not saved:
        return fail("；".join(errors) or "没有收到照片"), 400
    saved = saved[:room]

    t.attachments = json.dumps(cur + saved, ensure_ascii=False)
    for ph in saved:
        evidence.append("ticket", t.id, "attach", {
            "name": ph["name"], "sha256": ph["sha256"], "size": ph["size"],
        }, actor=f"居民(尾号{t.phone[-4:]})")
    db.session.commit()
    return ok({"added": len(saved), "errors": errors,
               "total": len(cur) + len(saved)})


# ------------------------------------------------------------------ 公告与联系人

@bp.get("/notices")
def notices():
    from app.notice.ingest import visible_query
    items = (visible_query("resident")
             # 与内部列表同一套排序：先置顶、再按**置顶时间**。
             # 居民端早先按 published_at 排，两条置顶件的先后就和内部不一样——
             # 社区刚置顶的重点通知反而排在旧件后面，"置顶"在居民眼里没生效。
             .order_by(Notice.pinned.desc(), Notice.pinned_at.desc(),
                       Notice.published_at.desc())
             .limit(50).all())
    return ok({"items": [{
        "id": n.id, "title": n.title, "issuer": n.issuer, "doc_no": n.doc_no,
        "category": n.category,
        "issued_at": n.issued_at.strftime("%Y-%m-%d") if n.issued_at else "",
        "published_at": n.published_at.strftime("%Y-%m-%d") if n.published_at else "",
        "summary": n.summary, "pinned": n.pinned,
    } for n in items]})


@bp.get("/contacts")
def contacts():
    from app.portal_seed import grouped_contacts
    return ok({"groups": [{
        "level": g["level"], "label": g["label"],
        "items": [{"name": c.name, "duty": c.duty, "phone": c.phone,
                   "address": c.address, "service_hours": c.service_hours}
                  for c in g["items"]],
    } for g in grouped_contacts()]})


@bp.get("/roles")
def roles():
    """对外说明"四类使用者各能做什么"，也供居民端底部的说明区使用"""
    return ok({"roles": [{"key": k, "name": v} for k, v in ROLE_NAMES.items() if k != "admin"]})