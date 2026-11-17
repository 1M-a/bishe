"""数据清理与备份回归。

只使用独立临时 SQLite 和附件目录，验证管理员在清空业务数据前能得到
可下载的完整快照，清空后只保留管理员与系统配置，并且非管理员不能调用
这组高风险接口。

运行：.\\.venv\\Scripts\\python.exe scripts\\smoke_data_reset.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

ROOT = Path(tempfile.mkdtemp(prefix="smoke-data-reset-", dir=str(BASE / "data")))
DB = ROOT / "community.db"
EVIDENCE = ROOT / "evidence"
NOTICES = ROOT / "notices"

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["EVIDENCE_DIR"] = str(EVIDENCE)
os.environ["NOTICE_DIR"] = str(NOTICES)
os.environ["NOTICE_AUTO_IMPORT"] = "0"
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"

from app import create_app  # noqa: E402
from app import auth as auth_mod  # noqa: E402
from app.auth import seed_tasks, seed_users  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (AuditLog, FdeRecord, KbDoc, OrgContact, ResidentTicket,
                        Task, User)  # noqa: E402


app = create_app("dev")
# NOTICE_DIR 在生产配置中固定为项目 data/notices，不读取环境变量；测试必须
# 显式覆盖两个文件目录，避免高风险清理回归触碰真实工作区数据。
app.config["EVIDENCE_DIR"] = str(EVIDENCE)
app.config["NOTICE_DIR"] = str(NOTICES)
PASS = FAIL = 0
FAILED: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  [FAIL] {label}" + (f"  [{detail}]" if detail else ""))


def login(client, username: str, password: str = "123456"):
    auth_mod._LOGIN_FAIL.clear()
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)


def count(model) -> int:
    with app.app_context():
        return model.query.count()


def main() -> int:
    try:
        # 用 ORM 写入可识别的业务、系统配置和附件样本。
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        NOTICES.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / "sample.jpg").write_bytes(b"fake-evidence")
        (NOTICES / "pending.md").write_text("待导入公告", encoding="utf-8")
        with app.app_context():
            admin = User.query.filter_by(username="admin").first()
            worker = User.query.filter_by(username="wang").first()
            # 模拟误授 user.manage：破坏性清理仍必须拒绝非 admin 角色。
            worker.perms_json = json.dumps(["user.manage"])
            db.session.add(FdeRecord(raw_text="清理测试业务记录", creator="smoke"))
            db.session.add(ResidentTicket(
                code="999999", phone="13800000000", raw_text="清理测试诉求",
                normalized_text="清理测试诉求", content_hash="smoke-hash"))
            db.session.add(KbDoc(title="清理测试知识库", content="保留配置"))
            db.session.add(OrgContact(level="community", name="清理测试联系人",
                                       address="测试社区"))
            db.session.add(AuditLog(actor="smoke", agent="test", action="seed"))
            db.session.commit()
            admin_id = admin.id if admin else None
            worker_id = worker.id if worker else None

        check("独立库含管理员", admin_id is not None)
        check("独立库含非管理员", worker_id is not None)
        check("清理前有业务记录", count(FdeRecord) == 1 and count(ResidentTicket) == 1)

        admin_client = app.test_client()
        worker_client = app.test_client()
        check("管理员登录成功", login(admin_client, "admin", "admin123").status_code == 302)
        check("社区账号登录成功", login(worker_client, "wang").status_code == 302)

        page = admin_client.get("/system")
        html = page.get_data(as_text=True)
        check("管理员能打开系统页", page.status_code == 200)
        check("系统页显示清理入口", "清理演示数据" in html and "btnClearData" in html)

        r = admin_client.get("/api/admin/data-backups")
        body = r.get_json() or {}
        check("管理员可查看备份列表", r.status_code == 200 and body.get("code") == 0)

        r = worker_client.get("/api/admin/data-backups")
        check("非管理员查看备份返回 403", r.status_code == 403)
        r = worker_client.get("/system")
        check("非管理员打开系统页返回 403", r.status_code == 403)
        r = worker_client.post("/api/admin/data-clear", json={"confirm": "清空业务数据"})
        check("非管理员清空返回 403", r.status_code == 403)
        r = admin_client.post("/api/admin/data-clear", json={"confirm": "错误确认词"})
        check("错误确认词返回 400", r.status_code == 400)

        r = admin_client.post("/api/admin/data-clear", json={"confirm": "清空业务数据"})
        body = r.get_json() or {}
        check("管理员清空成功", r.status_code == 200 and body.get("code") == 0, body)
        data = body.get("data") or {}
        backup = data.get("backup") or {}
        backup_name = backup.get("name", "")
        check("清空返回备份名称", backup_name.startswith("data-backup-") and backup_name.endswith(".zip"), backup_name)

        check("FDE 已清空", count(FdeRecord) == 0)
        check("居民诉求已清空", count(ResidentTicket) == 0)
        with app.app_context():
            admin_after = User.query.filter_by(username="admin").first()
            worker_after = User.query.filter_by(username="wang").first()
            kb_after = KbDoc.query.filter_by(title="清理测试知识库").count()
            contact_after = OrgContact.query.filter_by(name="清理测试联系人").count()
            clear_audit = AuditLog.query.filter_by(action="data_clear").count()
        check("管理员账号保留", admin_after is not None and admin_after.id == admin_id)
        check("非管理员账号删除", worker_after is None)
        check("知识库配置保留", kb_after == 1)
        check("公开联系人配置保留", contact_after == 1)
        check("清空动作写入新审计", clear_audit == 1)
        check("附件已清理", not any(EVIDENCE.rglob("*")))
        check("待导入公告已清理", not any(NOTICES.rglob("*")))

        # 模拟应用重启时 init_auth() 的种子阶段：已经清空过的干净库不能
        # 把演示账号和演示任务重新写回来。
        with app.app_context():
            seed_users()
            seed_tasks()
            users_after_restart = User.query.count()
            tasks_after_restart = Task.query.count()
        check("清空后重启不重建演示账号", users_after_restart == 1, users_after_restart)
        check("清空后重启不重建演示任务", tasks_after_restart == 0, tasks_after_restart)

        r = admin_client.get("/api/admin/data-backups")
        items = ((r.get_json() or {}).get("data") or {}).get("items") or []
        found = next((x for x in items if x.get("name") == backup_name), None)
        check("清空后备份出现在列表", found is not None)
        check("备份元数据含附件数量", found is not None and found.get("evidence_count") == 1)

        r = admin_client.get(f"/api/admin/data-backups/{backup_name}/download")
        payload = r.data
        check("管理员可下载备份", r.status_code == 200 and len(payload) > 0,
              f"status={r.status_code}, bytes={len(payload)}, body={payload[:120]!r}")
        with zipfile.ZipFile(__import__("io").BytesIO(payload), "r") as archive:
            names = set(archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            check("备份含数据库快照", "community.db" in names)
            check("备份含清单", "manifest.json" in names)
            check("备份含附件", "evidence/sample.jpg" in names)
            check("清单记录清理前业务数据", manifest.get("tables", {}).get("fde_record") == 1)

        r = worker_client.get(f"/api/admin/data-backups/{backup_name}/download")
        check("非管理员下载备份被拒", r.status_code in (401, 403), r.status_code)
        r = admin_client.delete(f"/api/admin/data-backups/{backup_name}")
        check("管理员可删除备份", r.status_code == 200 and (r.get_json() or {}).get("code") == 0)
        r = admin_client.get(f"/api/admin/data-backups/{backup_name}/download")
        check("删除后备份不可下载", r.status_code == 404)

        print(f"\n结果：{PASS} 通过，{FAIL} 失败")
        if FAILED:
            print("失败项目：" + ", ".join(FAILED))
        return 1 if FAIL else 0
    finally:
        try:
            db.session.remove()
            db.engine.dispose()
        except Exception:
            pass
        shutil.rmtree(ROOT, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
