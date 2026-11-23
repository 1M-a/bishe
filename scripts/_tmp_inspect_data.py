# -*- coding: utf-8 -*-
"""临时排查：两个库的工单 / 居民诉求 / 物业账号 现状"""
import os, sys
sys.path.insert(0, r"e:\program\decrease a\bishe")
import sqlite3

for name, db in [("试用版trial.db", r"e:\program\decrease a\bishe\data\trial.db"),
                 ("调整版dev.db", r"e:\program\decrease a\bishe\data\dev.db")]:
    print("=" * 20, name, "=" * 20)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        rows = cur.execute("select id, order_no, property_co, status, sla_level, "
                           "sla_hours, work_item_id from work_order order by id").fetchall()
        print(f"[work_order] 共 {len(rows)} 条")
        for r in rows:
            print("   ", dict(r))
    except Exception as e:
        print("  work_order 表读取失败:", e)
    try:
        rows = cur.execute("select id, phone, name, address, status, fde_id, "
                           "item_count from resident_ticket order by id").fetchall()
        print(f"[resident_ticket] 共 {len(rows)} 条")
        for r in rows:
            print("   ", dict(r))
    except Exception as e:
        print("  resident_ticket 表读取失败:", e)
    try:
        rows = cur.execute("select id, username, name, role, org, status, phone "
                           "from user order by id").fetchall()
        print(f"[user] 共 {len(rows)} 条")
        for r in rows:
            print("   ", dict(r))
    except Exception as e:
        print("  user 表读取失败:", e)
    conn.close()