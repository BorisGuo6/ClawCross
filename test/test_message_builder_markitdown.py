import base64
import sys
import types

from src.services.message_builder import build_human_message
from src.services import markitdown_preprocessor as mdsvc


def _install_fake_markitdown(monkeypatch):
    fake = types.ModuleType("markitdown")

    class FakeStreamInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeMarkItDown:
        def __init__(self, enable_plugins=False):
            pass

        def convert_stream(self, stream, stream_info=None):
            name = getattr(stream_info, "filename", "file")
            text = stream.read().decode("utf-8", errors="replace")
            return types.SimpleNamespace(markdown=f"## converted {name}\n{text}")

    fake.MarkItDown = FakeMarkItDown
    fake.StreamInfo = FakeStreamInfo
    monkeypatch.setitem(sys.modules, "markitdown", fake)


def test_build_human_message_normalizes_uploaded_csv_with_markitdown(monkeypatch):
    _install_fake_markitdown(monkeypatch)
    monkeypatch.setenv("MARKITDOWN_ENABLED", "true")
    monkeypatch.setenv("LLM_VISION_SUPPORT", "false")
    payload = base64.b64encode(b"a,b\n1,2\n").decode("ascii")

    msg = build_human_message(
        "read this",
        files=[
            {
                "name": "table.csv",
                "type": "text",
                "content": f"data:text/csv;base64,{payload}",
                "mime_type": "text/csv",
            }
        ],
    )

    assert isinstance(msg.content, str)
    assert "read this" in msg.content
    assert "Markdown 预处理" in msg.content
    assert "## converted table.csv" in msg.content
    assert "a,b" in msg.content


def test_build_human_message_handles_legacy_raw_text_data_uri(monkeypatch):
    monkeypatch.setenv("MARKITDOWN_ENABLED", "true")
    monkeypatch.setenv("LLM_VISION_SUPPORT", "false")
    monkeypatch.setattr(mdsvc, "_markitdown_instance", lambda: (_ for _ in ()).throw(ImportError("missing")))

    msg = build_human_message(
        "",
        files=[
            {
                "name": "notes.txt",
                "type": "text",
                "content": "data:application/octet-stream;base64,legacy raw text",
            }
        ],
    )

    assert "legacy raw text" in msg.content


def test_build_human_message_keeps_pdf_as_direct_file_for_vision(monkeypatch):
    _install_fake_markitdown(monkeypatch)
    monkeypatch.setenv("MARKITDOWN_ENABLED", "true")
    monkeypatch.setenv("LLM_VISION_SUPPORT", "true")
    payload = base64.b64encode(b"%PDF fake").decode("ascii")

    msg = build_human_message(
        "read pdf",
        files=[
            {
                "name": "paper.pdf",
                "type": "pdf",
                "content": f"data:application/pdf;base64,{payload}",
                "mime_type": "application/pdf",
            }
        ],
    )

    assert isinstance(msg.content, list)
    text_part = next(part for part in msg.content if part.get("type") == "text")
    file_part = next(part for part in msg.content if part.get("type") == "file")
    assert "Markdown/文本预处理" in text_part["text"]
    assert "## converted paper.pdf" in text_part["text"]
    assert file_part["file"]["filename"] == "paper.pdf"
