"""冒烟：网页端台账上传导入（preview → apply）。

只跑接口链路，不起真实卷宗：用合成 ID 疫苗一个 17 列残疾人台账
（与 EXPORT_SPECS 的列方向一致），分别打 /api/persons/import/preview
和 /apply，验证：识别类型、清洗统计、写库、审计留痕、幂等、权限。

用法：python scripts/smoke_web_import.py
"""
import io
import os
import pathlib
import shutil
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "scripts"))

# --- 建隔离库（必须在 import app 之前：config.py 导入即锁 DATABASE_URL）---
SRC = BASE / "data" / "community.db"
DB = BASE / "data" / ("smoke_web_import_%d.db" % os.getpid())
if pathlib.Path(str(DB)).exists():
    raise SystemExit("测试库已存在，拒绝覆盖半途状态：%s" % DB)
for suf in ("", "-wal", "-shm"):
    s = pathlib.Path(str(SRC) + suf)
    if s.exists():
        shutil.copy2(s, str(DB) + suf)
os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                                  # noqa: E402
from app.models import AuditLog, Person                     # noqa: E402

app = create_app("dev")
PASS, FAIL = 0, 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, extra))


def login(username, pw="123456"):
    c = app.test_client()
    c.post("/login", data={"username": username, "password": pw})
    return c


def json_of(r):
    try:
        return r.get_json() or {}
    except Exception:                                       # noqa: BLE001
        return {}


def build_disabled_ledger():
    """17 列残疾人台账（表头第 2 行，数据第 3 行起）。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([])   # 第 1 行：大标题（合并）留空即可，解析器从第 3 行读数据
    ws.append(["序号", "姓名", "身份证号", "残疾证号", "年龄", "性别",
               "街道\n区划", "社区\n区划", "组", "现居住地地址", "残疾类别",
               "残疾\n等级", "残疾\n详情", "发证\n时间", "持证\n状态",
               "重度残疾人\n护理补贴", "享受低保情况"])
    # 3 个真数据 + 1 个重复身份证 + 1 个无身份证
    ws.append([1, "张三", "520201196801011234", "520201196801011234", 56,
               "男", "以朵街道", "住武社区", "昌盛组",
               "住武社区1栋1单元101室", "肢体残疾", "一级", "肢体残疾|一级;",
               "", "持证", "", ""])
    ws.append([2, "李四", "52020119720902234X", "52020119720902234X", 52,
               "女", "以朵街道", "以朵社区", "幸福组",
               "以朵安置房3-4-5", "视力残疾", "二级", "视力残疾|二级;",
               "", "持证", "", ""])
    ws.append([3, "王五", "520201198503033456", "520201198503033456", 39,
               "男", "以朵街道", "住武社区", "昌盛组",
               "住武社区2栋2单元202室", "听力残疾", "三级", "听力残疾|三级;",
               "", "持证", "", ""])
    ws.append([4, "张三", "520201196801011234", "520201196801011234", 56,  # 与 1 重复
               "男", "以朵街道", "住武社区", "昌盛组",
               "住武社区1栋1单元101室", "肢体残疾", "一级", "肢体残疾|一级;",
               "", "持证", "", ""])
    ws.append([5, "赵六", "1968", "1968", "", "男", "以朵街道", "住武社区",
               "昌盛组", "住武社区3栋3单元303室", "", "", "", "", "", "", ""])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def parts(buf, kind="auto", purge="0"):
    buf.seek(0)
    return {"file": (buf, "disabled.xlsx"), "kind": kind, "purge": purge}


def t_route():
    print("\n[1] 路由与权限")
    com = login("wang")                     # 社区无 person.import
    r = com.post("/api/persons/import/preview", data=parts(build_disabled_ledger()),
                 content_type="multipart/form-data")
    check("社区 preview 403", r.status_code == 403, r.status_code)
    r = com.post("/api/persons/import/apply", data=parts(build_disabled_ledger()),
                 content_type="multipart/form-data")
    check("社区 apply 403", r.status_code == 403, r.status_code)

    st = login("jiedao")                    # 街道有 person.import
    r = st.post("/api/persons/import/preview",
                data={"file": (io.BytesIO(b""), "x.txt"), "kind": "auto"},
                content_type="multipart/form-data")
    check("非 xlsx 后缀被拒 400", r.status_code == 400, r.status_code)
    r = st.post("/api/persons/import/preview", data={},
                content_type="multipart/form-data")
    check("缺文件被拒 400", r.status_code == 400, r.status_code)
    return st


def t_preview_apply(st):
    print("\n[2] 预览 → 导入 → 幂等 → 审计")

    r = st.post("/api/persons/import/preview", data=parts(build_disabled_ledger()),
                content_type="multipart/form-data")
    d = json_of(r).get("data") or {}
    check("preview 200 且识别为残疾人台账",
          r.status_code == 200 and d.get("kind") == "disabled", d.get("kind"))
    check("去重后 3 人", d.get("total") == 3, d.get("total"))
    stt = d.get("stat") or {}
    check("原始行 5、重复合并 1、跳过 1",
          stt.get("残疾_原始行") == 5 and stt.get("重复_合并") == 1
          and stt.get("跳过_无有效身份证") == 1,
          stt)
    check("样本给出了脱敏姓名与门牌",
          any((x.get("name") or "").startswith("张") and "101" in (x.get("address") or "")
              for x in (d.get("samples") or [])),
          (d.get("samples") or [])[:1])
    with app.app_context():
        n_before_preview = Person.query.count()
    r = st.post("/api/persons/import/preview", data=parts(build_disabled_ledger()),
                content_type="multipart/form-data")
    d = json_of(r).get("data") or {}
    with app.app_context():
        n_after_preview = Person.query.count()
    check("预览不写库", n_after_preview == n_before_preview,
          "%s → %s" % (n_before_preview, n_after_preview))

    with app.app_context():
        before_backups = len(list((BASE / "data" / "backups").glob("*"))) \
            if (BASE / "data" / "backups").exists() else 0

    r = st.post("/api/persons/import/apply", data=parts(build_disabled_ledger()),
                content_type="multipart/form-data")
    d = json_of(r).get("data") or {}
    with app.app_context():
        n = Person.query.count()
        n_distinct = Person.query.with_entities(Person.id_hash).distinct().count()
        check("apply 200 且新增 3 人", r.status_code == 200 and d.get("added") == 3,
              "%s / %s" % (r.status_code, d))
        check("库内仅多出 3 人（300 + 3）", n == n_before_preview + 3,
              "%s / 基数 %s" % (n, n_before_preview))
        check("重复身份证只进了一次（id_hash 唯一）",
              n_distinct == n, "%s / %s" % (n_distinct, n))
        check("导入前有备份文件生成（sqlite3.backup）",
              len(list((BASE / "data" / "backups").glob("*"))) > before_backups)
        aud = AuditLog.query.order_by(AuditLog.id.desc()).first()
        check("审计留痕在账本上（agent=person, action=import）",
              aud is not None and aud.agent == "person" and aud.action == "import",
              (aud.agent, aud.action) if aud else None)

    # 幂等：同一文件再导一遍，不再新增人
    r = st.post("/api/persons/import/apply", data=parts(build_disabled_ledger()),
                content_type="multipart/form-data")
    d = json_of(r).get("data") or {}
    check("重复导入不新增人", d.get("added", 999) == 0, d.get("added"))


def t_xls_rescue():
    print("\n[3] 空模板/别种文件兜底")
    st = login("jiedao")
    r = st.post("/api/persons/import/preview", data=parts(build_disabled_ledger(), kind="drowning"),
                content_type="multipart/form-data")
    check("手动指定类型不会抛 500", r.status_code in (200, 400), r.status_code)


t_route()
st = t_preview_apply(st if 'st' in dir() else login("jiedao"))
_t = t_xls_rescue()

print("\n" + "=" * 66)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 66)
sys.exit(1 if FAIL else 0)