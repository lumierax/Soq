"""Offline tests for the mobile importer; never uses an account or network."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import import_archive as importer


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        (self.root / "README.md").write_text("# phone-repo\nInitial description\n")
        self.workflow = self.root / ".github/workflows/import-and-build.yml"
        self.workflow.parent.mkdir(parents=True)
        self.workflow.write_text("user-created-workflow: preserved\n")

    def make_zip(self, additions=()):
        path = self.root / importer.ARCHIVE_NAME
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name in sorted(importer.REQUIRED | {"README.md", "frontend/src/App.tsx"}):
                info = zipfile.ZipInfo("NABD/" + name)
                info.external_attr = (stat.S_IFREG | (0o755 if name.endswith(".sh") else 0o644)) << 16
                archive.writestr(info, "project source: " + name)
            archive.writestr("NABD/.github/workflows/ios-build.yml", "must not import")
            archive.writestr("NABD/.github/workflows/import-and-build.yml", "must not replace")
            for name, payload, mode in additions:
                info = zipfile.ZipInfo(name)
                info.external_attr = mode << 16
                archive.writestr(info, payload)
        return importer.archive_digest(path)

    def run_import(self, digest):
        return importer.import_archive(self.root, digest, repository_name="phone-repo")

    def assert_no_sources_written(self):
        self.assertFalse((self.root / "frontend").exists())
        self.assertFalse((self.root / "backend").exists())
        self.assertFalse((self.root / importer.MARKER_NAME).exists())
        self.assertEqual(self.workflow.read_text(), "user-created-workflow: preserved\n")

    def test_first_import_keeps_workflow_backs_up_scaffold_and_preserves_executable(self):
        digest = self.make_zip()
        result = self.run_import(digest)
        self.assertFalse(result["already_imported"])
        self.assertEqual(self.workflow.read_text(), "user-created-workflow: preserved\n")
        self.assertFalse((self.root / ".github/workflows/ios-build.yml").exists())
        self.assertEqual((self.root / importer.BACKUP_NAME).read_text(), "# phone-repo\nInitial description\n")
        self.assertEqual((self.root / "scripts/build-ios.sh").stat().st_mode & 0o777, 0o755)
        self.assertEqual(json.loads((self.root / importer.MARKER_NAME).read_text())["archive_sha256"], digest)

    def test_second_run_preserves_source_edits(self):
        digest = self.make_zip()
        self.run_import(digest)
        app = self.root / "frontend/src/App.tsx"
        app.write_text("user edited this after import")
        result = self.run_import(digest)
        self.assertTrue(result["already_imported"])
        self.assertEqual(app.read_text(), "user edited this after import")

    def test_hash_mismatch_and_unconfigured_template_fail_before_writes(self):
        self.make_zip()
        for digest in ("0" * 64, "unconfigured"):
            with self.assertRaises(importer.ImportError):
                self.run_import(digest)
            self.assert_no_sources_written()

    def test_existing_source_directory_refused_even_without_matching_filename(self):
        digest = self.make_zip()
        directory = self.root / "frontend"
        directory.mkdir()
        (directory / "existing.txt").write_text("keep me")
        with self.assertRaisesRegex(importer.ImportError, "already contains"):
            self.run_import(digest)
        self.assertEqual((directory / "existing.txt").read_text(), "keep me")
        self.assertFalse((directory / "package.json").exists())
        self.assertFalse((self.root / importer.MARKER_NAME).exists())

    def test_custom_readme_is_not_overwritten(self):
        digest = self.make_zip()
        (self.root / "README.md").write_text("# My existing app\n")
        with self.assertRaisesRegex(importer.ImportError, "not the initial"):
            self.run_import(digest)
        self.assert_no_sources_written()
        self.assertEqual((self.root / "README.md").read_text(), "# My existing app\n")

    def test_traversal_absolute_backslash_and_empty_component_refused(self):
        for name in ("NABD/../escape.txt", "/NABD/escape.txt", "NABD\\escape.txt", "NABD//escape.txt", "OTHER/file.txt"):
            with self.subTest(name=name):
                digest = self.make_zip([(name, "unsafe", stat.S_IFREG | 0o644)])
                with self.assertRaises(importer.ImportError):
                    self.run_import(digest)
                self.assert_no_sources_written()
        self.assertFalse((self.root.parent / "escape.txt").exists())

    def test_archive_symlink_and_special_file_refused(self):
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o644):
            digest = self.make_zip([("NABD/escape", "../outside", mode)])
            with self.assertRaisesRegex(importer.ImportError, "Symlinks"):
                self.run_import(digest)
            self.assert_no_sources_written()

    def test_destination_symlink_refused(self):
        digest = self.make_zip()
        outside = self.root.parent / "outside"
        outside.mkdir()
        (self.root / "frontend").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(importer.ImportError, "symlink"):
            self.run_import(digest)
        self.assertFalse(list(outside.iterdir()))

    def test_git_metadata_and_runtime_credentials_refused(self):
        for path in (".git/config", ".env", "data/bot.sqlite3", "certificate.p12", "secret.pem", ".app-token", ".NABD-MOBILE-IMPORT.JSON", ".NABD-MOBILE-IMPORT-BACKUP/README.md"):
            digest = self.make_zip([("NABD/" + path, "not real credentials", stat.S_IFREG | 0o644)])
            with self.assertRaises(importer.ImportError):
                self.run_import(digest)
            self.assert_no_sources_written()

    def test_case_collision_and_file_directory_collision_refused(self):
        for path in ("Frontend/new.ts", "frontend/package.json/nested.txt"):
            digest = self.make_zip([("NABD/" + path, "collision", stat.S_IFREG | 0o644)])
            with self.assertRaises(importer.ImportError):
                self.run_import(digest)
            self.assert_no_sources_written()

    def test_size_and_entry_limits_are_enforced(self):
        digest = self.make_zip()
        for name, value in (("MAX_ARCHIVE", 8), ("MAX_FILE", 8), ("MAX_TOTAL", 8), ("MAX_ENTRIES", 2)):
            with self.subTest(name=name), patch.object(importer, name, value):
                with self.assertRaises(importer.ImportError):
                    self.run_import(digest)
                self.assert_no_sources_written()

    def test_crc_corruption_refused_before_any_source_write(self):
        self.make_zip()
        path = self.root / importer.ARCHIVE_NAME
        with zipfile.ZipFile(path) as archive:
            item = archive.getinfo("NABD/scripts/build-ios.sh")
            offset = item.header_offset + 30 + len(item.filename.encode()) + len(item.extra)
        data = bytearray(path.read_bytes())
        data[offset] ^= 1
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        with self.assertRaisesRegex(importer.ImportError, "corrupt"):
            self.run_import(digest)
        self.assert_no_sources_written()

    def test_existing_marker_from_other_release_or_incomplete_source_refused(self):
        digest = self.make_zip()
        self.run_import(digest)
        marker = self.root / importer.MARKER_NAME
        original = marker.read_text()
        marker.write_text(json.dumps({"schema": 1, "archive_sha256": "0" * 64, "file_count": 1}))
        with self.assertRaisesRegex(importer.ImportError, "another archive"):
            self.run_import(digest)
        marker.write_text(original)
        (self.root / "scripts/build-ios.sh").unlink()
        with self.assertRaisesRegex(importer.ImportError, "incomplete"):
            self.run_import(digest)

    def test_malformed_marker_is_a_descriptive_failure(self):
        digest = self.make_zip()
        self.run_import(digest)
        marker = self.root / importer.MARKER_NAME
        for invalid in ([], {}, {"schema": 1, "archive_sha256": digest, "file_count": True}):
            marker.write_text(json.dumps(invalid))
            with self.assertRaisesRegex(importer.ImportError, "marker is invalid"):
                self.run_import(digest)

    def test_real_delivered_source_archive_and_rerun(self):
        provided = os.environ.get("NABD_TEST_ARCHIVE")
        if not provided:
            self.skipTest("Set NABD_TEST_ARCHIVE to check the actual delivered source ZIP")
        source = Path(provided)
        target = self.root / importer.ARCHIVE_NAME
        target.write_bytes(source.read_bytes())
        digest = importer.archive_digest(target)
        result = self.run_import(digest)
        self.assertGreater(result["file_count"], 50)
        for relative in importer.REQUIRED:
            self.assertTrue((self.root / relative).is_file())
        with zipfile.ZipFile(source) as archive:
            for relative in importer.REQUIRED:
                self.assertEqual((self.root / relative).read_bytes(), archive.read("NABD/" + relative))
        self.assertEqual(self.workflow.read_text(), "user-created-workflow: preserved\n")
        (self.root / "frontend/src/App.tsx").write_text("keep real-source user edit")
        self.assertTrue(self.run_import(digest)["already_imported"])
        self.assertEqual((self.root / "frontend/src/App.tsx").read_text(), "keep real-source user edit")


if __name__ == "__main__":
    unittest.main()
