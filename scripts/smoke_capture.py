"""Screenshot intake lifecycle, authorization and export smoke test.

Uses an isolated temporary SQLite database and fake OCR/model output.
"""
from __future__ import annotations

import csv
import io
import json
import os
import pathlib
import sys
import tempfile

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
TEMP = tempfile.TemporaryDirectory(prefix="bise4-capture-")
ROOT = pathlib.Path(TEMP.name)
os.environ["DATABASE_URL"] = "sqlite:///" + str(ROOT / "capture.db")
os.environ["EVIDENCE_DIR"] = str(ROOT / "evidence")
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (AuditLog, CaptureDraft, FdeRecord, Ledger, User,
                        WorkItem, WorkOrder)  # noqa: E402
from app import capture as capture_service  # noqa: E402

app = create_app("dev")
PASS = FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  [PASS]", name)
    else:
        FAIL += 1
        print("  [FAIL]", name, detail)


def session_for(client, uid):
    with client.session_transaction() as sess:
        sess["uid"] = uid


def snapshot_counts():
    with app.app_context():
        return (FdeRecord.query.count(), WorkItem.query.count(),
                WorkOrder.query.count(), Ledger.query.count())


def audit_actions():
    with app.app_context():
        return {r[0] for r in db.session.query(AuditLog.action).filter(
            AuditLog.agent == "capture").all()}


def main():
    with app.app_context():
        staff = User(username="capture_staff", name="居委经办", role="community",
                     org="吟峰苑社区", status="active")
        peer = User(username="capture_peer", name="同社区", role="community",
                    org="吟峰苑社区", status="active")
        other = User(username="capture_other", name="其他社区", role="community",
                     org="另一社区", status="active")
        blank_a = User(username="capture_blank_a", name="空组织甲", role="community",
                       org="", status="active")
        blank_b = User(username="capture_blank_b", name="空组织乙", role="community",
                       org="", status="active")
        prop = User(username="capture_property", name="物业", role="property",
                    org="吟峰苑物业", status="active")
        db.session.add_all([staff, peer, other, blank_a, blank_b, prop])
        db.session.commit()
        ids = {u.username: u.id for u in (staff, peer, other, blank_a, blank_b, prop)}

    sample_text = ("吟峰苑 2号楼807、小马奔腾 Yesterday 8:46 PM 负一楼脏乱无灯，"
                   "不具备使用条件，拉重物卸货难。建议短期允许拉重物车辆上平台卸货，"
                   "长期彻底整修负一楼（保洁+照明）。")
    sample_row = {
        "source_community": "吟峰苑", "speaker": "2号楼807、小马奔腾",
        "sent_at": "Yesterday 8:46 PM", "category": "基础设施修缮",
        "tags": ["负一楼整修", "照明缺失", "重物卸货"],
        "pain": "负一楼脏乱无灯，不具备使用条件，拉重物卸货难",
        "suggestion": "短期允许拉重物车辆上平台卸货；长期彻底整修负一楼（保洁+照明）",
        "owner": "小区物业", "status": "待居委会核实",
        "item_type": "报修", "target_lines": ["物业"],
    }
    original_extract = capture_service.extract_text
    original_analyze = capture_service.analyze
    capture_service.extract_text = lambda _raw: (sample_text, "image/jpeg")
    capture_service.analyze = lambda _text, _hint: ([dict(sample_row)], "local_llm")

    def upload(client, community="吟峰苑"):
        return client.post("/api/fde/capture/preview", data={
            "community": community,
            "image": (io.BytesIO(b"\xff\xd8\xffsmoke-test"), "chat.jpg"),
        }, content_type="multipart/form-data")

    try:
        client = app.test_client()
        session_for(client, ids["capture_staff"])
        before = snapshot_counts()
        response = upload(client)
        payload = response.get_json()["data"]
        draft_id = payload["id"]
        check("截图识别生成结构化草稿", response.status_code == 200 and
              payload["rows"][0]["speaker"] == sample_row["speaker"])
        after_preview = snapshot_counts()
        check("预览阶段不写正式业务数据", before == after_preview, after_preview)

        other_client = app.test_client()
        session_for(other_client, ids["capture_other"])
        peer_client = app.test_client()
        session_for(peer_client, ids["capture_peer"])
        property_client = app.test_client()
        session_for(property_client, ids["capture_property"])
        check("同社区可审核草稿", peer_client.get(f"/api/fde/capture/{draft_id}").status_code == 200)
        check("跨社区不能读取草稿", other_client.get(f"/api/fde/capture/{draft_id}").status_code == 404)
        check("物业不能读取居委截图", property_client.get(f"/api/fde/capture/{draft_id}").status_code == 403)
        image_response = client.get(f"/api/fde/capture/{draft_id}/image")
        check("原图私有访问并禁止缓存", image_response.status_code == 200 and
              image_response.headers.get("Cache-Control") == "private, no-store" and
              image_response.headers.get("X-Content-Type-Options") == "nosniff")
        image_response.close()

        confirm = client.post(f"/api/fde/capture/{draft_id}/confirm",
                              json={"rows": payload["rows"]})
        result = confirm.get_json()["data"]
        check("确认后创建正式事项", confirm.status_code == 200 and result["item_count"] == 1)
        fde_id = result["fde_id"]
        with app.app_context():
            saved_raw = db.session.get(FdeRecord, fde_id).raw_text
        check("FDE 只保存确认摘要、不保存整段 OCR",
              saved_raw != sample_text and "Yesterday 8:46 PM" in saved_raw)
        retry = client.post(f"/api/fde/capture/{draft_id}/confirm",
                            json={"rows": payload["rows"]})
        check("重复确认幂等", retry.status_code == 200 and
              retry.get_json()["data"].get("idempotent") is True and
              snapshot_counts()[0] == before[0] + 1 and
              snapshot_counts()[1] == before[1] + 1 and
              snapshot_counts()[2] == before[2] + 1)

        csv_response = client.get(f"/api/fde/capture/{draft_id}/csv")
        csv_text = csv_response.get_data().decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(csv_text)))
        expected_header = ["来源小区", "发言人", "发言时间", "一级分类",
                           "二级标签", "痛点摘要", "居民建议", "责任主体", "当前状态"]
        check("CSV 九列顺序及中文数据正确", rows[0] == expected_header and
              rows[1][0:3] == ["吟峰苑", "2号楼807、小马奔腾", "Yesterday 8:46 PM"] and
              rows[1][4] == "负一楼整修；照明缺失；重物卸货")

        second = upload(client)
        second_data = second.get_json()["data"]
        second_id = second_data["id"]
        edited_rows = second_data["rows"]
        edited_rows[0]["speaker"] = " \t=1+1"
        second_confirm = client.post(f"/api/fde/capture/{second_id}/confirm",
                                     json={"rows": edited_rows})
        second_csv = client.get(f"/api/fde/capture/{second_id}/csv")
        second_rows = list(csv.reader(io.StringIO(
            second_csv.get_data().decode("utf-8-sig"))))
        with app.app_context():
            preserved_suggestion = json.loads(
                db.session.get(CaptureDraft, second_id).suggested_json)
            review_logs = AuditLog.query.filter_by(
                agent="capture", action="capture_confirm_review").order_by(
                    AuditLog.id.desc()).all()
        check("人工修订入库且原始建议保留", second_confirm.status_code == 200 and
              preserved_suggestion[0]["speaker"] == sample_row["speaker"] and
              any(second_id[:8] in a.input_digest and
                  "edited=True" in a.output_digest for a in review_logs))
        check("CSV 防公式注入含前导空白/制表符",
              second_csv.status_code == 200 and second_rows[1][1].startswith("'"))

        rejected_preview = upload(client)
        rejected_data = rejected_preview.get_json()["data"]
        rejected_id = rejected_data["id"]
        rejected_response = client.post(f"/api/fde/capture/{rejected_id}/reject")
        no_confirm = client.post(f"/api/fde/capture/{rejected_id}/confirm",
                                json={"rows": rejected_data["rows"]})
        check("作废草稿不能确认入库", rejected_response.status_code == 200 and no_confirm.status_code == 409 and
              snapshot_counts()[0] == before[0] + 2)

        blank_client = app.test_client()
        session_for(blank_client, ids["capture_blank_a"])
        blank_upload = upload(blank_client, community="空组织社区")
        blank_id = blank_upload.get_json()["data"]["id"]
        blank_peer_client = app.test_client()
        session_for(blank_peer_client, ids["capture_blank_b"])
        check("空组织账号只看自己的草稿",
              blank_peer_client.get(f"/api/fde/capture/{blank_id}").status_code == 404)

        actions = audit_actions()
        check("查看、确认、导出与作废均留审计",
              {"capture_preview", "capture_view", "capture_image_view",
               "capture_confirm_review", "capture_confirm",
               "capture_csv_export", "capture_reject"}.issubset(actions))
    finally:
        capture_service.extract_text = original_extract
        capture_service.analyze = original_analyze
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        TEMP.cleanup()

    print(f"\n截图收录冒烟测试：{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
