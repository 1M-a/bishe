# -*- coding: utf-8 -*-
"""邀请码生成入口（管理员发码用，纯打印，不做管理页面）

试用区管理员在部署机上运行，把打印结果截图发群即可：
    python scripts/gen_invites.py --site C --role resident --org "住武社区居民" --count 10 --prefix C-RES
    python scripts/gen_invites.py --site C --role community --org "住武社区" --count 5  --prefix C-COM
    python scripts/gen_invites.py --site C --role property  --org "住武物业" --count 5  --prefix C-PRO
    python scripts/gen_invites.py --site C --role street    --org "以朵街道" --count 2  --prefix C-STR

参数白名单：
  --role   ∈ {community, property, street, resident}（排除 admin）
  --site   ∈ {A, B, C}
  --count  ≤ 100
  --prefix 随机码前缀，建议 "C-RES"/"C-COM"/"C-PRO"/"C-STR"，最长 8 位
  --list   只打印当前站点已生成、未使用的码（不发新码）

说明：--site 仅作为一个"给哪一区配的码"的标记入库，不查当前 Config.SITE_CODE，
以管理员意图为准（部署机上跑脚本不必先设 SITE_CODE）。
"""
import argparse
import secrets
import sys
from datetime import datetime

BASE = __file__ and __import__("os").path.dirname(
    __import__("os").path.dirname(__file__))
sys.path.insert(0, BASE)

from app import create_app                                    # noqa: E402
from app.extensions import db                                 # noqa: E402
from app.models import InviteCode                             # noqa: E402

ALLOWED_ROLES = {"community", "property", "street", "resident"}
ALLOWED_SITES = {"A", "B", "C"}
ROLE_NAMES = {"community": "社区工作者", "property": "物业",
              "street": "街道", "resident": "居民"}
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"   # 去掉 0O/1I 等易混字符


def _new_code(prefix: str) -> str:
    return f"{prefix}-{''.join(secrets.choice(_ALPHABET) for _ in range(4))}"


def _insert_unique(prefix: str) -> str:
    """生成一个未占用的码（唯一索引兜底，冲突重试）"""
    while True:
        code = _new_code(prefix)
        try:
            db.session.add(InviteCode(
                code=code, role=args.role, org=args.org,
                site=args.site, used_by=""))
            db.session.commit()
            return code
        except Exception:                       # noqa: BLE001
            db.session.rollback()               # 唯一索引冲突，重试


parser = argparse.ArgumentParser(description="生成邀请码")
parser.add_argument("--site", default="A", choices=ALLOWED_SITES)
parser.add_argument("--role", default="resident", choices=ALLOWED_ROLES)
parser.add_argument("--org", default="")
parser.add_argument("--count", type=int, default=1)
parser.add_argument("--prefix", default="INV")
parser.add_argument("--list", action="store_true", help="只列出当前库未使用码")
args = parser.parse_args()

if not args.list and not args.org:
    sys.exit("生成模式必须提供 --org（码绑定单位全名或简称）")
if args.count < 1 or args.count > 100:
    sys.exit("--count 需在 1~100 之间")
if len(args.prefix) > 8:
    sys.exit("--prefix 最长 8 位")

app = create_app()
with app.app_context():
    db.create_all()   # 保证 invite_code 表存在

    if args.list:
        rows = InviteCode.query.filter_by(used_by="").order_by(
            InviteCode.code).all()
        if not rows:
            print("（当前库没有未使用的邀请码）")
        for r in rows:
            print(f"{r.code:<20} {ROLE_NAMES.get(r.role, r.role):<6} {r.org}  "
                  f"站点{r.site}  未使用")
        sys.exit(0)

    print(f"—— {ROLE_NAMES.get(args.role, args.role)} · {args.org} "
          f"· 站点{args.site} · {args.count} 张 ——")
    for _ in range(args.count):
        code = _insert_unique(args.prefix)
        print(f"{code:<20} {ROLE_NAMES.get(args.role, args.role):<6} "
              f"{args.org}  未使用")
    print(f"（生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")