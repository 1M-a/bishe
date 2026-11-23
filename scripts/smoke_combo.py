"""P0-1 组合反馈回归冒烟测试

覆盖组合反馈验收点：
  1. 三通道平级：只文字 / 只照片 / 只语音均可提交成功
  2. 组合：文字+照片 / 文字+语音 / 照片+语音 / 三合一 均可提交
  3. 三通道全空 → 拒绝
  4. 文字 >1500 字 → 400 拒绝；照片 11 张 → 前端拦截（后端含 room 校验）
  5. 语音类型白名单（按文件头）：webm/wav/m4a 放行，伪造扩展名拒绝
  6. 语音大小 ≤15MB、时长 ≤300s 兜底
  7. 附件进证据链（attach of type=audio）、禁止中文扩展名绕过
  8. 去重命中时向旧单追加音频

独立库 + 指向不可用的模型地址（走规则降级），确定性、不依赖 GPU。
"""
import io
import os
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = pathlib.Path(os.environ.get("SMOKE_COMBO_DB", BASE / "data" / "smoke_combo.db"))
if DB.exists():
    DB.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"   # 必然连不上 → 规则降级

from app import create_app            # noqa: E402
from app.extensions import db         # noqa: E402
from app import evidence, ratelimit   # noqa: E402
from app.models import AuditLog, EvidenceChain, ResidentTicket  # noqa: E402

app = create_app("dev")
PASS = FAIL = 0
FAILED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  [{detail}]" if detail else ""))


def audio_bytes(kind: str = "webm") -> bytes:
    """构造按文件头可识别的假音频字节（内容不真实，仅测类型判定）"""
    if kind == "webm":
        return b"\x1a\x45\xdf\xa3" + b"fake-webm-payload-" * 40
    if kind == "wav":
        return b"RIFF" + b"\x00" * 40 + b"WAVE" + b"fake-wave-" * 40
    return b"\x00\x00\x00\x18ftypM4A" + b"fake-m4a-" * 40


def submit(client, phone, text="", photos=None, audio=False, audio_kind="webm",
           audio_seconds=5, name="张三", address="3栋"):
    files = {"phone": phone, "name": name, "address": address}
    if text:
        files["text"] = text
    if photos:
        files["photos"] = [(io.BytesIO(raw), f"img{i}.png")
                           for i, raw in enumerate(photos)]
    if audio:
        files["audio"] = (io.BytesIO(audio_bytes(audio_kind)), "voice." + audio_kind)
        files["audio_seconds"] = str(audio_seconds)
    return client.post("/api/portal/report", data=files,
                       content_type="multipart/form-data")


def main():
    c = app.test_client()
    ratelimit.reset()

    print("\n[1] 三通道分别单独可提交")
    r = submit(c, "13800000001", text="楼道灯坏了三天，晚上很暗，2栋3单元门口")
    j = r.get_json()
    check("只文字→提交成功", j["code"] == 0, j.get("msg"))
    d = j["data"]
    check("只文字→无音频", d["audio"] == 0, d.get("audio"))
    check("只文字→拆出业务项", d["item_count"] >= 1, d["item_count"])

    ratelimit.reset()
    r = submit(c, "13800000002", photos=[b"\x89PNG\r\n\x1a\n" + b"photo-a" * 20])
    j = r.get_json()
    check("只照片（无文字）→提交成功", j["code"] == 0, j.get("msg"))
    check("只照片→假音频未上传", j["data"]["audio"] == 0, j["data"].get("audio"))
    d2 = j["data"]
    check("只照片→存 1 张图", d2["photos"] == 1, d2.get("photos"))

    ratelimit.reset()
    r = submit(c, "13800000003", audio=True)
    j = r.get_json()
    check("只语音（无文字）→提交成功", j["code"] == 0, j.get("msg"))
    d3 = j["data"]
    check("只语音→存 1 条音频", d3["audio"] == 1, d3.get("audio"))

    print("\n[2] 四种组合均可提交")
    ratelimit.reset()
    r = submit(c, "13800000004", text="供水管漏水，地面全是水",
               photos=[b"\x89PNG\r\n\x1a\n" + b"p1" * 20])
    j = r.get_json()
    check("文字+照片→成功", j["code"] == 0, j.get("msg"))
    check("文字+照片→1 图", j["data"]["photos"] == 1)

    ratelimit.reset()
    r = submit(c, "13800000005", text="路灯闪烁", audio=True, audio_kind="wav")
    j = r.get_json()
    check("文字+语音→成功", j["code"] == 0, j.get("msg"))
    check("文字+语音→1 音", j["data"]["audio"] == 1)

    ratelimit.reset()
    r = submit(c, "13800000006", photos=[b"\x89PNG\r\n\x1a\n" + b"p2" * 20],
               audio=True, audio_kind="m4a")
    j = r.get_json()
    check("照片+语音→成功", j["code"] == 0, j.get("msg"))
    check("照片+语音→1 图 1 音", j["data"]["photos"] == 1 and j["data"]["audio"] == 1)

    ratelimit.reset()
    r = submit(c, "13800000007", text="电梯坏了，有人被困",
               photos=[b"\x89PNG\r\n\x1a\n" + b"p3" * 20],
               audio=True, audio_kind="webm", audio_seconds=12)
    j = r.get_json()
    check("三合一→成功", j["code"] == 0, j.get("msg"))
    d7 = j["data"]
    check("三合一→1 图 1 音", d7["photos"] == 1 and d7["audio"] == 1)

    print("\n[3] 三通道全空 → 拒绝（前端拦截为主，后端同样防空）")
    r = c.post("/api/portal/report", data={
        "text": "", "phone": "13800000008", "name": "", "address": ""})
    j = r.get_json()
    check("全空→拒绝", j["code"] != 0, j.get("msg"))
    with app.app_context():
        n = ResidentTicket.query.filter_by(phone="13800000008").count()
    check("全空→未入库", n == 0, n)

    print("\n[4] 边界：文字上限 / 照片上限 / 语音时长 / 大小")
    r = submit(c, "13800000009", text="长" * (1600), photos=[])
    j = r.get_json()
    check("文字 >1500 → 400 拒绝", j["code"] != 0, j.get("msg"))

    r = submit(c, "13800000010", text="ok",
               photos=[b"\x89PNG\r\n\x1a\n" + bytes([i % 7 for i in range(30)]) + b"-p" for _ in range(11)])
    j = r.get_json()
    d = j["data"]
    check("照片 11 张 → 提示超上限（不阻断文本）",
          j["code"] == 0 and len(d.get("photo_errors")) > 0 and d.get("photos") == 0,
          (j.get("msg"), d.get("photo_errors")))

    r = submit(c, "13800000011", audio=True, audio_kind="wav", audio_seconds=301)
    j = r.get_json()
    check("语音 301s → 400 拒绝", j["code"] != 0, j.get("msg"))

    r = c.post("/api/portal/report", data={
        "text": "测试语音大小上限", "phone": "13800000012",
        "audio": (io.BytesIO(b"\x1a\x45\xdf\xa3" + b"A" * (16 * 1024 * 1024)), "voice.webm")},
        content_type="multipart/form-data")
    j = r.get_json()
    d = j["data"]
    check("语音 >15MB → 提示超上限（不阻断文本）",
          j["code"] == 0 and len(d.get("audio_errors")) > 0 and d.get("audio") == 0,
          (j.get("msg"), d.get("audio_errors")))

    print("\n[5] 类型白名单（按文件头认，不认扩展名）")
    r = c.post("/api/portal/report", data={
        "text": "冒名扩展名测试", "phone": "13800000013",
        "audio": (io.BytesIO(b"\x1a\x45\xdf\xa3" + b"ok" * 100), "voice.m4a"),
        "audio_seconds": "3"},
        content_type="multipart/form-data")
    j = r.get_json()
    check("webm 头但 m4a 扩展名→按头放行", j["code"] == 0 and j["data"]["audio"] == 1,
          j.get("msg"))

    r = c.post("/api/portal/report", data={
        "text": "垃圾头测试", "phone": "13800000014",
        "audio": (io.BytesIO(b"GARBAGE" + b"x" * 500), "voice.webm"),
        "audio_seconds": "3"},
        content_type="multipart/form-data")
    j = r.get_json()
    d = j["data"]
    check("不是音频头→文本仍保存但语音被拒",
          j["code"] == 0 and len(d.get("audio_errors")) > 0 and d.get("audio") == 0,
          (j.get("msg"), d.get("audio_errors")))

    print("\n[6] 证据链与落盘检查")
    with app.app_context():
        ticket = (ResidentTicket.query
                  .filter_by(phone="13800000003").order_by(ResidentTicket.id.desc()).first())
        atts = evidence.load_attachments(ticket.attachments)
        audio_item = [a for a in atts if a.get("type") == "audio"]
        aud_chain = (EvidenceChain.query
                     .filter(EvidenceChain.target_id == ticket.id,
                             EvidenceChain.action == "attach").all())
        stored_file = None
        if audio_item:
            ipath = os.path.join(app.config["EVIDENCE_DIR"],
                                 audio_item[0]["stored"])
            stored_file = os.path.exists(ipath)
    check("语音附件带 type=audio 标记", len(audio_item) == 1, audio_item)
    check("语音文件已落盘于 evidence/audio 目录", bool(stored_file))
    check("音频哈希进证据链", any("type" in (a.payload or "") and "audio" in (a.payload or "")
                              for a in aud_chain), [a.payload for a in aud_chain])

    with app.app_context():
        audit = AuditLog.query.filter(
            AuditLog.action.like("%voice%")).order_by(AuditLog.id.desc()).first()
    check("存在 voice_upload 审计", audit is not None, audit.action if audit else "")

    print("\n[7] 去重命中时向旧单追加音频")
    ratelimit.reset()
    first = submit(c, "13800000020", text="5栋楼下垃圾桶满溢，无人清理")
    j1 = first.get_json()
    check("首单提交成功", j1["code"] == 0, j1.get("msg"))
    tid1 = j1["data"]["ticket_id"]

    ratelimit.reset()
    r = submit(c, "13800000020", text="5栋楼下垃圾桶满溢，无人清理",
               audio=True, audio_kind="webm", audio_seconds=7)
    j2 = r.get_json()
    check("文本精确重复→复用原单", j2["data"]["deduplicated"], j2.get("msg"))
    check("原单号相同", j2["data"]["duplicate_of"] == tid1, j2["data"].get("duplicate_of"))
    check("追加 1 条语音", j2["data"]["audio"] == 1, j2["data"].get("audio"))
    with app.app_context():
        t2 = db.session.get(ResidentTicket, tid1)
        a2 = evidence.load_attachments(t2.attachments)
        has_aud = any(x.get("type") == "audio" for x in a2)
    check("音频已合并进原单 attachments", has_aud)

    print("\n[8] 页面静态检查（汇总条 / 录音机 / 1500 / 10 张）")
    html = c.get("/portal/report").get_data(as_text=True)
    check("报告页 200", html and "说说遇到的事" in html)
    check("汇总条存在", 'id="summary"' in html and "data-c=" in html)
    check("录音机按钮存在", 'id="mic"' in html and 'MediaRecorder' in html)
    check("文字上限 1500", 'maxlength="1500"' in html and "1500 字" in html)
    check("照片上限 10 张", "最多 10 张" in html and "MAX_PHOTOS = 10" in html)
    check("全空提示文案", "请至少选一种反馈方式" in html)
    check("拒绝麦克风引导卡", 'id="micDenied"' in html and "不影响您使用文字或照片反馈" in html)
    check("前端随表单上报语音时长", "audio_seconds" in html)
    check("录音完成后可试听/重录/删除", 'id="micRedo"' in html and 'id="micClear"' in html
          and '<audio id="playback"' in html)

    print("\n" + "=" * 50)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    if FAILED:
        print("失败项：" + "；".join(FAILED))
        sys.exit(1)
    print("ABC = P0-1 组合反馈全部通过")


if __name__ == "__main__":
    main()