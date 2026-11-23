# -*- coding: utf-8 -*-
"""临时验证：第 6 步 register() 邀请码注册全链路（跨区拒绝/同区成功/一码一人/phone 必填）"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

_tmp = tempfile.mkdtemp(prefix="regverify_")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_tmp, "t.db")
os.environ["SITE_CODE"] = "A"
os.environ["EVIDENCE_DIR"] = os.path.join(_tmp, "evidence")
os.environ["NOTICE_DIR"] = os.path.join(_tmp, "notices")

from app import create_app                       # noqa: E402
from app.extensions import db                    # noqa: E402
from app.models import InviteCode, User          # noqa: E402

app = create_app()
app.config["TESTING"] = True

SITE_A_STAFF = "A-STAFF-TEST1"
SITE_A_RES = "A-RES-TEST1"
SITE_A_RES2 = "A-RES-TEST2"   # 案例4 专用的未用居民码
SITE_C_RES = "C-RES-F75J"   # 报告中生成的 C 区码，登录 A 站应跨区拒绝

# 注册限流是 5 次/小时；本校验一组 6 个请求，直接关掉避免误触发
import app.ratelimit as _rt      # noqa: E402


def _no_limit(*a, **k):
    return True


_rt.allow = _no_limit


def seed():
    with app.app_context():
        db.create_all()
        db.session.add(InviteCode(code=SITE_A_STAFF, role="community",
                                  org="住武社区", site="A", used_by=""))
        db.session.add(InviteCode(code=SITE_A_RES, role="resident",
                                  org="住武社区居民", site="A", used_by=""))
        db.session.add(InviteCode(code=SITE_A_RES2, role="resident",
                                  org="住武社区居民", site="A", used_by=""))
        db.session.add(InviteCode(code=SITE_C_RES, role="resident",
                                  org="以朵社区居民", site="C", used_by=""))
        db.session.commit()


def post_reg(username, code, phone="", name="张三", pwd="Passw0rd!"):
    """每次新建独立 client 提交注册（避免上一个登录 session 残留）"""
    data = {"username": username, "name": name, "phone": phone,
            "invite_code": code, "password": pwd, "password2": pwd}
    with app.test_client() as c:
        return c.post("/register", data=data, follow_redirects=True)


def main():
    seed()

    # 1) 跨区码（C 站码在 A 站）→ 拒绝
    r = post_reg("wulanhaote", SITE_C_RES, phone="13800001234")
    body = r.get_data(as_text=True)
    print("1 跨区拒绝:", "邀请码属于 C 区" in body, r.status_code)

    # 2) 居民码（同区）→ 成功且 active，落 portalviews.home
    r = post_reg("wulanhaote", SITE_A_RES, phone="13800001234")
    print("2 居民注册:", r.status_code, len(r.history))
    with app.app_context():
        u = User.query.filter_by(username="wulanhaote").first()
        ic = InviteCode.query.filter_by(code=SITE_A_RES).first()
        print("2  已激活:", u is not None and u.status == "active",
              "| role:", u.role if u else None,
              "| 一码一人:", ic.used_by == "wulanhaote")

    # 3) 同码二次注册 → 拒绝（新 client，独立 session）
    r = post_reg("wulanhaote2", SITE_A_RES, phone="13800001234")
    body = r.get_data(as_text=True)
    print("3 一码一人:", "邀请码无效或已被使用" in body)

    # 4) 居民不填 phone → 拒绝（用未用过的居民码，排除"码已用"干扰）
    r = post_reg("wulanhaote3", SITE_A_RES2, phone="")
    body = r.get_data(as_text=True)
    print("4 居民phone必填:", "请填写手机号" in body)

    # 5) 工作人员码 → 成功且 pending，落 admin.pending
    r = post_reg("shegong01", SITE_A_STAFF, phone="13900001111")
    print("5 工作人员注册:", r.status_code, len(r.history))
    with app.app_context():
        u = User.query.filter_by(username="shegong01").first()
        print("5  待审核:", u is not None and u.status == "pending",
              "| org 由码带出:", u.org if u else None)

    # 6) 不存在的邀请码 → 拒绝
    r = post_reg("shegong02", "A-DOES-NOT-EXIST", phone="13900002222")
    body = r.get_data(as_text=True)
    print("6 无效码:", "邀请码无效或已被使用" in body)
    # 调试：找 err 相关片段
    import re
    m = re.search(r'<div class="err">([^<]*)</div>', body)
    print("6  err片段:", m.group(1) if m else "(未找到 err div)")


if __name__ == "__main__":
    main()