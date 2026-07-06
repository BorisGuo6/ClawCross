const { test, expect } = require("@playwright/test");

async function stubMessageCenterNetwork(page) {
  await page.route(
    /https:\/\/cdnjs\.cloudflare\.com\/.*marked.*\.js/,
    (route) =>
      route.fulfill({
        contentType: "application/javascript",
        body: 'window.marked = { parse: (s) => String(s || ""), setOptions() {} };',
      })
  );
  await page.route(
    /https:\/\/cdnjs\.cloudflare\.com\/.*highlight.*\.js/,
    (route) =>
      route.fulfill({
        contentType: "application/javascript",
        body: "window.hljs = { highlightAll() {}, highlightElement() {} };",
      })
  );
  await page.route(/https:\/\/cdnjs\.cloudflare\.com\/.*jszip.*\.js/, (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: "window.JSZip = function JSZip() {};",
    })
  );

  await page.route("**/proxy_check_session", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        valid: true,
        user_id: "test-user",
        has_password: true,
        mode: "local",
      }),
    });
  });

  await page.route("**/api/llm_config_status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ configured: true }),
    });
  });

  await page.route("**/proxy_groups", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true, groups: [] }),
    });
  });
}

test("message center loads pretext and uses it for overview label gutter sizing", async ({
  page,
}) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await stubMessageCenterNetwork(page);
  await page.route("https://example.test/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/html",
      body: "<body>runtime</body>",
    })
  );
  await page.route("http://127.0.0.1:3000/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/html",
      body: "<body>agent server</body>",
    })
  );
  await page.addInitScript(() => {
    window.alert = () => {};
    window.confirm = () => true;
    localStorage.setItem("clawcross_lang", "zh");
  });

  await page.goto("/mobile/group_chat");
  await expect(page.locator("#tab-chats")).toBeVisible();

  const result = await page.evaluate(() => {
    const names = ["超级超级超级长的中文研究协调员名字 AlphaBetaGammaDelta"];
    const expectedWidth = window.ClawcrossTextLayout
      ? window.ClawcrossTextLayout.measureLabelGutter(names, {
          font: "600 10px Arial",
          lineHeight: 12,
          minWidth: 108,
          maxWidth: 176,
          padding: 26,
        })
      : 0;
    _overviewDetailCache = {
      timeline: [
        { elapsed: 0, event: "start" },
        { elapsed: 8, event: "agent_call", agent: names[0] },
        { elapsed: 19, event: "agent_done", agent: names[0] },
      ],
      posts: [
        {
          elapsed: 14,
          author: names[0],
          content: "update",
        },
      ],
      current_round: 1,
    };
    showDiscussionOverview();
    const overlay = document.getElementById("oasis-overview-overlay");
    return {
      ready: Boolean(
        window.ClawcrossTextLayout &&
        typeof window.ClawcrossTextLayout.measureLabelGutter === "function"
      ),
      expectedWidth,
      overlayExists: Boolean(overlay),
      htmlHasMeasuredWidth: Boolean(
        overlay &&
        overlay.innerHTML.includes(
          `width:${expectedWidth}px;flex-shrink:0;overflow:hidden;border-right:1.5px solid #e2e8f0;`
        )
      ),
    };
  });

  expect(result.ready).toBeTruthy();
  expect(result.expectedWidth).toBeGreaterThan(110);
  expect(result.overlayExists).toBeTruthy();
  expect(result.htmlHasMeasuredWidth).toBeTruthy();
  expect(pageErrors).toEqual([]);
});

test("project harness keeps archive rows folded behind a full-width bottom bar", async ({
  page,
}) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await stubMessageCenterNetwork(page);
  await page.route("https://example.test/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/html",
      body: "<body>runtime</body>",
    })
  );
  await page.route("http://127.0.0.1:3000/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/html",
      body: "<body>agent server</body>",
    })
  );
  await page.addInitScript(() => {
    window.alert = () => {};
    window.confirm = () => true;
    localStorage.setItem("clawcross_lang", "zh");
    localStorage.removeItem("clawcross_harness_collapsed_buckets_v1");
  });

  await page.goto("/mobile/group_chat");
  await expect(page.locator("#tab-chats")).toBeVisible();

  await page.evaluate(() => {
    renderHarnessState({
      ok: true,
      projects: [
        {
          project_id: "active-one",
          title: "Active One",
          status: "active",
          metadata: { dashboard_bucket: "research" },
        },
        {
          project_id: "archived-one",
          title: "Archived One",
          status: "archived",
          metadata: { dashboard_bucket: "archive" },
        },
      ],
      tasks: [
        {
          task_id: "task-active",
          project_id: "active-one",
          title: "Live task",
          status: "active",
        },
        {
          task_id: "task-archive",
          project_id: "archived-one",
          title: "Old task",
          status: "archived",
        },
      ],
      agents: [],
      runs: [],
    });
  });

  const archiveSection = page.locator(".harness-archive-section");
  await expect(archiveSection).toHaveClass(/collapsed/);
  await expect(page.locator(".harness-archive-toggle-bar")).toBeVisible();
  const collapsedArchiveDims = await page.evaluate(() => {
    const board = document
      .querySelector(".harness-bucket-board")
      .getBoundingClientRect();
    const section = document
      .querySelector(".harness-archive-section")
      .getBoundingClientRect();
    const bar = document
      .querySelector(".harness-archive-toggle-bar")
      .getBoundingClientRect();
    return {
      boardWidth: board.width,
      sectionWidth: section.width,
      barWidth: bar.width,
    };
  });
  expect(
    Math.abs(
      collapsedArchiveDims.boardWidth - collapsedArchiveDims.sectionWidth
    )
  ).toBeLessThanOrEqual(1);
  expect(
    Math.abs(collapsedArchiveDims.sectionWidth - collapsedArchiveDims.barWidth)
  ).toBeLessThanOrEqual(1);
  await expect(
    page.locator(".harness-bucket-board .harness-project-name", {
      hasText: "Archived One",
    })
  ).toHaveCount(0);
  await expect(
    page.locator(".harness-archive-body .harness-project-name", {
      hasText: "Archived One",
    })
  ).toBeHidden();

  await page.locator(".harness-archive-toggle-bar").click();
  await expect(archiveSection).not.toHaveClass(/collapsed/);
  const expandedArchiveDims = await page.evaluate(() => {
    const board = document
      .querySelector(".harness-bucket-board")
      .getBoundingClientRect();
    const section = document
      .querySelector(".harness-archive-section")
      .getBoundingClientRect();
    const bar = document
      .querySelector(".harness-archive-toggle-bar")
      .getBoundingClientRect();
    return {
      boardWidth: board.width,
      sectionWidth: section.width,
      barWidth: bar.width,
    };
  });
  expect(
    Math.abs(expandedArchiveDims.boardWidth - expandedArchiveDims.sectionWidth)
  ).toBeLessThanOrEqual(1);
  expect(
    Math.abs(expandedArchiveDims.sectionWidth - expandedArchiveDims.barWidth)
  ).toBeLessThanOrEqual(1);
  await expect(
    page.locator(".harness-archive-body .harness-project-name", {
      hasText: "Archived One",
    })
  ).toBeVisible();
  await expect(
    page.locator(".harness-bucket-board .harness-project-name", {
      hasText: "Active One",
    })
  ).toBeVisible();
  expect(pageErrors).toEqual([]);
});

test("project harness shows sandbox runtime tabs from exposed workspace URLs", async ({
  page,
}) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await stubMessageCenterNetwork(page);
  await page.route("https://example.test/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/html",
      body: "<body>runtime</body>",
    })
  );
  await page.route("http://127.0.0.1:3000/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/html",
      body: "<body>agent server</body>",
    })
  );
  const relaySends = [];
  await page.route("**/proxy_harness_channel_session", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        relay: "http",
        channel_session_id: "relay-one",
        next_sequence: 1,
      }),
    })
  );
  await page.route(
    "**/proxy_harness_channel_session/relay-one/events?*",
    (route) => {
      const url = new URL(route.request().url());
      const after = Number(url.searchParams.get("after") || 0);
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          channel_session_id: "relay-one",
          events:
            after < 1
              ? [
                  {
                    sequence: 1,
                    event_type: "channel.message",
                    text: "ready\n",
                  },
                ]
              : [],
          next_sequence: 2,
          closed: false,
        }),
      });
    }
  );
  await page.route(
    "**/proxy_harness_channel_session/relay-one/send",
    async (route) => {
      const body = await route.request().postDataJSON();
      relaySends.push(body.text);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true, sent: true }),
      });
    }
  );
  await page.addInitScript(() => {
    window.alert = () => {};
    window.confirm = () => true;
    localStorage.setItem("clawcross_lang", "zh");
    localStorage.removeItem("clawcross_harness_selected_computer_v1");
    window.__harnessTerminalSockets = [];
    class MockTerminalWebSocket {
      constructor(url) {
        this.url = url;
        this.readyState = MockTerminalWebSocket.CONNECTING;
        window.__harnessTerminalSockets.push(this);
        setTimeout(() => {
          this.readyState = MockTerminalWebSocket.OPEN;
          if (typeof this.onopen === "function") this.onopen();
        }, 0);
      }
      send(data) {
        this.lastSent = data;
        if (typeof this.onmessage === "function") {
          this.onmessage({ data: `echo:${data}` });
        }
      }
      close() {
        this.readyState = MockTerminalWebSocket.CLOSED;
        if (typeof this.onclose === "function") this.onclose();
      }
    }
    MockTerminalWebSocket.CONNECTING = 0;
    MockTerminalWebSocket.OPEN = 1;
    MockTerminalWebSocket.CLOSING = 2;
    MockTerminalWebSocket.CLOSED = 3;
    window.WebSocket = MockTerminalWebSocket;
  });

  await page.goto("/mobile/group_chat");
  await expect(page.locator("#tab-chats")).toBeVisible();

  await page.evaluate(() => {
    renderHarnessState({
      ok: true,
      projects: [
        {
          project_id: "remote-ws",
          title: "Remote Workspace",
          status: "active",
          metadata: { dashboard_bucket: "research" },
        },
      ],
      tasks: [],
      agents: [],
      runners: [
        {
          runner_id: "runner-one",
          status: "idle",
          effective_status: "idle",
          metadata: { tunnel: { connected: true } },
        },
      ],
      workspaces: [
        {
          workspace_id: "remote-ws",
          sandbox_status: "running",
          remote: "dm-26zj-020",
          agent_server_url: "http://127.0.0.1:3000",
          exposed_urls: [
            { label: "vscode", url: "https://example.test/vscode" },
            { label: "browser", url: "https://example.test/browser" },
            { label: "terminal", url: "https://example.test/terminal" },
            {
              label: "terminal-pty",
              runner_id: "runner-one",
              channel_kind: "terminal",
              channel_id: "pty-one",
            },
            { label: "unsafe-terminal", url: "javascript:alert(1)" },
          ],
          metadata: { runner_id: "runner-one" },
        },
      ],
      runs: [],
    });
  });

  const runtimeTabs = page.locator(".harness-runtime-tabs");
  await expect(runtimeTabs).toBeVisible();
  await expect(
    runtimeTabs.locator(".harness-runtime-tab-name", { hasText: "VS Code" })
  ).toBeVisible();
  await expect(
    runtimeTabs.locator(".harness-runtime-tab-name", { hasText: "Browser" })
  ).toBeVisible();
  await expect(
    runtimeTabs.locator(".harness-runtime-tab-name", {
      hasText: "Agent Server",
    })
  ).toBeVisible();
  await expect(
    runtimeTabs.locator(".harness-runtime-tab.terminal")
  ).toHaveCount(2);
  await expect(
    runtimeTabs.locator('a[href="javascript:alert(1)"]')
  ).toHaveCount(0);
  await expect(
    runtimeTabs.locator(
      'button[data-harness-runtime-url="https://example.test/vscode"]'
    )
  ).toHaveCount(1);
  await expect(page.locator(".harness-runtime-pane iframe")).toHaveAttribute(
    "src",
    "https://example.test/vscode"
  );

  await runtimeTabs
    .locator('button[data-harness-runtime-url="https://example.test/browser"]')
    .click();
  await expect(page.locator(".harness-runtime-pane iframe")).toHaveAttribute(
    "src",
    "https://example.test/browser"
  );
  await expect(
    runtimeTabs.locator('a[href="https://example.test/terminal"]')
  ).toHaveCount(1);
  await runtimeTabs.locator("button.harness-runtime-tab.terminal").click();
  await expect(page.locator(".harness-terminal-pane")).toBeVisible();
  await page
    .locator(".harness-terminal-action", { hasText: "Connect" })
    .click();
  await expect(page.locator(".harness-terminal-status")).toHaveText("live");
  await expect(page.locator(".harness-terminal-output")).toContainText("ready");
  await page.locator(".harness-terminal-input").fill("pwd");
  await page.locator(".harness-terminal-input").press("Enter");
  await expect(page.locator(".harness-terminal-output")).toContainText("$ pwd");
  expect(relaySends).toContain("pwd\n");
  const socketUrls = await page.evaluate(() =>
    window.__harnessTerminalSockets.map((socket) => socket.url)
  );
  expect(socketUrls).toEqual([]);

  await page.evaluate(() => {
    renderHarnessState({
      ok: true,
      projects: [
        {
          project_id: "remote-ws",
          title: "Remote Workspace",
          status: "active",
          metadata: { dashboard_bucket: "research" },
        },
      ],
      tasks: [],
      agents: [],
      runners: [
        {
          runner_id: "runner-one",
          status: "idle",
          effective_status: "idle",
          metadata: { tunnel: { connected: true } },
        },
      ],
      workspaces: [
        {
          workspace_id: "remote-ws",
          sandbox_status: "failed",
          remote: "dm-26zj-020",
          agent_server_url: "",
          exposed_urls: [],
          health: { ready: false, error: "stale runtime" },
          metadata: { runner_id: "runner-one" },
        },
      ],
      runs: [],
    });
  });
  await expect(page.locator(".harness-runtime-tabs")).toHaveCount(0);
  await expect(page.locator(".harness-runtime-pane")).toHaveCount(0);
  await expect(page.locator(".harness-terminal-pane")).toHaveCount(0);
  expect(pageErrors).toEqual([]);
});
