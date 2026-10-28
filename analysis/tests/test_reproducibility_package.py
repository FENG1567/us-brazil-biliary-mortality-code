import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from analysis.build_reproducibility_package import build_package


class ReproducibilityPackageTests(unittest.TestCase):
    def test_manifest_archive_and_checksum_cover_same_package_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package"
            package.mkdir()
            source = package / "README.md"
            source.write_text("portable package\n", encoding="utf-8")
            archive = root / "out" / "package.zip"

            file_count, archive_hash, checksum_path = build_package(package, archive)

            manifest = json.loads((package / "package_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(file_count, 1)
            self.assertEqual(manifest["file_count"], 1)
            self.assertEqual(manifest["files"][0]["path"], "README.md")
            self.assertEqual(manifest["files"][0]["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(archive_hash, hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertEqual(checksum_path.read_text(encoding="ascii"), f"{archive_hash}  package.zip\n")

            with zipfile.ZipFile(archive) as contents:
                self.assertEqual(contents.testzip(), None)
                self.assertIn("package/README.md", contents.namelist())
                self.assertIn("package/package_manifest.json", contents.namelist())

    def test_archive_must_be_outside_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "package"
            package.mkdir()
            (package / "README.md").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside the package"):
                build_package(package, package / "package.zip")


if __name__ == "__main__":
    unittest.main()
