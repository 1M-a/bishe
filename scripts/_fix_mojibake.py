# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
"""一次性修复：dev.db 里被 bat 编码问题损坏的乱码文本。

成因：启动_调整版.bat 是 UTF-8 无 BOM，cmd 按 GBK(936) 解析文件，
"住武物业" 的 UTF-8 字节被按 GBK 双字节读取成『浣忔\\ue11f鐗╀笟』。
PUA 字符 \\ue11f 无法被 GBK 编码，不能走 encode/decode 还原，
只能做确定性字符串替换（这两个版本就只有这一个中文单位名）。

用法：python scripts/_fix_mojibake.py --apply   # 真正写库
      python scripts/_fix_mojibake.py           # 只扫描打印，不写
"""
import argparse
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MOJIBAKE = "\u6d63\u5fd4\ue11f\u9417\u2540\u7b1f"   # 浣忔\ue11f鐗笀（实测码点）
FIXED = "\u4f4f\u6b66\u7269\u4e1a"             # 住武物业


def scan(conn):
    hits = []
    for (t,) in conn.execute("select name from sqlite_master where type='table'"):
        cols = [r[1] for r in conn.execute("pragma table_info(%s)" % t)]
        for col in cols:
            try:
                rows = conn.execute(
                    "select rowid, %s from %s where %s is not null" % (col, t, col)).fetchall()
            except Exception:
                continue
            for rowid, v in rows:
                if isinstance(v, str) and MOJIBAKE in v:
                    hits.append((t, col, rowid))
    return hits


def fix_db(path, do_apply):
    conn = sqlite3.connect(path)
    hits = scan(conn)
    print("== %s 命中 %d 条 ==" % (os.path.basename(path), len(hits)))

    # audit_log 有 append-only 触发器；修复乱码需临时摘除，写完按原样重建
    trg_sql = {}
    if do_apply and any(t == "audit_log" for t, _, _ in hits):
        for r in conn.execute(
                "select name, sql from sqlite_master where type='trigger' "
                "and tbl_name='audit_log'").fetchall():
            trg_sql[r[0]] = r[1]
            conn.execute("drop trigger %s" % r[0])

    for t, col, rowid in hits:
        old = conn.execute(
            "select %s from %s where rowid=?" % (col, t), (rowid,)).fetchone()[0]
        new = old.replace(MOJIBAKE, FIXED)
        print("    %s.%s #%s\n      - %r\n      + %r" % (t, col, rowid, old, new))
        if do_apply:
            conn.execute("update %s set %s=? where rowid=?" % (t, col), (new, rowid))

    for name, sql in trg_sql.items():
        conn.execute(sql)
    if do_apply:
        conn.commit()
    conn.close()
    return len(hits)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    total = 0
    for name in ("dev.db", "trial.db", "community.db"):
        p = os.path.join(BASE, "data", name)
        if os.path.exists(p):
            total += fix_db(p, args.apply)
    print("总命中 %d 条\n%s" % (total, "已写库" if args.apply
                                else "加 --apply 真正写库"))