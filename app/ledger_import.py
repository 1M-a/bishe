"""网页端台账上传导入：识别 → 清洗 → 去重 → 脱敏 → 入库。

为什么网页要再包一层、不直接调 scripts/import_ledger.py：
那条脚本只接受「磁盘上的固定文件名 + 固定目录」，网页上传进来的是
request.files（BytesIO），两者输入形态完全不同。清洗、去重、脱敏、
写库的核心仍是 import_ledger 里的 Collector / read_* / apply ——
已经参数化支持 src 与 app 的同一套逻辑，网页端只补三件事：

  1. 把上传的 BytesIO 接到 read_* 上（原文件**不落盘**，
     身份证明文不碰磁盘）；
  2. 识别台账类型（按列数：残疾人 17 列 / 防溺水 21 列）；
  3. 拆成「识别预览」与「确认导入」两个动作，预览只统计不写库；
     确认时备份 → 写库 → 审计。

导入是幂等的：同一身份证重复导入只补标签、不新增人，换整批用 purge。
"""
import io
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "scripts"))

import import_ledger as il  # noqa: E402

# 台账类型 → 展示名。导出规格（app/personbook.EXPORT_SPECS）里：
# 残疾人 17 列、防溺水 21 列、留守儿童 14 列。列数是「看得出来是哪种表」
# 的唯一可靠信号 —— 表头行会因为模板版本而换行，列数不会。
KINDS = {
    "disabled": "残疾人台账",
    "drowning": "防溺水台账",
}
_KIND_MIN_COL = {"disabled": 15, "drowning": 19}
_SAMPLE_LIMIT = 5


class ImportFormatError(ValueError):
    """文件不是能识别的台账 / 有空缺，属于可预期的用户错误，不要打 500"""


def detect_kind(workbook):
    """按第一个工作表的列数判断台账类型，认不出抛 ImportFormatError。"""
    ws = workbook.worksheets[0]
    n = ws.max_column or 0
    if n >= _KIND_MIN_COL["drowning"]:
        return "drowning"
    if n >= _KIND_MIN_COL["disabled"]:
        return "disabled"
    raise ImportFormatError(
        "认不出这是哪张台账：列数是 %d。平台目前支持「残疾人总台账」（17 列）"
        "与「防溺水全员覆盖台账」（21 列），请确认选的是一张街道下发的原件。"
        % n)


def _open(bio):
    import openpyxl
    bio.seek(0)                      # 同一个 BytesIO 可能已被读过一遍
    wb = openpyxl.load_workbook(bio, data_only=True)
    if not wb.worksheets:
        wb.close()
        raise ImportFormatError("工作簿里没有工作表，无法导入")
    return wb


def collect(bio, kind="auto"):
    """解析上传内容 → Collector（清洗去重脱敏后的行集合）。

    kind: auto 按列数识别；指定 disabled/drowning 时按绑定表头解析。
    """
    wb = _open(bio)
    try:
        real = detect_kind(wb) if (kind or "auto") == "auto" else kind
        if real not in KINDS:
            raise ImportFormatError("不支持的台账类型：%s" % real)
    finally:
        wb.close()

    c = il.Collector()
    # 重新打开一次（BytesIO 已指到开头）：上下两行之间不共享工作簿对象，
    # 免得 read_* 内部 close 影响外面的识别逻辑。
    bio.seek(0)
    if real == "disabled":
        il.read_disabled(c, src=bio)
    else:
        il.read_drowning(c, src=bio)
    return c, real


def preview(c, kind):
    """不确定写库，只汇报「清洗出来后是什么样」。

    与命令行 report() 共用 il.summarize()，预览口径和最终入库口径
    天然一致 —— 网页端自己再算一遍，两边数字迟早漂移。
    """
    s = il.summarize(c)
    rows = list(c.rows.values())[:_SAMPLE_LIMIT]
    samples = [{
        "name": r["name"], "id_masked": r["id_masked"],
        "gender": r["gender"], "age": r["age"],
        "community": r["community"], "address": r["address"],
        "addr_level": r["addr_level"], "source": r["source"],
        "tags": [t[0] for t in r["tags"]],
    } for r in rows]
    return {
        "kind": kind,
        "kind_name": KINDS[kind],
        "stat": s["stat"],
        "total": s["total"],
        "addr_levels": s["addr_levels"],
        "communities": [{"name": k, "count": v} for k, v in s["communities"]],
        "tags": [{"name": k, "count": v} for k, v in s["tags"].most_common()],
        "multi_tag": s["multi_tag"],
        "weights": s["weights"],
        "samples": samples,
    }


def apply(c, kind, actor, purge=False, app=None):
    """备份 → 写库 → 审计。

    c: 已 collect 出来的 Collector（端点先 collect 一次同时做校验，
    apply 不再重新解析上传文件——大台账解析一次就够，没必要读两遍）；
    kind: 识别出的台账类型（"disabled"/"drowning"）；actor: 触发导入的
    用户名（写 AuditLog）；app: 当前运行的 Flask app，避免 apply 内部另开
    一条数据库连接。
    """
    from flask import current_app
    from app import _sqlite_path
    from app.extensions import db
    from app.models import AuditLog

    if app is None:
        app = current_app._get_current_object()

    # 在线备份：sqlite3.backup() 而不是复制文件，后者会丢 WAL 里没
    # checkpoint 的事务。备份成功与否都不阻断导入，但必须留痕。
    backup_file = ""
    db_path = _sqlite_path(app)
    if db_path:
        try:
            backup_file = il.backup(db_path)
        except Exception:  # noqa: BLE001
            backup_file = ""

    stat = dict(c.stat)
    result = il.apply(c, purge=purge, app=app)
    result["purge"] = bool(purge)
    result["backup"] = backup_file

    # 审计只记统计不记人：这条轨迹本身也可能被导出，不能把掩码门户敞开
    summary = "新增 %d 人、补标签 %d 人；置空旧底册：%s；备份：%s" % (
        result["added"], result["merged"], "是" if purge else "否",
        os.path.basename(backup_file) if backup_file else "无")
    db.session.add(AuditLog(
        actor=(actor or "")[:32], agent="person", action="import",
        input_digest="上传 %s，清洗后 %d 人（原始行：%d）" % (
            KINDS.get(kind, kind), result["total"],
            stat.get("残疾_原始行", 0) + stat.get("防溺_原始行", 0)),
        output_digest=summary[:256]))
    db.session.commit()
    return result