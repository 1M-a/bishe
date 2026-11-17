"""居务会议闭环回归：权限、法定人数、决议转任务、公开隔离与幂等。"""
import os
import pathlib
import sys
from datetime import datetime, timedelta

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
DB = BASE / "data" / "smoke_meetings.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from werkzeug.security import generate_password_hash  # noqa: E402
from app import create_app                            # noqa: E402
from app.extensions import db                         # noqa: E402
from app.models import AuditLog, Meeting, Notice, Task, User  # noqa: E402

app = create_app("dev")
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + name)
    else:
        FAIL += 1
        print("  [FAIL] " + name + (f"  {detail}" if detail else ""))


def login(c, user, pwd="123456"):
    return c.post("/login", data={"username": user, "password": pwd},
                  follow_redirects=False)


past = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
future = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M")

community = app.test_client()
street = app.test_client()
prop = app.test_client()
admin = app.test_client()
login(community, "wang")
login(street, "jiedao")
login(prop, "wuye")
login(admin, "admin", "admin123")

print("=" * 66)
print("A. 页面与权限")
print("=" * 66)
check("社区可打开居务议事页", community.get("/meetings").status_code == 200)
check("街道可监督查看会议页", street.get("/meetings").status_code == 200)
check("物业不能接触内部议事页", prop.get("/meetings").status_code == 403)
check("街道不能代社区建会", street.post("/api/meetings", json={}).status_code == 403)
check("物业不能读取会议接口", prop.get("/api/meetings").status_code == 403)
check("管理员不能把会议建到不存在的社区", admin.post("/api/meetings", json={
    "title": "孤儿会议", "kind": "居务会", "held_at": past,
    "org": "不存在社区",
}).status_code == 400)

print("=" * 66)
print("B. 建会、议题与通知")
print("=" * 66)
r = community.post("/api/meetings", json={
    "title": "老旧小区电梯更新议事会", "kind": "居民代表会议",
    "held_at": past, "location": "幸福里社区会议室",
})
j = r.get_json()
check("社区新建居民代表会议", r.status_code == 200 and j["code"] == 0, j)
mid = j["data"]["id"]
check("新会状态为 draft", j["data"]["status"] == "draft")
check("空议题不能发通知",
      community.post(f"/api/meetings/{mid}/notify").get_json()["code"] != 0)

r = community.post(f"/api/meetings/{mid}/items", json={
    "title": "电梯更新方案", "detail": "讨论资金分摊、施工时段和老年人出行保障。"})
item1 = r.get_json()["data"]["id"]
check("新增公开议题", r.get_json()["code"] == 0)
r = community.post(f"/api/meetings/{mid}/items", json={
    "title": "困难家庭个案", "detail": "内部讨论某户临时救助，不进入公开稿。"})
item2 = r.get_json()["data"]["id"]
check("新增内部议题", r.get_json()["code"] == 0)
check("发出会议通知", community.post(f"/api/meetings/{mid}/notify").get_json()["code"] == 0)
check("重复通知幂等", community.post(f"/api/meetings/{mid}/notify").get_json()["code"] == 0)

print("=" * 66)
print("C. 决议、法定人数与任务联动")
print("=" * 66)
r = community.post(f"/api/meetings/{mid}/items/{item1}/decision", json={
    "decision": "由幸福里物业三日内提交电梯更新施工计划并公示停梯安排。",
    "assignee_role": "property", "assignee_org": "不存在物业",
    "deadline": future, "is_public": True,
})
check("不存在的承办单位被拒绝", r.get_json()["code"] != 0)
r = community.post(f"/api/meetings/{mid}/items/{item1}/decision", json={
    "decision": "由幸福里物业三日内提交电梯更新施工计划并公示停梯安排。",
    "assignee_role": "property", "assignee_org": "幸福里物业",
    "deadline": future, "is_public": True,
})
check("公开决议保存成功", r.get_json()["code"] == 0, r.get_json())
r = community.post(f"/api/meetings/{mid}/items/{item2}/decision", json={
    "decision": "由民政条线单独联系该户核验材料，个案姓名和联系方式仅内部流转。",
    "is_public": False,
})
check("内部决议保存成功", r.get_json()["code"] == 0)

final_body = {
    "expected_count": 10, "present_count": 5,
    "minutes_md": "会议逐项听取居民代表意见，内部记录含个案讨论和现场发言，不得对外公开。",
    "public_summary": "会议审议了电梯更新方案，明确施工计划、公示和居民出行保障要求。",
}
r = community.post(f"/api/meetings/{mid}/finalize", json=final_body)
check("居民代表会议刚好一半不能定稿", r.status_code == 409 and r.get_json()["code"] != 0)
final_body["present_count"] = 6
r = community.post(f"/api/meetings/{mid}/finalize", json=final_body)
d = r.get_json()["data"]
check("超过半数后可定稿", r.status_code == 200 and d["meeting"]["status"] == "finalized", d)
check("公开决议自动转成一条督办任务", len(d["created_task_ids"]) == 1, d)
task_id = d["created_task_ids"][0]
with app.app_context():
    task = db.session.get(Task, task_id)
    check("任务承办单位与会议决议一致", task is not None and task.assignee_org == "幸福里物业")
    before_tasks = Task.query.count()
r = community.post(f"/api/meetings/{mid}/finalize", json=final_body)
with app.app_context():
    check("重复定稿不重复生成任务", Task.query.count() == before_tasks)
check("定稿后议题冻结",
      community.post(f"/api/meetings/{mid}/items", json={"title": "越界新增"}).status_code == 409)

print("=" * 66)
print("D. 居务公开与保密隔离")
print("=" * 66)
r = community.post(f"/api/meetings/{mid}/publish")
pd = r.get_json()["data"]
check("定稿后可发布居务公开", r.status_code == 200 and pd["notice_id"] > 0, r.get_json())
nid = pd["notice_id"]
with app.app_context():
    n = db.session.get(Notice, nid)
    body = n.body_md or ""
    check("公开稿包含公开决议", "提交电梯更新施工计划" in body)
    check("公开稿不含内部会议记录", "现场发言" not in body)
    check("公开稿不含内部个案决议", "单独联系该户" not in body)
    check("公告标记为公开且已发布", n.status == "published" and n.secret_level == "公开")
    before_notices = Notice.query.count()
r = community.post(f"/api/meetings/{mid}/publish")
with app.app_context():
    check("重复发布不重复生成公告", Notice.query.count() == before_notices)
check("居民端能看到居务公开", any(x["id"] == nid for x in
      (app.test_client().get("/api/portal/notices").get_json()["data"]["items"])))

print("=" * 66)
print("E. 行级隔离与审计")
print("=" * 66)
with app.app_context():
    db.session.add(User(username="other_comm", name="另一社区社工", role="community",
                        org="和谐里社区", status="active",
                        password_hash=generate_password_hash("123456")))
    other_meeting = Meeting(org="和谐里社区", kind="居务会", title="另一社区会议",
                            held_at=datetime.now(), creator="另一社区社工")
    db.session.add(other_meeting)
    db.session.commit()
    other_id = other_meeting.id
rows = community.get("/api/meetings").get_json()["data"]["items"]
check("社区列表只看本社区", all(x["org"] == "幸福里社区" for x in rows), rows)
check("社区按 id 读取别社区会议返回 404",
      community.get(f"/api/meetings/{other_id}").status_code == 404)
street_rows = street.get("/api/meetings").get_json()["data"]["items"]
check("街道监督视角可看各社区", {x["org"] for x in street_rows} >= {"幸福里社区", "和谐里社区"})
with app.app_context():
    actions = {x.action for x in AuditLog.query.filter_by(agent="meeting").all()}
    check("会议关键动作均有审计", {"create", "agenda_add", "notify", "decision", "finalize", "publish"} <= actions,
          actions)
    m = db.session.get(Meeting, mid)
    check("内部纪要只存会议表", "现场发言" in (m.minutes_md or "") and
          "现场发言" not in (db.session.get(Notice, nid).body_md or ""))

# 即使绕过正常定稿流程带入了脏公开摘要，发布口也必须再拦一次。
with app.app_context():
    risky = Meeting(org="幸福里社区", kind="居务会", title="隐私公开拦截测试",
                    held_at=datetime.now(), status="finalized", expected_count=3,
                    present_count=3, minutes_md="内部记录已经完成，不能直接对外发布。",
                    public_summary="居民李某住2栋2单元202室，会议决定对其开展个案帮扶。",
                    creator="王社工", finalized_at=datetime.now())
    db.session.add(risky)
    db.session.commit()
    risky_id = risky.id
r = community.post(f"/api/meetings/{risky_id}/publish")
check("居务公开拦截姓名与精确门牌", r.status_code == 400 and r.get_json()["code"] != 0,
      r.get_json())

print("=" * 66)
print(f"结果：通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 66)
sys.exit(1 if FAIL else 0)
