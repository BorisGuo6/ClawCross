"""Normalization rules for WeChat-to-Notion Reading List entries.

The Notion connector cannot create native Notion link-preview mentions, so the
automation writes markdown links. These helpers keep those links close to the
manual Reading List style: clean titles, clean URLs, no placeholder noise, and
no duplicate URLs on a date page.
"""

from __future__ import annotations

import re
from typing import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]+)\]\(([^)\n]+)\)")
BARE_URL_RE = re.compile(r"(?<!\]\()https?://[^\s<>\]\)\"']+")
TRAILING_BARE_URL_CHARS = ".,;:!?，。！？；：、"

XIAOHONGSHU_HOSTS = {
    "www.xiaohongshu.com",
    "xiaohongshu.com",
    "m.xiaohongshu.com",
}

XIAOHONGSHU_SHORTLINK_HOSTS = {
    "xhslink.com",
    "www.xhslink.com",
    "xhs.cn",
    "www.xhs.cn",
}

WECHAT_ARTICLE_HOSTS = {
    "mp.weixin.qq.com",
}

WECHAT_ARTICLE_QUERY_PARAMS = {
    "__biz",
    "mid",
    "idx",
    "sn",
}

TRACKING_PARAMS = {
    "app_platform",
    "app_version",
    "apptime",
    "author_share",
    "bbid",
    "from",
    "ignoreengage",
    "share_channel",
    "share_from_user_hidden",
    "share_id",
    "share_medium",
    "share_red_id",
    "share_source",
    "shareredid",
    "source",
    "spm_id_from",
    "tk",
    "ts",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
    "vd_source",
    "xhsshare",
    "xsec_source",
    "xsec_token",
    "xstag",
}

STRIP_ALL_QUERY_HOSTS = {
    "b23.tv",
    "m.bilibili.com",
    "space.bilibili.com",
    "www.bilibili.com",
    *XIAOHONGSHU_HOSTS,
    *XIAOHONGSHU_SHORTLINK_HOSTS,
    "zhuanlan.zhihu.com",
    "voxeldance.com",
    "www.voxeldance.com",
}

ROOT_SLASH_HOSTS = {
    "voxeldance.com",
    "www.originflow.ai",
    "www.pptmaster.app",
}

HOST_ALIASES = {
    "originflow.ai": "www.originflow.ai",
    "pptmaster.app": "www.pptmaster.app",
    "xiaohongshu.com": "www.xiaohongshu.com",
    "m.xiaohongshu.com": "www.xiaohongshu.com",
    "www.xhslink.com": "xhslink.com",
    "www.xhs.cn": "xhs.cn",
}

NOISE_LINES = {
    "<empty-block/>",
    "Unresolved source links:",
    "claude --permission-mode auto",
    "claude -permission-mode auto",
    "claude —permission-mode auto",
    "claude — permission-mode auto",
}

BAD_TITLE_MARKERS = {
    "",
    "source",
    "TWITTER BANNER TITLE META TAG",
    "untitled",
}

BARE_HOST_TITLES = {
    "arxiv.org",
    "gitee.com",
    "huggingface.co",
    "ieeexplore.ieee.org",
    "makerworld.com.cn",
    "mp.weixin.qq.com",
    "www.science.org",
    "www.sciencedirect.com",
}


def normalize_url(url: str) -> str:
    """Return a stable, share-param-free URL for Reading List storage."""

    value = url.strip().strip("<>")
    if not value:
        return value

    parsed = urlsplit(value)
    if not parsed.scheme and not parsed.netloc:
        value = f"https://{value}"
        parsed = urlsplit(value)

    scheme = "https" if parsed.scheme in {"http", "https", ""} else parsed.scheme
    netloc = parsed.netloc.lower()
    netloc = HOST_ALIASES.get(netloc, netloc)
    host = netloc.split("@")[-1].split(":")[0]

    query = ""
    if parsed.query and host in WECHAT_ARTICLE_HOSTS:
        kept_params = [
            (key, param_value)
            for key, param_value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() in WECHAT_ARTICLE_QUERY_PARAMS
        ]
        query = urlencode(kept_params, doseq=True)
    elif parsed.query and host not in STRIP_ALL_QUERY_HOSTS:
        kept_params = []
        for key, param_value in parse_qsl(parsed.query, keep_blank_values=True):
            normalized_key = key.lower()
            if normalized_key.startswith("utm_") or normalized_key in TRACKING_PARAMS:
                continue
            kept_params.append((key, param_value))
        query = urlencode(kept_params, doseq=True)

    path = _normalize_path(host, parsed.path)
    if not path and host in ROOT_SLASH_HOSTS:
        path = "/"

    fragment = ""
    if (
        host not in STRIP_ALL_QUERY_HOSTS
        and host not in WECHAT_ARTICLE_HOSTS
        and not parsed.fragment.startswith("utm_")
    ):
        fragment = parsed.fragment

    return urlunsplit((scheme, netloc, path, query, fragment))


def canonical_url(url: str) -> str:
    """Return a comparison key for duplicate detection."""

    normalized = normalize_url(url)
    parsed = urlsplit(normalized)
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def normalize_title(
    title: str,
    url: str,
    *,
    title_overrides: Mapping[str, str] | None = None,
) -> str:
    """Clean a markdown link title, using URL-derived fallback titles if needed."""

    cleaned = re.sub(r"\s+", " ", title.strip())
    cleaned = re.sub(r"\s+source$", "", cleaned, flags=re.IGNORECASE).strip()
    if cleaned == "小红书":
        cleaned = "小红书笔记"

    override = _lookup_title_override(url, title_overrides)
    if override:
        return override

    if _title_looks_bad(cleaned, url):
        return _derive_title_from_url(url)
    return cleaned


def normalize_markdown_links(
    text: str,
    *,
    title_overrides: Mapping[str, str] | None = None,
) -> str:
    """Normalize Reading List markdown and remove duplicate pure-link rows."""

    seen_urls: set[str] = set()
    output_lines: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if _is_noise_line(line):
            continue

        matches = list(MARKDOWN_LINK_RE.finditer(line))
        if not matches:
            output_lines.append(_normalize_bare_urls(line))
            continue

        pure_link_line = len(matches) == 1 and line.strip() == matches[0].group(0)
        if pure_link_line:
            key = canonical_url(matches[0].group(2))
            if key in seen_urls:
                continue
            seen_urls.add(key)

        def replace(match: re.Match[str]) -> str:
            title = match.group(1)
            url = match.group(2)
            normalized = normalize_url(url)
            cleaned_title = normalize_title(title, normalized, title_overrides=title_overrides)
            return f"[{cleaned_title}]({normalized})"

        output_lines.append(_normalize_bare_urls(MARKDOWN_LINK_RE.sub(replace, line)))

    return "\n".join(output_lines).strip()


def validate_reading_list_markdown(text: str) -> list[str]:
    """Return validation issues that should block a Notion write."""

    issues: list[str] = []
    for marker in ("Unresolved source links:", "<empty-block/>", "TWITTER BANNER TITLE META TAG"):
        if marker in text:
            issues.append(f"forbidden marker remains: {marker}")
    if "utm_source=chatgpt.com" in text:
        issues.append("chatgpt tracking parameter remains")

    seen_urls: set[str] = set()
    for match in MARKDOWN_LINK_RE.finditer(text):
        title = match.group(1).strip()
        url = match.group(2).strip()
        if re.search(r"\s+source$", title, flags=re.IGNORECASE):
            issues.append(f"placeholder source title remains: {title}")
        if _title_looks_bad(title, url):
            issues.append(f"bad link title remains: {title}")
        if _requires_canonical_url(url) and normalize_url(url) != url.strip().strip("<>"):
            issues.append(f"uncanonicalized URL remains: {normalize_url(url)}")

        key = canonical_url(url)
        if key in seen_urls:
            issues.append(f"duplicate normalized URL remains: {normalize_url(url)}")
        seen_urls.add(key)

    for raw_url in _iter_bare_urls(text):
        if _requires_canonical_url(raw_url) and normalize_url(raw_url) != raw_url.strip().strip("<>"):
            issues.append(f"uncanonicalized bare URL remains: {normalize_url(raw_url)}")

    return issues


def assert_valid_reading_list_markdown(text: str) -> None:
    """Raise ValueError when markdown is unsafe to write to Notion."""

    issues = validate_reading_list_markdown(text)
    if issues:
        raise ValueError("; ".join(issues))


def _lookup_title_override(
    url: str,
    title_overrides: Mapping[str, str] | None,
) -> str | None:
    if not title_overrides:
        return None
    normalized = normalize_url(url)
    candidates = (url, normalized, canonical_url(normalized))
    for candidate in candidates:
        title = title_overrides.get(candidate)
        if title:
            return title.strip()
    return None


def _is_noise_line(line: str) -> bool:
    normalized = re.sub(r"\s+", " ", line.strip())
    return normalized in NOISE_LINES


def _normalize_path(host: str, path: str) -> str:
    if host in XIAOHONGSHU_HOSTS:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "explore":
            return f"/discovery/item/{parts[1]}"
        if len(parts) >= 3 and parts[0] == "discovery" and parts[1] == "item":
            return f"/discovery/item/{parts[2]}"
    return path


def _normalize_bare_urls(line: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw_url = match.group(0)
        url = raw_url.rstrip(TRAILING_BARE_URL_CHARS)
        suffix = raw_url[len(url):]
        return f"{normalize_url(url)}{suffix}"

    return BARE_URL_RE.sub(replace, line)


def _iter_bare_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in BARE_URL_RE.finditer(text):
        raw_url = match.group(0)
        urls.append(raw_url.rstrip(TRAILING_BARE_URL_CHARS))
    return urls


def _requires_canonical_url(url: str) -> bool:
    parsed = urlsplit(url.strip().strip("<>"))
    host = parsed.netloc.lower().split("@")[-1].split(":")[0]
    if host in XIAOHONGSHU_HOSTS or host in XIAOHONGSHU_SHORTLINK_HOSTS or host in WECHAT_ARTICLE_HOSTS:
        return True
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.lower()
        if normalized_key.startswith("utm_") or normalized_key in TRACKING_PARAMS:
            return True
    return False


def _title_looks_bad(title: str, url: str) -> bool:
    stripped = title.strip()
    if stripped in BAD_TITLE_MARKERS or stripped.lower() in BAD_TITLE_MARKERS:
        return True
    if stripped.startswith(("http://", "https://")):
        return True
    if stripped.lower() in BARE_HOST_TITLES:
        return True
    if re.fullmatch(r"(?:www\.)?[a-z0-9.-]+\.[a-z]{2,}/?", stripped.lower()):
        return True

    normalized_url = normalize_url(url)
    parsed = urlsplit(normalized_url)
    host = parsed.netloc.lower()
    if host in WECHAT_ARTICLE_HOSTS and stripped.lower() in {"s", "mp.weixin.qq.com"}:
        return True
    return stripped.lower().rstrip("/") == parsed.netloc.lower()


def _derive_title_from_url(url: str) -> str:
    normalized = normalize_url(url)
    parsed = urlsplit(normalized)
    host = parsed.netloc.lower()
    parts = [part for part in parsed.path.split("/") if part]

    if "xiaohongshu.com" in host:
        return "小红书笔记"
    if host in WECHAT_ARTICLE_HOSTS:
        return "WeChat article"
    if host == "b23.tv" and parts:
        return f"Bilibili video {parts[-1]}"
    if host in {"www.bilibili.com", "m.bilibili.com"} and len(parts) >= 2 and parts[0] == "video":
        return f"Bilibili video {parts[1]}"
    if host == "arxiv.org" and len(parts) >= 2 and parts[0] == "abs":
        return f"arXiv {parts[1]}"
    if host == "github.com" and len(parts) >= 2:
        return parts[1]
    if host.endswith(".github.io"):
        return _slug_to_title(host.split(".", 1)[0])
    if host == "huggingface.co" and len(parts) >= 3 and parts[0] in {"datasets", "spaces"}:
        kind = "Dataset" if parts[0] == "datasets" else "Space"
        return f"{_slug_to_title(parts[-1])} Hugging Face {kind}"
    if parts:
        return _slug_to_title(parts[-1])
    bare_host = host.removeprefix("www.")
    return _slug_to_title(bare_host.split(".", 1)[0])


def _slug_to_title(value: str) -> str:
    if re.search(r"[A-Z]", value):
        return value
    slug = re.sub(r"[-_]+", " ", value).strip()
    return slug[:1].upper() + slug[1:] if slug else value
