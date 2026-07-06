(function () {
  "use strict";

  const STUN_SERVERS = [
    "stun:stun.l.google.com:19302",
    "stun:stun1.l.google.com:19302",
    "stun:stun2.l.google.com:19302",
    "stun:stun.cloudflare.com:3478",
    "stun:global.stun.twilio.com:3478",
  ];
  const COLLECT_TIMEOUT_MS = 4200;
  let activeRunId = 0;

  function byId(id) {
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

  function setText(id, value) {
    const el = byId(id);
    if (el) el.textContent = value;
  }

  function setStatus(message, state) {
    const el = byId("nat-status");
    if (!el) return;
    el.textContent = message;
    el.className = "nat-status-card" + (state ? ` ${state}` : "");
  }

  function parseCandidate(raw, server) {
    const original = String(raw || "").trim();
    if (!original) return null;
    const line = original
      .replace(/^a=/, "")
      .replace(/^candidate:/, "")
      .trim();
    const parts = line.split(/\s+/);
    const typIndex = parts.indexOf("typ");
    if (parts.length < 8 || typIndex < 0) return null;
    const extras = {};
    for (let i = typIndex + 2; i < parts.length - 1; i += 2) {
      extras[parts[i]] = parts[i + 1];
    }
    return {
      server,
      raw: original,
      foundation: parts[0],
      component: parts[1],
      protocol: String(parts[2] || "").toLowerCase(),
      priority: parts[3],
      address: parts[4],
      port: parts[5],
      type: parts[typIndex + 1],
      relatedAddress: extras.raddr || "",
      relatedPort: extras.rport || "",
      tcpType: extras.tcptype || "",
    };
  }

  function endpointOf(candidate) {
    return `${candidate.address}:${candidate.port}`;
  }

  function unique(values) {
    return Array.from(new Set(values.filter(Boolean)));
  }

  function isMdnsAddress(address) {
    return /\.local$/i.test(String(address || ""));
  }

  function isSharedOrPrivateIpv4(address) {
    const parts = String(address || "")
      .split(".")
      .map((part) => Number(part));
    if (
      parts.length !== 4 ||
      parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)
    ) {
      return false;
    }
    const [a, b] = parts;
    return (
      a === 10 ||
      a === 127 ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168) ||
      (a === 169 && b === 254) ||
      (a === 100 && b >= 64 && b <= 127)
    );
  }

  function isPrivateIpv6(address) {
    const lower = String(address || "").toLowerCase();
    return (
      lower === "::1" ||
      lower.startsWith("fe80:") ||
      lower.startsWith("fc") ||
      lower.startsWith("fd")
    );
  }

  function isPublicCandidate(candidate) {
    const address = candidate && candidate.address;
    if (!address || isMdnsAddress(address)) return false;
    if (address.includes(":")) return !isPrivateIpv6(address);
    return !isSharedOrPrivateIpv4(address);
  }

  function readSdpCandidates(pc, server) {
    const sdp =
      pc && pc.localDescription && pc.localDescription.sdp
        ? pc.localDescription.sdp
        : "";
    return sdp
      .split(/\r?\n/)
      .filter((line) => line.startsWith("a=candidate:"))
      .map((line) => parseCandidate(line.slice(2), server))
      .filter(Boolean);
  }

  function collectFromServer(server, runId) {
    return new Promise((resolve) => {
      const candidates = [];
      const errors = [];
      let pc = null;
      let settled = false;
      let timer = null;

      function addCandidate(raw) {
        const parsed = parseCandidate(raw, server);
        if (parsed) candidates.push(parsed);
      }

      function finish(status) {
        if (settled) return;
        settled = true;
        if (timer) clearTimeout(timer);
        if (pc) {
          readSdpCandidates(pc, server).forEach((candidate) =>
            candidates.push(candidate)
          );
          try {
            pc.close();
          } catch (_) {}
        }
        const seen = new Set();
        const deduped = candidates.filter((candidate) => {
          const key = `${candidate.server}|${candidate.raw}`;
          if (seen.has(key)) return false;
          seen.add(key);
          return true;
        });
        resolve({ server, status, candidates: deduped, errors });
      }

      try {
        pc = new RTCPeerConnection({
          iceServers: [{ urls: server }],
          iceCandidatePoolSize: 0,
        });
        pc.onicecandidate = (event) => {
          if (runId !== activeRunId) return finish("cancelled");
          if (event && event.candidate && event.candidate.candidate) {
            addCandidate(event.candidate.candidate);
          } else {
            finish("complete");
          }
        };
        pc.onicegatheringstatechange = () => {
          if (pc && pc.iceGatheringState === "complete") finish("complete");
        };
        pc.onicecandidateerror = (event) => {
          const code = event && event.errorCode ? event.errorCode : "error";
          const text =
            event && event.errorText ? event.errorText : "ICE candidate error";
          errors.push(`${code}: ${text}`);
        };
        pc.createDataChannel("clawcross-nat-probe");
        timer = setTimeout(() => finish("timeout"), COLLECT_TIMEOUT_MS);
        pc.createOffer()
          .then((offer) => pc.setLocalDescription(offer))
          .catch((error) => {
            errors.push(error && error.message ? error.message : String(error));
            finish("error");
          });
      } catch (error) {
        errors.push(error && error.message ? error.message : String(error));
        finish("error");
      }
    });
  }

  function impactForProfile(profile) {
    if (profile === "stable") {
      return {
        game: { label: "优秀", score: 92, color: "#16a34a" },
        p2p: { label: "良好", score: 82, color: "#0ea5e9" },
        video: { label: "良好", score: 84, color: "#0ea5e9" },
        remote: { label: "良好", score: 78, color: "#0ea5e9" },
      };
    }
    if (profile === "symmetric") {
      return {
        game: { label: "受限", score: 44, color: "#f59e0b" },
        p2p: { label: "受限", score: 34, color: "#dc2626" },
        video: { label: "一般", score: 56, color: "#f59e0b" },
        remote: { label: "受限", score: 32, color: "#dc2626" },
      };
    }
    return {
      game: { label: "未知", score: 18, color: "#94a3b8" },
      p2p: { label: "未知", score: 18, color: "#94a3b8" },
      video: { label: "未知", score: 18, color: "#94a3b8" },
      remote: { label: "未知", score: 18, color: "#94a3b8" },
    };
  }

  function analyzeResults(results) {
    const allCandidates = results.flatMap((result) => result.candidates || []);
    const srflx = allCandidates.filter(
      (candidate) => candidate.type === "srflx" && candidate.protocol === "udp"
    );
    const publicSrflx = srflx.filter(isPublicCandidate);
    const endpoints = unique(publicSrflx.map(endpointOf));
    const ports = unique(publicSrflx.map((candidate) => candidate.port));
    const ips = unique(publicSrflx.map((candidate) => candidate.address));
    const serversWithSrflx = results.filter((result) => {
      return (result.candidates || []).some(
        (candidate) =>
          candidate.type === "srflx" && candidate.protocol === "udp"
      );
    }).length;
    const serverCount = results.length;
    const privateOrSharedSrflx = srflx.filter(
      (candidate) => !isPublicCandidate(candidate)
    );

    let profile = "partial";
    let title = "未获得可用公网候选";
    let confidence = "LOW";
    let summary =
      "浏览器没有暴露公网 srflx 候选。常见原因是 WebRTC 被禁用、UDP 被拦截、STUN 不可达，或浏览器隐私策略收紧。";

    if (publicSrflx.length > 0 && serversWithSrflx < 2) {
      title = "公网候选可见，样本不足";
      confidence = "LOW";
      summary =
        "只从一个 STUN 目标获得公网端点，能确认浏览器看到公网映射，但不能判断映射是否稳定。";
    } else if (
      publicSrflx.length > 0 &&
      endpoints.length === 1 &&
      ports.length === 1
    ) {
      profile = "stable";
      title = "Cone-like / 映射稳定";
      confidence = "MED";
      summary =
        "多个 STUN 目标看到同一个公网 IP:端口。浏览器端可判断映射稳定，但不能继续严格区分 NAT1、NAT2、NAT3。";
    } else if (
      publicSrflx.length > 0 &&
      (endpoints.length > 1 || ports.length > 1)
    ) {
      profile = "symmetric";
      title = "NAT4 / Symmetric 可能性高";
      confidence = "MED";
      summary =
        "不同 STUN 目标看到的公网端点不一致。该模式通常会降低 P2P、远程访问和部分游戏联机成功率。";
    }

    if (privateOrSharedSrflx.length && !publicSrflx.length) {
      title = "疑似 CGNAT / 私网出口";
      confidence = "LOW";
      summary =
        "srflx 候选落在私网或运营商共享地址段，浏览器没有看到可直接公网路由的端点。";
    }

    return {
      allCandidates,
      srflx,
      publicSrflx,
      endpoints,
      ips,
      ports,
      serverCount,
      serversWithSrflx,
      profile,
      title,
      confidence,
      summary,
      impact: impactForProfile(profile),
    };
  }

  function renderServerRows(results) {
    const body = byId("nat-servers-table");
    if (!body) return;
    if (!results.length) {
      body.innerHTML = '<tr><td colspan="4">暂无数据</td></tr>';
      return;
    }
    body.innerHTML = results
      .map((result) => {
        const srflx = (result.candidates || []).filter(
          (candidate) =>
            candidate.type === "srflx" && candidate.protocol === "udp"
        );
        const endpoints = unique(srflx.map(endpointOf));
        const statusClass =
          result.status === "complete"
            ? ""
            : result.status === "timeout"
              ? "warn"
              : "error";
        const status =
          result.errors && result.errors.length
            ? `${result.status}; ${result.errors[0]}`
            : result.status;
        return `<tr>
                <td><code>${escapeHtml(result.server)}</code></td>
                <td><span class="nat-pill ${statusClass}">${escapeHtml(status)}</span></td>
                <td>${endpoints.length ? endpoints.map((item) => `<code>${escapeHtml(item)}</code>`).join("<br>") : "-"}</td>
                <td>${srflx.length}</td>
            </tr>`;
      })
      .join("");
  }

  function renderCandidates(candidates) {
    const list = byId("nat-candidate-list");
    if (!list) return;
    if (!candidates.length) {
      list.textContent = "暂无候选。";
      return;
    }
    list.innerHTML = candidates
      .map((candidate) => {
        const endpoint =
          candidate.address && candidate.port
            ? `${candidate.address}:${candidate.port}`
            : "-";
        const related = candidate.relatedAddress
          ? `<div>related: <code>${escapeHtml(candidate.relatedAddress)}:${escapeHtml(candidate.relatedPort)}</code></div>`
          : "";
        return `<div class="nat-candidate-card">
                <b>${escapeHtml(candidate.type || "candidate")} / ${escapeHtml(candidate.protocol || "-")}</b>
                <div>server: <code>${escapeHtml(candidate.server)}</code></div>
                <div>endpoint: <code>${escapeHtml(endpoint)}</code></div>
                ${related}
                <code>${escapeHtml(candidate.raw)}</code>
            </div>`;
      })
      .join("");
  }

  function renderImpact(impact) {
    const panel = byId("nat-app-impact");
    if (panel) panel.style.display = "block";
    Object.keys(impact).forEach((key) => {
      const item = document.querySelector(`[data-nat-impact="${key}"]`);
      if (!item) return;
      const value = impact[key];
      const bar = item.querySelector(".nat-impact-bar span");
      const label = item.querySelector("strong");
      if (bar) {
        bar.style.width = `${value.score}%`;
        bar.style.background = value.color;
      }
      if (label) label.textContent = `${value.label} · ${value.score}`;
    });
  }

  function renderAnalysis(results, analysis) {
    const card = byId("nat-result-card");
    if (card) {
      card.className = `nat-result-card nat-result-card--${analysis.profile}`;
    }
    setText("nat-result-title", analysis.title);
    setText("nat-result-confidence", `CONFIDENCE: ${analysis.confidence}`);
    setText("nat-result-summary", analysis.summary);
    setText(
      "nat-public-ip",
      analysis.ips.length ? analysis.ips.join(", ") : "-"
    );
    setText(
      "nat-public-port",
      analysis.ports.length ? analysis.ports.join(", ") : "-"
    );
    setText("nat-candidate-count", String(analysis.allCandidates.length));
    renderServerRows(results);
    renderCandidates(analysis.allCandidates);
    renderImpact(analysis.impact);
  }

  function resetNatCheck() {
    activeRunId += 1;
    const button = byId("nat-check-btn");
    if (button) {
      button.disabled = false;
      button.textContent = "开始检测";
    }
    const card = byId("nat-result-card");
    if (card) card.className = "nat-result-card nat-result-card--idle";
    setStatus(
      "等待检测。浏览器需要允许 WebRTC，检测只读取 ICE 候选，不访问麦克风或摄像头。"
    );
    setText("nat-result-title", "未检测");
    setText("nat-result-confidence", "CONFIDENCE: UNKNOWN");
    setText("nat-result-summary", "点击开始检测后显示结果。");
    setText("nat-public-ip", "-");
    setText("nat-public-port", "-");
    setText("nat-candidate-count", "0");
    renderServerRows([]);
    renderCandidates([]);
    const panel = byId("nat-app-impact");
    if (panel) panel.style.display = "none";
  }

  async function runNatCheck() {
    const button = byId("nat-check-btn");
    const RTCPeer =
      window.RTCPeerConnection ||
      window.webkitRTCPeerConnection ||
      window.mozRTCPeerConnection;
    if (!RTCPeer) {
      setStatus(
        "当前浏览器没有 RTCPeerConnection，无法执行 WebRTC NAT 检测。",
        "error"
      );
      return;
    }
    if (!window.RTCPeerConnection) {
      window.RTCPeerConnection = RTCPeer;
    }

    const runId = activeRunId + 1;
    activeRunId = runId;
    if (button) {
      button.disabled = true;
      button.textContent = "检测中...";
    }
    setStatus(
      `正在从 ${STUN_SERVERS.length} 个 STUN 服务器收集 ICE candidates...`,
      "running"
    );
    renderServerRows([]);
    renderCandidates([]);

    try {
      const results = await Promise.all(
        STUN_SERVERS.map((server) => collectFromServer(server, runId))
      );
      if (runId !== activeRunId) return;
      const analysis = analyzeResults(results);
      renderAnalysis(results, analysis);
      setStatus(
        `检测完成：${analysis.serversWithSrflx}/${analysis.serverCount} 个 STUN 目标返回 srflx 候选。`
      );
    } catch (error) {
      setStatus(
        `检测失败：${error && error.message ? error.message : String(error)}`,
        "error"
      );
    } finally {
      if (runId === activeRunId && button) {
        button.disabled = false;
        button.textContent = "重新检测";
      }
    }
  }

  window.runNatCheck = runNatCheck;
  window.resetNatCheck = resetNatCheck;
})();
