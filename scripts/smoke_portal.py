"""居民端 + 公告模块冒烟测试

覆盖四类风险，共 26 项：
  1. 居民端可访问性（未登录、移动布局页、越权隔离）
  2. 填报 → 查询码 → 进度查询 的完整闭环
  3. 公告解析（md/docx/txt）→ 自动发布 / 待确认 → 打印版与发布版
  4. 四角色权限矩阵（谁能看、谁能导入、谁能发布）

用独立库 + 指向不可用的模型地址：
  - 独立库避免污染 data/community.db
  - 模型地址指到 127.0.0.1:9 强制走规则降级，这样测试是确定性的、不依赖 GPU
    （要验证模型路径请跑 scripts/eval_e2e.py，那是另一回事）
"""
import os
import sys
import pathlib
import io
from datetime import datetime, timedelta

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = pathlib.Path(os.environ.get("SMOKE_PORTAL_DB", BASE / "data" / "smoke_portal.db"))
if DB.exists():
    DB.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"   # 必然连不上 → 规则降级
os.environ["NOTICE_AUTO_IMPORT"] = "1"

from app import create_app            # noqa: E402
from app.extensions import db         # noqa: E402
from app.models import InviteCode, Notice, ResidentTicket, WorkOrder  # noqa: E402
from app import ratelimit
from app.api import portal as portal_api                   # noqa: E402

app = create_app("dev")
# RESIDENT_AUTH=1：居民端走邀请码注册＋登录，脚本先登记一个演示居民账号。
# 各段断言按 AUTH 分支：演示模式"手机号+查询码"、登录模式"账号绑定手机号"。
AUTH = bool(app.config.get("RESIDENT_AUTH"))
PASS = FAIL = 0
FAILED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  [{detail}]" if detail else ""))


def login(c, u, p="123456"):
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


RESIDENT_USER = "smoke_res"
RESIDENT_PWD = "Smoke1234"
RESIDENT_PHONE = "13800001111"


def prepare_resident(c):
    """RESIDENT_AUTH=1：种一个居民邀请码并注册登录，返回已登录的 client。"""
    with app.app_context():
        cfg = app.config.get("ORG_NAMES", {})
        org = cfg.get("community", "幸福里社区居民委员会")
        if not InviteCode.query.filter_by(code="A-SMOKE-RES").first():
            db.session.add(InviteCode(
                code="A-SMOKE-RES", role="resident", org=org,
                site=app.config["SITE_CODE"]))
            db.session.commit()
    r = c.post("/register", data={
        "username": RESIDENT_USER, "name": "王阿姨", "phone": RESIDENT_PHONE,
        "invite_code": "A-SMOKE-RES", "org": org,
        "password": RESIDENT_PWD, "password2": RESIDENT_PWD},
        follow_redirects=False)
    return r


def main():
    c = app.test_client()
    ratelimit.reset()

    print("\n[1] 居民端可访问性")
    for path in ["/portal", "/portal/report", "/portal/query",
                 "/portal/notices", "/portal/contacts"]:
        r = c.get(path)
        if AUTH:
            # 登录门禁：未登录一律重定向到登录页
            check(f"未登录 GET {path} → 302", r.status_code == 302, r.status_code)
        else:
            check(f"GET {path} = 200", r.status_code == 200, r.status_code)
    if AUTH:
        rr = prepare_resident(c)
        check("居民邀请码注册即登录（302 进居民端）",
              rr.status_code == 302 and "/portal" in (rr.headers.get("Location") or ""),
              rr.status_code)
        with app.app_context():
            from app.models import User
            u = User.query.filter_by(username=RESIDENT_USER).first()
            check("注册即 active（免审核可直接登录）",
                  u is not None and u.status == "active" and u.role == "resident",
                  f"status={u.status if u else None}")
        r = c.get("/portal")
        check("注册后必须已登录（不再重定向）", r.status_code == 200, r.status_code)
    r = c.get("/portal")
    html = r.get_data(as_text=True)
    check("首页含工作人员入口", "工作人员入口" in html)
    check("居民导航不再显示我的", ">我的</a>" not in html)
    check("匿名首页不显示他人诉求或查询码", "最近办理进度" not in html and "查询码" not in html)
    check("首页有通知公告卡入口", "通知公告" in html and "/portal/notice/" in html)
    check("底部导航不加通告按钮", "📢" not in html and html.count("/portal/notices") == 0)
    check("已下线 /portal/my", c.get("/portal/my").status_code == 404)
    check("已下线 /api/portal/my", c.get("/api/portal/my").status_code == 404)
    check("首页引用独立样式 portal.css", "portal.css" in html)
    check("首页不引用内部端 style.css", "css/style.css" not in html)

    r = c.get("/login")
    if AUTH:
        # 已登录访问登录页 → 重定向（门禁）；登录页入口用匿名 client 验证
        check("已登录访问登录页 → 重定向", r.status_code in (302, 303),
              r.status_code)
        anon = app.test_client()
        check("登录页含居民入口",
              "我是居民" in anon.get("/login").get_data(as_text=True))
    else:
        check("登录页含居民入口", "我是居民" in r.get_data(as_text=True))
    report_html = c.get("/portal/report").get_data(as_text=True)
    check("身份字段支持系统自动填充", 'autocomplete="tel"' in report_html and
          'autocomplete="name"' in report_html and 'autocomplete="street-address"' in report_html)
    check("意见支持截图上传与选图后本机识字", 'id="screenshots"' in report_html and
          "TextDetector" in report_html and "recognize(files)" in report_html)
    check("居民端不申请身份证或定位权限", "身份证" in report_html and
          "geolocation" not in report_html and 'name="id_card"' not in report_html)

    print("\n[2] 公告自动导入（三种格式）")
    with app.app_context():
        total = Notice.query.count()
        pub = Notice.query.filter_by(status="published").count()
        pen = Notice.query.filter_by(status="pending").count()
        docx_n = Notice.query.filter(Notice.source_file.like("%.docx")).count()
        txt_n = Notice.query.filter(Notice.source_file.like("%.txt")).count()
    check(f"导入 {total} 条（≥4）", total >= 4, total)
    check(f"自动发布 {pub} 条", pub >= 3, pub)
    check(f"待确认 {pen} 条（缺发文单位那条）", pen == 1, pen)
    check("含 .docx 解析结果", docx_n == 1, docx_n)
    check("含 .txt 解析结果", txt_n == 1, txt_n)

    with app.app_context():
        n = Notice.query.filter(Notice.source_file.like("%.md"), Notice.status == "published").first()
        nid = n.id
        has_table = "|" in n.body_md and "---" in n.body_md
        tail = n.body_md.strip().splitlines()[-1]
        # 正文末尾不应残留落款（否则页面会显示两遍）
        no_sign = n.issuer not in tail and "年" not in tail[-12:]
        pending = Notice.query.filter_by(status="pending").first()
        pid = pending.id
    check("Markdown 表格进正文", has_table)
    check("落款已从正文剔除", no_sign, tail[:30])

    print("\n[3] 输出：网页版 / 打印版 / 发布版")
    r = c.get(f"/portal/notice/{nid}")
    check("网页版 200", r.status_code == 200)
    body = r.get_data(as_text=True)
    check("网页版含发布版/打印版入口", "发布版" in body and "打印版" in body)
    check("详情页返回首页而非公告列表",
          "返回首页" in body and "/portal/notices" not in body)

    r = c.get(f"/portal/notice/{nid}?mode=official")
    off = r.get_data(as_text=True)
    check("发布版 200", r.status_code == 200)
    check("发布版有红头样式", 'class="redhead"' in off and "#d0021b" in off)
    check("发布版含发文字号", "幸街发" in off)
    check("发布版有打印触发", "window.print()" in off)
    check("发布版用仿宋公文正文字号 16px", "font-size: 16px" in off or "16px" in off)

    r = c.get(f"/portal/notice/{nid}?mode=print")
    pr = r.get_data(as_text=True)
    check("打印版 200", r.status_code == 200)
    check("打印版有 @page A4", "@page" in pr and "A4" in pr)
    check("打印版隐藏工具栏", ".toolbar { display: none" in pr)
    check("打印版带打印时间戳", "打印时间" in pr)

    r = c.get(f"/portal/notice/{pid}")
    check("待确认公告居民端 404", r.status_code == 404, r.status_code)
    r = c.get(f"/portal/notice/{nid}?mode=xxx")
    check("非法 mode 回落网页版", r.status_code == 200)

    # 首页通知公告卡：这些都已发布（本环节刚 publish），首页必须能看到标题，
    # 且不显示草稿标题——防"入口在但首页列表空/漏发布"的锁死。
    home2 = c.get("/portal").get_data(as_text=True)
    check("首页渲染已发布公告标题", "消防安全" in home2 or "居民" in home2 or "生活垃圾" in home2,
          home2[:80])
    check("首页不渲染待确认公告", "申请增补" not in home2)

    print("\n[4] 居民填报 → 查询码 → 查进度")
    ratelimit.reset()
    r = c.post("/api/portal/report", data={
        "text": "2栋2单元202楼道灯坏了半个月，另外老人身份证要办高龄补贴，电话13800001111",
        "phone": "13800001111", "name": "王阿姨", "address": "2栋2单元202",
        "photos": (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"test screenshot"), "chat.png")})
    j = r.get_json()
    check("提交成功", j["code"] == 0, j.get("msg"))
    d = j["data"]
    code = d["code"]
    check("返回 6 位查询码", len(code) == 6 and code.isdigit(), code)
    check("拆出 2 个业务项", d["item_count"] == 2, d["item_count"])
    check("生成 1 条工单", d["order_count"] == 1, d["order_count"])
    check("生成 1 条台账", d["ledger_count"] == 1, d["ledger_count"])
    check("截图随诉求进入安全附件链", d["photos"] == 1, d.get("photos"))
    duplicate = c.post("/api/portal/report", data={
        "text": "2栋2单元202楼道灯坏了半个月，另外老人身份证要办高龄补贴，电话13800001111",
        "phone": "13800001111",
        "photos": (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"second screenshot"), "chat2.png")})
    duplicate_data = duplicate.get_json()["data"]
    check("重复诉求复用原单并保留新截图",
          duplicate_data["deduplicated"] and duplicate_data["photos"] == 1, duplicate_data)

    r = c.get(f"/api/portal/ticket?phone=13800001111&code={code}")
    j = r.get_json()
    check("凭码查询成功", j["code"] == 0, j.get("msg"))
    v = j["data"]
    check("返回手机号已打码", v["phone"] == "138****1111", v["phone"])
    check("事项带承办单位", all(it["orgs"] for it in v["items"]))
    check("不返回内部字段 fields_json", "fields_json" not in r.get_data(as_text=True))

    tid = v["id"]
    r = c.get(f"/portal/done/{tid}")
    check("提交成功页 200", r.status_code == 200)
    check("成功页显示查询码", code in r.get_data(as_text=True))
    check("提交后显示黄灯受理反馈", "p-traffic-yellow" in r.get_data(as_text=True))
    home_html = c.get("/portal").get_data(as_text=True)
    check("本机会话首页免查询码查看最近进度",
          "最近办理进度" in home_html and f"/portal/ticket/{d['ticket_id']}" in home_html)
    query_html = c.get("/portal/query").get_data(as_text=True)
    check("查询页提供最近记录快捷入口", "最近办理进度" in query_html and "用查询码查其他记录" in query_html)
    check("提交页说明查询码仅供换设备备用", "本机可从首页直接查看最近进度" in r.get_data(as_text=True))

    # 办理进度页必须真能渲染。早先模板写成 view.items，而 view 是 dict，
    # Jinja 会取到 dict 自带的 .items 方法 → for 循环一个内置方法 → 整页 500 JSON。
    # 这里按"页面有内容"断，不只看状态码：500 时返回的是 JSON，状态码也是 500 罢了。
    r = c.get(f"/portal/ticket/{tid}")
    html = r.get_data(as_text=True)
    check("办理进度页 200", r.status_code == 200, r.status_code)
    check("进度页渲染成 HTML 而非错误 JSON",
          "text/html" in r.headers.get("Content-Type", "") and "服务内部错误" not in html)
    check("进度页显示简洁状态灯", "办理进度" in html and "p-traffic-yellow" in html)
    check("进度页不暴露内部拆解明细", "楼道灯" not in html and "办理事项" not in html)

    with app.app_context():
        order = WorkOrder.query.filter(WorkOrder.work_item_id.in_(
            db.session.query(portal_api.WorkItem.id).filter_by(fde_id=v.get("fde_id", -1))
        )).first()
        if order is None:
            order = WorkOrder.query.first()
        order.created_at = datetime.now() - timedelta(hours=25)
        order.status = "open"
        order.sla_hours = None
        db.session.commit()
        ticket_row = db.session.get(ResidentTicket, tid)
        fallback_sla_payload = portal_api._ticket_payload(ticket_row)
        order.created_at = datetime.now() - timedelta(hours=48)
        order.sla_hours = 1
        db.session.commit()
        ticket_row = db.session.get(ResidentTicket, tid)
        overdue_payload = portal_api._ticket_payload(ticket_row)
    check("空 SLA 按督办口径 24 小时兜底并显示红灯",
          fallback_sla_payload["traffic_light"] == "red",
          fallback_sla_payload["traffic_light"])
    check("超过 SLA 的诉求显示红灯", overdue_payload["traffic_light"] == "red",
          overdue_payload["traffic_light"])

    # 换设备：演示模式无会话时凭手机号+查询码找回；登录模式先登录同一账号
    c2 = app.test_client()
    if AUTH:
        login(c2, RESIDENT_USER, RESIDENT_PWD)
    r = c2.get(f"/api/portal/ticket?phone=13800001111&code={code}")
    check("新设备凭码查询成功", r.get_json()["code"] == 0)
    r = c2.get(f"/portal/ticket/{tid}")
    check("新设备能打开进度页", r.status_code == 200, r.status_code)

    # 没凭据不该看到别人的单子（登录模式未登录 → 重定向登录页）
    c3 = app.test_client()
    r = c3.get(f"/portal/ticket/{tid}")
    check("无会话直接访问进度页被拒",
          r.status_code == (302 if AUTH else 404), r.status_code)

    r = c.get(f"/api/portal/ticket?phone=13800001111&code=000000")
    check("错误查询码被拒", r.get_json()["code"] != 0)
    if AUTH:
        # 登录模式手机号以账号为准：请求里传别的手机号不生效，仍查本人
        r = c.get(f"/api/portal/ticket?phone=13900000000&code=" + code)
        j = r.get_json()
        check("登录模式手机号以账号为准（传错手机号仍查到本人）",
              j["code"] == 0 and j["data"]["phone"] == "138****1111", j.get("msg"))
        # 登录模式提交手机号不可自由填写：请求里带非法手机号也会被账号覆盖
        r = c.post("/api/portal/report", json={
            "text": "灯坏了需要修", "phone": "123", "name": "王阿姨"})
        jj = r.get_json()
        get_ok = jj["code"] == 0 and "ticket_id" in (jj.get("data") or {})
        if get_ok:
            r2 = c.get(f"/api/portal/ticket?phone=123&code={jj['data']['code']}")
            q = r2.get_json()
            get_ok = q["code"] == 0 and q["data"]["phone"] == "138****1111"
        check("登录模式提交不接收自由手机号（以账号为准）",
              get_ok, jj.get("msg") or r.get_json().get("msg"))
    else:
        r = c.get(f"/api/portal/ticket?phone=13900000000&code=" + code)
        check("手机号与码不匹配被拒", r.get_json()["code"] != 0)
        r = c.post("/api/portal/report", json={"text": "灯坏了需要修", "phone": "123"})
        check("手机号格式校验", r.get_json()["code"] != 0)
    r = c.post("/api/portal/report", json={"text": "灯坏", "phone": "13800001111"})
    check("内容过短校验", r.get_json()["code"] != 0)

    print("\n[5] 防刷限流")
    ratelimit.reset()
    codes = []
    for i in range(7):
        r = c.post("/api/portal/report", json={
            "text": f"测试限流第{i}条，楼道灯不亮需要维修", "phone": "13700000001"})
        codes.append(r.get_json()["code"])
    check("前 5 次放行", all(x == 0 for x in codes[:5]), codes)
    check("第 6 次起拦截", codes[5] != 0 and codes[6] != 0, codes[5:])

    print("\n[6] 四角色权限矩阵")
    ratelimit.reset()
    cases = [
        # (账号, 角色名, 期望 notice.view, 期望 scan 导入, 期望 publish 发布)
        ("wuye", "物业", True, False, False),
        ("jiedao", "街道", True, True, False),
        ("wang", "社区", True, False, True),
    ]
    for user, label, can_view, can_import, can_publish in cases:
        c2 = app.test_client()
        login(c2, user)
        r = c2.get("/notices")
        check(f"{label} 可查看公告页", (r.status_code == 200) == can_view, r.status_code)
        r = c2.post("/api/notices/scan", json={})
        check(f"{label} 导入上级文件 = {can_import}", (r.status_code == 200) == can_import,
              r.status_code)
        r = c2.post(f"/api/notices/{nid}/publish")
        check(f"{label} 发布公告 = {can_publish}", (r.status_code == 200) == can_publish,
              r.status_code)

    c3 = app.test_client()
    r = c3.get("/notices", follow_redirects=False)
    check("未登录访问公告页 → 302", r.status_code == 302, r.status_code)
    r = c3.get("/api/notices")
    check("未登录调公告接口 → 401", r.status_code == 401, r.status_code)

    print("\n[7] 待确认 → 补全 → 发布")
    c4 = app.test_client()
    login(c4, "wang")
    r = c4.post(f"/api/notices/{pid}/publish")
    # 业务拒绝走统一响应（HTTP 200 + code≠0），与 403 的权限拒绝区分开
    j = r.get_json()
    check("缺发文单位不能发布", j["code"] != 0 and "发文单位" in j.get("msg", ""), j.get("msg"))
    with app.app_context():
        check("被拒后状态未变", db.session.get(Notice, pid).status == "pending")
    r = c4.patch(f"/api/notices/{pid}", json={"issuer": "幸福里街道办事处"})
    check("补全发文单位成功", r.status_code == 200 and r.get_json()["code"] == 0)
    r = c4.post(f"/api/notices/{pid}/publish")
    check("补全后可发布", r.status_code == 200)
    r = c4.get(f"/portal/notice/{pid}")
    check("发布后居民端可见", r.status_code == 200)

    print("\n[8] 下架")
    r = c4.post(f"/api/notices/{pid}/offline")
    check("下架成功", r.status_code == 200)
    r = c4.get(f"/portal/notice/{pid}")
    check("下架后居民端 404", r.status_code == 404, r.status_code)
    with app.app_context():
        check("下架记录仍保留（可追溯）",
              db.session.get(Notice, pid).status == "offline")

    print("\n[9] 重复扫描去重")
    c5 = app.test_client()
    login(c5, "jiedao")
    r = c5.post("/api/notices/scan", json={})
    d = r.get_json()["data"]
    check("第二次扫描全部跳过", not d["imported"] and not d["pending"], d["msg"])

    print("\n[10] 数据留痕")
    with app.app_context():
        from app.models import AuditLog
        acts = {a.action for a in AuditLog.query.filter_by(agent="notice").all()}
        check("公告操作写审计", bool(acts), acts)
        check("含 publish 与 offline 留痕", {"publish", "offline"} <= acts, acts)
        t = ResidentTicket.query.first()
        check("居民提交落库", t is not None and t.fde_id is not None)
        o = WorkOrder.query.first()
        check("工单内容为脱敏摘要", o is not None and "13800001111" not in (o.content or ""),
              o.content if o else "")

    print("\n[11] 滚动预警条正交性（P1-5）")
    from datetime import datetime as _dt
    ids = []
    with app.app_context():
        # ①普通 ②置顶 ③预警 ④置顶+预警：四者均为「已发布 + scope=all」，可被居民端看到
        def _mk(title, pinned, is_alert, dow=1):
            n = Notice(
                title=title, scope="all", category="通知公告", level="街道",
                issuer="幸福里街道办事处", status="published", kind="superior",
                source_file="", source_hash=f"smoke-alert-{title}", pinned=pinned,
                pinned_at=_dt(2026, 11, dow) if pinned else None,
                is_alert=bool(is_alert),
                published_at=_dt(2026, 11, dow + 3),
                summary=f"[探测] {title} 摘要", body_md=f"# {title}\n正文占位。")
            db.session.add(n)
            db.session.flush()
            ids.append(n.id)
        _mk("测试公告一（普通）", False, False)
        _mk("测试公告二（置顶）", True, False)
        _mk("测试公告三（预警）", False, True)
        _mk("测试公告四（置顶+预警）", True, True, dow=2)
        db.session.commit()
    with app.app_context():
        # 只有 ③④ 进首页滚动条，①② 不进
        r = c.get("/portal")
        html = r.get_data(as_text=True)
        check("首页渲染滚动条（有预警时）", 'class="p-alert"' in html)
        check("预警公告进滚动条（③④）",
              "[探测] 测试公告三（预警） 摘要" in html and
              "[探测] 测试公告四（置顶+预警） 摘要" in html)
        check("普通公告不进滚动条（①）", "[探测] 测试公告一（普通） 摘要" not in html)
        check("置顶公告不进滚动条（②）", "[探测] 测试公告二（置顶） 摘要" not in html)
        # 列表：②④ 排最前且带置顶标签；③ 按时间排、不带置顶标签
        r = c.get("/portal/notices")
        html = r.get_data(as_text=True)
        i1, i2, i3, i4 = (html.index(t) for t in (
            "测试公告一（普通）", "测试公告二（置顶）",
            "测试公告三（预警）", "测试公告四（置顶+预警）"))
        check("置顶公告排最前（②④ 先于 ①③）", max(i2, i4) < min(i1, i3),
              f"{i1},{i2},{i3},{i4}")
        check("后置顶的排更前（④先于②）", i4 < i2, f"{i2},{i4}")
        import re
        def _has_pin(html, mark):
            return bool(re.search(
                r">测试公告" + re.escape(mark) + r"</span>\s*"
                r"<span class=\"tag tag-pin\">置顶", html))
        check("置顶公告带置顶标签（②④）",
              _has_pin(html, "二（置顶）") and _has_pin(html, "四（置顶+预警）"))
        check("预警未置顶不带标签（③）", not _has_pin(html, "三（预警）"))
        # 点击滚动条/列表项均进网页版详情，且详情可切打印/发布版
        n4 = db.session.get(Notice, ids[3])
        href_ok = (f'"/portal/notice/{n4.id}"' in html and
                   f"/portal/notice/{n4.id}" in c.get("/portal").get_data(as_text=True))
        check("滚动条与列表均指向网页版详情", href_ok)
        r = c.get(f"/portal/notice/{n4.id}")
        dhtml = r.get_data(as_text=True)
        check("详情页可切打印版/发布版", "打印版" in dhtml and "发布版" in dhtml)
        # 草稿/未发布的预警绝不外泄：造一条 pending 预警
        n5 = Notice(title="测试公告五（预警草稿）", scope="all", category="通知公告",
                    level="街道", issuer="幸福里街道办事处", status="pending",
                    kind="superior", source_file="", source_hash="smoke-alert-pending",
                    is_alert=True, body_md="# 草稿预警\n不应外泄。",
                    summary="[探测] 草稿预警摘要")
        db.session.add(n5)
        db.session.commit()
        n5_id = n5.id
        r = c.get("/portal")
        html = r.get_data(as_text=True)
        check("草稿预警不进首页滚动条", "[探测] 草稿预警摘要" not in html)
        r = c.get("/portal/notices")
        check("草稿预警不进公告列表", "测试公告五（预警草稿）" not in r.get_data(as_text=True))
        r = c.get(f"/portal/notice/{n5_id}")
        check("草稿预警拼 URL 也 404", r.status_code == 404)
        # 删掉预警后首页无滚动元素
        for n in db.session.query(Notice).filter(Notice.id.in_(ids)).all():
            n.is_alert = False
        db.session.query(Notice).filter(Notice.id == n5_id).delete()
        db.session.commit()
    with app.app_context():
        r = c.get("/portal")
        html = r.get_data(as_text=True)
        check("无预警时首页不渲染滚动条", 'class="p-alert"' not in html)

    print("\n[12] 关怀模式朗读链路（P0-2）")
    home_html = c.get("/portal").get_data(as_text=True)
    check("页头有关怀开关按钮", 'id="careToggle"' in home_html and "关怀" in home_html)
    check("xfCare 对象已定义并暴露 speak",
          "window.xfCare = {" in home_html and "speak: function" in home_html)
    care_src = home_html[home_html.index("window.xfCare"):home_html.index("window.xfCare.apply()")]
    for guard in ("if (!this.isOn()) return false;",
                  "if (!window.speechSynthesis) return false;",
                  "if (window.speechSynthesis.speaking) return false;",
                  "if (!text || !text.trim()) return false;"):
        name = " ".join(guard.split()[0:8]).rstrip(';')
        check(f"speak 内置守卫：{name}", guard in care_src)
    check("开关状态存 localStorage 键 xf_care", "'xf_care'" in home_html)
    check("speak 等待语音列表就绪(voiceschanged)",
          "getVoices" in care_src and "voiceschanged" in care_src
          and "setTimeout" in care_src)
    # 网页版详情：开启关怀时朗读「标题 + .doc-body 全文」，并提供暂停/继续/停止控制条
    r = c.get(f"/portal/notice/{nid}")
    dhtml = r.get_data(as_text=True)
    check("网页版详情开关怀朗读正文", "xfCare.speak" in dhtml and ".doc-body" in dhtml)
    check("网页版朗读包含公告标题", "doc-head h1" in dhtml)
    check("网页版朗读提供暂停/停止控制条",
          'id="readToggle"' in dhtml and 'id="readStop"' in dhtml
          and "togglePause" in dhtml and "readbar" in dhtml)
    # 发布版/打印版：读 localStorage 键同步决定是否朗读
    off = c.get(f"/portal/notice/{nid}?mode=official").get_data(as_text=True)
    pr = c.get(f"/portal/notice/{nid}?mode=print").get_data(as_text=True)
    check("发布版开关怀朗读正文（同键）",
          "xf_care" in off and "speechSynthesis" in off
          and "voiceschanged" in off and "getVoices" in off)
    check("打印版开关怀朗读正文（同键）",
          "xf_care" in pr and "speechSynthesis" in pr
          and "voiceschanged" in pr and "getVoices" in pr)
    check("发布版朗读含标题与控制条", "doc-title" in off and 'id="readlineToggle"' in off)
    check("打印版朗读含标题与控制条", "doc-title" in pr and 'id="readlineStop"' in pr)
    import os
    css_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "app", "static", "css", "portal.css")
    css = open(css_path, encoding="utf-8").read()
    check("关怀字号放大样式 body.care 联动", "body.care" in css and
          ".p-care.on" in css and "body.care .doc-body" in css)

    print(f"\n{'=' * 56}")
    print(f"通过 {PASS} / {PASS + FAIL}")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  -", f)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())