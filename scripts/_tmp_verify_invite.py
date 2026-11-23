# -*- coding: utf-8 -*-
"""临时验证：invite_code 表建表 + 列结构"""
import sqlite3
import sys
sys.path.insert(0, ".")

from app import create_app                       # noqa: E402
from app.extensions import db                    # noqa: E402
from app.models import InviteCode                # noqa: E402

a = create_app()
with a.app_context():
    db.create_all()
    cols = [c.name for c in InviteCode.__table__.columns]
    print("InviteCode columns:", cols)
    uri = a.config["SQLALCHEMY_DATABASE_URI"]
    path = uri.split("sqlite:///", 1)[1]
    conn = sqlite3.connect(path)
    t = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    print("table created:", "invite_code" in t)
    conn.close()