"""从居委人工纠错中提取保守的可复用规则。

规则必须达到 3 次同向确认才自动应用；每次应用都增加计数并写入 Agent 轨迹，
因此“模型迭代”是可解释的规则增强，不伪装成未经验证的自动训练。
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func

from app.extensions import db
from app.models import ModelFeedback


MIN_SUPPORT = 3


def apply_rules(text: str, items: list[dict]) -> list[str]:
    if not text or not items:
        return []
    rows = (db.session.query(
                ModelFeedback.issue_type,
                ModelFeedback.trigger,
                ModelFeedback.before_json,
                ModelFeedback.after_json,
                func.count(ModelFeedback.id).label("support"))
            .filter(ModelFeedback.status == "approved",
                    ModelFeedback.issue_type.in_(("classify", "route")),
                    ModelFeedback.trigger != "")
            .group_by(ModelFeedback.issue_type, ModelFeedback.trigger,
                      ModelFeedback.before_json,
                      ModelFeedback.after_json)
            .having(func.count(ModelFeedback.id) >= MIN_SUPPORT)
            .all())
    corrections = []
    for issue_type, trigger, before_raw, after_raw, support in rows:
        if trigger not in text:
            continue
        try:
            before = json.loads(before_raw or "{}")
            after = json.loads(after_raw or "{}")
        except (TypeError, ValueError):
            continue
        for item in items:
            changed = False
            if before.get("type") and item.get("type") != before["type"]:
                continue
            if after.get("type"):
                old = item.get("type")
                item["type"] = after["type"]
                if old != item["type"]:
                    corrections.append(f"学习规则[{trigger}]：类型 {old}→{item['type']}（{support} 次确认）")
                    changed = True
            if after.get("target_lines"):
                old_lines = list(item.get("target_lines") or [])
                item["target_lines"] = list(dict.fromkeys(after["target_lines"]))
                if old_lines != item["target_lines"]:
                    corrections.append(
                        f"学习规则[{trigger}]：去向 {'/'.join(old_lines)}→"
                        f"{'/'.join(item['target_lines'])}（{support} 次确认）")
                    changed = True
            if changed:
                # 一次规则命中只记一次应用。计数落在该规则最早的确认样本上，
                # 避免 3 条支持样本把 1 次命中显示成 3 次。
                row = (ModelFeedback.query
                       .filter_by(issue_type=issue_type, trigger=trigger,
                                  before_json=before_raw, after_json=after_raw,
                                  status="approved")
                       .order_by(ModelFeedback.id.asc()).first())
                if row:
                    row.applied_count = (row.applied_count or 0) + 1
                    row.applied_at = datetime.now()
                break
    return corrections
