(function(global) {
    function escapeHtml(text) {
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * API / LLM 有时把换行打成字面量反斜杠+n（两个字符），界面会原样显示「\n」。
     * 在 Markdown 或纯文本展示前转成真实换行（不处理已含真实换行的正常字符串）。
     */
    function normalizeEscapedNewlines(s) {
        const str = s == null ? '' : String(s);
        if (str.indexOf('\\') === -1) return str;
        return str
            .replace(/\\r\\n/g, '\n')
            .replace(/\\n/g, '\n')
            .replace(/\\r/g, '\n')
            .replace(/\\t/g, '\t');
    }

    function configureMarked() {
        if (typeof global.marked === 'undefined') return;
        if (global.marked.__clawcrossConfigured) return;

        global.marked.setOptions({
            gfm: true,
            // LLM 常用「单行换行」分段；开启后单 \n 转为 <br>，移动端可读性更好
            breaks: true,
            highlight(code, lang) {
                if (typeof global.hljs === 'undefined') {
                    return escapeHtml(code);
                }
                const language = lang && global.hljs.getLanguage(lang) ? lang : 'plaintext';
                return global.hljs.highlight(code, { language }).value;
            },
            langPrefix: 'hljs language-'
        });

        global.marked.__clawcrossConfigured = true;
    }

    function render(content) {
        const raw = normalizeEscapedNewlines(content == null ? '' : String(content));
        configureMarked();
        if (!raw) return '';
        if (typeof global.marked === 'undefined') {
            return escapeHtml(raw).replace(/\n/g, '<br>');
        }
        return global.marked.parse(raw);
    }

    function highlight(root) {
        if (!root || typeof global.hljs === 'undefined') return;
        root.querySelectorAll('pre code').forEach((block) => {
            try {
                global.hljs.highlightElement(block);
            } catch (_) {
                // Ignore malformed snippets and keep rendering moving.
            }
        });
    }

    function renderInto(element, content) {
        if (!element) return;
        element.classList.add('markdown-body', 'tc-markdown');
        element.innerHTML = render(content);
        highlight(element);
    }

    global.ClawcrossMarkdown = {
        configure: configureMarked,
        escapeHtml,
        normalizeEscapedNewlines,
        render,
        highlight,
        renderInto,
    };

    configureMarked();
})(window);
