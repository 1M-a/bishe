"""离线验证 ECS 归档下载器的 SQLite 完整性守卫。"""
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ecs_archive_sync import _inspect, _sha256

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "community.db"


class ArchiveSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_snapshot_passes_integrity_and_counts(self):
        target = self.folder / "valid.db"
        source = sqlite3.connect(f"file:{SOURCE.as_posix()}?mode=ro", uri=True)
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
        info = _inspect(target)
        self.assertIn("work_order", info["counts"])
        self.assertEqual(len(_sha256(target)), 64)

    def test_non_database_is_rejected(self):
        target = self.folder / "bad.db"
        target.write_bytes(b"not a sqlite database")
        with self.assertRaises(sqlite3.DatabaseError):
            _inspect(target)

    def test_database_missing_required_tables_is_rejected(self):
        target = self.folder / "incomplete.db"
        conn = sqlite3.connect(target)
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(ValueError, "缺少关键表"):
            _inspect(target)


if __name__ == "__main__":
    unittest.main(verbosity=2)
