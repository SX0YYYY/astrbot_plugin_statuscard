(function () {
  "use strict";

  const state = {
    tab: "overview",
    historyRange: "24h",
    eventDays: "1",
    loaded: new Set(),
  };

  const $ = (id) => document.getElementById(id);
  const ESC = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  };

  function getBridge() {
    return window.AstrBotPluginPage || null;
  }

  async function waitForBridge(timeout) {
    const limit = timeout === undefined ? 3000 : timeout;
    const started = Date.now();
    while (!getBridge() && Date.now() - started < limit) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    return getBridge();
  }

  function esc(value) {
    return String(value === null || value === undefined ? "" : value).replace(
      /[&<>"']/g,
      (char) => ESC[char],
    );
  }

  function t(key, fallback, vars) {
    let text = fallback || key;
    const bridge = getBridge();
    if (bridge && typeof bridge.t === "function") {
      text = bridge.t("pages.status.strings." + key, text);
    }
    if (vars) {
      Object.keys(vars).forEach((name) => {
        text = text.split("{" + name + "}").join(String(vars[name]));
      });
    }
    return text;
  }

  function currentLocale() {
    const bridge = getBridge();
    if (bridge && typeof bridge.getLocale === "function") {
      return bridge.getLocale() || "zh-CN";
    }
    return "zh-CN";
  }

  function applyStaticI18n() {
    document.querySelectorAll("[data-i18n]").forEach((element) => {
      const key = element.getAttribute("data-i18n");
      element.textContent = t(key, element.textContent);
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((element) => {
      const key = element.getAttribute("data-i18n-placeholder");
      element.setAttribute(
        "placeholder",
        t(key, element.getAttribute("placeholder") || ""),
      );
    });
    document.querySelectorAll("[data-i18n-aria]").forEach((element) => {
      const key = element.getAttribute("data-i18n-aria");
      element.setAttribute(
        "aria-label",
        t(key, element.getAttribute("aria-label") || ""),
      );
    });
    document.documentElement.lang = currentLocale();
    document.title = t("app_name", "系统状态看板");
  }

  function toast(message) {
    const el = $("toast");
    if (!el) return;
    el.textContent = message;
    el.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => el.classList.remove("show"), 2600);
  }

  async function apiGet(name, params) {
    const bridge = getBridge();
    if (!bridge || typeof bridge.apiGet !== "function") {
      throw new Error(
        t("error_bridge", "插件页面桥接不可用，请从 AstrBot 插件页面打开"),
      );
    }
    const data = await bridge.apiGet(name, params);
    return typeof data === "string" ? JSON.parse(data) : data;
  }

  async function apiPost(name, body) {
    const bridge = getBridge();
    if (!bridge || typeof bridge.apiPost !== "function") {
      throw new Error(
        t("error_bridge", "插件页面桥接不可用，请从 AstrBot 插件页面打开"),
      );
    }
    const data = await bridge.apiPost(name, body);
    return typeof data === "string" ? JSON.parse(data) : data;
  }

  // ------------------------------------------------------------------
  // formatting
  // ------------------------------------------------------------------

  function formatBytes(value) {
    let number = Number(value);
    if (!Number.isFinite(number)) return "--";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let index = 0;
    while (Math.abs(number) >= 1024 && index < units.length - 1) {
      number /= 1024;
      index += 1;
    }
    const digits = index === 0 ? 0 : number >= 100 ? 0 : 1;
    return number.toFixed(digits) + " " + units[index];
  }

  function formatRate(value) {
    return formatBytes(value) + "/s";
  }

  function formatTime(ts) {
    const date = new Date(Number(ts) * 1000);
    return date.toLocaleTimeString(currentLocale(), {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function formatDateTime(ts) {
    const date = new Date(Number(ts) * 1000);
    return date.toLocaleString(currentLocale(), {
      hour12: false,
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function labelForTs(ts, range) {
    const date = new Date(Number(ts) * 1000);
    if (range === "24h") {
      return date.toLocaleTimeString(currentLocale(), {
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
      });
    }
    return date.toLocaleDateString(currentLocale(), {
      month: "2-digit",
      day: "2-digit",
    });
  }

  function pctText(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) {
      return "--";
    }
    return Number(value).toFixed(1) + "%";
  }

  function tone(percent) {
    if (!Number.isFinite(percent)) return "";
    if (percent >= 90) return "bad";
    if (percent >= 70) return "warn";
    return "good";
  }

  function metric(label, value, sub, percent) {
    const bar =
      percent === null || percent === undefined || !Number.isFinite(percent)
        ? ""
        : '<div class="bar ' +
          tone(percent) +
          '"><i style="width:' +
          Math.max(0, Math.min(100, percent)) +
          '%"></i></div>';
    return (
      '<div class="metric"><div class="metric-label">' +
      esc(label) +
      '</div><div class="metric-value">' +
      esc(value) +
      "</div>" +
      (sub ? '<div class="metric-sub">' + esc(sub) + "</div>" : "") +
      bar +
      "</div>"
    );
  }

  function empty(message) {
    return '<div class="empty">' + esc(message) + "</div>";
  }

  // ------------------------------------------------------------------
  // SVG chart
  // ------------------------------------------------------------------

  function niceMax(value) {
    if (!Number.isFinite(value) || value <= 0) return 1;
    const exp = Math.floor(Math.log10(value));
    const base = value / Math.pow(10, exp);
    const step = [1, 2, 5, 10].find((item) => base <= item) || 10;
    return step * Math.pow(10, exp);
  }

  function renderChart(container, series, options) {
    const labels = options.labels || [];
    const values = series
      .flatMap((item) => item.values)
      .filter((value) => value !== null && Number.isFinite(value));
    if (!labels.length || !values.length) {
      container.innerHTML = empty(t("empty_chart", "暂无图表数据"));
      return;
    }
    const width = 640;
    const height = options.height || 200;
    const left = 52;
    const right = 16;
    const top = 14;
    const bottom = 26;
    const plotW = width - left - right;
    const plotH = height - top - bottom;
    const max = options.max || niceMax(Math.max.apply(null, values) * 1.15);
    const formatter = options.format || ((value) => String(Math.round(value)));
    const count = labels.length;
    const x = (index) =>
      left + plotW * (index / Math.max(1, count - 1));
    const y = (value) =>
      top + plotH * (1 - Math.min(1, Math.max(0, value / max)));

    let svg =
      '<svg viewBox="0 0 ' +
      width +
      " " +
      height +
      '" role="img" aria-label="' +
      esc(t("chart_label", "趋势图")) +
      '">';
    svg +=
      '<rect class="plot-bg" x="' +
      left +
      '" y="' +
      top +
      '" width="' +
      plotW +
      '" height="' +
      plotH +
      '" rx="8" />';
    for (let index = 0; index <= 4; index += 1) {
      const lineY = top + plotH * (1 - index / 4);
      svg +=
        '<line class="grid" x1="' +
        left +
        '" y1="' +
        lineY +
        '" x2="' +
        (left + plotW) +
        '" y2="' +
        lineY +
        '" />';
      svg +=
        '<text class="axis" x="' +
        (left - 6) +
        '" y="' +
        (lineY + 4) +
        '" text-anchor="end">' +
        esc(formatter((max * index) / 4)) +
        "</text>";
    }
    const indexes = Array.from(
      new Set(
        [0, 0.25, 0.5, 0.75, 1].map((ratio) =>
          Math.round((count - 1) * ratio),
        ),
      ),
    );
    indexes.forEach((index) => {
      svg +=
        '<text class="axis" x="' +
        x(index) +
        '" y="' +
        (height - 6) +
        '" text-anchor="middle">' +
        esc(labels[index]) +
        "</text>";
    });
    series.forEach((item) => {
      const segments = [];
      let current = [];
      item.values.forEach((value, index) => {
        if (value === null || !Number.isFinite(value)) {
          if (current.length) segments.push(current.join(" "));
          current = [];
          return;
        }
        current.push(x(index).toFixed(1) + "," + y(value).toFixed(1));
      });
      if (current.length) segments.push(current.join(" "));
      segments.forEach((segment) => {
        svg +=
          '<polyline points="' +
          segment +
          '" fill="none" stroke="' +
          item.color +
          '" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round" />';
      });
      const numeric = item.values
        .map((value, index) => [index, value])
        .filter((pair) => pair[1] !== null && Number.isFinite(pair[1]));
      if (numeric.length) {
        const peak = numeric.reduce(
          (best, pair) => (pair[1] > best[1] ? pair : best),
          numeric[0],
        );
        if (peak[1] > 0) {
          svg +=
            '<circle cx="' +
            x(peak[0]) +
            '" cy="' +
            y(peak[1]) +
            '" r="3" fill="' +
            item.color +
            '" />';
          svg +=
            '<text class="peak" x="' +
            x(peak[0]) +
            '" y="' +
            (y(peak[1]) - 7) +
            '" text-anchor="middle">' +
            esc(formatter(peak[1])) +
            "</text>";
        }
      }
    });
    svg += "</svg>";
    container.innerHTML = svg;
  }

  function renderLegend(container, series) {
    container.innerHTML = series
      .map(
        (item) =>
          '<span class="legend-item"><i style="background:' +
          item.color +
          '"></i>' +
          esc(item.name) +
          "</span>",
      )
      .join("");
  }

  // ------------------------------------------------------------------
  // overview
  // ------------------------------------------------------------------

  async function loadOverview() {
    const data = await apiGet("overview");
    const cpu = data.cpu || {};
    const memory = data.memory || {};
    const swap = data.swap || {};
    const disk = data.disk || {};
    const host = data.host || "--";
    $("subtitle").textContent = host + " · " + (data.platform || "default");
    $("plugin-version").textContent = "v" + (data.plugin_version || "--");

    $("metrics").innerHTML = [
      metric(
        t("metric_cpu", "CPU"),
        pctText(cpu.percent),
        t("metric_cores", "{count} 核 · 负载 {load}", {
          count: cpu.count || 0,
          load:
            cpu.load1 === null || cpu.load1 === undefined
              ? "--"
              : cpu.load1,
        }),
        cpu.percent,
      ),
      metric(
        t("metric_memory", "内存"),
        pctText(memory.percent),
        t("metric_used_total", "{used} / {total}", {
          used: memory.used_text || "--",
          total: memory.total_text || "--",
        }),
        memory.percent,
      ),
      metric(
        t("metric_swap", "SWAP"),
        pctText(swap.percent),
        t("metric_used_total", "{used} / {total}", {
          used: swap.used_text || "--",
          total: swap.total_text || "--",
        }),
        swap.percent,
      ),
      metric(
        disk.path
          ? t("metric_disk_path", "磁盘 {path}", { path: disk.path })
          : t("metric_disk", "磁盘"),
        pctText(disk.percent),
        t("metric_used_total", "{used} / {total}", {
          used: disk.used_text || "--",
          total: disk.total_text || "--",
        }),
        disk.percent,
      ),
      metric(
        t("metric_uptime", "运行时间"),
        data.uptime || "--",
        t("label_astrbot", "AstrBot {version}", {
          version: data.astrbot_version || "--",
        }),
      ),
    ].join("");

    const network = data.network || {};
    $("network-month").textContent =
      network.month || t("label_month_flow", "本月流量");
    if (data.network_redacted) {
      $("network").innerHTML =
        '<div class="network-item"><span>' +
        esc(t("network_redacted", "网络数据已按脱敏配置隐藏")) +
        "</span></div>";
    } else {
      $("network").innerHTML = [
        [t("network_up", "上行"), formatRate(network.up_rate || 0)],
        [t("network_down", "下行"), formatRate(network.down_rate || 0)],
        [t("network_month_up", "本月上行"), network.month_up_text || "--"],
        [t("network_month_down", "本月下行"), network.month_down_text || "--"],
      ]
        .map(
          (pair) =>
            '<div class="network-item"><span>' +
            esc(pair[0]) +
            "</span><strong>" +
            esc(pair[1]) +
            "</strong></div>",
        )
        .join("");
    }

    const audit = data.audit || [];
    const banner = $("audit-banner");
    if (audit.length) {
      banner.hidden = false;
      banner.innerHTML = audit
        .map(
          (issue) =>
            '<div class="audit-item ' +
            esc(issue.level) +
            '"><strong>' +
            esc(
              issue.level === "error"
                ? t("audit_error_tag", "配置错误")
                : t("audit_warning_tag", "配置提醒"),
            ) +
            "</strong><span>" +
            esc(issue.message) +
            "</span></div>",
        )
        .join("");
    } else {
      banner.hidden = true;
      banner.innerHTML = "";
    }

    const monitors = data.monitors || {};
    $("monitor-summary").textContent = t(
      "monitor_summary",
      "正常 {ok} · 异常 {fail} · 未知 {unknown} · 配置错误 {config_error}",
      {
        ok: monitors.ok || 0,
        fail: monitors.fail || 0,
        unknown: monitors.unknown || 0,
        config_error: monitors.config_error || 0,
      },
    );
    const items = monitors.items || [];
    $("monitors").innerHTML = items.length
      ? items
          .map((item) => {
            const meta =
              item.config_error
                ? t("monitor_config_error", "配置不完整（{error}），未探测", {
                    error: item.config_error,
                  })
                : item.status === "fail" && item.last_error
                  ? item.last_error
                  : item.last_checked
                    ? t("monitor_last_check", "最近检查 {time}", {
                        time: formatTime(item.last_checked),
                      })
                    : t("monitor_not_checked", "尚未检查");
            return (
              '<div class="monitor"><i class="dot ' +
              esc(item.status) +
              '"></i><div class="monitor-main"><div class="monitor-name">' +
              esc(item.name) +
              '</div><div class="monitor-meta">' +
              esc(item.kind + " · " + meta) +
              "</div></div></div>"
            );
          })
          .join("")
      : empty(t("monitor_none", "没有配置服务监测目标"));

    const processes = data.processes || [];
    const tbody = $("process-table").querySelector("tbody");
    tbody.innerHTML = processes.length
      ? processes
          .map(
            (item) =>
              "<tr><td>" +
              esc(item.rank) +
              "</td><td>" +
              esc(item.name) +
              "</td><td>" +
              esc(item.cpu.toFixed(1) + "%") +
              "</td><td>" +
              esc(item.mem_mb.toFixed(0) + " MB") +
              "</td></tr>",
          )
          .join("")
      : '<tr><td colspan="4">' +
        esc(t("empty_process", "暂无进程采样")) +
        "</td></tr>";
  }

  // ------------------------------------------------------------------
  // history
  // ------------------------------------------------------------------

  async function exportCsv() {
    const bridge = getBridge();
    if (!bridge || typeof bridge.apiGet !== "function") {
      throw new Error(
        t("error_bridge", "插件页面桥接不可用，请从 AstrBot 插件页面打开"),
      );
    }
    const range = state.historyRange || "7d";
    const kinds = ["resources", "processes", "websites", "events"];
    let exported = 0;
    for (const kind of kinds) {
      const data = await bridge.apiGet("export", { kind, range });
      if (!data || !data.ok) {
        throw new Error(
          (data && data.error) ||
            t("export_partial", "导出在 {kind} 中断（已导出 {count} 个文件）", {
              kind,
              count: exported,
            }),
        );
      }
      if ((data.rows || []).length) exported += 1;
      downloadCsv(kind, range, data.rows || []);
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    toast(
      exported
        ? t("export_done", "已导出 {count} 个文件", { count: exported })
        : t("export_empty", "所选范围内没有可导出的数据"),
    );
  }

  function downloadCsv(kind, range, rows) {
    if (!rows.length) return;
    const headers = Object.keys(rows[0]);
    const esc = (value) => {
      const text = value === null || value === undefined ? "" : String(value);
      return /[",\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
    };
    const lines = [headers.join(",")];
    for (const row of rows) lines.push(headers.map((h) => esc(row[h])).join(","));
    const blob = new Blob(["\ufeff" + lines.join("\n")], {
      type: "text/csv;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "statuscard-" + kind + "-" + range + ".csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function loadHistory() {
    const data = await apiGet("history", { range: state.historyRange });
    $("network-note").textContent = data.network_note || "";
    const points = data.points || [];
    const labels = points.map((point) =>
      labelForTs(point.ts, state.historyRange),
    );
    const series = [
      {
        name: "CPU",
        color: "#2ecc71",
        values: points.map((point) => point.cpu),
      },
      {
        name: "RAM",
        color: "#4f8cff",
        values: points.map((point) => point.ram),
      },
      {
        name: "SWAP",
        color: "#f59e0b",
        values: points.map((point) => point.swap),
      },
    ];
    renderLegend($("history-legend"), series);
    renderChart($("history-chart"), series, {
      labels,
      max: 100,
      format: (value) => Math.round(value) + "%",
    });

    const network = data.network || [];
    if (data.network_redacted) {
      renderLegend($("network-legend"), []);
      $("network-chart").innerHTML =
        '<p class="muted">' +
        esc(t("network_redacted", "网络数据已按脱敏配置隐藏")) +
        "</p>";
      return;
    }
    const networkLabels = network.map((point) => formatTime(point.ts));
    const networkSeries = [
      {
        name: t("network_up", "上行"),
        color: "#10b981",
        values: network.map((point) => point.up),
      },
      {
        name: t("network_down", "下行"),
        color: "#ef4444",
        values: network.map((point) => point.down),
      },
    ];
    renderLegend($("network-legend"), networkSeries);
    renderChart($("network-chart"), networkSeries, {
      labels: networkLabels,
      height: 170,
      format: formatRate,
    });
  }

  // ------------------------------------------------------------------
  // events
  // ------------------------------------------------------------------

  function statusText(status) {
    return (
      {
        alert: t("status_alert", "异常"),
        recovery: t("status_recovery", "恢复"),
        failed: t("status_failed", "推送失败"),
      }[status] ||
      (status ? t("status_unknown", "未知") + " (" + status + ")" : t("status_unknown", "未知"))
    );
  }

  function kindText(kind) {
    return (
      {
        resource: t("kind_resource", "资源"),
        offline: t("kind_offline", "掉线"),
        service: t("kind_service", "服务"),
      }[kind] ||
      kind ||
      t("kind_service", "事件")
    );
  }

  async function loadEvents() {
    const data = await apiGet("events", { days: state.eventDays });
    const items = data.items || [];
    const counts = data.counts || {};
    const summary = Object.keys(counts)
      .map((key) => statusText(key) + " " + counts[key])
      .join(" · ");
    const summaryBox = $("event-summary");
    if (summaryBox) summaryBox.textContent = summary;
    $("event-list").innerHTML = items.length
      ? items
          .map((item) => {
            const badge =
              '<span class="badge ' +
              esc(item.status) +
              '">' +
              esc(statusText(item.status)) +
              "</span>";
            return (
              '<div class="event"><div class="event-time">' +
              esc(formatDateTime(item.ts)) +
              '</div><div class="event-main"><div class="event-title">' +
              badge +
              '<span class="event-name">' +
              esc(item.name) +
              '</span><span class="monitor-meta">' +
              esc(kindText(item.kind)) +
              "</span></div>" +
              (item.detail
                ? '<div class="event-detail">' + esc(item.detail) + "</div>"
                : "") +
              "</div></div>"
            );
          })
          .join("")
      : empty(t("empty_events", "所选范围内没有异常事件"));
  }

  // ------------------------------------------------------------------
  // reports
  // ------------------------------------------------------------------

  function reportDetails(data) {
    const blocks = [];
    const resources = data.resources || {};
    if (resources.available && resources.yesterday) {
      const yesterday = resources.yesterday;
      blocks.push(
        '<div class="detail-block"><h3>' +
          esc(t("report_resource", "资源")) +
          '</h3><div class="detail-grid">' +
          [
            [t("report_cpu_avg", "CPU 均值"), yesterday.cpu && yesterday.cpu.avg_text],
            [t("report_cpu_peak", "CPU 峰值"), yesterday.cpu && yesterday.cpu.peak_text],
            [t("report_ram_avg", "RAM 均值"), yesterday.ram && yesterday.ram.avg_text],
            [t("report_ram_peak", "RAM 峰值"), yesterday.ram && yesterday.ram.peak_text],
            [t("report_swap_avg", "SWAP 均值"), yesterday.swap && yesterday.swap.avg_text],
            [t("report_disk_avg", "磁盘均值"), yesterday.disk && yesterday.disk.avg_text],
          ]
            .map(
              (pair) =>
                '<div class="detail-item"><span>' +
                esc(pair[0]) +
                "</span><strong>" +
                esc(pair[1] || "--") +
                "</strong></div>",
            )
            .join("") +
          "</div></div>",
      );
    }
    const qq = data.qq || {};
    if (qq.yesterday) {
      const yesterday = qq.yesterday;
      blocks.push(
        '<div class="detail-block"><h3>' +
          esc(t("report_qq", "QQ")) +
          '</h3><div class="detail-grid">' +
          [
            [t("report_friends", "好友"), yesterday.friends_text],
            [t("report_groups", "群聊"), yesterday.groups_text],
            [t("report_members", "群员"), yesterday.members_text],
            [t("report_sent", "发送"), yesterday.sent_text],
            [t("report_recv", "接收"), yesterday.recv_text],
          ]
            .map(
              (pair) =>
                '<div class="detail-item"><span>' +
                esc(pair[0]) +
                "</span><strong>" +
                esc(pair[1] || "--") +
                "</strong></div>",
            )
            .join("") +
          "</div></div>",
      );
    }
    const token = data.token || {};
    if (token.available) {
      blocks.push(
        '<div class="detail-block"><h3>' +
          esc(t("report_token", "Token")) +
          '</h3><div class="detail-grid">' +
          [
            [t("report_total", "总量"), token.total_text],
            [t("report_calls", "调用次数"), token.calls_text],
            [t("report_success", "成功率"), token.success_text],
          ]
            .map(
              (pair) =>
                '<div class="detail-item"><span>' +
                esc(pair[0]) +
                "</span><strong>" +
                esc(pair[1] || "--") +
                "</strong></div>",
            )
            .join("") +
          "</div>" +
          (token.top_models && token.top_models.length
            ? '<div class="table-wrap"><table class="table"><thead><tr><th>' +
              esc(t("table_model", "模型")) +
              "</th><th>" +
              esc(t("table_tokens", "Token")) +
              "</th><th>" +
              esc(t("table_share", "占比")) +
              "</th></tr></thead><tbody>" +
              token.top_models
                .map(
                  (model) =>
                    "<tr><td>" +
                    esc(model.name) +
                    "</td><td>" +
                    esc(model.tokens_text) +
                    "</td><td>" +
                    esc(model.share_text) +
                    "</td></tr>",
                )
                .join("") +
              "</tbody></table></div>"
            : "") +
          "</div>",
      );
    }
    const website = data.website || {};
    if (website.available && website.targets && website.targets.length) {
      blocks.push(
        '<div class="detail-block"><h3>' +
          esc(t("report_website", "网站延迟")) +
          '</h3><div class="table-wrap"><table class="table"><thead><tr><th>' +
          esc(t("table_target", "目标")) +
          "</th><th>" +
          esc(t("table_avg", "平均")) +
          "</th><th>" +
          esc(t("table_peak", "峰值")) +
          "</th><th>" +
          esc(t("table_availability", "可用率")) +
          "</th></tr></thead><tbody>" +
          website.targets
            .map(
              (target) =>
                "<tr><td>" +
                esc(target.name) +
                "</td><td>" +
                esc(target.avg_text) +
                "</td><td>" +
                esc(target.peak_text) +
                "</td><td>" +
                esc(target.availability_text) +
                "</td></tr>",
            )
            .join("") +
          "</tbody></table></div></div>",
      );
    }
    const process = data.process || {};
    if (process.available && process.cpu && process.cpu.length) {
      blocks.push(
        '<div class="detail-block"><h3>' +
          esc(t("report_process", "Top 进程")) +
          '</h3><div class="table-wrap"><table class="table"><thead><tr><th>' +
          esc(t("table_rank", "#")) +
          "</th><th>" +
          esc(t("table_process", "进程")) +
          "</th><th>" +
          esc(t("table_cpu", "CPU")) +
          "</th><th>" +
          esc(t("table_mem", "内存")) +
          "</th></tr></thead><tbody>" +
          process.cpu
            .map(
              (item) =>
                "<tr><td>" +
                esc(item.rank) +
                "</td><td>" +
                esc(item.name) +
                "</td><td>" +
                esc(item.cpu_text) +
                "</td><td>" +
                esc(item.mem_text) +
                "</td></tr>",
            )
            .join("") +
          "</tbody></table></div></div>",
      );
    }
    return blocks.join("");
  }

  async function loadReports() {
    const data = await apiGet("reports");
    $("report-day").textContent = data.day
      ? t("report_data_day", "数据日 {day}", { day: data.day })
      : t("report_no_data", "暂无数据");
    const summary = data.summary || {};
    $("report-summary").innerHTML = [
      metric(
        t("report_anomaly", "异常 / 失败"),
        summary.anomaly_count || 0,
        t("report_yesterday", "昨日"),
        null,
      ),
      metric(
        t("report_token", "Token"),
        summary.token_total_text || t("report_no_data", "暂无数据"),
        t("report_last_7d", "最近 7 天"),
        null,
      ),
      metric(
        t("report_sent", "发送"),
        summary.sent_text || "--",
        t("report_yesterday", "昨日"),
        null,
      ),
      metric(
        t("report_recv", "接收"),
        summary.recv_text || "--",
        t("report_yesterday", "昨日"),
        null,
      ),
    ].join("");
    $("report-details").innerHTML = reportDetails(data);

    const runs = data.runs || [];
    const tbody = $("report-runs").querySelector("tbody");
    tbody.innerHTML = runs.length
      ? runs
          .map(
            (run) =>
              "<tr><td>" +
              esc(run.day || "--") +
              "</td><td>" +
              esc(formatDateTime(run.ts)) +
              "</td><td>" +
              (run.ok
                ? t("result_ok", "成功")
                : t("result_fail", "失败")) +
              "</td><td>" +
              esc(run.detail || "--") +
              "</td></tr>",
          )
          .join("")
      : '<tr><td colspan="4">' +
        esc(t("empty_runs", "暂无发送记录")) +
        "</td></tr>";
  }

  // ------------------------------------------------------------------
  // tabs
  // ------------------------------------------------------------------

  async function loadTab(name) {
    if (name === "overview") return loadOverview();
    if (name === "history") return loadHistory();
    if (name === "events") return loadEvents();
    if (name === "reports") return loadReports();
    return undefined;
  }

  async function activateTab(name, force) {
    state.tab = name;
    document.querySelectorAll(".tab").forEach((button) => {
      const active = button.dataset.tab === name;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
      button.setAttribute("tabindex", active ? "0" : "-1");
    });
    document.querySelectorAll(".panel").forEach((panel) => {
      panel.classList.toggle("active", panel.id === "panel-" + name);
    });
    if (!force && state.loaded.has(name)) return;
    try {
      await loadTab(name);
      state.loaded.add(name);
    } catch (error) {
      if (force) throw error;
      toast(error.message || t("error_load", "加载失败"));
    }
  }

  async function refreshCurrent() {
    const button = $("refresh");
    if (button && button.classList.contains("refreshing")) return;
    state.loaded.delete(state.tab);
    setRefreshState(button, "refreshing");
    try {
      await activateTab(state.tab, true);
      setRefreshState(button, "done");
      toast(t("refresh_done", "已刷新"));
    } catch (error) {
      setRefreshState(button, "failed");
      toast(error.message || t("error_load", "加载失败"));
    }
  }

  function setRefreshState(button, status) {
    if (!button) return;
    const panels = $("panels");
    button.classList.remove("refreshing", "refresh-done", "refresh-failed");
    if (panels) panels.classList.remove("is-refreshing");
    if (status === "refreshing") {
      button.classList.add("refreshing");
      button.disabled = true;
      button.textContent = t("refreshing", "刷新中…");
      if (panels) panels.classList.add("is-refreshing");
      return;
    }
    button.disabled = false;
    if (status === "done") {
      button.classList.add("refresh-done");
      button.textContent = t("refresh_done_short", "已刷新");
    } else if (status === "failed") {
      button.classList.add("refresh-failed");
      button.textContent = t("refresh_failed_short", "刷新失败");
    } else {
      button.textContent = t("refresh", "刷新");
    }
    if (status !== "idle") {
      clearTimeout(button._resetTimer);
      button._resetTimer = setTimeout(() => {
        setRefreshState(button, "idle");
      }, 2000);
    }
  }

  // ------------------------------------------------------------------
  // bindings
  // ------------------------------------------------------------------

  function bind() {
    const tabs = Array.from(document.querySelectorAll(".tab"));
    tabs.forEach((button, index) => {
      button.addEventListener("click", () => {
        activateTab(button.dataset.tab);
      });
      button.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
        event.preventDefault();
        const offset = event.key === "ArrowRight" ? 1 : -1;
        const next = tabs[(index + offset + tabs.length) % tabs.length];
        next.focus();
        activateTab(next.dataset.tab);
      });
    });
    $("refresh").addEventListener("click", refreshCurrent);
    $("export-csv").addEventListener("click", () => {
      exportCsv().catch((error) =>
        toast(error.message || t("error_load", "加载失败")),
      );
    });

    document.querySelectorAll("#range-switch button").forEach((button) => {
      button.addEventListener("click", () => {
        state.historyRange = button.dataset.range;
        document
          .querySelectorAll("#range-switch button")
          .forEach((item) => item.classList.toggle("active", item === button));
        loadHistory().catch((error) =>
          toast(error.message || t("error_load", "加载失败")),
        );
      });
    });

    document.querySelectorAll("#event-switch button").forEach((button) => {
      button.addEventListener("click", () => {
        state.eventDays = button.dataset.days;
        document
          .querySelectorAll("#event-switch button")
          .forEach((item) => item.classList.toggle("active", item === button));
        loadEvents().catch((error) =>
          toast(error.message || t("error_load", "加载失败")),
        );
      });
    });


  }

  function bindTheme() {
    const bridge = getBridge();
    if (!bridge || typeof bridge.onContext !== "function") return;
    bridge.onContext((context) => {
      if (!context) return;
      document.documentElement.dataset.theme = context.isDark
        ? "dark"
        : "light";
    });
  }

  async function init() {
    bind();
    const bridge = await waitForBridge();
    if (!bridge) {
      const message = t(
        "error_bridge",
        "插件页面桥接不可用，请从 AstrBot 插件页面打开",
      );
      $("subtitle").textContent = message;
      toast(message);
      return;
    }
    bindTheme();
    if (typeof bridge.ready === "function") {
      try {
        await bridge.ready();
      } catch (error) {
        // The page still works if the initial context is delayed.
      }
    }
    applyStaticI18n();
    await activateTab("overview");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
