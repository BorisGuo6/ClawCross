#!/usr/bin/env python3
"""Daily arXiv Robotics collision monitor for dashboard projects.

The monitor fetches recent arXiv papers from a subject category, compares each
paper against local dashboard project/task text, and records potential research
collisions as reports and optional harness reminders.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Iterable
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from utils.runtime_paths import DATA_DIR, ENV_FILE


ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
DEFAULT_CATEGORY = "cs.RO"
ARXIV_RSS_URL = "https://rss.arxiv.org/rss/{category}"
DEFAULT_MAX_RESULTS = 200
DEFAULT_LOOKBACK_HOURS = 36
DEFAULT_THRESHOLD = 0.16
DEFAULT_REPORT_DIR = DATA_DIR / "arxiv_collision" / "reports"
DEFAULT_STATE_PATH = DATA_DIR / "arxiv_collision" / "state.json"
DEFAULT_PROJECT_ID = "arxiv-collision-monitor"
DEFAULT_AGENT_ID = "arxiv-collision-agent"
STATE_SCHEMA_VERSION = "clawcross_arxiv_collision.v1"

SHORT_TERMS = {
    "ai",
    "3d",
    "rl",
    "vla",
    "vln",
    "vlm",
    "wm",
    "umi",
    "vr",
    "ar",
    "ros",
    "sim",
    "irl",
    "eef",
    "dof",
}

STOPWORDS = {
    "about",
    "above",
    "across",
    "after",
    "again",
    "against",
    "also",
    "among",
    "and",
    "are",
    "around",
    "based",
    "been",
    "before",
    "being",
    "between",
    "both",
    "but",
    "can",
    "could",
    "data",
    "demo",
    "does",
    "done",
    "each",
    "for",
    "from",
    "have",
    "into",
    "its",
    "may",
    "more",
    "most",
    "new",
    "not",
    "now",
    "one",
    "only",
    "our",
    "over",
    "paper",
    "project",
    "research",
    "should",
    "show",
    "such",
    "task",
    "than",
    "that",
    "the",
    "their",
    "then",
    "there",
    "these",
    "this",
    "through",
    "todo",
    "using",
    "very",
    "was",
    "were",
    "when",
    "where",
    "which",
    "while",
    "with",
    "within",
    "without",
    "work",
    "world",
    "would",
}


def load_env(env_path: Path = ENV_FILE) -> None:
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


load_env()


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def canonical_arxiv_id(value: str) -> str:
    raw = str(value or "").strip().rsplit("/", 1)[-1]
    return re.sub(r"v\d+$", "", raw)


def parse_iso_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def submitted_date_value(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M")


def date_window_utc(date_text: str) -> tuple[datetime, datetime]:
    try:
        target = datetime.strptime(date_text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("--date must use YYYY-MM-DD") from exc
    start = datetime(target.year, target.month, target.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1) - timedelta(minutes=1)
    return start, end


@dataclass
class ArxivPaper:
    title: str
    summary: str
    authors: list[str]
    published: str
    updated: str
    arxiv_id: str
    url: str
    pdf_url: str
    categories: list[str]
    primary_category: str = ""

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.summary}\n{' '.join(self.categories)}"


@dataclass
class DashboardProject:
    project_id: str
    title: str
    bucket: str = ""
    status: str = ""
    text: str = ""
    source_path: str = ""
    token_counts: Counter[str] = field(default_factory=Counter)
    phrases: set[str] = field(default_factory=set)


@dataclass
class Collision:
    paper: ArxivPaper
    project: DashboardProject
    score: float
    overlap_terms: list[str]
    overlap_phrases: list[str]
    is_new: bool = True

    @property
    def collision_id(self) -> str:
        paper_id = self.paper.arxiv_id or canonical_slug(self.paper.title)[:80]
        return f"{paper_id}::{self.project.project_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "collision_id": self.collision_id,
            "score": round(self.score, 4),
            "overlap_terms": self.overlap_terms,
            "overlap_phrases": self.overlap_phrases,
            "is_new": self.is_new,
            "paper": {
                "title": self.paper.title,
                "arxiv_id": self.paper.arxiv_id,
                "url": self.paper.url,
                "pdf_url": self.paper.pdf_url,
                "published": self.paper.published,
                "updated": self.paper.updated,
                "categories": self.paper.categories,
                "primary_category": self.paper.primary_category,
                "authors": self.paper.authors,
            },
            "project": {
                "project_id": self.project.project_id,
                "title": self.project.title,
                "bucket": self.project.bucket,
                "status": self.project.status,
                "source_path": self.project.source_path,
            },
        }


def _entry_text(entry: ET.Element, path: str) -> str:
    element = entry.find(path, ARXIV_NS)
    return normalize_space(element.text if element is not None else "")


def parse_arxiv_atom(xml_bytes: bytes | str) -> list[ArxivPaper]:
    data = xml_bytes.encode("utf-8") if isinstance(xml_bytes, str) else xml_bytes
    root = ET.fromstring(data)
    papers: list[ArxivPaper] = []
    for entry in root.findall("atom:entry", ARXIV_NS):
        title = _entry_text(entry, "atom:title")
        if not title:
            continue
        summary = _entry_text(entry, "atom:summary")
        published = _entry_text(entry, "atom:published")
        updated = _entry_text(entry, "atom:updated")
        raw_url = _entry_text(entry, "atom:id")
        arxiv_id = canonical_arxiv_id(raw_url)
        categories = [
            str(cat.attrib.get("term") or "").strip()
            for cat in entry.findall("atom:category", ARXIV_NS)
            if str(cat.attrib.get("term") or "").strip()
        ]
        primary_el = entry.find("arxiv:primary_category", ARXIV_NS)
        primary_category = str(primary_el.attrib.get("term") or "").strip() if primary_el is not None else ""
        authors = []
        for author_el in entry.findall("atom:author", ARXIV_NS):
            name = _entry_text(author_el, "atom:name")
            if name:
                authors.append(name)
        pdf_url = ""
        alternate_url = raw_url
        for link in entry.findall("atom:link", ARXIV_NS):
            href = str(link.attrib.get("href") or "").strip()
            if not href:
                continue
            if link.attrib.get("title") == "pdf":
                pdf_url = href if href.endswith(".pdf") else f"{href}.pdf"
            elif link.attrib.get("rel") == "alternate":
                alternate_url = href
        if arxiv_id and not pdf_url:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        papers.append(
            ArxivPaper(
                title=title,
                summary=summary,
                authors=authors,
                published=published,
                updated=updated,
                arxiv_id=arxiv_id,
                url=alternate_url or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""),
                pdf_url=pdf_url,
                categories=categories,
                primary_category=primary_category,
            )
        )
    return papers


def strip_markup(text: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", str(text or ""))
    return normalize_space(no_tags)


def parse_rss_datetime(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_arxiv_rss(
    *,
    category: str = DEFAULT_CATEGORY,
    start_dt: datetime,
    end_dt: datetime,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout: int = 30,
) -> list[ArxivPaper]:
    """Fetch the category RSS feed as a fallback when the API is unavailable."""
    url = ARXIV_RSS_URL.format(category=urllib.parse.quote(category))
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ClawCross arxiv-collision-agent/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"arXiv RSS unreachable: {exc}") from exc
    root = ET.fromstring(raw)
    papers: list[ArxivPaper] = []
    for item in root.findall("./channel/item"):
        title = normalize_space(item.findtext("title") or "")
        link = normalize_space(item.findtext("link") or "")
        description = strip_markup(item.findtext("description") or "")
        pub_date = normalize_space(item.findtext("pubDate") or "")
        pub_dt = parse_rss_datetime(pub_date)
        if pub_dt is not None and (pub_dt < start_dt.astimezone(timezone.utc) or pub_dt > end_dt.astimezone(timezone.utc)):
            continue
        creator = normalize_space(item.findtext("{http://purl.org/dc/elements/1.1/}creator") or "")
        authors = [part.strip() for part in re.split(r",|\band\b", creator) if part.strip()]
        arxiv_id = canonical_arxiv_id(link or title)
        match = re.search(r"(\d{4}\.\d{4,5})(?:v\d+)?", title + " " + link)
        if match:
            arxiv_id = match.group(1)
        if not link and arxiv_id:
            link = f"https://arxiv.org/abs/{arxiv_id}"
        if not title:
            continue
        papers.append(
            ArxivPaper(
                title=title,
                summary=description,
                authors=authors,
                published=pub_dt.isoformat() if pub_dt is not None else pub_date,
                updated=pub_dt.isoformat() if pub_dt is not None else pub_date,
                arxiv_id=arxiv_id,
                url=link,
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}.pdf" if arxiv_id else "",
                categories=[category],
                primary_category=category,
            )
        )
        if len(papers) >= max_results:
            break
    return papers


def fetch_arxiv_papers(
    *,
    category: str = DEFAULT_CATEGORY,
    start_dt: datetime,
    end_dt: datetime,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout: int = 30,
    retries: int = 3,
    batch_size: int = 100,
    allow_rss_fallback: bool = True,
) -> list[ArxivPaper]:
    search_query = (
        f"cat:{category} AND "
        f"submittedDate:[{submitted_date_value(start_dt)} TO {submitted_date_value(end_dt)}]"
    )
    papers: list[ArxivPaper] = []
    seen: set[str] = set()
    batch_size = max(1, min(2000, int(batch_size)))
    for start in range(0, max(0, int(max_results)), batch_size):
        params = urllib.parse.urlencode(
            {
                "search_query": search_query,
                "start": start,
                "max_results": min(batch_size, max_results - start),
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        request = urllib.request.Request(
            f"{ARXIV_API_URL}?{params}",
            headers={"User-Agent": "ClawCross arxiv-collision-agent/1.0"},
        )
        raw = b""
        last_error: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read()
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                message = RuntimeError(f"arXiv API failed: HTTP {exc.code} {body[:500]}")
                if exc.code in {429, 502, 503, 504}:
                    last_error = message
                    if attempt < max(1, retries) - 1:
                        time.sleep(3 * (attempt + 1))
                        continue
                    break
                raise message from exc
            except (TimeoutError, urllib.error.URLError, OSError) as exc:
                last_error = exc
                if attempt < max(1, retries) - 1:
                    time.sleep(3 * (attempt + 1))
                    continue
        if not raw:
            if allow_rss_fallback:
                return fetch_arxiv_rss(
                    category=category,
                    start_dt=start_dt,
                    end_dt=end_dt,
                    max_results=max_results,
                    timeout=timeout,
                )
            raise RuntimeError(f"arXiv API unreachable after {retries} attempts: {last_error}") from last_error

        if b"Rate exceeded" in raw:
            if allow_rss_fallback:
                return fetch_arxiv_rss(
                    category=category,
                    start_dt=start_dt,
                    end_dt=end_dt,
                    max_results=max_results,
                    timeout=timeout,
                )
            raise RuntimeError("arXiv API rate limit exceeded; retry later.")
        entries = parse_arxiv_atom(raw)
        if not entries:
            break
        for paper in entries:
            key = paper.arxiv_id or paper.url or paper.title
            if key in seen:
                continue
            seen.add(key)
            papers.append(paper)
        if len(entries) < batch_size or len(papers) >= max_results:
            break
        time.sleep(3)
    return papers[:max_results]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(as_text(item) for item in value)
    if isinstance(value, dict):
        preferred = []
        for key in ("title", "name", "summary", "description", "notes", "url", "arxiv_id", "body"):
            if key in value:
                preferred.append(as_text(value[key]))
        if preferred:
            return "\n".join(preferred)
        return "\n".join(as_text(item) for item in value.values())
    return str(value)


def default_dashboard_root() -> Path:
    for key in ("ARXIV_COLLISION_DASHBOARD_ROOT", "CLAWCROSS_DASHBOARD_ROOT", "DASHBOARD_ROOT"):
        value = os.getenv(key, "").strip()
        if value:
            return Path(value).expanduser()
    raise RuntimeError("dashboard root is not configured; pass --dashboard-root or set ARXIV_COLLISION_DASHBOARD_ROOT")


def _project_state_path(dashboard_root: Path, project: dict[str, Any]) -> Path | None:
    raw = str(project.get("state_path") or "").strip()
    if not raw:
        project_id = str(project.get("project_id") or "").strip()
        return dashboard_root / "state" / "projects" / f"{project_id}.json" if project_id else None
    candidate = dashboard_root.parent / raw
    if candidate.exists():
        return candidate
    return dashboard_root / raw


def load_dashboard_projects(
    dashboard_root: Path,
    *,
    include_archived: bool = True,
    open_tasks_only: bool = False,
) -> list[DashboardProject]:
    dashboard_root = dashboard_root.expanduser()
    portfolio_path = dashboard_root / "state" / "portfolio.json"
    tasks_path = dashboard_root / "state" / "tasks.json"
    if not portfolio_path.exists():
        raise FileNotFoundError(f"portfolio not found: {portfolio_path}")
    portfolio = load_json(portfolio_path)
    raw_tasks = load_json(tasks_path).get("tasks", []) if tasks_path.exists() else []
    tasks_by_project: dict[str, list[dict[str, Any]]] = {}
    for task in raw_tasks if isinstance(raw_tasks, list) else []:
        if not isinstance(task, dict):
            continue
        project_id = str(task.get("project_id") or "").strip()
        if not project_id:
            continue
        if open_tasks_only and str(task.get("status") or "").strip().lower() == "done":
            continue
        tasks_by_project.setdefault(project_id, []).append(task)

    projects: list[DashboardProject] = []
    for project in portfolio.get("projects", []) or []:
        if not isinstance(project, dict):
            continue
        project_id = str(project.get("project_id") or "").strip()
        if not project_id:
            continue
        bucket = str(project.get("bucket") or "").strip()
        status = str(project.get("status") or "").strip()
        if not include_archived and (bucket == "archive" or status == "archived"):
            continue
        state_path = _project_state_path(dashboard_root, project)
        project_doc: dict[str, Any] = {}
        if state_path and state_path.exists():
            project_doc = load_json(state_path)
        title = str(project_doc.get("title") or project.get("title") or project.get("name") or project_id)
        text_parts = [
            title,
            as_text(project_doc.get("description")),
            as_text(project_doc.get("summary")),
            as_text(project_doc.get("details")),
            as_text(project_doc.get("references")),
            as_text(project_doc.get("risks_decisions")),
            as_text(project_doc.get("timeline")),
        ]
        for task in tasks_by_project.get(project_id, []):
            text_parts.extend(
                [
                    as_text(task.get("title")),
                    as_text(task.get("description")),
                    as_text(task.get("result")),
                ]
            )
            if str(task.get("status") or "").strip().lower() != "done":
                text_parts.append(as_text(task.get("comments")))
        text = normalize_space("\n".join(part for part in text_parts if part))
        token_counts = keyword_counts(text)
        projects.append(
            DashboardProject(
                project_id=project_id,
                title=title,
                bucket=bucket,
                status=status,
                text=text,
                source_path=str(state_path or portfolio_path),
                token_counts=token_counts,
                phrases=keyword_phrases(token_counts),
            )
        )
    return projects


def canonical_slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return slug or "item"


def stem_token(token: str) -> str:
    if len(token) > 5 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if len(token) > 6 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 5 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    lowered = str(text or "").lower()
    lowered = re.sub(r"([a-z])([0-9])", r"\1 \2", lowered)
    lowered = re.sub(r"([0-9])([a-z])", r"\1 \2", lowered)
    tokens = []
    for raw in re.findall(r"[a-z0-9][a-z0-9-]*", lowered):
        token = stem_token(raw.strip("-"))
        if not token or token in STOPWORDS:
            continue
        if len(token) < 3 and token not in SHORT_TERMS:
            continue
        tokens.append(token)
    return tokens


def keyword_counts(text: str) -> Counter[str]:
    return Counter(tokenize(text))


def keyword_phrases(counts: Counter[str]) -> set[str]:
    tokens = [token for token, _ in counts.most_common(120)]
    phrases = set()
    for size in (2, 3):
        for idx in range(0, max(0, len(tokens) - size + 1)):
            phrases.add(" ".join(tokens[idx : idx + size]))
    return phrases


def build_idf(projects: list[DashboardProject], papers: list[ArxivPaper]) -> dict[str, float]:
    docs: list[set[str]] = [set(project.token_counts) for project in projects]
    docs.extend(set(keyword_counts(paper.text)) for paper in papers)
    doc_count = max(1, len(docs))
    df: Counter[str] = Counter()
    for doc in docs:
        df.update(doc)
    return {term: math.log((doc_count + 1) / (count + 1)) + 1.0 for term, count in df.items()}


def weighted_total(terms: Iterable[str], idf: dict[str, float], cap: int | None = None) -> float:
    selected = list(terms)
    if cap is not None:
        selected = selected[:cap]
    return sum(idf.get(term, 1.0) for term in selected)


def score_project_paper(
    project: DashboardProject,
    paper: ArxivPaper,
    idf: dict[str, float],
) -> tuple[float, list[str], list[str]]:
    paper_counts = keyword_counts(paper.text)
    paper_terms = set(paper_counts)
    project_terms = set(project.token_counts)
    overlap = paper_terms & project_terms
    if not overlap:
        return 0.0, [], []

    overlap_ranked = sorted(overlap, key=lambda term: (idf.get(term, 1.0), paper_counts[term], project.token_counts[term]), reverse=True)
    overlap_weight = weighted_total(overlap_ranked, idf)
    paper_weight = weighted_total(paper_terms, idf)
    project_top_terms = [term for term, _ in project.token_counts.most_common(100)]
    project_weight = weighted_total(project_top_terms, idf) or 1.0
    paper_coverage = overlap_weight / max(1.0, paper_weight)
    project_coverage = overlap_weight / max(1.0, project_weight)

    paper_phrase_terms = keyword_counts(paper.title + " " + paper.summary[:800])
    paper_phrases = keyword_phrases(paper_phrase_terms)
    phrase_overlap = sorted(project.phrases & paper_phrases)[:8]

    title_terms = set(tokenize(paper.title))
    title_hits = title_terms & project_terms
    phrase_boost = min(0.12, 0.035 * len(phrase_overlap))
    title_boost = min(0.08, 0.012 * len(title_hits))
    score = min(1.0, 0.68 * paper_coverage + 0.32 * project_coverage + phrase_boost + title_boost)
    return score, overlap_ranked[:14], phrase_overlap


def find_collisions(
    papers: list[ArxivPaper],
    projects: list[DashboardProject],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_matches_per_paper: int = 3,
) -> list[Collision]:
    idf = build_idf(projects, papers)
    collisions: list[Collision] = []
    for paper in papers:
        matches: list[Collision] = []
        for project in projects:
            score, overlap_terms, overlap_phrases = score_project_paper(project, paper, idf)
            if score < threshold:
                continue
            if len(overlap_terms) < 4 and not overlap_phrases:
                continue
            matches.append(Collision(paper=paper, project=project, score=score, overlap_terms=overlap_terms, overlap_phrases=overlap_phrases))
        matches.sort(key=lambda item: item.score, reverse=True)
        collisions.extend(matches[:max_matches_per_paper])
    collisions.sort(key=lambda item: item.score, reverse=True)
    return collisions


def load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {"schema_version": STATE_SCHEMA_VERSION, "seen": {}, "runs": []}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": STATE_SCHEMA_VERSION, "seen": {}, "runs": []}
    if not isinstance(data, dict):
        return {"schema_version": STATE_SCHEMA_VERSION, "seen": {}, "runs": []}
    data.setdefault("schema_version", STATE_SCHEMA_VERSION)
    data.setdefault("seen", {})
    data.setdefault("runs", [])
    return data


def update_collision_state(
    collisions: list[Collision],
    *,
    state_path: Path,
    dedupe: bool = True,
) -> tuple[list[Collision], dict[str, Any]]:
    state = load_state(state_path)
    seen = state.setdefault("seen", {})
    now = utc_now_iso()
    new_collisions: list[Collision] = []
    for collision in collisions:
        previous = seen.get(collision.collision_id)
        collision.is_new = not bool(previous)
        if collision.is_new or not dedupe:
            new_collisions.append(collision)
        if collision.is_new:
            seen[collision.collision_id] = {
                "first_seen_at": now,
                "paper_title": collision.paper.title,
                "paper_url": collision.paper.url,
                "project_id": collision.project.project_id,
                "project_title": collision.project.title,
                "score": round(collision.score, 4),
            }
    state.setdefault("runs", []).append(
        {
            "run_at": now,
            "collision_count": len(collisions),
            "new_collision_count": len(new_collisions),
        }
    )
    state["runs"] = state["runs"][-100:]
    state["updated_at"] = now
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return new_collisions, state


def collision_summary_line(collision: Collision) -> str:
    paper = collision.paper
    project = collision.project
    terms = ", ".join(collision.overlap_terms[:8])
    phrases = "; ".join(collision.overlap_phrases[:3])
    reason = terms if not phrases else f"{terms}; phrases: {phrases}"
    return (
        f"- score={collision.score:.3f} [{project.bucket}/{project.project_id}] {project.title}\n"
        f"  Paper: {paper.title} ({paper.url})\n"
        f"  Overlap: {reason}"
    )


def render_report(
    *,
    start_dt: datetime,
    end_dt: datetime,
    category: str,
    papers: list[ArxivPaper],
    projects: list[DashboardProject],
    collisions: list[Collision],
    new_collisions: list[Collision],
    threshold: float,
) -> str:
    lines = [
        "# arXiv Robotics Collision Report",
        "",
        f"- Window UTC: {start_dt.isoformat()} -> {end_dt.isoformat()}",
        f"- Category: {category}",
        f"- Papers fetched: {len(papers)}",
        f"- Dashboard projects compared: {len(projects)}",
        f"- Threshold: {threshold:.3f}",
        f"- Collisions: {len(collisions)} total, {len(new_collisions)} new",
        "",
    ]
    if not collisions:
        lines.append("No collisions detected.")
        return "\n".join(lines) + "\n"

    if new_collisions:
        lines.append("## New Collisions")
        lines.append("")
        lines.extend(collision_summary_line(item) for item in new_collisions)
        lines.append("")
    repeated = [item for item in collisions if not item.is_new]
    if repeated:
        lines.append("## Previously Seen")
        lines.append("")
        lines.extend(collision_summary_line(item) for item in repeated[:20])
        if len(repeated) > 20:
            lines.append(f"- ... {len(repeated) - 20} more previously seen collisions omitted")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_report(report_dir: Path, report: str, run_label: str | None = None) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    label = run_label or utc_now().strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"{label}.md"
    path.write_text(report, encoding="utf-8")
    latest = report_dir / "latest.md"
    latest.write_text(report, encoding="utf-8")
    return path


def notify_harness(
    *,
    user_id: str,
    report: str,
    report_path: Path,
    new_collisions: list[Collision],
    all_collisions: list[Collision],
) -> dict[str, Any]:
    from harness.store import apply_harness_event

    now_date = utc_now().strftime("%Y%m%d")
    task_id = f"arxiv_collision_{now_date}"
    status = "needs_user" if new_collisions else "done"
    title = f"arXiv Robotics collision check {now_date}"
    description = (
        "Daily arXiv cs.RO monitor comparing new Robotics papers against dashboard projects. "
        f"Report: {report_path}"
    )
    apply_harness_event(
        user_id,
        {
            "action": "project_upsert",
            "project_id": DEFAULT_PROJECT_ID,
            "project_title": "arXiv Robotics Collision Monitor",
            "project_summary": "Daily arXiv Robotics papers vs dashboard project collision alerts.",
            "metadata": {"monitor": {"category": DEFAULT_CATEGORY}},
        },
    )
    apply_harness_event(
        user_id,
        {
            "action": "task_upsert",
            "project_id": DEFAULT_PROJECT_ID,
            "project_title": "arXiv Robotics Collision Monitor",
            "task_id": task_id,
            "title": title,
            "description": description,
            "status": status,
            "priority": "high" if new_collisions else "normal",
            "assignee": user_id,
            "metadata": {
                "arxiv_collision": {
                    "report_path": str(report_path),
                    "new_collision_count": len(new_collisions),
                    "collision_count": len(all_collisions),
                }
            },
        },
    )
    comment_body = report
    if len(comment_body) > 12000:
        comment_body = comment_body[:11800] + f"\n\n[truncated; full report: {report_path}]\n"
    apply_harness_event(
        user_id,
        {
            "action": "task_comment",
            "agent_id": DEFAULT_AGENT_ID,
            "project_id": DEFAULT_PROJECT_ID,
            "task_id": task_id,
            "message": comment_body,
            "kind": "needs_user" if new_collisions else "result",
        },
    )
    return {"task_id": task_id, "status": status, "new_collision_count": len(new_collisions)}


def maybe_sync_dashboard(
    *,
    user_id: str,
    dashboard_root: Path,
) -> dict[str, Any]:
    from harness.dashboard_sync import sync_harness_to_dashboard

    return sync_harness_to_dashboard(
        user_id,
        dashboard_root=dashboard_root,
        project_id=DEFAULT_PROJECT_ID,
        create_missing=True,
    )


def run_collision_check(
    *,
    dashboard_root: Path,
    category: str = DEFAULT_CATEGORY,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    max_results: int = DEFAULT_MAX_RESULTS,
    request_timeout: int = 60,
    threshold: float = DEFAULT_THRESHOLD,
    include_archived: bool = True,
    open_tasks_only: bool = False,
    state_path: Path = DEFAULT_STATE_PATH,
    report_dir: Path = DEFAULT_REPORT_DIR,
    dedupe: bool = True,
    notify: bool = False,
    user_id: str = "default",
    sync_dashboard: bool = False,
) -> dict[str, Any]:
    if end_dt is None:
        end_dt = utc_now()
    if start_dt is None:
        start_dt = end_dt - timedelta(hours=max(1, int(lookback_hours)))
    papers = fetch_arxiv_papers(
        category=category,
        start_dt=start_dt,
        end_dt=end_dt,
        max_results=max_results,
        timeout=request_timeout,
    )
    projects = load_dashboard_projects(
        dashboard_root,
        include_archived=include_archived,
        open_tasks_only=open_tasks_only,
    )
    collisions = find_collisions(papers, projects, threshold=threshold)
    new_collisions, state = update_collision_state(collisions, state_path=state_path, dedupe=dedupe)
    report = render_report(
        start_dt=start_dt,
        end_dt=end_dt,
        category=category,
        papers=papers,
        projects=projects,
        collisions=collisions,
        new_collisions=new_collisions,
        threshold=threshold,
    )
    run_label = end_dt.strftime("%Y%m%dT%H%M%SZ")
    report_path = write_report(report_dir, report, run_label)
    notify_result = None
    if notify and new_collisions:
        notify_result = notify_harness(
            user_id=user_id,
            report=report,
            report_path=report_path,
            new_collisions=new_collisions,
            all_collisions=collisions,
        )
    dashboard_sync_result = None
    if sync_dashboard:
        dashboard_sync_result = maybe_sync_dashboard(user_id=user_id, dashboard_root=dashboard_root)
    latest_json = {
        "ok": True,
        "category": category,
        "window_start": start_dt.isoformat(),
        "window_end": end_dt.isoformat(),
        "papers_fetched": len(papers),
        "projects_compared": len(projects),
        "collision_count": len(collisions),
        "new_collision_count": len(new_collisions),
        "report_path": str(report_path),
        "state_path": str(state_path),
        "collisions": [collision.to_dict() for collision in collisions],
        "notify": notify_result,
        "dashboard_sync": dashboard_sync_result,
    }
    latest_path = report_dir / "latest.json"
    latest_path.write_text(json.dumps(latest_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state["latest_report_path"] = str(report_path)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return latest_json


def run_scheduled_collision_job() -> dict[str, Any]:
    dashboard_root = default_dashboard_root()
    category = os.getenv("ARXIV_COLLISION_CATEGORY", DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
    max_results = int(os.getenv("ARXIV_COLLISION_MAX_RESULTS", str(DEFAULT_MAX_RESULTS)) or DEFAULT_MAX_RESULTS)
    request_timeout = int(os.getenv("ARXIV_COLLISION_REQUEST_TIMEOUT", "60") or "60")
    lookback_hours = int(os.getenv("ARXIV_COLLISION_LOOKBACK_HOURS", str(DEFAULT_LOOKBACK_HOURS)) or DEFAULT_LOOKBACK_HOURS)
    threshold = float(os.getenv("ARXIV_COLLISION_THRESHOLD", str(DEFAULT_THRESHOLD)) or DEFAULT_THRESHOLD)
    state_path = Path(os.getenv("ARXIV_COLLISION_STATE_PATH", str(DEFAULT_STATE_PATH))).expanduser()
    report_dir = Path(os.getenv("ARXIV_COLLISION_REPORT_DIR", str(DEFAULT_REPORT_DIR))).expanduser()
    user_id = os.getenv("ARXIV_COLLISION_USER_ID", os.getenv("CLAWCROSS_USER_ID", "default")).strip() or "default"
    return run_collision_check(
        dashboard_root=dashboard_root,
        category=category,
        lookback_hours=lookback_hours,
        max_results=max_results,
        request_timeout=request_timeout,
        threshold=threshold,
        include_archived=env_flag("ARXIV_COLLISION_INCLUDE_ARCHIVED", True),
        open_tasks_only=env_flag("ARXIV_COLLISION_OPEN_TASKS_ONLY", False),
        state_path=state_path,
        report_dir=report_dir,
        dedupe=env_flag("ARXIV_COLLISION_DEDUPE", True),
        notify=env_flag("ARXIV_COLLISION_NOTIFY_HARNESS", True),
        user_id=user_id,
        sync_dashboard=env_flag("ARXIV_COLLISION_SYNC_DASHBOARD", False),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check new arXiv Robotics papers against ClawCross dashboard projects.")
    parser.add_argument("--dashboard-root", type=Path, default=None)
    parser.add_argument("--category", default=os.getenv("ARXIV_COLLISION_CATEGORY", DEFAULT_CATEGORY))
    parser.add_argument("--date", default="", help="Exact UTC date to check, YYYY-MM-DD. Overrides --lookback-hours.")
    parser.add_argument("--lookback-hours", type=int, default=int(os.getenv("ARXIV_COLLISION_LOOKBACK_HOURS", str(DEFAULT_LOOKBACK_HOURS))))
    parser.add_argument("--max-results", type=int, default=int(os.getenv("ARXIV_COLLISION_MAX_RESULTS", str(DEFAULT_MAX_RESULTS))))
    parser.add_argument("--request-timeout", type=int, default=int(os.getenv("ARXIV_COLLISION_REQUEST_TIMEOUT", "60")))
    parser.add_argument("--threshold", type=float, default=float(os.getenv("ARXIV_COLLISION_THRESHOLD", str(DEFAULT_THRESHOLD))))
    parser.add_argument("--state-path", type=Path, default=Path(os.getenv("ARXIV_COLLISION_STATE_PATH", str(DEFAULT_STATE_PATH))).expanduser())
    parser.add_argument("--report-dir", type=Path, default=Path(os.getenv("ARXIV_COLLISION_REPORT_DIR", str(DEFAULT_REPORT_DIR))).expanduser())
    parser.add_argument("--skip-archived", action="store_true", help="Do not compare archived dashboard projects.")
    parser.add_argument("--open-tasks-only", action="store_true", help="Only include non-done dashboard task text.")
    parser.add_argument("--no-dedupe", action="store_true", help="Report already-seen paper/project collisions again.")
    parser.add_argument("--notify-harness", action="store_true", default=env_flag("ARXIV_COLLISION_NOTIFY_HARNESS", False))
    parser.add_argument("--sync-dashboard", action="store_true", default=env_flag("ARXIV_COLLISION_SYNC_DASHBOARD", False))
    parser.add_argument("--user-id", default=os.getenv("ARXIV_COLLISION_USER_ID", os.getenv("CLAWCROSS_USER_ID", "default")))
    parser.add_argument("--json", action="store_true", help="Print JSON summary instead of the Markdown report.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dashboard_root = args.dashboard_root or default_dashboard_root()
    start_dt = None
    end_dt = None
    if args.date:
        start_dt, end_dt = date_window_utc(args.date)
    result = run_collision_check(
        dashboard_root=dashboard_root,
        category=args.category,
        start_dt=start_dt,
        end_dt=end_dt,
        lookback_hours=args.lookback_hours,
        max_results=args.max_results,
        request_timeout=args.request_timeout,
        threshold=args.threshold,
        include_archived=not args.skip_archived,
        open_tasks_only=args.open_tasks_only,
        state_path=args.state_path,
        report_dir=args.report_dir,
        dedupe=not args.no_dedupe,
        notify=args.notify_harness,
        user_id=args.user_id,
        sync_dashboard=args.sync_dashboard,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(Path(result["report_path"]).read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
