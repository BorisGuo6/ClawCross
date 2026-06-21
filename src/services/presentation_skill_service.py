"""Curated AI presentation skill support for ClawCross.

This module does not vendor upstream GitHub skills. It distills their public
workflow ideas into a ClawCross managed skill, plus lightweight catalog and
scaffold helpers that agents can call through MCP or CLI.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

_src_dir = Path(__file__).resolve().parents[1]
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))


DEFAULT_PRESENTATION_SKILL_NAME = "ai-presentation-maker"
DEFAULT_PRESENTATION_CATEGORY = "presentation"


@dataclass(frozen=True)
class PresentationSkillSource:
    id: str
    name: str
    github: str
    role: str
    upstream_format: str
    distilled_features: tuple[str, ...]
    clawcross_usage: str
    caveat: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["distilled_features"] = list(self.distilled_features)
        return payload


PRESENTATION_SKILL_SOURCES: tuple[PresentationSkillSource, ...] = (
    PresentationSkillSource(
        id="baoyu-slide-deck",
        name="Baoyu Slide Deck",
        github="https://github.com/JimLiu/baoyu-skills/tree/main/skills/baoyu-slide-deck",
        role="Image-first reading/share deck workflow",
        upstream_format="Agent skill generating slide images, then optional PDF/PPTX merge",
        distilled_features=(
            "content analysis before slide production",
            "outline review and confirmation gate",
            "per-slide prompt files as reproducibility records",
            "image backend selection and batch rendering",
            "merge generated slides into PDF or PPTX",
        ),
        clawcross_usage=(
            "Use for visual social/share decks when fixed images are acceptable "
            "or when image generation is the main creative step."
        ),
    ),
    PresentationSkillSource(
        id="guizang-ppt-skill",
        name="Guizang PPT Skill",
        github="https://github.com/op7418/guizang-ppt-skill",
        role="Opinionated HTML deck system",
        upstream_format="Single-file HTML deck with editorial and Swiss layout systems",
        distilled_features=(
            "Style A editorial magazine mode and Style B Swiss grid mode",
            "locked layout families and preset themes instead of free-form colors",
            "image prompt workflow for photos, infographics, UI scenes, and covers",
            "multi-platform cover specs",
            "low-performance static mode and layout QA checklist",
        ),
        clawcross_usage=(
            "Use for talks, product analysis, demo day decks, and strong personal "
            "narrative decks where HTML is the primary artifact."
        ),
        caveat="Its core upstream output is HTML, not native editable PPTX.",
    ),
    PresentationSkillSource(
        id="frontend-slides",
        name="Frontend Slides",
        github="https://github.com/zarazhangrui/frontend-slides/tree/main",
        role="Frontend-native web presentation workflow",
        upstream_format="Single HTML presentations and PPT-to-web conversion",
        distilled_features=(
            "zero-dependency single HTML artifact",
            "visual style discovery through generated previews",
            "PPT conversion that preserves text, images, and notes when possible",
            "curated distinctive style presets to avoid generic AI visuals",
            "browser preview plus PDF/share deployment path",
        ),
        clawcross_usage=(
            "Use when a coding agent should produce editable HTML/CSS slides "
            "with a preview-first style selection loop."
        ),
    ),
    PresentationSkillSource(
        id="huashu-design",
        name="Huashu Design",
        github="https://github.com/alchaincyf/huashu-design",
        role="HTML-native design system for prototypes, slides, and motion",
        upstream_format="Agent-agnostic design skill with HTML, export, critique, and motion tools",
        distilled_features=(
            "three-direction visual advisor for fuzzy briefs",
            "HTML deck plus editable PPTX export target",
            "animation/video export mindset for motion-heavy slides",
            "5-dimensional design critique with actionable fixes",
            "design style library and variant comparison loop",
        ),
        clawcross_usage=(
            "Use for high-polish visual direction, design review, motion decks, "
            "and when the deck doubles as an interactive artifact."
        ),
    ),
    PresentationSkillSource(
        id="qiaomu-anything-to-notebooklm",
        name="Qiaomu Anything to NotebookLM",
        github="https://github.com/joeseesun/qiaomu-anything-to-notebooklm",
        role="Multi-source ingestion and research pack creation",
        upstream_format="Source processor for NotebookLM outputs such as PPT, mind map, podcast, and quiz",
        distilled_features=(
            "multi-source ingestion from web, WeChat, YouTube, PDF, Markdown, Office, image, audio, and ZIP",
            "research-question ladder before final synthesis",
            "NotebookLM-style source-grounded brief creation",
            "structured output targets including PPT and mind map",
        ),
        clawcross_usage=(
            "Use before deck generation when the user gives many sources and "
            "needs a grounded research brief rather than immediate slide design."
        ),
    ),
    PresentationSkillSource(
        id="ppt-master",
        name="PPT Master",
        github="https://github.com/hugohe3/ppt-master/tree/main",
        role="Native editable PowerPoint harness",
        upstream_format="Python-based workflow producing native shapes, animations, notes, and narration",
        distilled_features=(
            "real editable PPTX instead of flattened slide images",
            "source-document to deck workflow",
            "reuse an existing PPTX template",
            "speaker notes and optional audio narration",
            "local pipeline with optional image acquisition",
        ),
        clawcross_usage=(
            "Use when the acceptance criterion requires a real PowerPoint file "
            "that people can edit in PowerPoint."
        ),
    ),
    PresentationSkillSource(
        id="html-ppt-skill",
        name="HTML PPT Skill",
        github="https://github.com/lewislulu/html-ppt-skill/tree/main",
        role="Theme-rich static HTML PPT studio",
        upstream_format="Static HTML/CSS/JS deck system with themes, layouts, animations, and presenter mode",
        distilled_features=(
            "theme, full-deck template, and single-page layout catalog",
            "CSS and canvas animation packs",
            "presenter mode with slide preview and speaker script",
            "no-build static HTML delivery",
            "showcase and screenshot verification habit",
        ),
        clawcross_usage=(
            "Use for fast professional HTML decks when theme variety and "
            "presenter-mode ergonomics matter more than native PPTX."
        ),
    ),
)


def presentation_skill_catalog() -> dict[str, Any]:
    """Return the curated upstream skill catalog."""
    return {
        "version": "2026-06-21",
        "policy": (
            "ClawCross distills workflow features from public presentation skills "
            "without vendoring their code. Install upstream repositories separately "
            "when their scripts or assets are required."
        ),
        "sources": [source.to_payload() for source in PRESENTATION_SKILL_SOURCES],
    }


def build_presentation_scaffold(
    topic: str,
    *,
    audience: str = "",
    format: str = "html",
    sources: str = "",
    style: str = "",
    constraints: str = "",
) -> dict[str, Any]:
    """Build a deck-planning scaffold for a ClawCross agent."""
    target_format = (format or "html").strip().lower()
    if target_format not in {"html", "pptx", "images", "research-pack", "motion"}:
        target_format = "html"

    route_by_format = {
        "html": ["guizang-ppt-skill", "frontend-slides", "html-ppt-skill", "huashu-design"],
        "pptx": ["ppt-master", "huashu-design", "baoyu-slide-deck"],
        "images": ["baoyu-slide-deck", "guizang-ppt-skill"],
        "research-pack": ["qiaomu-anything-to-notebooklm", "baoyu-slide-deck"],
        "motion": ["huashu-design", "html-ppt-skill"],
    }
    source_map = {source.id: source for source in PRESENTATION_SKILL_SOURCES}
    recommended = [source_map[item].to_payload() for item in route_by_format[target_format]]

    return {
        "topic": topic.strip(),
        "audience": audience.strip(),
        "format": target_format,
        "sources": sources.strip(),
        "style": style.strip(),
        "constraints": constraints.strip(),
        "recommended_sources": recommended,
        "workflow": [
            {
                "step": "source_intake",
                "output": "source_manifest.md",
                "checks": [
                    "list every provided URL/file/transcript",
                    "separate facts from interpretation",
                    "mark missing permissions or unavailable sources",
                ],
            },
            {
                "step": "message_hierarchy",
                "output": "deck_brief.md",
                "checks": [
                    "one-sentence thesis",
                    "3-5 supporting points",
                    "audience decision or desired action",
                    "objections and proof needed",
                ],
            },
            {
                "step": "style_direction",
                "output": "style_manifest.json",
                "checks": [
                    "choose HTML, PPTX, image deck, research pack, or motion route",
                    "pick 1 primary style and 1 fallback",
                    "record typography, color, density, and media rules",
                ],
            },
            {
                "step": "slide_manifest",
                "output": "slides.json",
                "checks": [
                    "slide title, goal, content bullets, visual intent, notes",
                    "mark charts, diagrams, screenshots, generated images, and citations",
                    "keep one primary message per slide",
                ],
            },
            {
                "step": "artifact_build",
                "output": "deck.html or deck.pptx plus assets/",
                "checks": [
                    "use semantic HTML/CSS when output is HTML",
                    "use native editable shapes when output is PPTX",
                    "save per-slide prompt or source trace when images are generated",
                ],
            },
            {
                "step": "qa",
                "output": "qa_report.md",
                "checks": [
                    "browser preview or PowerPoint open check",
                    "text overflow and contrast check",
                    "source/citation check",
                    "export check for PDF/PPTX/screenshots when requested",
                ],
            },
        ],
        "artifact_schema": {
            "deck_brief": "deck_brief.md",
            "slide_manifest": "slides.json",
            "style_manifest": "style_manifest.json",
            "html_output": "deck.html",
            "pptx_output": "deck.pptx",
            "image_prompts": "prompts/NN-slide.md",
            "exports": ["deck.pdf", "cover.png", "speaker_notes.md"],
            "qa": "qa_report.md",
        },
        "agent_prompt": build_agent_prompt(
            topic,
            audience=audience,
            format=target_format,
            sources=sources,
            style=style,
            constraints=constraints,
        ),
    }


def build_agent_prompt(
    topic: str,
    *,
    audience: str = "",
    format: str = "html",
    sources: str = "",
    style: str = "",
    constraints: str = "",
) -> str:
    """Return a prompt that asks a ClawCross agent to run the deck workflow."""
    lines = [
        "Use the ClawCross AI presentation workflow.",
        f"Topic: {topic.strip() or '[fill topic]'}",
        f"Audience: {audience.strip() or '[infer if not provided]'}",
        f"Target artifact: {format.strip() or 'html'}",
        f"Style preference: {style.strip() or '[choose with preview-first rationale]'}",
        f"Constraints: {constraints.strip() or '[none]'}",
    ]
    if sources.strip():
        lines.append(f"Sources: {sources.strip()}")
    lines.extend(
        [
            "",
            "Workflow:",
            "1. Build source_manifest.md and deck_brief.md before slide production.",
            "2. Choose a route: research pack, HTML deck, native PPTX, image deck, or motion deck.",
            "3. Create slides.json with title, goal, content, visual intent, speaker notes, and citations.",
            "4. Build the artifact, then run visual/export QA and write qa_report.md.",
            "5. Report the output paths and unresolved risks.",
        ]
    )
    return "\n".join(lines)


def build_presentation_skill_markdown() -> str:
    """Generate the ClawCross managed SKILL.md content."""
    catalog_lines: list[str] = []
    for source in PRESENTATION_SKILL_SOURCES:
        catalog_lines.extend(
            [
                f"### {source.name}",
                f"- GitHub: {source.github}",
                f"- Role: {source.role}",
                f"- Use in ClawCross: {source.clawcross_usage}",
            ]
        )
        if source.caveat:
            catalog_lines.append(f"- Caveat: {source.caveat}")
        catalog_lines.append("")

    return f"""---
name: {DEFAULT_PRESENTATION_SKILL_NAME}
description: Research, plan, and build AI-assisted presentation decks using ClawCross presentation workflow patterns.
category: {DEFAULT_PRESENTATION_CATEGORY}
platform: clawcross
---

# AI Presentation Maker

Use this skill when a user asks for PPT, slide deck, keynote, talk slides, report deck, HTML slides, native PPTX, speaker notes, or source-to-presentation work.

## Core Decision

Pick the output route before building:

- `research-pack`: many sources first; create source manifest, synthesis, and slide-ready brief.
- `html`: primary ClawCross default for browser-presentable decks.
- `pptx`: use when the user needs a real editable PowerPoint file.
- `images`: use when the deck is meant for reading, sharing, or social cards.
- `motion`: use when animation or MP4 export is part of the acceptance criteria.

## Workflow

1. Source intake: list every file, URL, transcript, image, and pasted note. Produce `source_manifest.md`.
2. Message hierarchy: write one thesis, 3-5 supporting points, audience decision, proof, and objections in `deck_brief.md`.
3. Route and style: choose HTML/PPTX/images/research-pack/motion, then write `style_manifest.json`.
4. Slide manifest: produce `slides.json` with title, goal, content, visual intent, speaker notes, and citations.
5. Artifact build: create `deck.html`, `deck.pptx`, slide images, or a research pack according to the route.
6. QA: verify text fit, contrast, source grounding, export/open behavior, and write `qa_report.md`.

## Route Rules

- For HTML decks, prefer semantic HTML/CSS, fixed 16:9 slides, keyboard navigation, print/export CSS, and a presenter or speaker-notes path.
- For PPTX decks, keep objects editable as native shapes/text/tables where possible. Do not flatten everything into one image unless the user asked for image slides.
- For image decks, save per-slide prompt files before rendering so the deck can be regenerated.
- For research-heavy decks, do not start slide layout until the source brief is accepted or the user explicitly says to proceed.
- For motion decks, include timing, scenes, export format, and fallback static frames.

## QA Checklist

- The first slide communicates the thesis without needing narration.
- Each slide has one dominant message.
- Long source material is traceable through citations or notes.
- Text does not overflow at the target viewport or exported page size.
- Color contrast is acceptable.
- HTML opens without a build step unless the user approved a build pipeline.
- PPTX opens in PowerPoint/Keynote/LibreOffice when PPTX is requested.
- Generated images have saved prompts and source notes.

## Upstream Inspiration Catalog

ClawCross distills these public skills as workflow references. It does not vendor their code or assets.

{chr(10).join(catalog_lines)}
"""


def install_presentation_skill(
    user_id: str,
    *,
    team: str = "",
    name: str = DEFAULT_PRESENTATION_SKILL_NAME,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Install or update the generated managed skill for a user/team."""
    from webot.skills import create_skill, edit_skill, get_skill

    skill_name = (name or DEFAULT_PRESENTATION_SKILL_NAME).strip().lower()
    content = build_presentation_skill_markdown().replace(
        f"name: {DEFAULT_PRESENTATION_SKILL_NAME}",
        f"name: {skill_name}",
        1,
    )
    existing = get_skill(user_id, name=skill_name, team=team)
    if existing and not overwrite:
        return {
            "success": True,
            "created": False,
            "updated": False,
            "message": f"Skill '{skill_name}' already exists. Pass overwrite=true to update.",
            "path": existing.get("path", ""),
            "team": team,
        }

    if existing and overwrite:
        result = edit_skill(user_id, name=skill_name, content=content, team=team)
        result["created"] = False
        result["updated"] = bool(result.get("success"))
        return result

    result = create_skill(
        user_id,
        name=skill_name,
        content=content,
        category=DEFAULT_PRESENTATION_CATEGORY,
        team=team,
    )
    result["created"] = bool(result.get("success"))
    result["updated"] = False
    return result


def format_presentation_result(payload: dict[str, Any], *, as_json: bool = False) -> str:
    """Format presentation helper payloads for CLI/MCP output."""
    if as_json:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if "workflow" in payload:
        lines = [
            f"Presentation scaffold: {payload.get('topic', '')}",
            f"Format: {payload.get('format', '')}",
            f"Audience: {payload.get('audience', '') or '(infer)'}",
            "",
            "Recommended sources:",
        ]
        for source in payload.get("recommended_sources", []):
            lines.append(f"- {source.get('id')}: {source.get('role')}")
        lines.extend(["", "Agent prompt:", payload.get("agent_prompt", "")])
        return "\n".join(lines).strip()

    if "sources" in payload:
        lines = [
            "AI Presentation Skill Catalog",
            f"Version: {payload.get('version', '')}",
            "",
            payload.get("policy", ""),
            "",
        ]
        for source in payload.get("sources", []):
            lines.append(f"- {source.get('id')}: {source.get('role')}")
            lines.append(f"  GitHub: {source.get('github')}")
            lines.append(f"  ClawCross: {source.get('clawcross_usage')}")
        return "\n".join(lines).strip()

    return json.dumps(payload, ensure_ascii=False, indent=2)


def presentation_research_report() -> dict[str, Any]:
    """Return a compact timestamped report for docs/UI surfaces."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **presentation_skill_catalog(),
    }
