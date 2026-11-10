"""Reject mismatched or incomplete release packages before they can be published."""
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.release_check import check_release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.project = self.root / "pyproject.toml"
        self.project.write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
        self.wheel = self.root / "local_agent_cli-1.2.3-py3-none-any.whl"
        for name in ["local_agent/__main__.py", "local_agent/tui/app.py", "local_agent/tui/app.tcss"]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def package(self, version="1.2.3", css=True):
        with zipfile.ZipFile(self.wheel, "w") as archive:
            archive.writestr("local_agent_cli-1.2.3.dist-info/METADATA",
                             f"Name: local-agent-cli\nVersion: {version}\n")
            for name in ["local_agent/__main__.py", "local_agent/tui/app.py"]:
                archive.writestr(name, "")
            if css:
                archive.writestr("local_agent/tui/app.tcss", "")

    def test_manifest_matches_validated_wheel(self):
        self.package()
        manifest = check_release(self.project, self.root, "v1.2.3")
        self.assertEqual(manifest.read_text(),
                         f"{hashlib.sha256(self.wheel.read_bytes()).hexdigest()}  {self.wheel.name}\n")

    def test_wrong_tag_does_not_write_a_manifest(self):
        self.package()
        with self.assertRaisesRegex(ValueError, "does not match"):
            check_release(self.project, self.root, "v1.2.4")
        self.assertFalse((self.root / "SHA256SUMS").exists())

    def test_stale_or_missing_wheels_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Expected only"):
            check_release(self.project, self.root)
        self.package()
        (self.root / "stale.whl").write_bytes(b"stale")
        with self.assertRaisesRegex(ValueError, "Expected only"):
            check_release(self.project, self.root)

    def test_wrong_metadata_or_missing_ui_assets_are_rejected(self):
        self.package(version="1.2.2")
        with self.assertRaisesRegex(ValueError, "metadata"):
            check_release(self.project, self.root)
        self.package(css=False)
        with self.assertRaisesRegex(ValueError, "assets"):
            check_release(self.project, self.root)

    def test_stale_build_files_are_rejected(self):
        self.package()
        with zipfile.ZipFile(self.wheel, "a") as archive:
            archive.writestr("local_agent/removed_module.py", "")
        with self.assertRaisesRegex(ValueError, "differ from the source"):
            check_release(self.project, self.root)


if __name__ == "__main__":
    unittest.main()
