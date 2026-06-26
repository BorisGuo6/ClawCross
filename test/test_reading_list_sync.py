# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

import json
import os
from unittest import mock

from src.services.reading_list_sync import (
    CommandResult,
    extract_reading_list_entries,
    format_reading_list_sync_summary,
    sync_wechat_file_helper_reading_list,
)
import src.services.reading_list_sync as reading_list_sync


def _wx_runner(payload):
    def run(_args, _timeout):
        return CommandResult(0, json.dumps(payload, ensure_ascii=False), "")

    return run


def _clear_reading_list_env():
    return mock.patch.dict(
        os.environ,
        {
            "CLAWCROSS_READING_LIST_PAGE_ID": "",
            "NOTION_READING_LIST_PAGE_ID": "",
            "CLAWCROSS_READING_LIST_PARENT": "",
            "NOTION_READING_LIST_PARENT": "",
            "CLAWCROSS_READING_LIST_DATA_SOURCE_ID": "",
            "NOTION_READING_LIST_DATA_SOURCE_ID": "",
        },
        clear=False,
    )


def test_dry_run_extracts_normalizes_and_deduplicates_wechat_links():
    payload = {
        "messages": [
            {
                "content": (
                    "<title><![CDATA[CameraNoise]]></title>"
                    "<url>https://lizaigc.github.io/CameraNoise/?utm_source=wechat</url>"
                )
            },
            {"content": "duplicate https://lizaigc.github.io/CameraNoise/"},
            {"content": "paper https://arxiv.org/abs/2605.02881?utm_source=chatgpt.com"},
            {"content": "image https://mmbiz.qpic.cn/sz_mmbiz_jpg/example.jpg"},
        ],
    }

    with _clear_reading_list_env():
        summary = sync_wechat_file_helper_reading_list(
            dry_run=True,
            target_date="2026-06-25",
            wx_runner=_wx_runner(payload),
        )

    assert summary["ok"] is True
    assert summary["messages_scanned"] == 4
    assert summary["links_found"] == 4
    assert summary["unique_links"] == 2
    assert summary["new_links"] == 2
    assert summary["duplicates_skipped"] == 1
    assert summary["skipped_noise"] == 1
    report = format_reading_list_sync_summary(summary)
    assert "new_links: 2" in report
    assert "CameraNoise" not in report
    assert "arxiv.org" not in report


def test_extract_resolves_shared_shortlinks_and_skips_noise_urls():
    messages = [
        {"content": "video https://b23.tv/abc123?share_medium=android&share_source=weixin"},
        {"content": "bad https://mp.weixin.qq.com/mp/waerrpage?foo=bar"},
        {"content": "meeting https://vc.feishu.cn/j/123456"},
    ]

    def resolver(url, _timeout):
        assert url == "https://b23.tv/abc123"
        return "https://www.bilibili.com/video/BV1abcDEF/?share_source=WEIXIN&vd_source=dirty"

    entries, counts = extract_reading_list_entries(messages, url_resolver=resolver)

    assert [entry.url for entry in entries] == ["https://www.bilibili.com/video/BV1abcDEF/"]
    assert [entry.canonical for entry in entries] == ["https://www.bilibili.com/video/BV1abcDEF"]
    assert counts == {
        "links_found": 3,
        "skipped_noise": 2,
        "duplicates_skipped": 0,
        "resolved_links": 1,
    }


def test_default_file_helper_retries_filehelper_alias():
    payload = {"messages": [{"content": "https://example.com/article"}]}
    calls = []

    def wx_runner(args, _timeout):
        calls.append(args)
        if args[1] == "filehelper":
            return CommandResult(0, json.dumps(payload), "")
        return CommandResult(1, "", "找不到 文件传输助手 的消息记录")

    summary = sync_wechat_file_helper_reading_list(
        dry_run=True,
        target_date="2026-06-25",
        wx_runner=wx_runner,
    )

    assert summary["ok"] is True
    assert summary["messages_scanned"] == 1
    assert calls[0][1] == "文件传输助手"
    assert calls[1][1] == "filehelper"


def test_write_mode_blocks_without_notion_target(tmp_path):
    payload = {"messages": [{"content": "https://example.com/article?utm_source=wechat"}]}

    with _clear_reading_list_env(), mock.patch.object(
        reading_list_sync, "SYNC_STATE_PATH", tmp_path / "sync-pages.json"
    ):
        summary = sync_wechat_file_helper_reading_list(
            target_date="2026-06-25",
            mode="local",
            wx_runner=_wx_runner(payload),
        )

    assert summary["ok"] is False
    assert summary["blocker"] == "missing_notion_target"
    assert summary["new_links"] == 0


def test_update_existing_page_skips_existing_canonical_urls(tmp_path):
    payload = {
        "messages": [
            {"content": "[Existing](https://example.com/article?utm_source=wechat)"},
            {"content": "[New Product](https://product.example.com/launch?utm_source=wechat)"},
        ]
    }
    calls = []

    def notion_runner(args, input_text, _timeout):
        calls.append((args, input_text))
        if args == ["pages", "get", "page-1"]:
            return CommandResult(0, "# Reading List\n\n[Existing](https://example.com/article)\n", "")
        if args == ["pages", "update", "page-1"]:
            assert input_text is not None
            assert "New Product" in input_text
            assert input_text.count("https://example.com/article") == 1
            return CommandResult(0, "", "")
        return CommandResult(1, "", "unexpected command")

    with mock.patch.object(reading_list_sync, "SYNC_STATE_PATH", tmp_path / "sync-pages.json"):
        summary = sync_wechat_file_helper_reading_list(
            target_date="2026-06-25",
            mode="local",
            page_id="page-1",
            wx_runner=_wx_runner(payload),
            notion_runner=notion_runner,
        )

    assert summary["ok"] is True
    assert summary["updated"] is True
    assert summary["notion_action"] == "updated"
    assert summary["new_links"] == 1
    assert summary["duplicates_skipped"] == 1
    assert calls[0][0] == ["pages", "get", "page-1"]
    assert calls[1][0] == ["pages", "update", "page-1"]


def test_create_under_parent_uses_daily_title_and_caches_page_id(tmp_path):
    payload = {"messages": [{"content": "[New](https://example.com/new?utm_source=wechat)"}]}
    calls = []

    def notion_runner(args, input_text, _timeout):
        calls.append((args, input_text))
        if args == ["pages", "create", "--parent", "page:month-page", "--json"]:
            assert input_text is not None
            assert input_text.startswith("# 6.25\n\n")
            return CommandResult(0, json.dumps({"id": "daily-page"}), "")
        return CommandResult(1, "", "unexpected command")

    with mock.patch.object(reading_list_sync, "SYNC_STATE_PATH", tmp_path / "sync-pages.json"):
        summary = sync_wechat_file_helper_reading_list(
            target_date="2026-06-25",
            mode="local",
            parent="page:month-page",
            wx_runner=_wx_runner(payload),
            notion_runner=notion_runner,
        )

        cached = json.loads((tmp_path / "sync-pages.json").read_text(encoding="utf-8"))

    assert summary["ok"] is True
    assert summary["notion_action"] == "created"
    assert summary["notion_page_id"] == "daily-page"
    assert cached["2026-06-25"] == "daily-page"
    assert calls[0][0] == ["pages", "create", "--parent", "page:month-page", "--json"]


def test_cached_daily_page_is_reused_before_parent(tmp_path):
    payload = {"messages": [{"content": "[New](https://example.com/new?utm_source=wechat)"}]}
    state_path = tmp_path / "sync-pages.json"
    state_path.write_text(json.dumps({"2026-06-25": "daily-page"}), encoding="utf-8")
    calls = []

    def notion_runner(args, input_text, _timeout):
        calls.append((args, input_text))
        if args == ["pages", "get", "daily-page"]:
            return CommandResult(0, "# 6.25\n\n", "")
        if args == ["pages", "update", "daily-page"]:
            return CommandResult(0, "", "")
        return CommandResult(1, "", "unexpected command")

    with mock.patch.object(reading_list_sync, "SYNC_STATE_PATH", state_path):
        summary = sync_wechat_file_helper_reading_list(
            target_date="2026-06-25",
            mode="local",
            parent="page:month-page",
            wx_runner=_wx_runner(payload),
            notion_runner=notion_runner,
        )

    assert summary["ok"] is True
    assert summary["notion_action"] == "updated"
    assert summary["notion_page_id"] == "daily-page"
    assert calls[0][0] == ["pages", "get", "daily-page"]
    assert calls[1][0] == ["pages", "update", "daily-page"]


def test_validation_failure_returns_blocker_without_notion_write(tmp_path):
    payload = {"messages": [{"content": "[New](https://example.com/new)"}]}
    calls = []

    def notion_runner(args, input_text, _timeout):
        calls.append((args, input_text))
        return CommandResult(1, "", "should not be called")

    with mock.patch.object(reading_list_sync, "SYNC_STATE_PATH", tmp_path / "sync-pages.json"), mock.patch.object(
        reading_list_sync, "_new_daily_page_markdown", side_effect=ValueError("bad title")
    ):
        summary = sync_wechat_file_helper_reading_list(
            target_date="2026-06-25",
            mode="local",
            parent="page:month-page",
            wx_runner=_wx_runner(payload),
            notion_runner=notion_runner,
        )

    assert summary["ok"] is False
    assert summary["blocker"] == "reading_list_validation_failed: bad title"
    assert calls == []


def test_default_write_mode_delegates_to_codex(tmp_path):
    payload = {"messages": [{"content": "[New](https://example.com/new?utm_source=wechat)"}]}
    prompts = []

    def codex_runner(prompt, _timeout):
        prompts.append(prompt)
        return CommandResult(
            0,
            json.dumps(
                {
                    "ok": True,
                    "updated": True,
                    "date": "2026-06-25",
                    "notion_page_id": "daily-page",
                    "notion_action": "updated",
                    "new_links": 1,
                    "duplicates_skipped": 0,
                    "skipped_noise": 0,
                    "blocker": "",
                }
            ),
            "",
        )

    with mock.patch.object(reading_list_sync, "SYNC_STATE_PATH", tmp_path / "sync-pages.json"):
        summary = sync_wechat_file_helper_reading_list(
            target_date="2026-06-25",
            wx_runner=_wx_runner(payload),
            codex_runner=codex_runner,
        )

    assert summary["ok"] is True
    assert summary["mode"] == "codex"
    assert summary["notion_action"] == "updated"
    assert summary["new_links"] == 1
    assert "mode: codex" in format_reading_list_sync_summary(summary)
    assert "Use your own Notion connector/app integration" in prompts[0]
    assert "ntn" in prompts[0]
    assert "across the Reading List root/month/daily pages, not only today's daily page" in prompts[0]
    assert "search the Reading List for each canonical URL" in prompts[0]


def test_codex_unstructured_response_returns_blocker():
    payload = {"messages": [{"content": "[New](https://example.com/new)"}]}

    summary = sync_wechat_file_helper_reading_list(
        target_date="2026-06-25",
        wx_runner=_wx_runner(payload),
        codex_runner=lambda _prompt, _timeout: CommandResult(0, "done", ""),
    )

    assert summary["ok"] is False
    assert summary["blocker"] == "codex_sync_unstructured_response"


def test_codex_response_parser_tolerates_tool_trace_prefix():
    payload = {"messages": [{"content": "[New](https://example.com/new)"}]}
    schema_echo = (
        '{"ok":true,"updated":true,"date":"YYYY-MM-DD","notion_page_id":"",'
        '"notion_action":"created|updated|no_changes|blocked","new_links":0,'
        '"duplicates_skipped":0,"skipped_noise":0,"blocker":""}'
    )
    final_json = (
        "[tool:Tool: codex_apps/notion_fetch] "
        '{"ok":true,"updated":true,"date":"2026-06-25",'
        '"notion_page_id":"daily-page","notion_action":"created",'
        '"new_links":1,"duplicates_skipped":0,"skipped_noise":0,"blocker":""}'
    )
    stdout = json.dumps(
        {
            "entries": [
                {"role": "user", "textPreview": schema_echo},
                {"role": "assistant", "textPreview": final_json},
            ],
        },
        ensure_ascii=False,
    )

    summary = sync_wechat_file_helper_reading_list(
        target_date="2026-06-25",
        wx_runner=_wx_runner(payload),
        codex_runner=lambda _prompt, _timeout: CommandResult(0, stdout, ""),
    )

    assert summary["ok"] is True
    assert summary["notion_action"] == "created"
    assert summary["notion_page_id"] == "daily-page"
    assert summary["new_links"] == 1
