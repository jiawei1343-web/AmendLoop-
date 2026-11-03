import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("correction_installer", root / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source, self.dest = self.root / "source", self.root / "installed"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("skill v1", encoding="utf-8")
        (self.source / "logic.py").write_text("old logic", encoding="utf-8")
        installer.install(self.source, self.dest)

    def tearDown(self):
        self.tmp.cleanup()

    def upgrade(self, **kwargs):
        return installer.install(self.source, self.dest, upgrade=True, backup_dir=self.root / "backups", **kwargs)

    def test_upgrade_backs_up_original_and_preserves_unmanaged_data(self):
        (self.dest / "personal.json").write_text("user-owned", encoding="utf-8")
        (self.source / "logic.py").write_text("new logic", encoding="utf-8")
        result = self.upgrade()
        self.assertEqual(result["status"], "upgraded")
        self.assertEqual((self.dest / "personal.json").read_text(), "user-owned")
        with zipfile.ZipFile(result["package_backup"]) as z:
            self.assertEqual(z.read("logic.py"), b"old logic")
        self.assertFalse(result["database_modified"])

    def test_user_edit_blocks_upgrade(self):
        (self.source / "logic.py").write_text("new logic")
        (self.dest / "logic.py").write_text("user custom")
        with self.assertRaisesRegex(ValueError, "edited"):
            self.upgrade()
        self.assertEqual((self.dest / "logic.py").read_text(), "user custom")

    def test_new_file_collision_blocks_upgrade(self):
        (self.source / "new.txt").write_text("package")
        (self.dest / "new.txt").write_text("user file")
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            self.upgrade()

    def test_interrupted_replacement_rolls_back(self):
        (self.source / "logic.py").write_text("new logic")
        before = installer.files(self.dest)
        real_replace, calls = installer.os.replace, []
        def fail_second(src, dest):
            calls.append(str(dest))
            if len(calls) == 2:
                raise OSError("simulated write failure")
            return real_replace(src, dest)
        with patch.object(installer.os, "replace", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "simulated"):
                self.upgrade()
        self.assertEqual(installer.files(self.dest), before)

    def test_upgrade_preview_does_not_write(self):
        (self.source / "logic.py").write_text("new logic")
        result = self.upgrade(check_only=True)
        self.assertEqual(result["status"], "ready")
        self.assertEqual((self.dest / "logic.py").read_text(), "old logic")
        self.assertFalse((self.root / "backups").exists())

    def test_source_manifest_tampering_and_path_escape_are_rejected(self):
        (self.source / installer.MANIFEST).write_text(json.dumps(dict(name=installer.NAME, version="0.2.0", files={"wrong": "hash"})))
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.upgrade()
        for path in ("../escape", "C:/escape", "a\\..\\escape", "/absolute"):
            with self.assertRaises(ValueError):
                installer.safe_relative(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
