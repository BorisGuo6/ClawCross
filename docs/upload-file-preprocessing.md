# Uploaded File Preprocessing

ClawCross uses [`microsoft/markitdown`](https://github.com/microsoft/markitdown)
as an optional preprocessing layer for uploaded files before agents read them.
The goal is to turn rich documents into Markdown that can be consumed by LLMs
without asking agents to parse PDFs or Office formats manually.

## Scope

Preprocessing is applied to already-uploaded local bytes only:

- main chat `/v1/chat/completions` file parts
- ACP main-chat file parts
- group-chat file attachments for internal, ACP, and HTTP agents
- system-trigger attachments
- MCP `read_file` when a binary/rich document is encountered

ClawCross does not pass user-controlled URLs to MarkItDown. This follows
MarkItDown's security guidance: the library can perform I/O with the current
process privileges, so inputs must be narrowed before conversion.

## Supported Formats

The ClawCross dependency installs document-focused MarkItDown extras:

```text
markitdown[pdf,docx,pptx,xlsx,xls,outlook]
```

This covers PDF, Word, PowerPoint, Excel, Outlook `.msg`, plus MarkItDown's
built-in text/data/web formats such as HTML, CSV, JSON, XML, Markdown, plain
text, EPUB, and ZIP.

Frontend upload pickers expose:

```text
.pdf, .docx, .pptx, .xlsx, .xls, .msg, .epub
```

Rich document uploads are capped at 10 MB by the frontend and by backend
`MARKITDOWN_MAX_INPUT_BYTES`.

## Configuration

Optional `config/.env` keys:

| Key | Purpose | Default |
|---|---|---|
| `MARKITDOWN_ENABLED` | enable preprocessing | `true` |
| `MARKITDOWN_MAX_INPUT_BYTES` | max uploaded file bytes sent to converter | `52428800` |
| `MARKITDOWN_MAX_OUTPUT_CHARS` | max Markdown chars included in prompt/tool output | `50000` |
| `MARKITDOWN_ENABLE_PLUGINS` | allow MarkItDown plugins | `false` |

Plugins are disabled by default. Do not enable plugins on untrusted shared
servers without reviewing the plugin's I/O behavior.

## Failure Behavior

If MarkItDown is unavailable or a conversion fails:

- UTF-8 text-like payloads still decode and attach as text.
- PDFs fall back to the existing PyMuPDF extraction path in main chat.
- Unsupported binary/media files remain as descriptions or multimodal file
  parts, depending on model support.

Agents should treat the generated Markdown as a preprocessing aid, not as a
high-fidelity document conversion.
