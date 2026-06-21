# AI Presentation Skills

This page documents ClawCross support for AI PPT / slide-deck workflows.

ClawCross does not vendor the upstream projects below. It keeps a curated
feature catalog and generates a ClawCross managed skill that agents can use
through MCP or CLI.

## GitHub Sources

| Source | ClawCross Role |
|---|---|
| [`JimLiu/baoyu-skills/skills/baoyu-slide-deck`](https://github.com/JimLiu/baoyu-skills/tree/main/skills/baoyu-slide-deck) | image-first reading/share deck workflow, outline gate, per-slide prompts, PDF/PPTX merge |
| [`op7418/guizang-ppt-skill`](https://github.com/op7418/guizang-ppt-skill) | opinionated HTML decks, editorial and Swiss layout systems, theme lock, visual QA |
| [`zarazhangrui/frontend-slides`](https://github.com/zarazhangrui/frontend-slides/tree/main) | frontend-native single HTML decks, visual style discovery, PPT-to-web conversion |
| [`alchaincyf/huashu-design`](https://github.com/alchaincyf/huashu-design) | HTML-native design direction, slide/motion/prototype workflow, critique and export loop |
| [`joeseesun/qiaomu-anything-to-notebooklm`](https://github.com/joeseesun/qiaomu-anything-to-notebooklm) | multi-source ingestion and research-pack preparation before deck generation |
| [`hugohe3/ppt-master`](https://github.com/hugohe3/ppt-master/tree/main) | real editable PPTX route with native shapes, notes, template reuse, and local pipeline |
| [`lewislulu/html-ppt-skill`](https://github.com/lewislulu/html-ppt-skill/tree/main) | static HTML PPT studio with theme/layout/template catalog, animations, and presenter mode |

## What ClawCross Adds

- A curated presentation skill catalog in `src/services/presentation_skill_service.py`.
- MCP tools:
  - `presentation_skill_catalog`
  - `presentation_skill_scaffold`
  - `presentation_skill_install`
- CLI helpers:
  - `uv run scripts/cli.py presentation-skill catalog`
  - `uv run scripts/cli.py presentation-skill scaffold --topic "..."`
  - `uv run scripts/cli.py presentation-skill install -u admin`
- A generated managed skill named `ai-presentation-maker` for ClawCross agents.

## Routing Rules

Use the route before building:

| Route | Use When | Strong References |
|---|---|---|
| `research-pack` | many sources must be grounded before slides | Qiaomu, Baoyu |
| `html` | browser-presentable deck is acceptable or preferred | Guizang, Frontend Slides, HTML PPT Skill, Huashu |
| `pptx` | user needs a real editable PowerPoint | PPT Master, Huashu, Baoyu merge |
| `images` | deck is mainly for reading, sharing, or social cards | Baoyu, Guizang |
| `motion` | animation, MP4, or GIF export is part of acceptance | Huashu, HTML PPT Skill |

## MCP Usage

Agents can call:

```text
presentation_skill_catalog(as_json=true)
presentation_skill_scaffold(
  topic="AI robotics project update",
  audience="research team",
  format="html",
  sources="meeting notes and dashboard links",
  style="Swiss grid, technical, low ornament",
  constraints="8 slides, include speaker notes",
  as_json=true
)
presentation_skill_install(username="admin", team="", overwrite=false)
```

`presentation_skill_install` writes a ClawCross managed skill through
`webot.skills`. It does not clone GitHub repositories.

## CLI Usage

```bash
uv run scripts/cli.py presentation-skill catalog

uv run scripts/cli.py presentation-skill scaffold \
  --topic "UMI Image Layered World Model" \
  --audience "robotics research group" \
  --format html \
  --style "technical Swiss grid" \
  --constraints "10 slides, speaker notes"

uv run scripts/cli.py -u admin presentation-skill install
uv run scripts/cli.py -u admin presentation-skill install --team "Research" --overwrite
```

## Managed Skill Contract

The generated `ai-presentation-maker` skill instructs agents to produce:

- `source_manifest.md`
- `deck_brief.md`
- `style_manifest.json`
- `slides.json`
- `deck.html` or `deck.pptx`
- optional `prompts/NN-slide.md`, `deck.pdf`, `cover.png`, `speaker_notes.md`
- `qa_report.md`

## Non-Goals

- No automatic upstream clone or marketplace install.
- No vendored templates/assets/scripts from the listed repositories.
- No promise that HTML decks are native editable PPTX.
- No automatic NotebookLM, image API, or browser automation setup.

Install upstream projects separately when their assets or scripts are required.
