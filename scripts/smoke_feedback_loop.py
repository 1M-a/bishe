"""居民数据治理与居委纠错闭环回归测试。

覆盖：清洗、精确/近似去重、数字实体防误合并、角色权限、三次人工确认、
规则自动应用、Agent 轨迹和审计脱敏。使用独立 SQLite 库且强制走规则降级，
结果不依赖本地模型是否启动。
"""
import os
import pathlib
import sys


BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_feedback_loop.db"
if DB.exists():
    DB.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.intake import normalize_text  # noqa: E402
from app.models import (AgentStep, AuditLog, FdeRecord, ModelFeedback,
                        ResidentTicket, WorkItem)  # noqa: E402
from app import ratelimit  # noqa: E402


app = create_app("dev")
PASS = FAIL = 0
FAILED = []


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f"  [{detail}]" if detail else ""))


def login(client, username, password="123456"):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)


def submit(client, text, phone):
    return client.post("/api/portal/report", json={"text": text, "phone": phone})


def latest_item(ticket_id):
    with app.app_context():
        ticket = db.session.get(ResidentTicket, ticket_id)
        return (WorkItem.query.filter_by(fde_id=ticket.fde_id)
                .order_by(WorkItem.id.asc()).first())


def main():
    resident = app.test_client()
    community = app.test_client()
    property_client = app.test_client()
    street = app.test_client()
    ratelimit.reset()

    print("\n[1] 清洗与去重")
    normalized, flags = normalize_text("  您好！！！  ５栋楼道灯坏了。谢谢您  ")
    check("全角与空白已归一", normalized == "5栋楼道灯坏了。", normalized)
    check("清洗动作可解释", {"空白归一", "重复标点压缩", "礼貌填充语移除"} <= set(flags), flags)

    first = submit(resident, "您好！！！ 5栋楼道照明灯损坏需要维修。谢谢您", "13800001001")
    first_data = first.get_json()["data"]
    check("首条诉求正常建单", first.status_code == 200 and not first_data["deduplicated"])
    ticket_id = first_data["ticket_id"]
    code = first_data["code"]
    with app.app_context():
        base_counts = (ResidentTicket.query.count(), FdeRecord.query.count(), WorkItem.query.count())

    exact = submit(resident, "5栋楼道照明灯损坏需要维修。", "13800001001")
    exact_data = exact.get_json()["data"]
    check("精确重复返回原查询码", exact_data["deduplicated"] and exact_data["code"] == code,
          exact_data)
    check("精确重复标记为 exact", exact_data["duplicate_kind"] == "exact", exact_data)
    merged_page = resident.get(f"/portal/done/{ticket_id}?merged=1")
    check("居民端简短提示已合并", "已合并到原记录" in merged_page.get_data(as_text=True))

    similar = submit(resident, "5栋楼道照明灯损坏需要维修", "13800001001")
    similar_data = similar.get_json()["data"]
    check("近似重复合并原单", similar_data["deduplicated"] and similar_data["ticket_id"] == ticket_id,
          similar_data)
    check("近似重复标记为 similar", similar_data["duplicate_kind"] == "similar", similar_data)
    with app.app_context():
        after_dedupe = (ResidentTicket.query.count(), FdeRecord.query.count(), WorkItem.query.count())
    check("重复提交不新增 FDE/业务项/居民单", after_dedupe == base_counts,
          f"{base_counts} -> {after_dedupe}")

    other_building = submit(resident, "6栋楼道照明灯损坏需要维修", "13800001001")
    check("楼栋数字不同不误合并", not other_building.get_json()["data"]["deduplicated"])
    other_phone = submit(resident, "5栋楼道照明灯损坏需要维修。", "13800001002")
    check("不同手机号独立建单", not other_phone.get_json()["data"]["deduplicated"])

    print("\n[2] 居委权限边界")
    login(community, "wang")
    login(property_client, "wuye")
    login(street, "jiedao")
    check("社区可打开纠错台", community.get("/feedback").status_code == 200)
    check("社区可读取候选项", community.get("/api/feedback/candidates").status_code == 200)
    check("物业不能打开纠错台", property_client.get("/feedback").status_code == 403)
    check("物业不能读取纠错接口", property_client.get("/api/feedback").status_code == 403)
    check("街道不能提交模型纠错", street.post("/api/feedback", json={}).status_code == 403)
    pii_rejected = community.post("/api/feedback", json={
        "work_item_id": latest_item(ticket_id).id,
        "issue_type": "classify",
        "trigger": "联系13800009999",
        "corrected_type": "报修",
        "corrected_lines": ["物业"],
        "reason": "测试敏感信息不得进入纠错样本",
    })
    check("纠错样本拒绝手机号和身份证", pii_rejected.status_code == 400,
          pii_rejected.status_code)
    ungrounded = community.post("/api/feedback", json={
        "work_item_id": latest_item(ticket_id).id,
        "issue_type": "classify",
        "trigger": "高空抛物",
        "corrected_type": "隐患排查",
        "corrected_lines": ["综治"],
        "reason": "无关触发词不能借用这条业务项累计支持数",
    })
    check("触发词必须能回指事项原文", ungrounded.status_code == 400,
          ungrounded.status_code)

    print("\n[3] 三次确认形成规则")
    training = [
        ("1栋有人高空抛物需要处理", "13800002001"),
        ("2栋发现高空抛物需要处理", "13800002002"),
        ("3栋居民反映高空抛物需要处理", "13800002003"),
    ]
    feedback_ids = []
    for index, (text, phone) in enumerate(training, start=1):
        response = submit(resident, text, phone)
        data = response.get_json()["data"]
        item = latest_item(data["ticket_id"])
        check(f"第 {index} 条初始规则判为其他", item.type == "其他", item.type)
        correction = community.post("/api/feedback", json={
            "work_item_id": item.id,
            "issue_type": "classify",
            "trigger": "高空抛物",
            "corrected_type": "隐患排查",
            "corrected_lines": ["综治"],
            "reason": "高空抛物属于公共安全隐患，需要纳入隐患排查",
        })
        correction_data = correction.get_json()["data"]
        feedback_ids.append(correction_data["id"])
        check(f"第 {index} 次确认支持数正确", correction_data["support"] == index,
              correction_data)
        check(f"第 {index} 次确认生效门槛正确",
              correction_data["rule_ready"] is (index >= 3), correction_data)
        if index == 1:
            duplicate = community.post("/api/feedback", json={
                "work_item_id": item.id,
                "issue_type": "classify",
                "trigger": "高空抛物",
                "corrected_type": "隐患排查",
                "corrected_lines": ["综治"],
                "reason": "重复提交同一条纠错不应累计支持数",
            })
            check("同一业务项相同纠错不能重复计数", duplicate.status_code == 409,
                  duplicate.status_code)

    with app.app_context():
        check("三条确认样本均可追溯", ModelFeedback.query.count() == 3,
              ModelFeedback.query.count())

    print("\n[4] 新诉求命中规则并留痕")
    applied = submit(resident, "4栋再次发现高空抛物需要处理", "13800002004")
    applied_data = applied.get_json()["data"]
    applied_item = latest_item(applied_data["ticket_id"])
    check("第四条自动修正为隐患排查", applied_item.type == "隐患排查", applied_item.type)
    check("修正后的业务线正确", applied_item.target_lines == "综治", applied_item.target_lines)
    with app.app_context():
        parser_steps = AgentStep.query.filter_by(fde_id=applied_item.fde_id, agent="parser").all()
        check("Agent 轨迹记录学习规则", any("学习规则[高空抛物]" in (x.reason or "")
                                          for x in parser_steps),
              [x.reason for x in parser_steps])
        feedback_rows = ModelFeedback.query.order_by(ModelFeedback.id.asc()).all()
        check("一次命中只计一次应用", sum(x.applied_count or 0 for x in feedback_rows) == 1,
              [x.applied_count for x in feedback_rows])

    overview = community.get("/api/feedback").get_json()["data"]
    check("纠错台显示一条可用规则", overview["counts"]["rules_ready"] == 1,
          overview["counts"])
    check("纠错台应用次数不虚高", overview["counts"]["applications"] == 1,
          overview["counts"])

    print("\n[5] 审计与隐私")
    with app.app_context():
        dedupe_logs = AuditLog.query.filter_by(agent="portal", action="dedupe").all()
        feedback_logs = AuditLog.query.filter_by(agent="feedback", action="create").all()
        audit_text = " ".join(
            f"{x.actor} {x.input_digest} {x.output_digest}"
            for x in AuditLog.query.all())
        check("去重动作写入审计", len(dedupe_logs) >= 2, len(dedupe_logs))
        check("纠错动作写入审计", len(feedback_logs) == 3, len(feedback_logs))
        check("审计中不含完整居民手机号", all(phone not in audit_text for _text, phone in training)
              and "13800001001" not in audit_text, audit_text[:300])

    print(f"\n{'=' * 56}")
    print(f"通过 {PASS} / {PASS + FAIL}")
    if FAILED:
        print("失败项：")
        for name in FAILED:
            print("  -", name)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
