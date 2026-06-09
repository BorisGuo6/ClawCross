#!/usr/bin/env python3
"""Render a deterministic ClawCross report/demo video with HyperFrames.

This is intentionally an HTML-to-MP4 renderer, not a diffusion video model.
It is useful for weekly reports, project status cards, and demo summaries where
layout fidelity and repeatability matter more than photorealistic generation.
"""

from __future__ import annotations

import argparse
import html
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from string import Template


DEFAULT_TITLE = "ClawCross can render AI-assisted video reports"
DEFAULT_SUBTITLE = (
    "A coding agent writes HTML motion, Chromium captures frames, "
    "and FFmpeg encodes a normal MP4 artifact."
)
DEFAULT_VERDICT = (
    "Best fit: deterministic reports, demos, TODO recaps, and research summaries."
)


HTML_TEMPLATE = Template(
    """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=$width, height=$height" />
    <title>$title</title>
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
    <style>
      * { box-sizing: border-box; }
      html, body {
        margin: 0;
        width: ${width}px;
        height: ${height}px;
        overflow: hidden;
        background: #10151c;
        color: #f3f0e6;
        font-family: Arial, "Helvetica Neue", sans-serif;
      }
      .stage {
        position: relative;
        width: ${width}px;
        height: ${height}px;
        background:
          radial-gradient(circle at 18% 18%, rgba(91, 214, 202, 0.28), transparent 26%),
          radial-gradient(circle at 83% 19%, rgba(255, 195, 90, 0.20), transparent 28%),
          linear-gradient(135deg, #111922 0%, #17242b 52%, #25191c 100%);
      }
      .grid {
        position: absolute;
        inset: 0;
        opacity: 0.34;
        background-image:
          linear-gradient(rgba(255,255,255,0.055) 1px, transparent 1px),
          linear-gradient(90deg, rgba(255,255,255,0.055) 1px, transparent 1px);
        background-size: 82px 82px;
      }
      .content {
        position: absolute;
        left: 92px;
        right: 92px;
        top: 86px;
        bottom: 72px;
      }
      .kicker {
        display: flex;
        align-items: center;
        gap: 16px;
        color: #9de7dd;
        font-size: 30px;
        opacity: 0;
      }
      .dot {
        width: 18px;
        height: 18px;
        border-radius: 50%;
        background: #64d8cb;
        box-shadow: 0 0 28px rgba(100, 216, 203, 0.9);
      }
      h1 {
        width: 1040px;
        margin: 68px 0 0;
        font-size: 92px;
        line-height: 1.0;
        font-weight: 850;
        letter-spacing: 0;
        opacity: 0;
      }
      h1 span { color: #ffc35a; }
      .subtitle {
        width: 870px;
        margin-top: 36px;
        color: rgba(243, 240, 230, 0.72);
        font-size: 33px;
        line-height: 1.28;
        opacity: 0;
      }
      .panel {
        position: absolute;
        right: 0;
        top: 78px;
        width: 510px;
        padding: 40px;
        border: 1px solid rgba(243, 240, 230, 0.17);
        background: rgba(10, 13, 18, 0.54);
        opacity: 0;
      }
      .panel h2 {
        margin: 0 0 32px;
        color: #ffc35a;
        font-size: 31px;
      }
      .metric { margin-top: 24px; }
      .metric-row {
        display: flex;
        justify-content: space-between;
        margin-bottom: 12px;
        font-size: 24px;
      }
      .track {
        height: 16px;
        border-radius: 18px;
        background: rgba(243, 240, 230, 0.12);
        overflow: hidden;
      }
      .fill {
        height: 100%;
        width: 0;
        border-radius: 18px;
        background: linear-gradient(90deg, #64d8cb, #ffc35a);
      }
      .cards {
        position: absolute;
        left: 0;
        bottom: 82px;
        display: flex;
        gap: 24px;
      }
      .card {
        width: 334px;
        min-height: 166px;
        padding: 24px;
        border: 1px solid rgba(243, 240, 230, 0.17);
        background: rgba(7, 10, 14, 0.62);
        opacity: 0;
      }
      .card strong {
        display: block;
        margin-bottom: 15px;
        color: #f3f0e6;
        font-size: 27px;
        line-height: 1.15;
      }
      .card p {
        margin: 0;
        color: rgba(243, 240, 230, 0.66);
        font-size: 20px;
        line-height: 1.25;
      }
      .verdict {
        position: absolute;
        left: 0;
        right: 0;
        bottom: 0;
        color: #f3f0e6;
        font-size: 38px;
        line-height: 1.18;
        opacity: 0;
      }
      .verdict b { color: #64d8cb; }
      .stamp {
        position: absolute;
        right: 0;
        bottom: 0;
        color: rgba(243, 240, 230, 0.54);
        font-size: 23px;
      }
    </style>
  </head>
  <body>
    <div id="root" class="stage" data-composition-id="main" data-start="0" data-duration="$duration" data-width="$width" data-height="$height">
      <div class="grid"></div>
      <main class="content">
        <div class="kicker"><span class="dot"></span>ClawCross AI video renderer · local test</div>
        <h1>$title_markup</h1>
        <p class="subtitle">$subtitle</p>
        <section class="panel">
          <h2>Observed capability</h2>
          <div class="metric">
            <div class="metric-row"><span>Layout control</span><span>high</span></div>
            <div class="track"><div class="fill" data-width="88%"></div></div>
          </div>
          <div class="metric">
            <div class="metric-row"><span>Photorealism</span><span>low</span></div>
            <div class="track"><div class="fill" data-width="22%"></div></div>
          </div>
          <div class="metric">
            <div class="metric-row"><span>ClawCross fit</span><span>useful</span></div>
            <div class="track"><div class="fill" data-width="78%"></div></div>
          </div>
        </section>
        <section class="cards">
          <article class="card"><strong>1. Write HTML</strong><p>Scene, copy, layout, charts and timing are regular web code.</p></article>
          <article class="card"><strong>2. Capture frames</strong><p>Headless Chromium renders the timeline deterministically.</p></article>
          <article class="card"><strong>3. Encode MP4</strong><p>FFmpeg outputs a normal file for reports and demos.</p></article>
        </section>
        <div class="verdict">Verdict: <b>$verdict</b></div>
        <div class="stamp">Generated by ClawCross · $date</div>
      </main>
    </div>
    <script>
      window.__timelines = window.__timelines || {};
      const tl = gsap.timeline({ paused: true });
      tl.fromTo(".kicker", { opacity: 0, y: 20 }, { opacity: 1, y: 0, duration: 0.55, ease: "power2.out" }, 0.15)
        .fromTo("h1", { opacity: 0, y: 34 }, { opacity: 1, y: 0, duration: 0.75, ease: "power3.out" }, 0.45)
        .fromTo(".subtitle", { opacity: 0, y: 24 }, { opacity: 1, y: 0, duration: 0.65, ease: "power2.out" }, 0.85)
        .fromTo(".panel", { opacity: 0, x: 64 }, { opacity: 1, x: 0, duration: 0.75, ease: "power3.out" }, 1.1)
        .to(".fill", { width: (i, el) => el.dataset.width, duration: 0.9, stagger: 0.12, ease: "power2.out" }, 1.45)
        .fromTo(".card", { opacity: 0, y: 42 }, { opacity: 1, y: 0, duration: 0.55, stagger: 0.14, ease: "power3.out" }, 1.85)
        .fromTo(".verdict", { opacity: 0, y: 36 }, { opacity: 1, y: 0, duration: 0.7, ease: "power3.out" }, 2.75);
      window.__timelines["main"] = tl;
    </script>
  </body>
</html>
"""
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a ClawCross HTML video report.")
    parser.add_argument("--output", default="data/generated_videos/clawcross-ai-video.mp4")
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--subtitle", default=DEFAULT_SUBTITLE)
    parser.add_argument("--verdict", default=DEFAULT_VERDICT)
    parser.add_argument("--duration", type=float, default=6.0)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--package", default="hyperframes@0.6.81", help="npx package spec")
    parser.add_argument("--skip-check", action="store_true", help="Skip hyperframes check")
    parser.add_argument("--keep-workdir", action="store_true", help="Keep the generated HyperFrames project")
    return parser.parse_args()


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(cmd), file=sys.stderr)
    subprocess.run(cmd, cwd=cwd, check=True)


def marked_title(title: str) -> str:
    escaped = html.escape(title)
    for token in ("AI-assisted", "AI video", "ClawCross", "video reports"):
        if token in escaped:
            return escaped.replace(token, f"<span>{token}</span>", 1)
    return escaped


def render_html(args: argparse.Namespace) -> str:
    return HTML_TEMPLATE.substitute(
        width=args.width,
        height=args.height,
        duration=f"{args.duration:g}",
        title=html.escape(args.title),
        title_markup=marked_title(args.title),
        subtitle=html.escape(args.subtitle),
        verdict=html.escape(args.verdict),
        date=html.escape(__import__("datetime").date.today().isoformat()),
    )


def main() -> int:
    args = parse_args()
    if shutil.which("npx") is None:
        print("npx is required. Install Node.js/npm first.", file=sys.stderr)
        return 2

    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = Path.cwd() / output
    output.parent.mkdir(parents=True, exist_ok=True)

    tmp_ctx = None
    if args.keep_workdir:
        work_root = output.parent / f"{output.stem}.hyperframes"
        if work_root.exists():
            shutil.rmtree(work_root)
        work_root.mkdir(parents=True)
    else:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="clawcross-ai-video-")
        work_root = Path(tmp_ctx.name)

    try:
        project_dir = work_root / "render"
        run(["npx", "-y", args.package, "init", str(project_dir)])
        (project_dir / "index.html").write_text(render_html(args), encoding="utf-8")
        if not args.skip_check:
            run(["npm", "run", "check"], cwd=project_dir)
        run(["npm", "run", "render", "--", "--output", str(output)], cwd=project_dir)
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()

    print(str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
