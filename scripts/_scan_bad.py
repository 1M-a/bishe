import sqlite3, sys

for db in ("e:/program/decrease a/bishe/data/dev.db", "e:/program/decrease a/bishe/data/trial.db"):
    print("=== %s ===" % db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    for (t,) in c.execute("select name from sqlite_master where type='table'"):
        cols = [r[1] for r in c.execute("pragma table_info(%s)" % t)]
        for col in cols:
            try:
                rows = c.execute("select %s from %s where %s is not null" % (col, t, col)).fetchall()
            except Exception:
                continue
            for (v,) in rows:
                if isinstance(v, str) and ("\ue11f" in v or "\ufffd" in v):
                    print("  BAD", t, col, repr(v[:40]))
    print("  scan done (%d tables)" % len(list(c.execute("select name from sqlite_master where type='table'"))))