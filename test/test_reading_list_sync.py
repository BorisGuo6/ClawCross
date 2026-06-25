# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

import json
import os
from unittest import mock

from src.services.reading_list_sync import (
    CommandResult,
    format_reading_list_sync_summary,
    sync_wechat_file_helper_reading_list,
)


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


def test_write_mode_blocks_without_notion_target():
    payload = {"messages": [{"content": "https://example.com/article?utm_source=wechat"}]}

    with _clear_reading_list_env():
        summary = sync_wechat_file_helper_reading_list(
            target_date="2026-06-25",
            wx_runner=_wx_runner(payload),
        )

    assert summary["ok"] is False
    assert summary["blocker"] == "missing_notion_target"
    assert summary["new_links"] == 0


def test_update_existing_page_skips_existing_canonical_urls():
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

    summary = sync_wechat_file_helper_reading_list(
        target_date="2026-06-25",
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
