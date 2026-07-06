(function () {
  const EXAMPLE_ACCOUNTS = [
    {
      name: "main",
      provider: "anyrouter",
      api_user: "paste-new-api-user-here",
      cookies: {
        session: "paste-session-cookie-here",
        acw_tc: "paste-waf-cookie-if-present",
      },
    },
  ];

  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function setStatus(message, tone) {
    const el = $("anyrouter-autolog-status");
    if (!el) return;
    el.className = "nat-status-card anyrouter-status";
    if (tone) el.classList.add(tone);
    el.textContent = message;
  }

  function setBusy(isBusy) {
    ["anyrouter-run-input-btn", "anyrouter-run-saved-btn"].forEach((id) => {
      const button = $(id);
      if (button) button.disabled = isBusy;
    });
  }

  function parseJsonTextarea(id, fallback, label) {
    const el = $(id);
    const text = (el && el.value ? el.value : "").trim();
    if (!text) return fallback;
    try {
      return JSON.parse(text);
    } catch (error) {
      throw new Error(`${label} JSON 解析失败: ${error.message}`);
    }
  }

  function readInputConfig() {
    return {
      accounts: parseJsonTextarea("anyrouter-config-input", [], "账号"),
      providers: parseJsonTextarea("anyrouter-providers-input", {}, "Provider"),
    };
  }

  async function fetchJson(url, options, allowFalseOk) {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      ...(options || {}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || (!allowFalseOk && data.ok === false)) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    return data;
  }

  function renderSavedPreview(config) {
    const host = $("anyrouter-autolog-results");
    if (!host) return;
    const accounts = Array.isArray(config && config.accounts)
      ? config.accounts
      : [];
    if (!accounts.length) {
      host.innerHTML = "";
      return;
    }
    const updated = config.updated_at
      ? `<div class="anyrouter-result-domain">Updated: ${escapeHtml(config.updated_at)}</div>`
      : "";
    host.innerHTML = accounts
      .map((account, index) => {
        const cookies =
          account && account.cookies && typeof account.cookies === "object"
            ? Object.keys(account.cookies)
            : [];
        return `
                <article class="anyrouter-result-card">
                    <div class="anyrouter-result-head">
                        <div>
                            <div class="anyrouter-result-title">${escapeHtml(account.name || `Account ${index + 1}`)}</div>
                            <div class="anyrouter-result-domain">${escapeHtml(account.provider || "anyrouter")} · ${cookies.length} cookies</div>
                        </div>
                        <span class="nat-pill">saved</span>
                    </div>
                    ${updated}
                </article>
            `;
      })
      .join("");
  }

  function quotaCell(label, value) {
    const display = value == null ? "-" : value;
    return `<div class="anyrouter-quota-cell"><span>${escapeHtml(label)}</span><strong>${escapeHtml(display)}</strong></div>`;
  }

  function renderRunResults(payload) {
    const host = $("anyrouter-autolog-results");
    if (!host) return;
    const results = Array.isArray(payload && payload.results)
      ? payload.results
      : [];
    if (!results.length) {
      host.innerHTML = "";
      return;
    }
    host.innerHTML = results
      .map((result) => {
        const after = result.after && result.after.success ? result.after : {};
        const delta = result.delta || {};
        const missingWaf = Array.isArray(result.missing_waf_cookies)
          ? result.missing_waf_cookies
          : [];
        const warning = missingWaf.length
          ? `<div class="anyrouter-warning">缺少 WAF cookie: ${escapeHtml(missingWaf.join(", "))}</div>`
          : "";
        const beforeError =
          result.before && result.before.success === false
            ? `<div class="anyrouter-warning">读取前失败: ${escapeHtml(result.before.error || result.before.status_code || "unknown")}</div>`
            : "";
        const afterError =
          result.after && result.after.success === false
            ? `<div class="anyrouter-warning">读取后失败: ${escapeHtml(result.after.error || result.after.status_code || "unknown")}</div>`
            : "";
        return `
                <article class="anyrouter-result-card ${result.success ? "success" : "error"}">
                    <div class="anyrouter-result-head">
                        <div>
                            <div class="anyrouter-result-title">${escapeHtml(result.name || "Account")}</div>
                            <div class="anyrouter-result-domain">${escapeHtml(result.provider || "-")} · ${escapeHtml(result.domain || "-")}</div>
                        </div>
                        <span class="nat-pill ${result.success ? "" : "error"}">${result.success ? "ok" : "failed"}</span>
                    </div>
                    <div class="anyrouter-result-message">${escapeHtml(result.message || "")}</div>
                    <div class="anyrouter-quota-grid">
                        ${quotaCell("余额", after.quota)}
                        ${quotaCell("已用", after.used_quota)}
                        ${quotaCell("奖励", delta.check_in_reward)}
                    </div>
                    ${warning}${beforeError}${afterError}
                </article>
            `;
      })
      .join("");
  }

  window.loadAnyrouterAutologConfig = async function () {
    try {
      const data = await fetchJson("/api/anyrouter-autolog/config");
      if (data.has_saved_config) {
        setStatus(
          "已加载保存配置预览。保存配置不会回填 masked cookie。",
          "success"
        );
        renderSavedPreview(data.config || {});
      } else {
        setStatus("没有保存配置。", "warn");
        renderSavedPreview({});
      }
    } catch (error) {
      setStatus(error.message, "error");
    }
  };

  window.saveAnyrouterAutologConfig = async function () {
    try {
      const config = readInputConfig();
      const data = await fetchJson("/api/anyrouter-autolog/config", {
        method: "POST",
        body: JSON.stringify({ config }),
      });
      setStatus("配置已保存。", "success");
      renderSavedPreview(data.config || {});
    } catch (error) {
      setStatus(error.message, "error");
    }
  };

  window.runAnyrouterAutolog = async function (useSaved) {
    setBusy(true);
    try {
      const body = useSaved ? {} : { config: readInputConfig() };
      setStatus(
        useSaved ? "正在运行保存配置..." : "正在运行当前输入...",
        "running"
      );
      const data = await fetchJson(
        "/api/anyrouter-autolog/run",
        {
          method: "POST",
          body: JSON.stringify(body),
        },
        true
      );
      const tone = data.ok ? "success" : "warn";
      setStatus(
        `完成: ${data.success_count || 0}/${data.total_count || 0} 成功`,
        tone
      );
      renderRunResults(data);
    } catch (error) {
      setStatus(error.message, "error");
    } finally {
      setBusy(false);
    }
  };

  document.addEventListener("DOMContentLoaded", () => {
    const accountsInput = $("anyrouter-config-input");
    const providersInput = $("anyrouter-providers-input");
    if (accountsInput && !accountsInput.value.trim()) {
      accountsInput.value = JSON.stringify(EXAMPLE_ACCOUNTS, null, 2);
    }
    if (providersInput && !providersInput.value.trim()) {
      providersInput.value = "{}";
    }
  });
})();
