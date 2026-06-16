from src.services.reading_list_rules import (
    normalize_markdown_links,
    normalize_url,
    validate_reading_list_markdown,
)


def test_normalizes_xiaohongshu_share_links_and_removes_noise():
    text = """
Unresolved source links:
[小红书 source](https://www.xiaohongshu.com/discovery/item/6a1ac1ab0000000006034fa8?app_platform=android&ignoreEngage=true&app_version=9.32.0&share_from_user_hidden=true&xsec_source=app_share&type=video&xsec_token=abc&author_share=1&xhsshare=&shareRedId=ODc1&apptime=1780173968&share_id=abc&share_channel=wechat)
"""

    normalized = normalize_markdown_links(text)

    assert normalized == (
        "[小红书笔记](https://www.xiaohongshu.com/discovery/item/6a1ac1ab0000000006034fa8)"
    )
    assert validate_reading_list_markdown(normalized) == []


def test_cleans_known_tracking_params_without_destroying_content_params():
    assert normalize_url("https://b23.tv/FR7x4Yx?share_medium=android&share_source=weixin") == (
        "https://b23.tv/FR7x4Yx"
    )
    assert normalize_url("https://zhuanlan.zhihu.com/p/1927639262235993491?utm_source=wechat") == (
        "https://zhuanlan.zhihu.com/p/1927639262235993491"
    )
    assert normalize_url("https://voxeldance.com/?result=success&headimgurl=a&nick_name=b") == (
        "https://voxeldance.com/"
    )
    assert normalize_url("https://example.com/search?q=robotics&utm_source=chatgpt.com") == (
        "https://example.com/search?q=robotics"
    )


def test_normalizes_bare_xiaohongshu_urls_and_shortlinks():
    text = """
小红书链接 https://m.xiaohongshu.com/explore/6a1ac1ab0000000006034fa8?xsec_token=abc&xsec_source=app_share&share_channel=wechat。
短链 https://www.xhslink.com/a/1abc2def?xhsshare=CopyLink&appuid=123
"""

    normalized = normalize_markdown_links(text)

    assert (
        "https://www.xiaohongshu.com/discovery/item/6a1ac1ab0000000006034fa8。"
        in normalized
    )
    assert "https://xhslink.com/a/1abc2def" in normalized
    assert "xsec_token" not in normalized
    assert "xhsshare" not in normalized
    assert validate_reading_list_markdown(normalized) == []


def test_canonicalizes_wechat_article_share_urls():
    assert normalize_url(
        "https://mp.weixin.qq.com/s?__biz=MzA&mid=224748&idx=1&sn=abc"
        "&chksm=tracking&scene=21&subscene=10000&clicktime=1780173968"
        "#wechat_redirect"
    ) == "https://mp.weixin.qq.com/s?__biz=MzA&mid=224748&idx=1&sn=abc"


def test_replaces_placeholder_titles_with_overrides_and_url_fallbacks():
    text = """
[TWITTER BANNER TITLE META TAG](https://www.pptmaster.app/?utm_source=chatgpt.com)
[https://arxiv.org/abs/2605.02881](https://arxiv.org/abs/2605.02881)
[huggingface.co](https://huggingface.co/spaces/Stable-X/ReconViaGen-v0.5)
"""
    normalized = normalize_markdown_links(
        text,
        title_overrides={
            "https://www.pptmaster.app": "PPTMaster",
            "https://arxiv.org/abs/2605.02881": "ReconViaGen: Reconstructive Video Generation",
        },
    )

    assert "[PPTMaster](https://www.pptmaster.app/)" in normalized
    assert "[ReconViaGen: Reconstructive Video Generation](https://arxiv.org/abs/2605.02881)" in normalized
    assert "[ReconViaGen-v0.5 Hugging Face Space](https://huggingface.co/spaces/Stable-X/ReconViaGen-v0.5)" in normalized
    assert validate_reading_list_markdown(normalized) == []


def test_deduplicates_pure_link_rows_by_normalized_url():
    text = """
[CameraNoise](https://lizaigc.github.io/CameraNoise/)
[CameraNoise duplicate](https://lizaigc.github.io/CameraNoise)
[DreamZero](https://amirbar.net/dreamzero/)
[DreamZero duplicate](https://amirbar.net/dreamzero/?utm_source=chatgpt.com)
"""

    normalized = normalize_markdown_links(text)

    lines = normalized.splitlines()
    assert lines == [
        "[CameraNoise](https://lizaigc.github.io/CameraNoise/)",
        "[DreamZero](https://amirbar.net/dreamzero/)",
    ]
    assert validate_reading_list_markdown(normalized) == []


def test_preserves_existing_notion_preview_and_media_lines():
    text = """
<unknown {"url":"https://www.notion.so/external_object_instance","alt":"external_object_instance"}/>
![](https://www.notion.so/image/example.png)
<empty-block/>
[小红书 source](https://www.xiaohongshu.com/discovery/item/abc?xsec_source=app_share)
"""

    normalized = normalize_markdown_links(text)

    assert '<unknown {"url":"https://www.notion.so/external_object_instance"' in normalized
    assert "![](https://www.notion.so/image/example.png)" in normalized
    assert "<empty-block/>" not in normalized
    assert "[小红书笔记](https://www.xiaohongshu.com/discovery/item/abc)" in normalized
    assert validate_reading_list_markdown(normalized) == []


def test_validator_blocks_the_known_bad_reading_list_shapes():
    bad_text = """
[TWITTER BANNER TITLE META TAG](https://www.pptmaster.app/?utm_source=chatgpt.com)
[小红书 source](https://www.xiaohongshu.com/discovery/item/abc?share_channel=wechat)
[https://arxiv.org/abs/2605.02881](https://arxiv.org/abs/2605.02881)
[A](https://example.com/article?utm_source=chatgpt.com)
[A duplicate](https://example.com/article)
Unresolved source links:
<empty-block/>
"""

    issues = validate_reading_list_markdown(bad_text)

    assert any("TWITTER BANNER TITLE META TAG" in issue for issue in issues)
    assert any("placeholder source title remains" in issue for issue in issues)
    assert any("bad link title remains" in issue for issue in issues)
    assert any("chatgpt tracking parameter remains" in issue for issue in issues)
    assert any("uncanonicalized URL remains" in issue for issue in issues)
    assert any("duplicate normalized URL remains" in issue for issue in issues)
    assert any("Unresolved source links" in issue for issue in issues)


def test_validator_blocks_dirty_urls_even_with_clean_titles():
    issues = validate_reading_list_markdown(
        "[小红书笔记](https://www.xiaohongshu.com/explore/abc?xsec_token=dirty&share_channel=wechat)\n"
        "https://mp.weixin.qq.com/s?__biz=MzA&mid=1&idx=1&sn=abc&scene=21#wechat_redirect"
    )

    assert any("uncanonicalized URL remains" in issue for issue in issues)
    assert any("uncanonicalized bare URL remains" in issue for issue in issues)
