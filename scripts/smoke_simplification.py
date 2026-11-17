"""角色导航精简与社区岗位权限回归。"""
import json
import os
import pathlib
import re
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
DB = BASE / "data" / "smoke_simplification.db"
for suffix in ("", "-wal", "-shm"):
    path = pathlib.Path(str(DB) + suffix)
    if path.exists():
        path.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"

from werkzeug.security import generate_password_hash  # noqa: E402
from app import create_app  # noqa: E402
from app.auth import (COMMUNITY_BASE_PERMS, COMMUNITY_KB_PERMS,
                      COMMUNITY_LEAD_PERMS, user_perms)  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import User  # noqa: E402

app = create_app("dev")
passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  [PASS]", label)
    else:
        failed += 1
        print("  [FAIL]", label, detail)


def login(username, password="123456"):
    client = app.test_client()
    client.post("/login", data={"username": username, "password": password})
    return client


def nav_of(client, path):
    html = client.get(path).get_data(as_text=True)
    match = re.search(r'<nav class="nav">(.*?)</nav>', html, re.S)
    return match.group(1) if match else ""


print("[1] 按身份精简导航")
community = login("wang")
nav = nav_of(community, "/")
for label in ("事项登记", "待办中心", "工单办理", "居民与台账",
              "收发文", "政策助手", "材料报送", "督办中心", "智能复核"):
    check("社区显示 " + label, label in nav)
for label in ("FDE 首录台", "协作轨迹", "模型纠错", "流程监控",
              "风险预警", "通知公告", "发文台", "工作周报", "报表中心"):
    check("社区不再单列 " + label, label not in nav)

property_client = login("wuye")
nav = nav_of(property_client, "/orders")
check("物业仅保留三个业务入口", nav.count("<a ") == 3, nav.count("<a "))
for label in ("我的工单", "街道任务", "通知与来函"):
    check("物业显示 " + label, label in nav)
for label in ("流程监控", "风险预警", "督办中心"):
    check("物业不显示 " + label, label not in nav)

street = login("jiedao")
nav = nav_of(street, "/tasks")
for label in ("任务督办", "督办中心", "居务监督", "收发文",
              "材料报送", "审计日志"):
    check("街道显示 " + label, label in nav)
check("街道不显示访问策略", "访问策略" not in nav)

admin = login("admin", "admin123")
nav = nav_of(admin, "/admin/users")
for label in ("FDE 首录台", "智能复核", "督办中心", "收发文",
              "材料报送", "访问策略"):
    check("管理员显示合并入口 " + label, label in nav)
for label in ("协作轨迹", "模型纠错", "物业工单", "流程监控",
              "风险预警", "通知公告", "发文台", "工作周报", "报表中心"):
    check("管理员不再重复单列 " + label, label not in nav)

print("[2] 合并页面仍可访问")
for path, labels in (
        ("/process", ("流程督办", "风险清单", "工单明细")),
        ("/risk", ("流程督办", "风险清单", "工单明细")),
        ("/notices", ("上级来文", "居民公告", "正式发文")),
        ("/docs", ("上级来文", "居民公告", "正式发文")),
        ("/weekly", ("周报", "月报")),
        ("/reports", ("周报", "月报")),
        ("/feedback", ("模型纠错", "处理轨迹")),
        ("/trace", ("模型纠错", "处理轨迹"))):
    response = community.get(path)
    html = response.get_data(as_text=True)
    check(path + " 可访问", response.status_code == 200, response.status_code)
    check(path + " 内部切换完整", all(x in html for x in labels))

for path in ("/", "/tasks", "/orders", "/process", "/risk", "/trace",
             "/feedback", "/meetings", "/persons", "/ledgers", "/kb",
             "/notices", "/docs", "/weekly", "/reports", "/audit",
             "/security/policy", "/admin/users", "/system"):
    response = admin.get(path)
    check("管理员入口仍可打开 " + path, response.status_code == 200, response.status_code)

print("[3] 社区岗位权限模板")
with app.app_context():
    worker = User(
        username="worker_test", name="普通经办", role="community",
        org="幸福里社区", status="active", perms_json=None,
        password_hash=generate_password_hash("12345678"),
    )
    db.session.add(worker)
    db.session.commit()
    check("普通社区账号默认最小权限", user_perms(worker) == COMMUNITY_BASE_PERMS)
    check("普通经办无发布签发权限",
          not (user_perms(worker) & {
              "notice.manage", "doc.manage", "weekly.manage",
              "meeting.manage", "report.generate", "feedback.manage",
          }))
    check("负责人模板包含发布签发",
          {"notice.manage", "doc.manage", "weekly.manage", "meeting.manage",
           "report.generate", "order.verdict"} <= COMMUNITY_LEAD_PERMS)
    check("知识管理员模板包含知识与纠错",
          {"kb.manage", "feedback.view", "feedback.manage"} <= COMMUNITY_KB_PERMS)

    wang = User.query.filter_by(username="wang").first()
    wang_perms = set(json.loads(wang.perms_json or "[]"))
    check("演示负责人保留完整居委会闭环",
          COMMUNITY_LEAD_PERMS <= wang_perms and COMMUNITY_KB_PERMS <= wang_perms)

print("=" * 60)
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
