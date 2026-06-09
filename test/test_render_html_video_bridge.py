import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_html_video_bridge.py"
SPEC = importlib.util.spec_from_file_location("render_html_video_bridge", SCRIPT_PATH)
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(bridge)


def test_load_template_meta_reads_core_fields(tmp_path):
    template_dir = tmp_path / "templates" / "frame-test"
    template_dir.mkdir(parents=True)
    (template_dir / "template.html-video.yaml").write_text(
        """
spec_version: 1
id: frame-test
engine: hyperframes
source_entry: source/index.html
output:
  duration:
    default_sec: 6
""",
        encoding="utf-8",
    )

    meta = bridge.load_template_meta(template_dir)

    assert meta["id"] == "frame-test"
    assert meta["engine"] == "hyperframes"
    assert meta["source_entry"] == "source/index.html"
    assert meta["default_duration"] == 6


def test_project_id_selects_template_and_variables(tmp_path):
    repo = tmp_path / "html-video"
    project_dir = repo / ".html-video" / "projects" / "proj_123"
    project_dir.mkdir(parents=True)
    payload = {"templateId": "frame-test", "variables": {"title": "ClawCross"}}
    (project_dir / "project.json").write_text(json.dumps(payload), encoding="utf-8")

    project = bridge.load_project(repo, "proj_123")

    assert project["templateId"] == "frame-test"
    assert project["variables"]["title"] == "ClawCross"


def test_apply_variables_supports_mustache_and_upper_tokens():
    text = "<h1>{{title}}</h1><p>{{ nested.summary }}</p><b>__TITLE__</b>"
    variables = {"title": "ClawCross", "nested": {"summary": "video bridge"}}

    rendered = bridge.apply_variables(text, variables)

    assert "<h1>ClawCross</h1>" in rendered
    assert "<p>video bridge</p>" in rendered
    assert "<b>ClawCross</b>" in rendered


def test_copy_template_payload_promotes_source_entry_to_root_index(tmp_path):
    template_dir = tmp_path / "template"
    (template_dir / "source").mkdir(parents=True)
    (template_dir / "source" / "index.html").write_text("<html>source</html>", encoding="utf-8")
    (template_dir / "source" / "asset.txt").write_text("asset", encoding="utf-8")
    (template_dir / "package.json").write_text("{}", encoding="utf-8")
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    index_path = bridge.copy_template_payload(template_dir, "source/index.html", project_dir)

    assert index_path == project_dir / "index.html"
    assert index_path.read_text(encoding="utf-8") == "<html>source</html>"
    assert (project_dir / "asset.txt").read_text(encoding="utf-8") == "asset"
    assert not (project_dir / "package.json").exists()


def test_ensure_hyperframes_contract_wraps_plain_body():
    text = "<html><head></head><body><main>Hello</main></body></html>"

    patched = bridge.ensure_hyperframes_contract(text, duration=4, width=1280, height=720)

    assert 'data-composition-id="main"' in patched
    assert 'data-duration="4"' in patched
    assert 'data-width="1280"' in patched
    assert 'data-height="720"' in patched
    assert "window.__timelines" in patched


def test_patch_timed_media_adds_missing_data_start():
    text = '<video id="a-roll" src="clip.mp4"></video><audio src="x.mp3" data-start="2"></audio>'

    patched = bridge.patch_timed_media(text)

    assert '<video id="a-roll" src="clip.mp4" data-start="0" muted>' in patched
    assert '<audio src="x.mp3" data-start="2">' in patched
