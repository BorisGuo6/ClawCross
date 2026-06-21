import base64
import sys
import types

from src.services import markitdown_preprocessor as svc


def _install_fake_markitdown(monkeypatch):
    fake = types.ModuleType("markitdown")

    class FakeStreamInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeMarkItDown:
        def __init__(self, enable_plugins=False):
            self.enable_plugins = enable_plugins

        def convert_stream(self, stream, stream_info=None):
            name = getattr(stream_info, "filename", "file")
            text = stream.read().decode("utf-8", errors="replace")
            return types.SimpleNamespace(markdown=f"# {name}\n\n{text}")

    fake.__version__ = "fake"
    fake.MarkItDown = FakeMarkItDown
    fake.StreamInfo = FakeStreamInfo
    monkeypatch.setitem(sys.modules, "markitdown", fake)
    return fake


def test_decode_data_uri_base64_and_mislabeled_text():
    blob, mime = svc.decode_attachment_bytes("data:text/plain;base64,aGVsbG8=")
    assert blob == b"hello"
    assert mime == "text/plain"

    blob, mime = svc.decode_attachment_bytes("data:application/octet-stream;base64,plain text")
    assert blob == b"plain text"
    assert mime == "application/octet-stream"


def test_markitdown_conversion_uses_stream_info(monkeypatch):
    _install_fake_markitdown(monkeypatch)
    monkeypatch.setenv("MARKITDOWN_ENABLED", "true")

    payload = base64.b64encode(b"a,b\n1,2\n").decode("ascii")
    result = svc.convert_uploaded_attachment_to_markdown(
        name="table.csv",
        content=payload,
        mime_type="text/csv",
    )

    assert result.ok
    assert result.source == "markitdown"
    assert "# table.csv" in result.markdown
    assert "a,b" in result.markdown


def test_missing_markitdown_falls_back_to_utf8_text(monkeypatch):
    monkeypatch.setenv("MARKITDOWN_ENABLED", "true")
    monkeypatch.setattr(svc, "_markitdown_instance", lambda: (_ for _ in ()).throw(ImportError("missing")))

    result = svc.convert_uploaded_attachment_to_markdown(
        name="notes.txt",
        content="plain notes",
        mime_type="text/plain",
    )

    assert result.ok
    assert result.source == "text-decode"
    assert result.markdown == "plain notes"


def test_input_size_limit_skips_conversion(monkeypatch):
    _install_fake_markitdown(monkeypatch)
    monkeypatch.setenv("MARKITDOWN_MAX_INPUT_BYTES", "3")

    result = svc.convert_uploaded_attachment_to_markdown(
        name="large.docx",
        content=b"1234",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert not result.ok
    assert result.skipped
    assert "MARKITDOWN_MAX_INPUT_BYTES" in result.error
