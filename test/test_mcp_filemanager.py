import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import mcp_servers.filemanager as filemanager
from webot.workspace import SessionWorkspace


class FileManagerTests(unittest.TestCase):
    def test_read_file_supports_offset_pagination(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "notes.txt").write_text("abcdefghij", encoding="utf-8")
            workspace = SessionWorkspace(root=root, cwd=root, mode="shared", remote="")
            with patch.object(filemanager, "resolve_session_workspace", return_value=workspace):
                result = asyncio.run(
                    filemanager.read_file("alice", "notes.txt", offset=2, limit=4)
                )
            self.assertIn("cdef", result)
            self.assertIn("offset: 2", result)
            self.assertIn("offset=6", result)

    def test_read_file_supports_line_windows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "lines.txt").write_text("a\nb\nc\nd\n", encoding="utf-8")
            workspace = SessionWorkspace(root=root, cwd=root, mode="shared", remote="")
            with patch.object(filemanager, "resolve_session_workspace", return_value=workspace):
                result = asyncio.run(
                    filemanager.read_file("alice", "lines.txt", start_line=2, line_count=2)
                )
            self.assertIn("行 2-3", result)
            self.assertIn("b\nc\n", result)
            self.assertIn("start_line=4", result)

    def test_write_file_supports_replace_range_and_sha_guard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "draft.txt"
            path.write_text("hello world", encoding="utf-8")
            workspace = SessionWorkspace(root=root, cwd=root, mode="shared", remote="")
            with patch.object(filemanager, "resolve_session_workspace", return_value=workspace):
                sha = filemanager._file_sha256(str(path))
                result = asyncio.run(
                    filemanager.write_file(
                        "alice",
                        "draft.txt",
                        "Claw",
                        mode="replace_range",
                        start=6,
                        end=11,
                        expected_sha256=sha,
                    )
                )
            self.assertIn("已范围替换", result)
            self.assertEqual(path.read_text(encoding="utf-8"), "hello Claw")

    def test_write_file_rejects_mismatched_sha(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "draft.txt"
            path.write_text("hello", encoding="utf-8")
            workspace = SessionWorkspace(root=root, cwd=root, mode="shared", remote="")
            with patch.object(filemanager, "resolve_session_workspace", return_value=workspace):
                result = asyncio.run(
                    filemanager.write_file(
                        "alice",
                        "draft.txt",
                        "x",
                        expected_sha256="bad",
                    )
                )
            self.assertIn("sha256 不匹配", result)
            self.assertEqual(path.read_text(encoding="utf-8"), "hello")


if __name__ == "__main__":
    unittest.main()
