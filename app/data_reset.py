"""管理员数据清理前的可回溯备份与清空。

备份不是简单复制 community.db：SQLite 开 WAL 时，直接复制主文件可能漏掉
尚未 checkpoint 的事务。这里用 sqlite3.Connection.backup() 生成一致快照，
再把证据附件和待导入公告文件一起打进私有 zip 包。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime
from pathlib import Path


BACKUP_DIRNAME = "backups"
BACKUP_RE = re.compile(r"^data-backup-\d{8}-\d{6}-[0-9a-f]{8}\.zip$")
PRESERVED_TABLES = {"user", "org_contact", "kb_doc"}
REQUIRED_BACKUP_ENTRY = "community.db"
_RESET_LOCK = threading.Lock()


class DataResetError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _db_path(app) -> Path:
    uri = str(app.config.get("SQLALCHEMY_DATABASE_URI") or "")
    if not uri.startswith("sqlite:///"):
        raise DataResetError("当前数据库不是 SQLite，暂不支持此清理按钮")
    raw = uri[len("sqlite:///"):]
    if not raw or raw == ":memory:":
        raise DataResetError("内存数据库没有可下载的快照文件")
    return Path(raw).expanduser().resolve()


def backup_dir(app) -> Path:
    root = _db_path(app).parent / BACKUP_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not BACKUP_RE.fullmatch(name):
        raise DataResetError("备份文件名不合法")
    return name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for table in _tables(conn):
        try:
            out[table] = int(conn.execute(
                'SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"'
            ).fetchone()[0])
        except Exception:
            out[table] = -1
    return out


def _files(root: Path) -> list[dict]:
    if not root.exists():
        return []
    out = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        out.append({
            "path": rel,
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        })
    return out


def _copy_sqlite_snapshot(source: Path, target: Path) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source), timeout=30)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    check = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    try:
        if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise DataResetError("生成的数据库快照校验失败")
        return _counts(check)
    finally:
        check.close()


def create_backup(app, actor: str, reason: str = "before_clear") -> dict:
    """生成私有 zip 备份，返回可供管理员页面展示的元数据。"""
    db_path = _db_path(app)
    if not db_path.exists():
        raise DataResetError("数据库文件不存在")

    evidence_root = Path(app.config["EVIDENCE_DIR"]).resolve()
    notice_root = Path(app.config.get("NOTICE_DIR") or
                       db_path.parent / "notices").resolve()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"data-backup-{stamp}-{uuid.uuid4().hex[:8]}.zip"
    dest = backup_dir(app) / filename
    temp_dir = Path(tempfile.mkdtemp(prefix="data-backup-", dir=str(backup_dir(app))))
    snapshot = temp_dir / REQUIRED_BACKUP_ENTRY
    try:
        counts = _copy_sqlite_snapshot(db_path, snapshot)
        evidence_files = _files(evidence_root)
        notice_files = _files(notice_root)
        manifest = {
            "format": 1,
            "filename": filename,
            "created_at": _now(),
            "actor": (actor or "admin")[:32],
            "reason": reason,
            "database": "community.db",
            "tables": counts,
            "preserved_tables": sorted(PRESERVED_TABLES),
            "files": {
                "evidence": evidence_files,
                "notices": notice_files,
            },
        }
        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6) as archive:
            archive.write(snapshot, REQUIRED_BACKUP_ENTRY)
            archive.writestr("manifest.json",
                             json.dumps(manifest, ensure_ascii=False, indent=2))
            for item in evidence_files:
                archive.write(evidence_root / item["path"],
                              f"evidence/{item['path']}")
            for item in notice_files:
                archive.write(notice_root / item["path"],
                              f"notices/{item['path']}")
        with zipfile.ZipFile(dest, "r") as archive:
            bad = archive.testzip()
            if bad:
                raise DataResetError(f"备份压缩包校验失败：{bad}")
        return {
            "name": filename,
            "created_at": manifest["created_at"],
            "actor": manifest["actor"],
            "size": dest.stat().st_size,
            "sha256": _sha256(dest),
            "tables": counts,
            "evidence_count": len(evidence_files),
            "notice_file_count": len(notice_files),
            "download_url": f"/api/admin/data-backups/{filename}/download",
        }
    except Exception:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _drop_audit_triggers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name='audit_log' AND name LIKE 'trg_audit_log_no_%' "
        "AND sql IS NOT NULL ORDER BY name"
    ).fetchall()
    for sql in rows:
        name = re.search(r'CREATE TRIGGER(?: IF NOT EXISTS)?\s+([\w"]+)',
                         sql[0], re.I)
        if name:
            trigger_name = name.group(1).strip('"')
            conn.execute('DROP TRIGGER IF EXISTS "' +
                         trigger_name.replace('"', '""') + '"')
    return [row[0] for row in rows]


def _clear_files(root: Path) -> int:
    if not root.exists():
        return 0
    removed = 0
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
            removed += 1
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    root.mkdir(parents=True, exist_ok=True)
    return removed


def clear_business_data(app) -> dict:
    """清空业务数据；保留管理员账号、知识库和公开联系人配置。"""
    db_path = _db_path(app)
    if not db_path.exists():
        raise DataResetError("数据库文件不存在")

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.isolation_level = None
    counts_before = {}
    deleted = {}
    try:
        conn.execute("PRAGMA busy_timeout=20000")
        conn.execute("PRAGMA foreign_keys=OFF")
        counts_before = _counts(conn)
        tables = _tables(conn)
        clear_tables = [t for t in tables if t not in PRESERVED_TABLES and t != "user"]
        conn.execute("BEGIN IMMEDIATE")
        audit_triggers = _drop_audit_triggers(conn)
        for table in clear_tables:
            safe = '"' + table.replace('"', '""') + '"'
            deleted[table] = int(conn.execute(
                f"SELECT COUNT(*) FROM {safe}").fetchone()[0])
            conn.execute(f"DELETE FROM {safe}")
        if "user" in tables:
            deleted["user_non_admin"] = int(conn.execute(
                'SELECT COUNT(*) FROM "user" WHERE role IS NULL OR role <> ?',
                ("admin",)).fetchone()[0])
            conn.execute('DELETE FROM "user" WHERE role IS NULL OR role <> ?',
                         ("admin",))
        for table in clear_tables:
            try:
                conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
            except sqlite3.OperationalError:
                break
        for trigger_sql in audit_triggers:
            conn.execute(trigger_sql)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()

    evidence_removed = _clear_files(Path(app.config["EVIDENCE_DIR"]).resolve())
    notice_removed = _clear_files(Path(app.config.get("NOTICE_DIR") or
                                       db_path.parent / "notices").resolve())
    return {
        "counts_before": counts_before,
        "deleted": deleted,
        "evidence_files_removed": evidence_removed,
        "notice_files_removed": notice_removed,
        "preserved_tables": sorted(PRESERVED_TABLES),
    }


def clear_with_backup(app, actor: str) -> tuple[dict, dict]:
    """串行执行“先备份、后清空”，避免两个管理员同时清理。"""
    if not _RESET_LOCK.acquire(blocking=False):
        raise DataResetError("已有清理任务正在执行，请稍后刷新页面")
    try:
        backup = create_backup(app, actor, reason="before_clear")
        result = clear_business_data(app)
        return backup, result
    finally:
        _RESET_LOCK.release()


def list_backups(app) -> list[dict]:
    rows = []
    for path in sorted(backup_dir(app).glob("*.zip"), reverse=True):
        if not BACKUP_RE.fullmatch(path.name):
            continue
        try:
            with zipfile.ZipFile(path, "r") as archive:
                manifest = json.loads(archive.read("manifest.json"))
            rows.append({
                "name": path.name,
                "created_at": manifest.get("created_at", ""),
                "actor": manifest.get("actor", ""),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
                "tables": manifest.get("tables", {}),
                "evidence_count": len(manifest.get("files", {}).get("evidence", [])),
                "notice_file_count": len(manifest.get("files", {}).get("notices", [])),
                "download_url": f"/api/admin/data-backups/{path.name}/download",
            })
        except Exception:
            # 损坏/半上传的文件不当成可用备份展示，管理员可以在服务器上清理。
            continue
    return rows


def backup_path(app, name: str) -> Path:
    path = backup_dir(app) / _safe_name(name)
    if not path.is_file():
        raise FileNotFoundError(name)
    return path
