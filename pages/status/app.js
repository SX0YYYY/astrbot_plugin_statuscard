(function () {
  "use strict";

  const bridge = window.AstrBotPluginPage;
  const state = {
    tab: "overview",
    historyRange: "24h",
    eventDays: "1",
    loaded: new Set(),
    todosLoaded: false,
  };

  const $ = (id) => document.getElementById(id);
  const ESC = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  };

  function esc(value) {
    return String(value === null || value === undefined ? "" : value).replace(
      /[&<>"']/g,
      (char) => ESC[char],
    );
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
    if (!bridge || typeof bridge.apiGet !== "function") {
      throw new Error("插件页面桥接不可用，请从 AstrBot 插件页面打开");
    }
    const data = await bridge.apiGet(name, params);
    return typeof data === "string" ? JSON.parse(data) : data;
  }

  async function apiPost(name, body) {
    if (!bridge || typeof bridge.apiPost !== "function") {
      throw new Error("插件页面桥接不可用，请从 AstrBot 插件页面打开");
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
    return date.toLocaleTimeString("zh-CN", {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function formatDateTime(ts) {
    const date = new Date(Number(ts) * 1000);
    return date.toLocaleString("zh-CN", {
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
      return date.toLocaleTimeString("zh-CN", {
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
      });
    }
    return date.toLocaleDateString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
    });
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
      container.innerHTML = empty("暂无图表数据");
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
      '" role="img" aria-label="趋势图">';
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
    $("subtitle").textContent =
      host + " · " + (data.platform || "default") + " · " + (data.uptime || "--");
    $("plugin-version").textContent = "v" + (data.plugin_version || "--");

    $("metrics").innerHTML = [
      metric(
        "CPU",
        (cpu.percent || 0).toFixed(1) + "%",
        (cpu.count || 0) +
          " 核 · 负载 " +
          (cpu.load1 === null || cpu.load1 === undefined
            ? "--"
            : cpu.load1),
        cpu.percent,
      ),
      metric(
        "内存",
        (memory.percent || 0).toFixed(1) + "%",
        (memory.used_text || "--") + " / " + (memory.total_text || "--"),
        memory.percent,
      ),
      metric(
        "SWAP",
        (swap.percent || 0).toFixed(1) + "%",
        (swap.used_text || "--") + " / " + (swap.total_text || "--"),
        swap.percent,
      ),
      metric(
        "磁盘",
        disk.percent === null || disk.percent === undefined
          ? "--"
          : disk.percent.toFixed(1) + "%",
        (disk.used_text || "--") + " / " + (disk.total_text || "--"),
        disk.percent,
      ),
      metric("运行时间", data.uptime || "--", "AstrBot " + (data.astrbot_version || "--")),
      metric(
        "待办",
        String((data.todos && data.todos.open) || 0),
        "未完成",
        null,
      ),
    ].join("");

    const network = data.network || {};
    $("network-month").textContent = network.month || "本月流量";
    $("network").innerHTML = [
      ["上行", formatRate(network.up_rate || 0)],
      ["下行", formatRate(network.down_rate || 0)],
      ["本月上行", network.month_up_text || "--"],
      ["本月下行", network.month_down_text || "--"],
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

    const monitors = data.monitors || {};
    $("monitor-summary").textContent =
      "正常 " +
      (monitors.ok || 0) +
      " · 异常 " +
      (monitors.fail || 0) +
      " · 未知 " +
      (monitors.unknown || 0);
    const items = monitors.items || [];
    $("monitors").innerHTML = items.length
      ? items
          .map((item) => {
            const meta =
              item.status === "fail" && item.last_error
                ? item.last_error
                : item.last_checked
                  ? "最近检查 " + formatTime(item.last_checked)
                  : "尚未检查";
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
      : empty("没有配置服务监测目标");

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
      : '<tr><td colspan="4">暂无进程采样</td></tr>';
  }

  // ------------------------------------------------------------------
  // history
  // ------------------------------------------------------------------

  async function loadHistory() {
    const data = await apiGet("history", { range: state.historyRange });
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
    const networkLabels = network.map((point) => formatTime(point.ts));
    const networkSeries = [
      {
        name: "上行",
        color: "#10b981",
        values: network.map((point) => point.up),
      },
      {
        name: "下行",
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
      { alert: "异常", recovery: "恢复", failed: "推送失败" }[status] ||
      status ||
      "事件"
    );
  }

  function kindText(kind) {
    return (
      { resource: "资源", offline: "掉线", service: "服务" }[kind] ||
      kind ||
      "事件"
    );
  }

  async function loadEvents() {
    const data = await apiGet("events", { days: state.eventDays });
    const items = data.items || [];
    const counts = data.counts || {};
    const summary = Object.keys(counts)
      .map((key) => statusText(key) + " " + counts[key])
      .join(" · ");
    if (summary) toast(summary);
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
      : empty("所选范围内没有异常事件");
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
        '<div class="detail-block"><h3>资源</h3><div class="detail-grid">' +
          [
            ["CPU 均值", yesterday.cpu && yesterday.cpu.avg_text],
            ["CPU 峰值", yesterday.cpu && yesterday.cpu.peak_text],
            ["RAM 均值", yesterday.ram && yesterday.ram.avg_text],
            ["RAM 峰值", yesterday.ram && yesterday.ram.peak_text],
            ["SWAP 均值", yesterday.swap && yesterday.swap.avg_text],
            ["磁盘均值", yesterday.disk && yesterday.disk.avg_text],
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
        '<div class="detail-block"><h3>QQ</h3><div class="detail-grid">' +
          [
            ["好友", yesterday.friends_text],
            ["群聊", yesterday.groups_text],
            ["群员", yesterday.members_text],
            ["发送", yesterday.sent_text],
            ["接收", yesterday.recv_text],
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
        '<div class="detail-block"><h3>Token</h3><div class="detail-grid">' +
          [
            ["总量", token.total_text],
            ["调用次数", token.calls_text],
            ["成功率", token.success_text],
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
            ? '<div class="table-wrap"><table class="table"><thead><tr><th>模型</th><th>Token</th><th>占比</th></tr></thead><tbody>' +
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
    const todo = data.todo || {};
    if (todo.available) {
      blocks.push(
        '<div class="detail-block"><h3>待办</h3><div class="detail-grid">' +
          [
            ["昨日新增", todo.created_count],
            ["昨日完成", todo.completed_count],
            ["未完成", todo.open_count],
          ]
            .map(
              (pair) =>
                '<div class="detail-item"><span>' +
                esc(pair[0]) +
                "</span><strong>" +
                esc(pair[1]) +
                "</strong></div>",
            )
            .join("") +
          "</div></div>",
      );
    }
    const website = data.website || {};
    if (website.available && website.targets && website.targets.length) {
      blocks.push(
        '<div class="detail-block"><h3>网站延迟</h3><div class="table-wrap"><table class="table"><thead><tr><th>目标</th><th>平均</th><th>峰值</th><th>可用率</th></tr></thead><tbody>' +
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
        '<div class="detail-block"><h3>Top 进程</h3><div class="table-wrap"><table class="table"><thead><tr><th>#</th><th>进程</th><th>CPU</th><th>内存</th></tr></thead><tbody>' +
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
      ? "数据日 " + data.day
      : "暂无数据";
    const summary = data.summary || {};
    $("report-summary").innerHTML = [
      metric("异常 / 失败", summary.anomaly_count || 0, "昨日", null),
      metric("Token", summary.token_total_text || "暂无数据", "最近 7 天", null),
      metric("发送", summary.sent_text || "--", "昨日", null),
      metric("接收", summary.recv_text || "--", "昨日", null),
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
              (run.ok ? "成功" : "失败") +
              "</td><td>" +
              esc(run.detail || "--") +
              "</td></tr>",
          )
          .join("")
      : '<tr><td colspan="4">暂无发送记录</td></tr>';
  }

  // ------------------------------------------------------------------
  // todos
  // ------------------------------------------------------------------

  async function loadTodos() {
    const data = await apiGet("todos", { include_done: "true" });
    const todos = data.todos || [];
    const open = todos.filter((item) => item.status !== "done");
    const done = todos.filter((item) => item.status === "done");
    $("todo-summary").textContent =
      "未完成 " + open.length + " · 已完成 " + done.length;
    $("todo-list").innerHTML = todos.length
      ? todos
          .map((item) => {
            const isDone = item.status === "done";
            return (
              '<div class="todo ' +
              (isDone ? "done" : "") +
              '"><span class="todo-id">#' +
              esc(item.id) +
              '</span><span class="todo-content">' +
              esc(item.content) +
              '</span><span class="todo-meta">' +
              esc(
                isDone
                  ? "已完成 " + (item.done_day || "")
                  : "创建于 " + (item.created_day || ""),
              ) +
              '</span><span class="todo-actions">' +
              (isDone
                ? ""
                : '<button type="button" data-action="complete" data-id="' +
                  esc(item.id) +
                  '">完成</button>') +
              '<button type="button" class="danger" data-action="delete" data-id="' +
              esc(item.id) +
              '">删除</button></span></div>'
            );
          })
          .join("")
      : empty("暂无待办；在上方输入内容后点击“添加”。");
    state.todosLoaded = true;
  }

  async function todoAction(action, id) {
    const data = await apiPost("todos", { action: action, id: Number(id) });
    if (!data || data.ok === false) {
      throw new Error((data && data.message) || "操作失败");
    }
    toast(data.message || "操作完成");
    await loadTodos();
    if (state.tab === "overview") {
      await loadOverview();
    }
  }

  // ------------------------------------------------------------------
  // tabs
  // ------------------------------------------------------------------

  async function loadTab(name) {
    if (name === "overview") return loadOverview();
    if (name === "history") return loadHistory();
    if (name === "events") return loadEvents();
    if (name === "reports") return loadReports();
    if (name === "todos") return loadTodos();
    return undefined;
  }

  async function activateTab(name, force) {
    state.tab = name;
    document.querySelectorAll(".tab").forEach((button) => {
      const active = button.dataset.tab === name;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll(".panel").forEach((panel) => {
      panel.classList.toggle("active", panel.id === "panel-" + name);
    });
    if (!force && state.loaded.has(name)) return;
    try {
      await loadTab(name);
      state.loaded.add(name);
    } catch (error) {
      toast(error.message || "加载失败");
    }
  }

  async function refreshCurrent() {
    state.loaded.delete(state.tab);
    if (state.tab === "todos") state.todosLoaded = false;
    await activateTab(state.tab, true);
  }

  // ------------------------------------------------------------------
  // bindings
  // ------------------------------------------------------------------

  function bind() {
    document.querySelectorAll(".tab").forEach((button) => {
      button.addEventListener("click", () => {
        activateTab(button.dataset.tab);
      });
    });
    $("refresh").addEventListener("click", refreshCurrent);

    document.querySelectorAll("#range-switch button").forEach((button) => {
      button.addEventListener("click", () => {
        state.historyRange = button.dataset.range;
        document
          .querySelectorAll("#range-switch button")
          .forEach((item) => item.classList.toggle("active", item === button));
        loadHistory().catch((error) => toast(error.message || "加载失败"));
      });
    });

    document.querySelectorAll("#event-switch button").forEach((button) => {
      button.addEventListener("click", () => {
        state.eventDays = button.dataset.days;
        document
          .querySelectorAll("#event-switch button")
          .forEach((item) => item.classList.toggle("active", item === button));
        loadEvents().catch((error) => toast(error.message || "加载失败"));
      });
    });

    $("todo-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = $("todo-input");
      const content = input.value.trim();
      if (!content) {
        toast("请输入待办内容");
        return;
      }
      try {
        const data = await apiPost("todos", {
          action: "add",
          content: content,
        });
        if (!data || data.ok === false) {
          throw new Error((data && data.message) || "添加失败");
        }
        input.value = "";
        toast(data.message || "已添加");
        await loadTodos();
      } catch (error) {
        toast(error.message || "添加失败");
      }
    });

    $("todo-list").addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      try {
        await todoAction(button.dataset.action, button.dataset.id);
      } catch (error) {
        toast(error.message || "操作失败");
      }
    });

    if (bridge && typeof bridge.onContext === "function") {
      bridge.onContext((context) => {
        if (!context) return;
        document.documentElement.dataset.theme = context.isDark
          ? "dark"
          : "light";
      });
    }
  }

  async function init() {
    bind();
    if (bridge && typeof bridge.ready === "function") {
      try {
        await bridge.ready();
      } catch (error) {
        // The page still works if the initial context is delayed.
      }
    }
    await activateTab("overview");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
