"""知识库接口：文档管理 / 检索 / 基于资料的问答"""
import hashlib

from flask import Blueprint, current_app, request

from app.agents.knowledge import KnowledgeAgent, contains_prompt_injection
from app.agents.security import detect_sensitive
from app.api import fail, ok
from app.auth import current_user, perm_required
from app.extensions import db
from app.kb.store import get_index, rebuild, split_chunks
from app.models import KbDoc
from app.search import like_all

bp = Blueprint("kb", __name__)
agent = KnowledgeAgent()


def _rebuild():
    rebuild(current_app.config.get("KB_CHUNK_SIZE", 420),
            current_app.config.get("KB_EMBED_MODEL", ""))


def _audit(action: str, detail: str):
    """知识库增删必须留痕。

    为什么这里也要审计：知识库是"系统回答居民政策问题"的唯一依据，
    删掉一份《高龄津贴申领指南》，之后所有相关咨询都会变成"查无依据"——
    影响面比改一条公告还大，却没有发布/下架那样的审批动作。
    文档上有 created_by，但删除动作本身不会留下任何痕迹，事后无法回答
    "这份文件是谁、什么时候拿掉的"。导出都要留痕，删依据更该留。
    """
    from app.models import AuditLog
    u = current_user()
    db.session.add(AuditLog(actor=(u.name if u else "system"), agent="kb",
                            action=action, input_digest=detail[:240]))
    db.session.commit()


@bp.get("/stats")
@perm_required("kb.view")
def stats():
    idx = get_index()
    cats = (db.session.query(KbDoc.category, db.func.count(KbDoc.id))
            .group_by(KbDoc.category).all())
    return ok({**idx.stats(),
               "doc_count": KbDoc.query.count(),
               "categories": [{"name": c, "count": n} for c, n in cats]})


@bp.get("/docs")
@perm_required("kb.view")
def list_docs():
    q = KbDoc.query
    cat = request.args.get("category", "").strip()
    kw = request.args.get("q", "").strip()
    if cat:
        q = q.filter_by(category=cat)
    if kw:
        # 标题检索也走统一的 LIKE 构造：contains() 拼出来的 '%kw%' 同样不转义元字符，
        # 搜一个下划线就能把整张表捞出来。
        cond = like_all([KbDoc.title], kw)
        if cond is not None:
            q = q.filter(cond)
    docs = q.order_by(KbDoc.id).all()
    return ok([{
        "id": d.id, "title": d.title, "category": d.category, "source": d.source,
        "content_hash": hashlib.sha256(d.content.encode("utf-8")).hexdigest(),
        "chars": len(d.content),
        "chunks": len(split_chunks(d.content, current_app.config.get("KB_CHUNK_SIZE", 420))),
        "created_by": d.created_by,
        "created_at": d.created_at.strftime("%Y-%m-%d"),
    } for d in docs])


@bp.get("/docs/<int:did>")
@perm_required("kb.view")
def doc_detail(did):
    d = KbDoc.query.get_or_404(did)
    return ok({"id": d.id, "title": d.title, "category": d.category,
               "source": d.source, "content": d.content,
               "content_hash": hashlib.sha256(d.content.encode("utf-8")).hexdigest()})


@bp.post("/docs")
@perm_required("kb.manage")
def create_doc():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    if not title:
        return fail("请填写文档标题")
    if len(title) > 128:
        return fail("文档标题不能超过 128 字")
    if len(content) < 20:
        return fail("正文太短（至少 20 字），请粘贴完整的政策或指南内容")
    if len(content) > 200_000:
        return fail("正文超过 20 万字，请拆分为多份文件后入库")
    pii = detect_sensitive(content)
    if pii:
        _audit("reject_doc_pii", f"拒绝含强敏感信息的文档《{title}》："
               + "、".join(sorted({x['type'] for x in pii})))
        return fail("知识库只存政策与工作规范，正文含身份证号或个人手机号，请脱敏后再入库")
    if contains_prompt_injection(content):
        _audit("reject_doc_injection", f"拒绝疑似提示词注入文档《{title}》")
        return fail("文档包含疑似模型控制指令，已拒绝入库，请删除相关指令后重试")
    if KbDoc.query.filter_by(title=title).first():
        return fail("同名文档已存在，请改名或删除原文档")
    u = current_user()
    d = KbDoc(title=title, category=(data.get("category") or "政策文件").strip(),
              source=(data.get("source") or "").strip(), content=content,
              tags=(data.get("tags") or "").strip(), created_by=u.name if u else "")
    db.session.add(d)
    db.session.commit()
    _rebuild()
    _audit("create_doc", f"新增知识库文档《{title}》（{d.category}，{len(content)} 字）")
    return ok({"id": d.id, "chunks": len(split_chunks(content,
                current_app.config.get("KB_CHUNK_SIZE", 420)))})


@bp.delete("/docs/<int:did>")
@perm_required("kb.manage")
def delete_doc(did):
    d = KbDoc.query.get_or_404(did)
    title = d.title
    db.session.delete(d)
    db.session.commit()
    _rebuild()
    _audit("delete_doc", f"删除知识库文档《{title}》（id={did}）")
    return ok({"deleted": title})


@bp.get("/search")
@perm_required("kb.view")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return fail("请输入检索内容")
    if len(q) > 300:
        return fail("检索内容不能超过 300 字")
    try:
        top_k = max(1, min(int(request.args.get("top_k", 4)), 10))
    except ValueError:
        top_k = 4
    u = current_user()
    return ok(agent.run({"query": q, "top_k": top_k,
                         "actor": u.name if u else "system"}))


@bp.post("/ask")
@perm_required("kb.view")
def ask():
    question = (request.get_json(silent=True) or {}).get("question", "").strip()
    if not question:
        return fail("请输入问题")
    if len(question) > 500:
        return fail("问题不能超过 500 字")
    if contains_prompt_injection(question):
        return fail("问题包含疑似模型控制指令，请改为具体的政策或办理问题")
    u = current_user()
    return ok(agent.answer(question, top_k=current_app.config.get("KB_TOP_K", 4),
                           actor=u.name if u else "system"))
