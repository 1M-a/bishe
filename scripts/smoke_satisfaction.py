"""回归：满意度 1–5 星 + 街道报表均值（P1-4）

守的点：
1. 老库迁移补列后，已有数据不丢，rating/rating_comment 可写可读；
2. confirm 接口：评分校验（非 1~5 拒绝 / 非整数拒绝 / 空则跳过）；
3. 评分只随"已解决"收：没解决提交评分不落库；
4. 居民端 ticket 页有星控、已评分后显示评价卡；
5. 报表：本期已评星诉求 → 均分快照写入报表并出现在摘要/详情/导出件。

跑法：独立库 data/smoke_satisfaction.db，每次重建。

注意：HTTP 调用必须在模块顶层跑（不进 app.app_context()），否则 Flask 3.x
test_client 的登录 cookie 不会随后续请求发送——smoke_report_export.py 已验证。
"""
import json
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_satisfaction.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os_env = __import__("os").environ
os_env["DATABASE_URL"] = "sqlite:///" + str(DB)
os_env["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
os_env["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                                   # noqa: E402
from app.extensions import db                                # noqa: E402
from app.models import ResidentTicket                        # noqa: E402

app = create_app("dev")
app.config["TESTING"] = True

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{extra}]" if extra and not cond else ""))


def seed():
    """造两条居民诉求（含完整链条：诉求→首录→业务项→已办结工单）。

    confirm 只允许对"已办结"的工单表态（resident_confirm 按 status==done 过滤），
    所以每张单都要挂到一条 done 的工单下，否则接口拦在第一步，评分根本没机会触发。
    """
    from datetime import datetime
    from app.models import FdeRecord, WorkItem, WorkOrder
    f1 = FdeRecord(raw_text="走廊灯坏了", source="text", creator="测试",
                   created_at=datetime(2026, 11, 5))
    f2 = FdeRecord(raw_text="门锁失灵", source="text", creator="测试",
                   created_at=datetime(2026, 11, 6))
    db.session.add_all([f1, f2])
    db.session.commit()

    i1 = WorkItem(fde_id=f1.id, type="报修", subject="楼道走廊灯故障",
                  target_lines="物业", status="done",
                  created_at=datetime(2026, 11, 5))
    i2 = WorkItem(fde_id=f2.id, type="报修", subject="入户门锁失灵",
                  target_lines="物业", status="done",
                  created_at=datetime(2026, 11, 6))
    db.session.add_all([i1, i2])
    db.session.commit()

    o1 = WorkOrder(order_no="SD-1001", work_item_id=i1.id, property_co="幸福里物业",
                   content="走廊灯故障维修", sla_hours=24, status="done",
                   assignee="李师傅", done_at=datetime(2026, 11, 7),
                   created_at=datetime(2026, 11, 5))
    o2 = WorkOrder(order_no="SD-1002", work_item_id=i2.id, property_co="幸福里物业",
                   content="门锁失灵维修", sla_hours=24, status="done",
                   assignee="王师傅", done_at=datetime(2026, 11, 8),
                   created_at=datetime(2026, 11, 6))
    db.session.add_all([o1, o2])
    db.session.commit()

    t1 = ResidentTicket(code="111111", phone="13800000001", name="居民甲",
                        address="1栋101", raw_text="走廊灯坏了",
                        normalized_text="走廊灯坏 cf",
                        fde_id=f1.id, created_at=datetime(2026, 11, 5))
    t2 = ResidentTicket(code="222222", phone="13800000002", name="居民乙",
                        address="2栋201", raw_text="门锁失灵",
                        normalized_text="门锁失灵 cf",
                        fde_id=f2.id, created_at=datetime(2026, 11, 6))
    db.session.add_all([t1, t2])
    db.session.commit()
    return t1.id, t2.id


# ================================================================ 数据准备
with app.app_context():
    t1id, t2id = seed()

    # ---- 迁移补列 ----
    cols = {c["name"] for c in __import__("sqlalchemy").inspect(db.engine).get_columns("resident_ticket")}
    check("迁移补列 rating", "rating" in cols)
    check("迁移补列 rating_comment", "rating_comment" in cols)


c = app.test_client()


# ---- confirm 接口 ----
def confirm(tid, payload):
    return c.post(f"/api/portal/ticket/{tid}/confirm",
                  json=payload, headers={"X-Forwarded-For": "10.1.1.1"})


def ticket_attr(tid, attr):
    with app.app_context():
        return getattr(db.session.get(ResidentTicket, tid), attr)


# 未授权（不属于本会话）
r = confirm(t1id, {"ok": True, "rating": 5})
check("未授权渲染 403", r.status_code == 403, f"got {r.status_code}")

# 伪造会话后可评
with c.session_transaction() as s:
    s["portal_tickets"] = [t1id, t2id]
r = confirm(t1id, {"ok": True, "rating": 5})
j = r.get_json() or {}
check("已解决+5星 成功", r.status_code == 200 and j.get("code") == 0,
      f"got {r.status_code} {j}")
check("rating 落库=5", ticket_attr(t1id, "rating") == 5)
check("未带评分评论默认为空", ticket_attr(t1id, "rating_comment") == "")

# 没解决带评分 → 不落评分
r = confirm(t2id, {"ok": False, "note": "门锁还是坏的，进不去", "rating": 1})
check("没解决带评分+note 成功", r.status_code == 200, f"got {r.status_code}")
check("没解决不落评分", ticket_attr(t2id, "rating") is None)

# 评分边界：0 / 6 / 3.5 / 非数字
r = confirm(t1id, {"ok": True, "rating": 0})
check("0 星被拒", r.status_code == 400)
r = confirm(t1id, {"ok": True, "rating": 6})
check("6 星被拒", r.status_code == 400)
r = confirm(t1id, {"ok": True, "rating": 3.5})
check("3.5 星被拒", r.status_code == 400)
r = confirm(t1id, {"ok": True, "rating": "abc"})
check("非数字被拒", r.status_code == 400)

# 自由文本评分评论
r = confirm(t2id, {"ok": True, "rating": 4,
                   "rating_comment": "处理及时，态度不错"})
check("评分评论落库", ticket_attr(t2id, "rating") == 4
      and ticket_attr(t2id, "rating_comment") == "处理及时，态度不错")

# ---- 报表均值 ----
with app.app_context():
    from app.agents.report import ReportAgent
    agent = ReportAgent()
    data = agent.run({"period": "2026-11", "org": "幸福里社区", "actor": "测试"})
check("报表含 satisfaction 快照", "satisfaction" in data)
sat = data["satisfaction"]
check("报表已评数=2", sat.get("rated") == 2, f"got {sat.get('rated')}")
check("报表均分=4.5", sat.get("avg") == 4.5, f"got {sat.get('avg')}")
check("报表摘要含满意度", "满意度" in data["summary"])
check("摘要含均分 4.5", "4.5" in data["summary"])

# ---- 报表详情 / 导出：需要社区账号登录（report.view） ----
login = c.post("/login", data={"username": "wang", "password": "123456"},
               follow_redirects=False)
check("社区账号登录成功", login.status_code in (302, 303), f"got {login.status_code}")

r = c.get("/api/reports")
listj = r.get_json() or {}
rid = (listj.get("data") or [{}])[0].get("id")
r = c.get(f"/api/reports/{rid}")
dj = (r.get_json() or {}).get("data") or {}
check("详情接口带 satisfaction", "satisfaction" in dj)

# 导出 xlsx 概览含"居民满意度"
import io as _io
import zipfile as _zf
import xml.etree.ElementTree as _ET

def _cells(sheet_xml: str) -> dict:
    NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = _ET.fromstring(sheet_xml)
    out = {}
    for cc in root.iter(f"{{{NS['m']}}}c"):
        ref = cc.get("r")
        col = "".join(ch for ch in ref if ch.isalpha())
        row = int("".join(ch for ch in ref if ch.isdigit()))
        t = cc.get("t")
        if t == "inlineStr":
            node = cc.find(f"{{{NS['m']}}}is/{{{NS['m']}}}t")
            out[(row, col)] = node.text if node is not None else ""
        else:
            v = cc.find(f"{{{NS['m']}}}v")
            if v is not None and v.text is not None:
                out[(row, col)] = v.text
    return out

r = c.get(f"/reports/{rid}/export/xlsx")
z = _zf.ZipFile(_io.BytesIO(r.data))
names = _ET.fromstring(z.read("xl/workbook.xml"))
sheets = {}
for i, nm in enumerate(names.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet"), 1):
    sheets[nm.get("name")] = _cells(z.read(f"xl/worksheets/sheet{i}.xml").decode())
ov = sheets.get("月报概览") or {}
labels = [str(v) for (row, col), v in ov.items() if col == "A"]
values = [str(v) for (row, col), v in ov.items() if col == "B"]
check("导出件含满意度指标", any("居民满意度" in (lab or "") for lab in labels), labels)
check("导出件含均分值", any(v in ("4.5", "4.50") or v.startswith("4.5") for v in values), values)

# ---- 居民端 ticket 页 ----
with c.session_transaction() as s:
    s["portal_tickets"] = [t2id]
r = c.get(f"/portal/ticket/{t2id}", follow_redirects=False)
html = r.data.decode()
check("ticket 页含星控容器", "stars" in html)
check("ticket 页已评分显示", "满意度评价" in html and "4/5" in html)

print("=" * 60)
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  ✗", f)
    sys.exit(1)