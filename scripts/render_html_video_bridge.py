#!/usr/bin/env python3
"""Render html-video HyperFrames templates through upstream HyperFrames.

html-video is the project/template layer. HyperFrames is the deterministic
HTML-to-MP4 renderer. This bridge copies a selected html-video template into a
temporary HyperFrames project, runs HyperFrames check, then renders an MP4.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_HYPERFRAMES_PACKAGE = "hyperframes@0.6.81"


class BridgeError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render an html-video HyperFrames template with upstream HyperFrames."
    )
    parser.add_argument("--html-video-repo", default=None, help="Path to nexu-io/html-video checkout")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--template", help="html-video template id, e.g. frame-bold-signal")
    source.add_argument("--project-id", help="html-video project id under .html-video/projects")
    parser.add_argument("--vars-file", help="JSON variables to substitute into the template")
    parser.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Template variable override. Can be repeated.",
    )
    parser.add_argument("--output", default="data/generated_videos/html-video-bridge.mp4")
    parser.add_argument("--duration", type=float, help="Override data-duration in the entry HTML")
    parser.add_argument("--package", default=DEFAULT_HYPERFRAMES_PACKAGE, help="npx HyperFrames package spec")
    parser.add_argument("--skip-check", action="store_true", help="Skip HyperFrames lint/validate/inspect")
    parser.add_argument("--check-timeout", type=int, default=180, help="Seconds before HyperFrames check is stopped")
    parser.add_argument("--render-timeout", type=int, default=900, help="Seconds before HyperFrames render is stopped")
    parser.add_argument("--keep-workdir", action="store_true", help="Keep generated HyperFrames project")
    return parser.parse_args()


def run(cmd: list[str], cwd: Path | None = None, timeout: int | None = None) -> None:
    print("+", " ".join(cmd), file=sys.stderr)
    subprocess.run(cmd, cwd=cwd, check=True, timeout=timeout)


def resolve_html_video_repo(raw: str | None) -> Path:
    candidates: list[Path] = []
    if raw:
        candidates.append(Path(raw).expanduser())
    if os.environ.get("HTML_VIDEO_REPO"):
        candidates.append(Path(os.environ["HTML_VIDEO_REPO"]).expanduser())
    cwd = Path.cwd()
    candidates.extend(
        [
            cwd / "html-video",
            cwd.parent / "html-video",
            cwd.parent / "ai-video-eval" / "html-video",
            Path("/Users/boris/workspace/ai-video-eval/html-video"),
        ]
    )
    for candidate in candidates:
        if (candidate / "templates").is_dir() and (candidate / "package.json").is_file():
            return candidate.resolve()
    raise BridgeError(
        "Could not find html-video repo. Pass --html-video-repo or set HTML_VIDEO_REPO."
    )


def strip_yaml_scalar(value: str) -> str:
    value = value.split("#", 1)[0].strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def yaml_scalar(text: str, key: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(key)}:\s*(.+?)\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return None
    return strip_yaml_scalar(match.group(1))


def yaml_number(text: str, key: str) -> float | None:
    value = yaml_scalar(text, key)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def load_template_meta(template_dir: Path) -> dict[str, Any]:
    meta_path = template_dir / "template.html-video.yaml"
    if not meta_path.is_file():
        raise BridgeError(f"Template metadata not found: {meta_path}")
    text = meta_path.read_text(encoding="utf-8")
    template_id = yaml_scalar(text, "id") or template_dir.name
    engine = yaml_scalar(text, "engine")
    source_entry = yaml_scalar(text, "source_entry") or "index.html"
    default_duration = yaml_number(text, "default_sec")
    max_duration = yaml_number(text, "max_sec")
    min_duration = yaml_number(text, "min_sec")
    return {
        "id": template_id,
        "engine": engine,
        "source_entry": source_entry,
        "default_duration": default_duration,
        "max_duration": max_duration,
        "min_duration": min_duration,
    }


def find_template_dir(repo: Path, template_id: str) -> Path:
    direct = repo / "templates" / template_id
    if (direct / "template.html-video.yaml").is_file():
        return direct
    for meta_path in (repo / "templates").glob("*/template.html-video.yaml"):
        meta = load_template_meta(meta_path.parent)
        if meta["id"] == template_id:
            return meta_path.parent
    raise BridgeError(f"Template {template_id!r} not found under {repo / 'templates'}")


def load_project(repo: Path, project_id: str) -> dict[str, Any]:
    project_path = repo / ".html-video" / "projects" / project_id / "project.json"
    if not project_path.is_file():
        raise BridgeError(f"Project JSON not found: {project_path}")
    return json.loads(project_path.read_text(encoding="utf-8"))


def parse_vars(args: argparse.Namespace, project: dict[str, Any] | None) -> dict[str, Any]:
    variables: dict[str, Any] = {}
    if project:
        raw_project_vars = project.get("variables")
        if isinstance(raw_project_vars, dict):
            variables.update(raw_project_vars)
    if args.vars_file:
        variables.update(json.loads(Path(args.vars_file).read_text(encoding="utf-8")))
    for item in args.var:
        if "=" not in item:
            raise BridgeError(f"--var must be KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        try:
            variables[key] = json.loads(value)
        except json.JSONDecodeError:
            variables[key] = value
    return variables


def copy_template_payload(template_dir: Path, source_entry: str, project_dir: Path) -> Path:
    skip_names = {"package.json", "template.html-video.yaml", "SKILL.md", "example.md"}
    for child in template_dir.iterdir():
        if child.name in skip_names:
            continue
        dest = project_dir / child.name
        if child.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(child, dest)
        else:
            shutil.copy2(child, dest)

    source_path = template_dir / source_entry
    if not source_path.is_file():
        raise BridgeError(f"Template source_entry not found: {source_path}")

    source_parent = source_path.parent
    if source_parent != template_dir:
        for child in source_parent.iterdir():
            dest = project_dir / child.name
            if child.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(child, dest)
            elif child.name != source_path.name:
                shutil.copy2(child, dest)

    index_path = project_dir / "index.html"
    shutil.copy2(source_path, index_path)
    return index_path


def flatten_for_substitution(value: Any, prefix: str = "") -> dict[str, str]:
    if isinstance(value, dict):
        merged: dict[str, str] = {}
        for key, nested in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            merged.update(flatten_for_substitution(nested, child_prefix))
        return merged
    if isinstance(value, list):
        return {prefix: json.dumps(value, ensure_ascii=False)}
    return {prefix: str(value)}


def apply_variables(text: str, variables: dict[str, Any]) -> str:
    flat: dict[str, str] = {}
    for key, value in variables.items():
        flat.update(flatten_for_substitution(value, str(key)))
    for key, value in flat.items():
        escaped = value
        upper_key = re.sub(r"[^A-Za-z0-9]+", "_", key).upper()
        for token in (f"{{{{{key}}}}}", f"{{{{ {key} }}}}", f"__{upper_key}__"):
            text = text.replace(token, escaped)
    return text


def patch_timed_media(text: str) -> str:
    def add_data_start(match: re.Match[str]) -> str:
        tag = match.group(0)
        if re.search(r"\bdata-start=", tag):
            patched = tag
        else:
            patched = tag[:-1] + ' data-start="0">'
        if patched.lower().startswith("<video") and not re.search(r"\b(muted|data-has-audio=)", patched):
            patched = patched[:-1] + " muted>"
        return patched

    return re.sub(r"<(?:video|audio)\b(?=[^>]*\bsrc=)[^>]*>", add_data_start, text, flags=re.IGNORECASE)


def ensure_hyperframes_contract(text: str, duration: float, width: int = 1920, height: int = 1080) -> str:
    has_composition = "data-composition-id=" in text
    if not has_composition:
        body_match = re.search(r"<body(?P<attrs>[^>]*)>(?P<body>.*)</body>", text, re.IGNORECASE | re.DOTALL)
        if body_match:
            attrs = body_match.group("attrs")
            body = body_match.group("body")
            wrapped = (
                f"<body{attrs}>\n"
                f'<div id="root" data-composition-id="main" data-start="0" '
                f'data-duration="{duration:g}" data-width="{width}" data-height="{height}">\n'
                f"{body}\n"
                "</div>\n"
                "</body>"
            )
            text = text[: body_match.start()] + wrapped + text[body_match.end() :]

    if "data-composition-id=" in text:
        if not re.search(r'data-duration=["\'][^"\']+["\']', text):
            text = re.sub(
                r'(data-composition-id=["\'][^"\']+["\'][^>]*?)',
                rf'\1 data-duration="{duration:g}"',
                text,
                count=1,
            )
        if not re.search(r'data-width=["\'][^"\']+["\']', text):
            text = re.sub(r'(data-composition-id=["\'][^"\']+["\'])', rf'\1 data-width="{width}"', text, count=1)
        if not re.search(r'data-height=["\'][^"\']+["\']', text):
            text = re.sub(r'(data-composition-id=["\'][^"\']+["\'][^>]*?)', rf'\1 data-height="{height}"', text, count=1)

    if "window.__timelines" not in text:
        if "gsap" not in text.lower():
            gsap_script = '<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>\n'
            if "</head>" in text:
                text = text.replace("</head>", f"{gsap_script}</head>", 1)
            else:
                text = f"{gsap_script}{text}"
        timeline_script = (
            "\n<script>\n"
            "  window.__timelines = window.__timelines || {};\n"
            "  if (window.gsap) {\n"
            "    const clawcrossBridgeTimeline = gsap.timeline({ paused: true });\n"
            f"    clawcrossBridgeTimeline.to({{}}, {{ duration: {duration:g} }});\n"
            "    window.__timelines.main = clawcrossBridgeTimeline;\n"
            "  }\n"
            "</script>\n"
        )
        text = text.replace("</body>", f"{timeline_script}</body>", 1)
    return text


def patch_entry_html(
    index_path: Path,
    variables: dict[str, Any],
    duration: float | None,
    width: int = 1920,
    height: int = 1080,
) -> None:
    text = index_path.read_text(encoding="utf-8")
    text = patch_timed_media(text)
    if variables:
        text = apply_variables(text, variables)
    if duration is not None and re.search(r'data-duration=["\'][^"\']+["\']', text):
        duration_text = f'{duration:g}'
        text = re.sub(
            r'data-duration=(["\'])[^"\']+\1',
            lambda m: f"data-duration={m.group(1)}{duration_text}{m.group(1)}",
            text,
            count=1,
        )
    if duration is not None:
        text = ensure_hyperframes_contract(text, duration, width, height)
    index_path.write_text(text, encoding="utf-8")


def patch_supporting_html(path: Path, variables: dict[str, Any]) -> None:
    text = path.read_text(encoding="utf-8")
    text = patch_timed_media(text)
    if variables:
        text = apply_variables(text, variables)
    path.write_text(text, encoding="utf-8")


def make_work_root(output: Path, keep: bool) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    if keep:
        work_root = output.parent / f"{output.stem}.html-video-bridge"
        if work_root.exists():
            shutil.rmtree(work_root)
        work_root.mkdir(parents=True)
        return work_root, None
    tmp_ctx = tempfile.TemporaryDirectory(prefix="clawcross-html-video-bridge-")
    return Path(tmp_ctx.name), tmp_ctx


def main() -> int:
    args = parse_args()
    if shutil.which("npx") is None:
        print("npx is required. Install Node.js/npm first.", file=sys.stderr)
        return 2

    try:
        repo = resolve_html_video_repo(args.html_video_repo)
        project = load_project(repo, args.project_id) if args.project_id else None
        template_id = args.template or project.get("templateId")
        if not template_id:
            raise BridgeError("Project has no templateId; pass --template explicitly.")
        template_dir = find_template_dir(repo, template_id)
        meta = load_template_meta(template_dir)
        if meta["engine"] != "hyperframes":
            raise BridgeError(
                f"Template {template_id!r} uses engine {meta['engine']!r}; "
                "this bridge only renders engine: hyperframes templates."
            )
        variables = parse_vars(args, project)
        duration = args.duration or meta["default_duration"] or meta["max_duration"] or 5.0
        output = Path(args.output).expanduser()
        if not output.is_absolute():
            output = Path.cwd() / output
        output.parent.mkdir(parents=True, exist_ok=True)

        work_root, tmp_ctx = make_work_root(output, args.keep_workdir)
        try:
            project_dir = work_root / "render"
            run(["npx", "-y", args.package, "init", str(project_dir)])
            index_path = copy_template_payload(template_dir, meta["source_entry"], project_dir)
            patch_entry_html(index_path, variables, duration)
            for html_path in project_dir.rglob("*.html"):
                if html_path != index_path:
                    patch_supporting_html(html_path, variables)
            if not args.skip_check:
                run(["npm", "run", "check"], cwd=project_dir, timeout=args.check_timeout)
            run(
                ["npm", "run", "render", "--", "--output", str(output)],
                cwd=project_dir,
                timeout=args.render_timeout,
            )
        finally:
            if tmp_ctx is not None:
                tmp_ctx.cleanup()
        print(str(output))
        return 0
    except BridgeError as exc:
        print(f"render_html_video_bridge: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
