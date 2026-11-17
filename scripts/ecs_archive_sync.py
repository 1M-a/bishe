#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""从 ECS 拉取只读业务库归档，不覆盖本机工作库。

用法：
    python scripts/ecs_archive_sync.py                 # 拉取一次
    python scripts/ecs_archive_sync.py --install-task  # 登录后立即开始，每小时同步
    python scripts/ecs_archive_sync.py --status         # 查看归档状态
    python scripts/ecs_archive_sync.py --archive-dir F:\\bese4-1 --install-task

归档位于 %LOCALAPPDATA%\CommunityAgent\ECSArchive，默认保留 180 天。
文件含真实业务数据，仅允许当前 Windows 用户及系统管理员访问。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import snapshot_apply as sa  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HOST = os.environ.get("ECS_HOST", "47.108.235.113")
USER = os.environ.get("ECS_USER", "Administrator")
KEY = Path(os.environ.get("ECS_KEY", str(Path.home() / ".ssh" / "ecs_community_agent")))
REMOTE_PY = r"C:\Python313\python.exe"
REMOTE_APPLY = r"C:\app\snapshot_apply.py"
REMOTE_EXPORT = r"C:\data-incoming\archive-export.db"
TASK_NAME = "CommunityAgent-ECS-Archive"
ARCHIVE_DIR = Path(os.environ.get(
    "COMMUNITY_ARCHIVE_DIR",
    str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        / "CommunityAgent" / "ECSArchive"),
))
MANIFEST = ARCHIVE_DIR / "manifest.jsonl"
LOCK = ARCHIVE_DIR / ".sync.lock"
RETENTION_DAYS = 180
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL",
            "-o", "ConnectTimeout=30", "-o", "BatchMode=yes"]


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True, timeout=timeout)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding).strip()
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", "replace").strip()


def _ssh(command: str) -> tuple[int, str, str]:
    p = _run(["ssh", "-i", str(KEY), *SSH_OPTS, f"{USER}@{HOST}", command], 180)
    return p.returncode, _decode(p.stdout), _decode(p.stderr)


def _scp(source: str, destination: str) -> tuple[int, str]:
    p = _run(["scp", "-i", str(KEY), *SSH_OPTS, source, destination], 900)
    return p.returncode, _decode(p.stderr)


def _secure_archive_dir() -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        ARCHIVE_DIR.chmod(0o700)
        return
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    p = _run(["icacls", str(ARCHIVE_DIR), "/inheritance:r", "/grant:r",
              f"{user}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"], 30)
    if p.returncode:
        raise RuntimeError("无法限制归档目录权限：" + _decode(p.stderr or p.stdout))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _inspect(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        check = conn.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise ValueError("SQLite quick_check 未通过")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"user", "work_order", "audit_log"}
        missing = required - tables
        if missing:
            raise ValueError("快照缺少关键表：" + ", ".join(sorted(missing)))
        counts = sa.counts(conn)
        return {"counts": counts, "tables": len(tables)}
    finally:
        conn.close()


def _existing_hashes() -> set[str]:
    if not MANIFEST.exists():
        return set()
    hashes: set[str] = set()
    with MANIFEST.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
                if item.get("sha256"):
                    hashes.add(item["sha256"])
            except json.JSONDecodeError:
                continue
    # Recover deduplication if a previous process stopped after renaming the DB
    # but before appending its manifest record.
    for path in ARCHIVE_DIR.glob("ecs-*-*.db"):
        suffix = path.stem.rsplit("-", 1)[-1]
        if len(suffix) == 10:
            hashes.add("prefix:" + suffix)
    return hashes


def _reconcile_manifest() -> None:
    """Index a complete snapshot left behind if a prior run stopped before manifest append."""
    indexed = set()
    if MANIFEST.exists():
        for line in MANIFEST.read_text(encoding="utf-8").splitlines():
            try:
                indexed.add(json.loads(line).get("file"))
            except json.JSONDecodeError:
                continue
    missing = []
    for path in ARCHIVE_DIR.glob("ecs-*.db"):
        if path.name in indexed:
            continue
        digest = _sha256(path)
        info = _inspect(path)
        parts = path.stem.split("-")
        try:
            captured = datetime.strptime("-".join(parts[1:3]), "%Y%m%d-%H%M%S").astimezone()
        except ValueError:
            captured = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        missing.append({
            "captured_at": captured.isoformat(timespec="seconds"),
            "file": path.name,
            "sha256": digest,
            "bytes": path.stat().st_size,
            "counts": info["counts"],
        })
    if missing:
        with MANIFEST.open("a", encoding="utf-8") as f:
            for entry in missing:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _prune() -> None:
    cutoff = datetime.now().astimezone() - timedelta(days=RETENTION_DAYS)
    for path in ARCHIVE_DIR.glob("ecs-*.db"):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            if modified < cutoff:
                path.unlink()
        except OSError:
            pass
    if MANIFEST.exists():
        kept = []
        for line in MANIFEST.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                if datetime.fromisoformat(entry["captured_at"]) >= cutoff:
                    kept.append(json.dumps(entry, ensure_ascii=False))
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
        MANIFEST.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def sync_once() -> int:
    _secure_archive_dir()
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        print("已有同步任务运行，本次跳过。")
        return 0

    temp_path: Path | None = None
    try:
        # A prior interrupted run may leave SQLite sidecars beside its partial download.
        for artifact in ARCHIVE_DIR.glob("ecs-download-*.part*"):
            artifact.unlink(missing_ok=True)
        _reconcile_manifest()
        if not KEY.is_file():
            raise RuntimeError(f"SSH 密钥不存在：{KEY}")
        rc, out, err = _ssh(f'"{REMOTE_PY}" "{REMOTE_APPLY}" --export "{REMOTE_EXPORT}"')
        if rc:
            raise RuntimeError("ECS 生成快照失败：" + (err or out))

        fd, temp_name = tempfile.mkstemp(prefix="ecs-download-", suffix=".part", dir=ARCHIVE_DIR)
        os.close(fd)
        temp_path = Path(temp_name)
        rc, err = _scp(f"{USER}@{HOST}:{REMOTE_EXPORT}", str(temp_path))
        if rc:
            raise RuntimeError("ECS 快照下载失败：" + err)

        info = _inspect(temp_path)
        digest = _sha256(temp_path)
        for suffix in ("-wal", "-shm"):
            Path(str(temp_path) + suffix).unlink(missing_ok=True)
        known = _existing_hashes()
        if digest in known or "prefix:" + digest[:10] in known:
            print("远端数据无变化，未生成重复归档。")
            return 0

        captured = datetime.now().astimezone()
        stamp = captured.strftime("%Y%m%d-%H%M%S")
        final_path = ARCHIVE_DIR / f"ecs-{stamp}-{digest[:10]}.db"
        os.replace(temp_path, final_path)
        temp_path = None
        entry = {
            "captured_at": captured.isoformat(timespec="seconds"),
            "file": final_path.name,
            "sha256": digest,
            "bytes": final_path.stat().st_size,
            "counts": info["counts"],
        }
        with MANIFEST.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _prune()
        print(f"已归档 {final_path.name}（{entry['bytes'] // 1024} KB）")
        print("行数：" + ", ".join(f"{k}={v}" for k, v in info["counts"].items()
                                 if k in ("work_order", "task", "ledger", "audit_log")))
        print(f"归档目录：{ARCHIVE_DIR}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"同步失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)
        LOCK.unlink(missing_ok=True)


def status() -> int:
    _secure_archive_dir()
    files = sorted(ARCHIVE_DIR.glob("ecs-*.db"), key=lambda p: p.stat().st_mtime)
    print(f"归档目录：{ARCHIVE_DIR}")
    print(f"快照数：{len(files)}")
    if files:
        print(f"最早：{files[0].name}")
        print(f"最新：{files[-1].name}")
    print(f"保留期：{RETENTION_DAYS} 天")
    return 0


def install_task() -> int:
    if os.name != "nt":
        print("定时任务只支持 Windows。", file=sys.stderr)
        return 1
    _secure_archive_dir()
    python = Path(sys.executable).resolve()
    script = Path(__file__).resolve()
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    def psquote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    archive_arg = f' --archive-dir "{ARCHIVE_DIR}"'
    command = (
        "$ErrorActionPreference='Stop'; "
        "$a=New-ScheduledTaskAction -Execute " + psquote(str(python))
        + " -Argument " + psquote(f'-X utf8 "{script}"{archive_arg}')
        + " -WorkingDirectory " + psquote(str(ROOT))
        + "; $logon=New-ScheduledTaskTrigger -AtLogOn -User " + psquote(user)
        + "; $hourly=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2)"
        + " -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)"
        + "; $s=New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew"
        + "; $p=New-ScheduledTaskPrincipal -UserId " + psquote(user)
        + " -LogonType Interactive -RunLevel Limited"
        + "; Register-ScheduledTask -TaskName " + psquote(TASK_NAME)
        + " -Action $a -Trigger @($logon,$hourly) -Settings $s -Principal $p -Force | Out-Null"
    )
    encoded = __import__("base64").b64encode(command.encode("utf-16le")).decode("ascii")
    p = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], 60)
    if p.returncode:
        print("创建 Windows 定时任务失败：" + _decode(p.stderr or p.stdout), file=sys.stderr)
        return 1
    print(f"已安装 {TASK_NAME}：当前用户登录后立即同步，此后每小时同步一次。")
    print(f"归档目录：{ARCHIVE_DIR}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-task", action="store_true", help="安装当前用户每小时自动同步任务")
    parser.add_argument("--status", action="store_true", help="查看本机归档状态")
    parser.add_argument(
        "--archive-dir",
        default=None,
        help="本机归档目录；不填则使用 COMMUNITY_ARCHIVE_DIR 或默认目录",
    )
    args = parser.parse_args()
    if args.archive_dir:
        global ARCHIVE_DIR, MANIFEST, LOCK
        ARCHIVE_DIR = Path(args.archive_dir).expanduser()
        MANIFEST = ARCHIVE_DIR / "manifest.jsonl"
        LOCK = ARCHIVE_DIR / ".sync.lock"
    if args.install_task:
        return install_task()
    if args.status:
        return status()
    return sync_once()


if __name__ == "__main__":
    sys.exit(main())
