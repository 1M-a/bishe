# -*- coding: utf-8 -*-
import sqlite3
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "dev.db")

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 1) 找出所有含乱码的文本值（含 \ue11f 即可判定为损坏）
bad_examples = set()
for (t,) in conn.execute("select name from sqlite_master where type='table'"):
    cols = [r[1] for r in conn.execute("pragma table_info(%s)" % t)]
    for col in cols:
        try:
            rows = conn.execute(
                "select %s from %s where %s is not null" % (col, t, col)).fetchall()
        except Exception:
            continue
        for (v,) in rows:
            if isinstance(v, str) and ("\ue11f" in v):
                start = v.find("\ue11f")
                bad_examples.add(v[max(0, start - 12): start + 16])

print("=== 乱码样本片段 ===")
for s in bad_examples:
    print("  " + repr(s))

# 2) 验证还原：把“住武物业”按 GBK 误读 UTF-8 的字节，看能否复原出样本中的乱码
try:
    mangled = "住武物业".encode("utf-8").decode("gbk", errors="replace")
    print("\n=== 模拟还原 ===")
    print("  mangled =", repr(mangled))
except Exception as e:
    print("  encode/decode 异常:", e)

conn.close()