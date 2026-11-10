(function () {
  "use strict";

  const state = {
    tab: "overview",
    historyRange: "24h",
    eventDays: "1",
    loaded: new Set(),
    loadedAt: new Map(),
    locale: "",
    timezone: "",
    historySeq: 0,
    eventsSeq: 0,
    overviewSeq: 0,
    reportsSeq: 0,
    exporting: false,
    reportDay: null,
    reportDayShown: null,
    // 配置时区的“今天”，由后端 reports payload 提供（reports() 的上限）。
    reportToday: null,
  };

  const $ = (id) => document.getElementById(id);
  // Status words come from the API with a stable status_key; map them to the
  // existing i18n keys instead of rendering the Chinese status text.
  const STATUS_I18N = {
    alert: "status_alert",
    recovery: "status_recovery",
    recovered: "status_recovered",
    failed: "status_failed",
    unknown: "status_unknown",
    // An outage that is still open. It used to reuse "alert", so the
    // English page showed "Alert" (a new incident) for a row whose text
    // says "未恢复" (an incident that never recovered).
    unresolved: "status_unresolved",
  };

  // The backend sends a stable code for the Chinese duration words so the
  // English page does not print them verbatim: "进行中", "跨窗口关闭" and
  // the "（窗口前开始）" suffix (v2.20.14).
  const DURATION_I18N = {
    ongoing: "report_ongoing",
    cross_window: "report_cross_window",
    started_before_window: "report_started_before_window",
  };

  // Backend empty_reason strings are Chinese. Translate the known ones so the
  // English page does not show Chinese sentences inside an otherwise English
  // report; unknown strings pass through unchanged.
  const EMPTY_REASON_I18N = {
    "暂无资源采样数据": "reason_no_resource_samples",
    "暂无 QQ 快照或发送数据；从日报数据底座启用后开始积累": "reason_no_qq_data",
    "已按脱敏配置隐藏 QQ 统计": "reason_qq_redacted",
    "暂无网站延迟采样数据": "reason_no_website_samples",
    "暂无进程采样数据": "reason_no_process_samples",
    "暂无数据（所选时间范围内没有 provider 请求）": "reason_no_provider_data",
    "暂无趋势数据": "reason_no_trend_data",
  };

  function translateEmptyReason(text) {
    const key = EMPTY_REASON_I18N[String(text || "").trim()];
    return key ? t(key, text) : text;
  }

  // Structured codes carry the date placeholders; the whitelist below can
  // only translate a fixed sentence, so a dynamic "since {date}" reason
  // stayed Chinese in the English dashboard.
  const EMPTY_REASON_CODE_KEYS = {
    website_since: "reason_website_since",
    website_none: "reason_website_none",
    process_since: "reason_process_since",
    process_none: "reason_process_none",
    // The resource section's "sampling started {date}" sentences were sent
    // without a code, so the English page showed the Chinese sentence (the
    // whitelist below only knows a few fixed strings) (v2.20.19).
    resource_since: "reason_resource_since",
    resource_incomplete_day: "reason_resource_incomplete_day",
    resource_incomplete_span: "reason_resource_incomplete_span",
    // The LLM quality section used to fall back to the Chinese sentence,
    // because the backend sent the reason without a code. The English page
    // then showed "未找到 AstrBot 数据库（data_v4.db）" inside an otherwise
    // English report -- and that is exactly the state a user hits when the
    // LLM section cannot be built (v2.20.14).
    reason_no_provider_db: "reason_no_provider_db",
    reason_provider_query_failed: "reason_provider_query_failed",
    reason_no_llm_calls: "reason_no_llm_calls",
    reason_llm_data_broken: "reason_llm_data_broken",
  };

  function emptyReasonText(section) {
    if (!section || section.available !== false || !section.empty_reason) {
      return null;
    }
    const key = EMPTY_REASON_CODE_KEYS[section.empty_reason_code];
    if (key) {
      return t(key, section.empty_reason, section.empty_reason_vars || {});
    }
    return translateEmptyReason(section.empty_reason);
  }
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

  function isEnglish() {
    return String(state.locale || "").toLowerCase().startsWith("en");
  }

  // Redaction placeholders are Chinese constants produced by the backend
  // (redactor.HIDDEN). They are data, not UI copy, so the English page used
  // to show "已隐藏" for the host, monitor names and process names
  // (v2.20.17).
  const HIDDEN_PLACEHOLDERS = {
    "已隐藏": "hidden_value",
    "已隐藏（错误详情含主机或地址信息）": "error_hidden",
    "（已隐藏地址）": "address_hidden",
  };

  function localizeHidden(value) {
    const text = String(value === null || value === undefined ? "" : value);
    const key = HIDDEN_PLACEHOLDERS[text];
    if (key) return t(key, text);
    if (!isEnglish()) return text;
    // Partial placeholders (a masked disk mount "（已隐藏地址）/share") are
    // not the whole value, so the map above cannot match them. Replace the
    // longest marker first, then the short one (2026-11-06 audit).
    return text
      .split("已隐藏（错误详情含主机或地址信息）")
      .join(t("error_hidden", "已隐藏（错误详情含主机或地址信息）"))
      .split("（已隐藏地址）")
      .join(t("address_hidden", "（已隐藏地址）"))
      .split("已隐藏")
      .join(t("hidden_value", "已隐藏"));
  }

  // KIND_LABELS is Chinese for "进程"; the stable key lets the English page
  // translate the type instead of leaking the Chinese word (v2.20.17).
  function monitorKindText(item) {
    const key = item && item.kind_key;
    if (key) {
      // Build the key through a variable so the i18n contract test does not
      // treat "monitor_kind_" as a literal key that must exist verbatim.
      const prefix = "monitor_kind_";
      const translated = t(prefix + key, "");
      if (translated && translated !== prefix + key) return translated;
    }
    return (item && item.kind) || "?";
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

  // The bridge has no request timeout: when a route hangs (a stuck SQLite
  // probe, a slow backend), the panel stayed on its skeleton forever and the
  // range buttons stayed disabled, because the loading reset only runs after
  // the promise settles. GETs are capped at 30s; POSTs (verify/preview) can
  // legitimately probe dozens of targets, so they get 90s (v2.20.21).
  const API_GET_TIMEOUT_MS = 30000;
  const API_POST_TIMEOUT_MS = 90000;

  function withTimeout(promise, ms, label) {
    let timer = 0;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(
        () => reject(new Error(label || t("error_timeout", "请求超时，请重试"))),
        ms,
      );
    });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
  }

  async function apiGet(name, params) {
    const bridge = getBridge();
    if (!bridge || typeof bridge.apiGet !== "function") {
      throw new Error(
        t("error_bridge", "插件页面桥接不可用，请从 AstrBot 插件页面打开"),
      );
    }
    const data = await withTimeout(
      bridge.apiGet(name, params),
      API_GET_TIMEOUT_MS,
    );
    return typeof data === "string" ? JSON.parse(data) : data;
  }

  async function apiPost(name, body) {
    const bridge = getBridge();
    if (!bridge || typeof bridge.apiPost !== "function") {
      throw new Error(
        t("error_bridge", "插件页面桥接不可用，请从 AstrBot 插件页面打开"),
      );
    }
    const data = await withTimeout(
      bridge.apiPost(name, body),
      API_POST_TIMEOUT_MS,
    );
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
    // Round before deciding to carry, mirroring collectors.format_bytes:
    // otherwise 1023.6 stays in B and toFixed(0) prints "1024 B".
    while (Math.abs(Math.round(number)) >= 1024 && index < units.length - 1) {
      number /= 1024;
      index += 1;
    }
    const digits = index === 0 ? 0 : 2;
    let text = number.toFixed(digits);
    // 200.00 KB/s reads like a measurement with fake precision; the axis
    // ticks are exact multiples now, so drop a trailing ".00"/"0".
    if (digits && text.indexOf(".") !== -1) {
      text = text.replace(/\.?0+$/, "");
    }
    return text + " " + units[index];
  }

  function formatRate(value) {
    return formatBytes(value) + "/s";
  }

  function formatDuration(seconds) {
    const total = Number(seconds);
    if (!Number.isFinite(total) || total < 0) return "--";
    const days = Math.floor(total / 86400);
    const rest = Math.floor(total % 86400);
    const clock =
      String(Math.floor(rest / 3600)).padStart(2, "0") +
      ":" +
      String(Math.floor((rest % 3600) / 60)).padStart(2, "0") +
      ":" +
      String(rest % 60).padStart(2, "0");
    if (!days) return clock;
    return currentLocale().toLowerCase().startsWith("zh")
      ? days + "天" + clock
      : days + "d " + clock;
  }

  function methodText(detail) {
    const key = String(detail || "");
    const map = {
      image: ["method_image", "图片"],
      "image (partial)": ["method_image_partial", "图片（部分成功）"],
      "image unconfirmed": ["method_image_unconfirmed", "图片（未确认）"],
      text: ["method_text", "文字"],
      "text (partial)": ["method_text_partial", "文字（部分成功）"],
      "text unconfirmed": ["method_text_unconfirmed", "文字（未确认）"],
      "weekly-image": ["method_weekly_image", "周报图片"],
      "weekly-image (partial)": [
        "method_weekly_image_partial",
        "周报图片（部分成功）",
      ],
      "weekly-image unconfirmed": [
        "method_weekly_image_unconfirmed",
        "周报图片（未确认）",
      ],
      "weekly-text": ["method_weekly_text", "周报文字"],
      "weekly-text (partial)": [
        "method_weekly_text_partial",
        "周报文字（部分成功）",
      ],
      "weekly-text unconfirmed": [
        "method_weekly_text_unconfirmed",
        "周报文字（未确认）",
      ],
      "sections empty": ["method_sections_empty", "未配置内容"],
      "weekly sections empty": [
        "method_weekly_sections_empty",
        "周报未配置内容",
      ],
      "build failed": ["method_build_failed", "聚合失败"],
      "targets empty": ["method_targets_empty", "目标为空"],
      "send failed": ["method_send_failed", "发送失败"],
      "weekly send failed": ["method_weekly_send_failed", "周报发送失败"],
      "db unavailable": ["method_db_unavailable", "数据库不可用"],
      "db rebuilt": ["method_db_rebuilt", "库重建后跳过"],
      "weekly db rebuilt": ["method_weekly_db_rebuilt", "周报库重建后跳过"],
      "image retry cross-day": [
        "method_image_retry_cross_day",
        "图片重试跨天放弃",
      ],
      "send failed (cross-day)": [
        "method_send_failed_cross_day",
        "发送失败（跨天放弃）",
      ],
      "weekly-image retry cross-week": [
        "method_weekly_image_retry_cross_week",
        "周报图片重试跨周放弃",
      ],
      "weekly send failed (cross-week)": [
        "method_weekly_send_failed_cross_week",
        "周报发送失败（跨周放弃）",
      ],
    };
    const entry = map[key];
    return entry ? t(entry[0], entry[1]) : key || "--";
  }

  function safeDate(ts) {
    const number = Number(ts);
    return Number.isFinite(number) ? new Date(number * 1000) : null;
  }

  // Intl throws RangeError for an invalid IANA name; the backend validates
  // the configured zone, but a stale page or a hand-edited config must never
  // break every timestamp on the dashboard.
  function tzName() {
    const value = state.timezone;
    if (!value) return undefined;
    try {
      new Intl.DateTimeFormat("en", { timeZone: value }).format(0);
      return value;
    } catch (error) {
      state.timezone = "";
      return undefined;
    }
  }

  function formatTime(ts) {
    const date = safeDate(ts);
    if (!date) return "--";
    return date.toLocaleTimeString(currentLocale(), {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      timeZone: tzName(),
    });
  }

  function formatDateTime(ts) {
    const date = safeDate(ts);
    if (!date) return "--";
    return date.toLocaleString(currentLocale(), {
      hour12: false,
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      timeZone: tzName(),
    });
  }

  function labelForTs(ts, range, withDate) {
    const date = safeDate(ts);
    if (!date) return "--";
    if (range === "24h" && !withDate) {
      return date.toLocaleTimeString(currentLocale(), {
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
        timeZone: tzName(),
      });
    }
    const day = date.toLocaleDateString(currentLocale(), {
      month: "2-digit",
      day: "2-digit",
      timeZone: tzName(),
    });
    if (range !== "24h") return day;
    // A 24h window can cross midnight; printing only HH:MM made it read as
    // "15:33 – 05:26", which looks backwards.
    const time = date.toLocaleTimeString(currentLocale(), {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      timeZone: tzName(),
    });
    return day + " " + time;
  }

  function rangeLabel(range) {
    return (
      {
        "24h": t("range_24h", "24 小时"),
        "7d": t("range_7d", "7 天"),
        "30d": t("range_30d", "30 天"),
      }[range] || range
    );
  }

  // Say out loud how much history the selected window actually contains:
  // with only a few days of samples, 7d and 30d look identical and users
  // assume the button did nothing.
  function historyRangeNote(points, range, truncated) {
    if (!points.length) return "";
    const first = Number(points[0].ts);
    const last = Number(points[points.length - 1].ts);
    if (!Number.isFinite(first) || !Number.isFinite(last)) return "";
    const days = Math.max(0, (last - first) / 86400);
    const shownDays = Math.max(1, Math.round(days));
    // Compare days in the *display* timezone (display.timezone), not the
    // browser's: toDateString() ignores timeZone, so a viewer in UTC saw
    // "23:00 – 07:00" without dates for a window that really crossed midnight.
    const dayOf = (ts) =>
      new Date(ts * 1000).toLocaleDateString(currentLocale(), {
        timeZone: tzName(),
      });
    const sameDay = dayOf(first) === dayOf(last);
    const text = t(
      "history_range_note",
      "数据范围 {from} – {to}（{days} 天）",
      {
        from: labelForTs(first, range, !sameDay),
        to: labelForTs(last, range, !sameDay),
        days: shownDays,
      },
    );
    const wanted = { "24h": 1, "7d": 7, "30d": 30 }[range] || 1;
    if (range !== "24h" && days + 0.5 < wanted) {
      if (truncated) {
        // The history route caps at 20k rows. Saying "only N days of data,
        // less than 30" described the cap as missing data and contradicted
        // the truncation toast (v2.20.19).
        return (
          text +
          " · " +
          t(
            "history_range_truncated",
            "范围数据超出上限，仅显示最近 {days} 天",
            { days: shownDays },
          )
        );
      }
      return (
        text +
        " · " +
        t("history_range_short", "数据仅 {days} 天，不足 {range}", {
          days: shownDays,
          range: rangeLabel(range),
        })
      );
    }
    return text;
  }

  function setHistoryLoading(loading) {
    const panel = $("panel-history");
    if (panel) panel.classList.toggle("is-loading", loading);
    const box = $("history-loading");
    if (box) box.hidden = !loading;
    document.querySelectorAll("#range-switch button").forEach((button) => {
      button.disabled = loading;
      button.setAttribute("aria-busy", loading ? "true" : "false");
    });
  }

  function setRangeActive() {
    document.querySelectorAll("#range-switch button").forEach((button) => {
      button.classList.toggle(
        "active",
        button.dataset.range === state.historyRange,
      );
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
    // The card surface stays neutral; only the number picks up the warning
    // colour, so a wall of five cards does not read as five alarms.
    const level = tone(percent);
    const className = "metric" + (level ? " tone-" + level : "");
    const bar =
      percent === null || percent === undefined || !Number.isFinite(percent)
        ? ""
        : '<div class="bar ' +
          level +
          '"><i style="width:' +
          Math.max(0, Math.min(100, percent)) +
          '%"></i></div>';
    return (
      '<div class="' +
      className +
      '"><div class="metric-label">' +
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

  // The three database-backed tabs used to show only a short toast and an
  // otherwise blank panel. After the toast faded the page looked like "no
  // data was ever recorded", which is the most misleading empty state
  // (v2.20.19).
  function showDbUnavailable(panel) {
    const message = t(
      "db_unavailable_panel",
      "数据库暂时不可用，正在自动恢复。请稍后切换标签或刷新页面重试。",
    );
    if (panel === "history") {
      ["history-legend", "network-legend"].forEach((id) => {
        const node = $(id);
        if (node) node.innerHTML = "";
      });
      ["history-chart", "network-chart"].forEach((id) => {
        const node = $(id);
        if (node) node.innerHTML = empty(message);
      });
      const note = $("history-range-note");
      if (note) note.textContent = "";
    } else if (panel === "events") {
      const list = $("event-list");
      if (list) list.innerHTML = empty(message);
      const summary = $("event-summary");
      if (summary) summary.textContent = "";
    } else if (panel === "reports") {
      const summary = $("report-summary");
      if (summary) summary.innerHTML = "";
      const details = $("report-details");
      if (details) details.innerHTML = empty(message);
      const table = $("report-runs");
      const body = table && table.querySelector("tbody");
      if (body) body.innerHTML = "";
      // The header kept the previous successful day and its arrows looked
      // clickable while the body said "database unavailable"; the user could
      // not tell whether the date was still valid (v2.20.21).
      const dayLabel = $("report-day");
      if (dayLabel) dayLabel.textContent = t("report_no_data", "暂无数据");
      state.reportDayShown = null;
      ["report-prev", "report-next"].forEach((id) => {
        const node = $(id);
        if (node) {
          node.disabled = true;
          node.setAttribute("aria-disabled", "true");
        }
      });
    }
  }

  // ------------------------------------------------------------------
  // SVG chart
  // ------------------------------------------------------------------

  function niceMax(value, base) {
    if (!Number.isFinite(value) || value <= 0) return 1;
    // Byte rates need a 1024-based unit step: nice-rounding 189632 to
    // 200000 and then printing it as KB/s produced "195.31 KB/s" ticks,
    // which look like arbitrary cut-offs. Snap the rounded number in the
    // displayed unit instead, so the axis reads 50/100/150/200 KB/s.
    const stepBase = base === 1024 ? 1024 : 10;
    const unit =
      stepBase === 1024
        ? Math.pow(1024, Math.max(0, Math.floor(Math.log(value) / Math.log(1024))))
        : 1;
    const scaled = value / unit;
    const exp = Math.max(0, Math.floor(Math.log10(scaled)));
    const fraction = scaled / Math.pow(10, exp);
    const step = [1, 2, 5, 10].find((item) => fraction <= item) || 10;
    return step * Math.pow(10, exp) * unit;
  }

  function renderChart(container, series, options) {
    // The container node is stable across redraws; bind its listeners once
    // per render and abort the previous set, otherwise every range change
    // leaves another closure (and its series data) behind.
    if (container._chartAbort) container._chartAbort.abort();
    const controller = new AbortController();
    container._chartAbort = controller;
    const labels = options.labels || [];
    const values = series
      .flatMap((item) => item.values)
      .filter((value) => value !== null && Number.isFinite(value));
    if (!labels.length || !values.length) {
      container.innerHTML = empty(t("empty_chart", "暂无图表数据"));
      // A previous non-empty render left role="img", tabIndex=0 and a stale
      // aria-label behind. The keydown listener is aborted above, so a
      // keyboard user could focus a "chart" whose arrow keys do nothing and
      // a screen reader announced a chart that is not there (v2.20.21).
      container.removeAttribute("role");
      container.removeAttribute("aria-label");
      container.tabIndex = -1;
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
    const max =
      options.max || niceMax(Math.max.apply(null, values) * 1.15, options.niceBase);
    const formatter = options.format || ((value) => String(Math.round(value)));
    const count = labels.length;
    // Place samples by timestamp, not by array index: with a 30-day window
    // and only a few days of history, index spacing stretches that data over
    // the whole chart and looks identical to the 7-day view. A time axis
    // leaves the empty part of the window visibly empty.
    const timestamps = Array.isArray(options.timestamps)
      ? options.timestamps
      : null;
    const windowStart = Number(options.windowStart);
    const windowEnd = Number(options.windowEnd);
    const useTimeAxis = Boolean(
      timestamps &&
      timestamps.length === count &&
      Number.isFinite(windowStart) &&
      Number.isFinite(windowEnd) &&
      windowEnd > windowStart,
    );
    const span = useTimeAxis ? windowEnd - windowStart : 0;
    const x = (index) =>
      useTimeAxis
        ? left +
          plotW *
            Math.min(
              1,
              Math.max(0, (Number(timestamps[index]) - windowStart) / span),
            )
        : left + plotW * (index / Math.max(1, count - 1));
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
    (options.gaps || []).forEach((gap) => {
      if (!useTimeAxis) return;
      const gapStart = Math.max(Number(gap.start), windowStart);
      const gapEnd = Math.min(Number(gap.end), windowEnd);
      if (!(gapEnd > gapStart)) return;
      const gapX = left + plotW * ((gapStart - windowStart) / span);
      const gapW = plotW * ((gapEnd - gapStart) / span);
      svg +=
        '<rect class="chart-gap" x="' +
        gapX.toFixed(1) +
        '" y="' +
        top +
        '" width="' +
        Math.max(2, gapW).toFixed(1) +
        '" height="' +
        plotH +
        '" rx="4" />';
    });
    let lastTickText = null;
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
      const tickText = String(formatter((max * index) / 4));
      // A zero-traffic window (max=1) formatted in whole bytes produced
      // "0 B/s / 0 B/s / 1 B/s / 1 B/s / 1 B/s" -- five labels for two
      // values. Keep the grid lines but not the duplicate text (v2.20.19).
      if (tickText === lastTickText) continue;
      lastTickText = tickText;
      svg +=
        '<text class="axis" x="' +
        (left - 6) +
        '" y="' +
        (lineY + 4) +
        '" text-anchor="end">' +
        esc(tickText) +
        "</text>";
    }
    // The first and last ticks are anchored to the plot edges, not centred:
    // a date-stamped label ("11/02 12:31") is wider than the 16px right
    // margin and was clipped by the viewBox.
    if (useTimeAxis && options.labelForTime) {
      [0, 0.25, 0.5, 0.75, 1].forEach((ratio) => {
        svg +=
          '<text class="axis" x="' +
          (left + plotW * ratio) +
          '" y="' +
          (height - 6) +
          '" text-anchor="' +
          (ratio === 0 ? "start" : ratio === 1 ? "end" : "middle") +
          '">' +
          esc(options.labelForTime(windowStart + span * ratio)) +
          "</text>";
      });
    } else {
      const indexes = Array.from(
        new Set(
          [0, 0.25, 0.5, 0.75, 1].map((ratio) =>
            Math.round((count - 1) * ratio),
          ),
        ),
      );
      indexes.forEach((index, position) => {
        svg +=
          '<text class="axis" x="' +
          x(index) +
          '" y="' +
          (height - 6) +
          '" text-anchor="' +
          (position === 0 ? "start" : position === indexes.length - 1 ? "end" : "middle") +
          '">' +
          esc(labels[index]) +
          "</text>";
      });
    }
    const peakLabels = [];
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
          const peakX = x(peak[0]);
          let peakY = y(peak[1]) - 7;
          // Two series peaking at almost the same x used to print their
          // labels on top of each other (119.4 KB/s over 161.03 KB/s).
          // Lift the later label above any label already placed nearby.
          for (const placed of peakLabels) {
            if (
              Math.abs(placed.x - peakX) < 52 &&
              Math.abs(placed.y - peakY) < 13
            ) {
              peakY = placed.y - 13;
            }
          }
          peakY = Math.max(top + 10, peakY);
          peakLabels.push({ x: peakX, y: peakY });
          svg +=
            '<circle cx="' +
            peakX +
            '" cy="' +
            y(peak[1]) +
            '" r="3" fill="' +
            item.color +
            '" />';
          svg +=
            '<text class="peak" x="' +
            peakX +
            '" y="' +
            peakY +
            '" text-anchor="middle">' +
            esc(formatter(peak[1])) +
            "</text>";
        }
      }
    });
    svg +=
      '<line class="hover-line" x1="0" y1="' +
      top +
      '" x2="0" y2="' +
      (top + plotH) +
      '" style="display:none" />';
    series.forEach((item, seriesIndex) => {
      svg +=
        '<circle class="hover-dot" data-series="' +
        seriesIndex +
        '" cx="0" cy="0" r="3.5" fill="' +
        item.color +
        '" style="display:none" />';
    });
    svg += "</svg>";
    container.innerHTML = svg;
    attachChartTip(container, controller, {
      width,
      left,
      plotW,
      count,
      x,
      y,
      series,
      formatter,
      labels,
      timestamps,
      windowStart,
      windowEnd,
      useTimeAxis,
      span,
      titleFor: options.titleFor,
    });
  }

  // Adds a hover/keyboard readout to a rendered chart. The SVG itself stays
  // static: one reference line and one dot per series are moved around, and
  // an HTML tooltip shows the exact values for the nearest sample.
  function attachChartTip(container, controller, ctx) {
    const svg = container.querySelector("svg");
    if (!svg) return;
    const line = svg.querySelector(".hover-line");
    const dots = Array.from(svg.querySelectorAll(".hover-dot"));
    let tip = container.querySelector(".chart-tip");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "chart-tip";
      container.appendChild(tip);
    }
    tip.style.display = "none";
    container.tabIndex = 0;
    // Keyboard/screen-reader users landed on an empty focusable box
    // (v2.20.20).
    container.setAttribute("role", "img");
    container.setAttribute(
      "aria-label",
      t("chart_aria_label", "资源趋势图（可用方向键查看数值）"),
    );
    const titleFor =
      ctx.titleFor || ((index) => (ctx.labels || [])[index] || "");

    function indexAt(clientX) {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return -1;
      const localX = ((clientX - rect.left) * ctx.width) / rect.width;
      const ratio = Math.max(0, Math.min(1, (localX - ctx.left) / ctx.plotW));
      if (ctx.useTimeAxis) {
        const target = ctx.windowStart + ctx.span * ratio;
        let best = 0;
        let bestDiff = Infinity;
        ctx.timestamps.forEach((ts, index) => {
          const diff = Math.abs(Number(ts) - target);
          if (diff < bestDiff) {
            bestDiff = diff;
            best = index;
          }
        });
        return best;
      }
      return Math.max(
        0,
        Math.min(ctx.count - 1, Math.round(ratio * (ctx.count - 1))),
      );
    }

    function hide() {
      if (line) line.style.display = "none";
      dots.forEach((dot) => {
        dot.style.display = "none";
      });
      tip.style.display = "none";
      container.removeAttribute("data-active-index");
    }

    function show(index) {
      if (index < 0 || index >= ctx.count) return;
      container.setAttribute("data-active-index", String(index));
      const px = ctx.x(index);
      if (line) {
        line.setAttribute("x1", px);
        line.setAttribute("x2", px);
        line.style.display = "";
      }
      let rows = "";
      ctx.series.forEach((item, seriesIndex) => {
        const value = item.values[index];
        const dot = dots[seriesIndex];
        if (value === null || !Number.isFinite(value)) {
          if (dot) dot.style.display = "none";
          return;
        }
        if (dot) {
          dot.setAttribute("cx", px);
          dot.setAttribute("cy", ctx.y(value));
          dot.style.display = "";
        }
        rows +=
          '<div class="chart-tip-row"><i style="background:' +
          item.color +
          '"></i><span>' +
          esc(item.name) +
          "</span><b>" +
          esc(ctx.formatter(value)) +
          "</b></div>";
      });
      tip.innerHTML =
        '<div class="chart-tip-title">' +
        esc(titleFor(index)) +
        "</div>" +
        rows;
      tip.style.display = "";
      const box = container.getBoundingClientRect();
      const svgBox = svg.getBoundingClientRect();
      const pointX = (px / ctx.width) * svgBox.width + (svgBox.left - box.left);
      const tipWidth = tip.offsetWidth || 150;
      let leftPx = pointX + 12;
      if (leftPx + tipWidth > box.width - 6) {
        leftPx = pointX - tipWidth - 12;
      }
      tip.style.left = Math.max(6, leftPx) + "px";
      tip.style.top = Math.max(6, svgBox.top - box.top + 8) + "px";
    }

    svg.addEventListener("pointermove", (event) => {
      show(indexAt(event.clientX));
    });
    svg.addEventListener("pointerleave", hide);
    svg.addEventListener("pointercancel", hide);
    container.addEventListener(
      "pointerup",
      () => {
        setTimeout(hide, 1400);
      },
      { signal: controller.signal },
    );
    container.addEventListener(
      "focus",
      () => {
        show(0);
      },
      { signal: controller.signal },
    );
    container.addEventListener("blur", hide, { signal: controller.signal });
    container.addEventListener(
      "keydown",
      (event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        const raw = container.getAttribute("data-active-index");
        const current = raw === null ? -1 : Number(raw);
        const next =
          event.key === "ArrowRight"
            ? Math.min(ctx.count - 1, current < 0 ? 0 : current + 1)
            : Math.max(0, current < 0 ? ctx.count - 1 : current - 1);
        show(next);
      },
      { signal: controller.signal },
    );
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

  function updateSubtitle(data) {
    const host = localizeHidden(data.host || "--");
    // "default" is the plugin's internal platform key, not something a user
    // recognises; showing "host · default" read like a broken value.
    const platform =
      data.platform && String(data.platform) !== "default"
        ? String(data.platform)
        : "";
    // The page does not auto-refresh, so the timestamp is the only way to
    // tell whether a figure is current or an hour old.
    const updated = String(data.generated_at || "").split(" ").pop();
    $("subtitle").textContent = [
      host,
      platform,
      updated ? t("updated_at", "更新于 {time}", { time: updated }) : "",
    ]
      .filter(Boolean)
      .join(" · ");
    $("subtitle").removeAttribute("data-i18n");
    $("plugin-version").textContent = "v" + (data.plugin_version || "--");
  }

  async function loadOverview() {
    // A stale-flag from the reports tab must not colour another tab's toast.
    state.reportsStale = false;
    const seq = ++state.overviewSeq;
    const data = await apiGet("overview");
    if (seq !== state.overviewSeq) return;
    if (data.timezone) state.timezone = String(data.timezone);
    const cpu = data.cpu || {};
    const memory = data.memory || {};
    const swap = data.swap || {};
    const disk = data.disk || {};
    updateSubtitle(data);

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
        // The backend sends "available" (same definition as the percentage
        // above); SWAP and disk keep the "used" wording.
        memory.available
          ? t("metric_available_total", "{used} 可用 / {total}", {
              used: memory.used_text || "--",
              total: memory.total_text || "--",
            })
          : t("metric_used_total", "{used} / {total}", {
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
          ? t("metric_disk_path", "磁盘 {path}", { path: localizeHidden(disk.path) })
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
        data.uptime_seconds !== undefined && data.uptime_seconds !== null
          ? formatDuration(data.uptime_seconds)
          : data.uptime || "--",
        t("label_astrbot", "AstrBot {version}", {
          version: data.astrbot_version || "--",
        }),
      ),
    ].join("");

    // Every configured mount, not just the worst one. The metric above shows
    // the fullest disk; with several mounts the others were invisible, and
    // a mount that cannot be read (a pulled network share) disappeared
    // completely instead of showing "--" like the status card does
    // (v2.20.14).
    const diskDetail = $("disk-detail");
    const diskDetailCard = $("disk-detail-card");
    const disks = Array.isArray(disk.disks) ? disk.disks : [];
    if (diskDetail && diskDetailCard) {
      if (disks.length > 1) {
        diskDetailCard.hidden = false;
        diskDetail.innerHTML = disks
          .map(function (item) {
            const path = item.path || "--";
            const percent =
              item.percent === null || item.percent === undefined
                ? null
                : Number(item.percent);
            const value =
              percent === null || !Number.isFinite(percent)
                ? "--"
                : percent.toFixed(1) + "%";
            const sub = t("metric_used_total", "{used} / {total}", {
              used: item.used_text || "--",
              total: item.total_text || "--",
            });
            return (
              '<div class="network-item"><span>' +
              esc(localizeHidden(path)) +
              "</span><span>" +
              esc(value) +
              " · " +
              esc(sub) +
              "</span></div>"
            );
          })
          .join("");
      } else {
        diskDetailCard.hidden = true;
        diskDetail.innerHTML = "";
      }
    }

    const network = data.network || {};
    // Keep the translated heading and append the month: the month alone used
    // to replace "本月流量" entirely (and stayed Chinese-only "2026-10" in the
    // English UI).
    $("network-month").textContent =
      t("label_month_flow", "本月流量") +
      (network.month ? " · " + network.month : "");
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

    const quality = data.provider_quality || {};
    if (quality.available) {
      $("provider-overview").innerHTML = [
        [t("quality_today_calls", "今日调用"), String(quality.calls || 0)],
        [t("quality_success", "成功率"), quality.success_text || "--"],
        [t("quality_failed_aborted", "失败 · 中断"), (quality.errors || 0) + " · " + (quality.aborted || 0)],
        [t("quality_streak", "当前连续失败"), String(quality.error_streak || 0)],
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
      $("provider-summary").textContent =
        t("quality_scope_today", "今天 · 按展示时区") +
        (quality.truncated
          ? " · " +
            t("quality_truncated", "数据超 2 万条，仅统计最近部分，合计可能偏小")
          : "");
    } else {
      // The backend ships fixed Chinese reasons; translate the known codes
      // so an English dashboard does not leak Chinese strings.
      const reasonCodes = {
        no_db: t("quality_reason_no_db", "未找到 AstrBot 数据库（data_v4.db）"),
        query_failed: t("quality_reason_query_failed", "provider_stats 查询失败"),
        no_data_today: t("quality_reason_no_data_today", "今天还没有 LLM 调用记录"),
      };
      const reason =
        (quality.reason_code && reasonCodes[quality.reason_code]) ||
        quality.empty_reason ||
        t("report_no_data", "暂无数据");
      $("provider-overview").innerHTML = empty(reason);
      $("provider-summary").textContent = "--";
    }

    // ``info`` items are neutral notices ("notification center is active");
    // rendering them as a "配置提醒" banner put a permanent, self-explaining
    // warning on every overview load. They stay visible in 状态卡诊断.
    const audit = (data.audit || []).filter(
      (issue) => issue && issue.level !== "info",
    );
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
    } else if (data.audit_status === "failed") {
      // An empty audit list also means "the audit did not run". Hiding the
      // banner in that case was the same misleading all-green result the
      // diagnostic command already avoids (v2.20.21).
      banner.hidden = false;
      banner.innerHTML =
        '<div class="audit-item warning"><strong>' +
        esc(t("audit_failed_tag", "配置体检")) +
        "</strong><span>" +
        esc(
          t(
            "audit_failed_message",
            "本次配置体检未完成（执行失败或超时），请查看日志或使用「状态卡诊断」。",
          ),
        ) +
        "</span></div>";
    } else {
      banner.hidden = true;
      banner.innerHTML = "";
    }

    const monitors = data.monitors || {};
    if (monitors.enabled === false) {
      $("monitor-summary").textContent = t(
        "monitor_disabled",
        "服务监测已关闭",
      );
      $("monitors").innerHTML = empty(
        t(
          "monitor_disabled_hint",
          "在配置页开启「服务可用性监测」后，这里会显示探测结果。",
        ),
      );
    } else {
      $("monitor-summary").textContent = t(
        "monitor_summary",
        "正常 {ok} · 探测失败 {degraded} · 异常 {fail} · 未知 {unknown} · 配置错误 {config_error}",
        {
          ok: monitors.ok || 0,
          degraded: monitors.degraded || 0,
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
                  : (item.status === "fail" || item.status === "degraded") &&
                      item.last_error
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
                esc(localizeHidden(item.name)) +
                '</div><div class="monitor-meta">' +
                esc(monitorKindText(item) + " · " + localizeHidden(meta)) +
                "</div></div></div>"
              );
            })
            .join("")
        : empty(t("monitor_none", "没有配置服务监测目标"));
    }

    const processes = data.processes || [];
    const tbody = $("process-table").querySelector("tbody");
    tbody.innerHTML = processes.length
      ? processes
          .map(
            (item) =>
              "<tr><td>" +
              esc(item.rank) +
              "</td><td>" +
              esc(localizeHidden(item.name)) +
              "</td><td>" +
              esc(
                Number.isFinite(Number(item.cpu))
                  ? Number(item.cpu).toFixed(1) + "%"
                  : "--",
              ) +
              "</td><td>" +
              esc(
                Number.isFinite(Number(item.mem_mb))
                  ? Number(item.mem_mb).toFixed(0) + " MB"
                  : "--",
              ) +
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

  function exportCsv() {
    if (state.exporting) return Promise.resolve();
    const button = $("export-csv");
    const label = button ? button.textContent : "";
    state.exporting = true;
    if (button) {
      button.disabled = true;
      button.textContent = t("exporting", "导出中…");
    }
    return runExport().finally(() => {
      state.exporting = false;
      if (button) {
        button.disabled = false;
        button.textContent = label;
      }
    });
  }

  async function runExport() {
    const bridge = getBridge();
    if (!bridge || typeof bridge.apiGet !== "function") {
      throw new Error(
        t("error_bridge", "插件页面桥接不可用，请从 AstrBot 插件页面打开"),
      );
    }
    const range = state.historyRange || "7d";
    const kinds = ["resources", "processes", "websites", "events"];
    let exported = 0;
    let truncated = false;
    for (const kind of kinds) {
      // Use the module-level apiGet: it parses a JSON *string* from the
      // bridge. Calling bridge.apiGet directly meant a string payload made
      // every export fail with "导出在 resources 中断（已导出 0 个文件）"
      // while the data was there (v2.20.20).
      const data = await apiGet("export", { kind, range });
      if (data && data.db_unavailable) {
        throw new Error(
          t("db_unavailable_retry", "数据库暂时不可用，正在自动恢复，请稍后重试导出"),
        );
      }
      if (!data || !data.ok) {
        throw new Error(
          (data && data.error) ||
            t("export_partial", "导出在 {kind} 中断（已导出 {count} 个文件）", {
              kind,
              count: exported,
            }),
        );
      }
      if (data.truncated) truncated = true;
      if ((data.rows || []).length) exported += 1;
      downloadCsv(kind, range, data.rows || []);
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    const done = exported
      ? t("export_done", "已导出 {count} 个文件", { count: exported })
      : t("export_empty", "所选范围内没有可导出的数据");
    toast(
      truncated
        ? t("export_truncated", "数据量超出上限，仅导出最近的记录") + " · " + done
        : done,
    );
  }

  function downloadCsv(kind, range, rows) {
    if (!rows.length) return;
    const headers = Object.keys(rows[0]);
    const esc = (value) => {
      let text = value === null || value === undefined ? "" : String(value);
      // A bare CR or CRLF inside a process name/event detail would shift the
      // following CSV rows in Excel/WPS, so flatten every newline first.
      text = text.replace(/\r\n|\r|\n/g, " ");
      // Excel/WPS executes a cell starting with =,+,-,@ as a formula, and
      // event details can carry text returned by probed services. The
      // leading pipe covers the legacy DDE vector (v2.20.0). Leading spaces
      // and full-width variants bypassed the check (v2.20.5).
      if (/^[\s\u3000]*[=+\-@\t|＝＋－＠]/.test(text)) text = "'" + text;
      return /[",]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
    };
    const lines = [headers.join(",")];
    for (const row of rows) lines.push(headers.map((h) => esc(row[h])).join(","));
    // RFC 4180 line separator; some legacy spreadsheets ignored bare LF.
    const blob = new Blob(["\ufeff" + lines.join("\r\n")], {
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
    // A stale-flag from the reports tab must not colour another tab's toast.
    state.reportsStale = false;
    const seq = ++state.historySeq;
    const range = state.historyRange;
    setHistoryLoading(true);
    let data;
    try {
      data = await apiGet("history", { range });
    } finally {
      if (seq === state.historySeq) setHistoryLoading(false);
    }
    if (seq !== state.historySeq) return;
    if (data.db_unavailable) {
      showDbUnavailable("history");
      toast(
        t("db_unavailable", "数据库暂时不可用，正在自动恢复；当前显示的可能是旧数据"),
      );
      return;
    }
    const noteKind = data.network_note_kind;
    $("network-note").textContent =
      noteKind === "redacted"
        ? t("network_note_redacted", "已按脱敏配置隐藏网络数据。")
        : noteKind === "db"
          ? t(
              "network_note_db",
              "网络折线来自日报采样（约 {minutes} 分钟一条），与资源趋势同一时间窗口。",
              {
                minutes: Math.max(
                  1,
                  Math.round((data.network_sample_seconds || 300) / 60),
                ),
              },
            )
          : noteKind === "state"
            ? // The backend sentence is Chinese only; the i18n key carries
              // the same text per language (it used to be a dead key, so the
              // English dashboard printed the Chinese sentence verbatim).
              t("network_note_state", data.network_note || "")
            : data.network_note || "";
    if (data.truncated) {
      toast(t("history_truncated", "范围数据超出上限，仅显示最近的记录"));
    }
    const points = data.points || [];
    $("history-range-note").textContent = historyRangeNote(
      points,
      range,
      Boolean(data.truncated),
    );
    // A 24h window that crosses midnight must put the date on the axis
    // labels; the old call omitted the flag and the axis read
    // "10:00 16:00 22:00 04:00 10:00" (v2.20.5).
    const dayKey = (ts) => {
      const parsed = safeDate(ts);
      return parsed
        ? parsed.toLocaleDateString(currentLocale(), { timeZone: tzName() })
        : "";
    };
    const firstTs = points.length ? points[0].ts : 0;
    const lastTs = points.length ? points[points.length - 1].ts : 0;
    const crossesDay = Boolean(firstTs && lastTs && dayKey(firstTs) !== dayKey(lastTs));
    const labels = points.map((point) =>
      labelForTs(point.ts, range, crossesDay && range === "24h"),
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
      timestamps: points.map((point) => point.ts),
      windowStart: data.window_start,
      windowEnd: data.window_end,
      gaps: data.gaps || [],
      // The time axis samples the whole window, not the points: a 24h window
      // always spans two calendar days, so its ticks need the date. Without
      // it the axis read "12:31 · 18:31 · 00:31 · 06:31 · 12:31" and looked
      // backwards (the labels array was fixed in v2.20.5, but it is ignored
      // whenever timestamps are available -- this path is the one used).
      labelForTime: (ts) => labelForTs(ts, range, range === "24h"),
      titleFor: (index) => formatDateTime(points[index] && points[index].ts),
    });
    const gapNote = $("history-gap-note");
    if (gapNote) {
      const gaps = data.gaps || [];
      gapNote.hidden = gaps.length === 0;
      gapNote.textContent = gaps.length
        ? t(
            "history_gaps",
            "检测到 {count} 段停机盲区（灰色区域）：期间插件或 Bot 停机，无采样数据。",
            { count: gaps.length },
          )
        : "";
    }

    const network = data.network || [];
    if (data.network_redacted) {
      renderLegend($("network-legend"), []);
      $("network-chart").innerHTML =
        '<p class="muted">' +
        esc(t("network_redacted", "网络数据已按脱敏配置隐藏")) +
        "</p>";
      return;
    }
    const networkLabels = network.map((point) => labelForTs(point.ts, range));
    // Same cross-midnight flag as the resource chart: the network series has
    // its own timestamps and may cover a different window (state.json vs the
    // sampled table), so it cannot reuse the resource chart's flag.
    const networkFirst = network.length ? network[0].ts : 0;
    const networkLast = network.length ? network[network.length - 1].ts : 0;
    const networkCrossesDay = Boolean(
      networkFirst &&
        networkLast &&
        dayKey(networkFirst) !== dayKey(networkLast),
    );
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
      niceBase: 1024,
      timestamps: network.map((point) => point.ts),
      // The state.json fallback holds about one hour of points. Mapping it
      // onto the selected 7d/30d window squeezed them into the last sliver
      // of the axis (a vertical line at the right edge). Let that path use
      // the index axis instead (v2.20.21).
      windowStart: data.network_note_kind === "state" ? null : data.window_start,
      windowEnd: data.network_note_kind === "state" ? null : data.window_end,
      labelForTime: (ts) => labelForTs(ts, range, range === "24h"),
      titleFor: (index) =>
        formatDateTime(network[index] && network[index].ts),
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
        connection: t("kind_connection", "连接状态"),
        service: t("kind_service", "服务"),
        provider: t("kind_provider", "LLM"),
      }[kind] ||
      kind ||
      t("kind_event", "事件")
    );
  }

  async function loadEvents() {
    // A stale-flag from the reports tab must not colour another tab's toast.
    state.reportsStale = false;
    const seq = ++state.eventsSeq;
    const data = await apiGet("events", { days: state.eventDays });
    if (seq !== state.eventsSeq) return;
    if (data.db_unavailable) {
      showDbUnavailable("events");
      toast(
        t("db_unavailable", "数据库暂时不可用，正在自动恢复；当前显示的可能是旧数据"),
      );
      return;
    }
    const items = data.items || [];
    const counts = data.counts || {};
    const summary =
      ["alert", "recovery", "failed"]
        .map((key) => statusText(key) + " " + (counts[key] ?? 0))
        .join(" · ") +
      (data.counts_truncated
        ? " · " +
          t("events_counts_truncated", "统计仅覆盖最近 {limit} 条", {
            limit: data.counts_limit || 2000,
          })
        : "") +
      (data.truncated
        ? " · " + t("events_truncated", "仅显示最近 200 条")
        : "");
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
              // Alert rows carry the first failed probe; older rows (and
              // recoveries) have no started_ts and keep showing their ts.
              esc(formatDateTime(item.started_ts || item.ts)) +
              '</div><div class="event-main"><div class="event-title">' +
              badge +
              '<span class="event-name">' +
              esc(localizeHidden(item.name)) +
              '</span><span class="monitor-meta">' +
              esc(kindText(item.kind)) +
              "</span></div>" +
              (item.detail
                ? '<div class="event-detail">' +
                  esc(localizeHidden(item.detail)) +
                  "</div>"
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

  function formatReportDay(day) {
    const parsed = new Date(String(day) + "T00:00:00Z");
    if (Number.isNaN(parsed.getTime())) return String(day || "");
    return parsed.toLocaleDateString(currentLocale(), {
      month: "short",
      day: "numeric",
      timeZone: "UTC",
    });
  }

  // The backend used to send a Chinese-only window_label ("昨日" / "10月30日"
  // / "近 7 天"); it now also sends a structured code so the label can be
  // translated while still following the real report day.
  function reportScopeLabel(data, summary) {
    const code = String((summary && summary.window_label_code) || "");
    if (code === "today") return t("report_today", "今天");
    if (code === "yesterday") return t("report_yesterday", "昨日");
    if (code === "last_day") return t("report_last_day", "最后一天");
    if (code === "last_days") {
      const days = Number(
        (summary && summary.window_label_days) ||
          (summary && summary.span_days) ||
          0,
      );
      return t("report_scope_last_days", "近 {days} 天", { days });
    }
    if (code === "date") {
      return formatReportDay(String((data && data.day) || ""));
    }
    return (
      (summary && summary.window_label) || t("report_yesterday", "昨日")
    );
  }

  // ``duration_code`` is a *suffix* marker for "started before the window",
  // not a replacement for the whole duration. Rendering it as one dropped
  // the number ("04:15:38（窗口前开始）" became just "窗口前开始" / "Started
  // before the window"), leaving the dashboard table without the outage
  // length while the report image still showed it.
  function reportDurationText(item) {
    const raw = String((item && item.duration) || "");
    const code = item && item.duration_code;
    if (!code) return raw || "--";
    const zh = String(currentLocale()).toLowerCase().indexOf("zh") === 0;
    const suffix = function (value) {
      return value ? (zh ? "（" + value + "）" : " (" + value + ")") : "";
    };
    if (code === "started_before_window") {
      const marker = "（窗口前开始）";
      const value = raw.endsWith(marker)
        ? raw.slice(0, -marker.length).trim()
        : raw;
      if (!value) return t("report_started_before_window", "窗口前开始");
      return value + suffix(t("report_started_before_window", "窗口前开始"));
    }
    if (code === "cross_window") {
      const base = t("report_cross_window", "跨窗口关闭");
      return raw.indexOf("窗口前开始") >= 0
        ? base + suffix(t("report_started_before_window", "窗口前开始"))
        : base;
    }
    return t(DURATION_I18N[code] || code, raw || "--");
  }

  function reportDetails(data) {
    const blocks = [];
    // The backend computes an empty_reason per section; dropping it left a
    // blank page with no explanation for a fresh install or a disabled module.
    const emptyReasons = [
      ...new Set(
        ["resources", "token", "process", "website", "qq", "provider_quality"]
          .map((key) => emptyReasonText(data[key]))
          .filter(Boolean),
      ),
    ];
    if (emptyReasons.length) {
      blocks.push(
        '<div class="detail-block"><h3>' +
          esc(t("report_unavailable", "部分内容不可用")) +
          '</h3><p class="muted">' +
          emptyReasons.map(esc).join("<br>") +
          "</p></div>",
      );
    }
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
          "</div>" +
          (resources.gaps && resources.gaps.length
            ? '<p class="muted">' +
              esc(
                t(
                  "report_gaps",
                  "停机盲区 {count} 段：期间插件或 Bot 停机，无采样数据。",
                  { count: resources.gaps.length },
                ),
              ) +
              "</p>"
            : "") +
          "</div>",
      );
    }
    const anomalies = data.anomalies || {};
    const anomalyItems = anomalies.items || [];
    if (anomalies.data_truncated) {
      // The truncation notice must be visible even when the table is empty:
      // a day whose rows all fell over the cap shows "no incidents" while
      // real ones were dropped (v2.20.24 audit).
      blocks.push(
        '<div class="detail-block"><h3>' +
          esc(t("report_anomaly_events", "异常事件")) +
          '</h3><p class="muted">' +
          esc(
            t(
              "report_anomaly_data_truncated",
              "事件量超出单日统计上限，可能有故障未被统计，故障时长也可能不准确。",
            ),
          ) +
          "</p></div>",
      );
    }
    if (anomalyItems.length) {
      // The image report has this table; the page used to show only the
      // count, so "4 次异常" was all an operator saw here.
      blocks.push(
        '<div class="detail-block"><h3>' +
          esc(t("report_anomaly_events", "异常事件")) +
          (anomalies.unresolved
            ? ' <span class="muted">' +
              esc(
                t("report_unresolved_n", "未恢复 {n} 项", {
                  n: anomalies.unresolved,
                }),
              ) +
              "</span>"
            : "") +
          '</h3><div class="table-wrap"><table class="table"><thead><tr><th>' +
          esc(t("table_time", "时间")) +
          "</th><th>" +
          esc(t("table_name", "名称")) +
          "</th><th>" +
          esc(t("table_status", "状态")) +
          "</th><th>" +
          esc(t("table_duration", "时长")) +
          "</th></tr></thead><tbody>" +
          anomalyItems
            .map(
              (item) =>
                "<tr><td>" +
                esc(item.time || "--") +
                "</td><td>" +
                esc(localizeHidden(item.name || "--")) +
                "</td><td>" +
                esc(
                  item.status_key && STATUS_I18N[item.status_key]
                    ? t(STATUS_I18N[item.status_key], item.status || "--")
                    : item.status || "--",
                ) +
                "</td><td>" +
                esc(reportDurationText(item)) +
                "</td></tr>",
            )
            .join("") +
          "</tbody></table></div>" +
          (anomalies.total > anomalyItems.length
            ? '<p class="muted">' +
              esc(
                t("report_more_events", "另有 {n} 条未列出", {
                  n: anomalies.total - anomalyItems.length,
                }),
              ) +
              "</p>"
            : "") +
          "</div>",
      );
    }
    const qq = data.qq || {};
    // `yesterday` is an object even when the section is hidden or empty, so
    // testing it alone rendered a block of "--" instead of the reason.
    if (qq.available && qq.yesterday) {
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
    const quality = data.provider_quality || {};
    if (quality.available) {
      const stats = quality.stats || {};
      const pairs = [
        [t("report_calls", "调用次数"), stats.calls_text],
        [t("quality_success", "成功率"), stats.success_rate_text],
        [t("quality_failed_aborted", "失败 · 中断"), (stats.errors_text || "--") + " · " + (stats.aborted_text || "--")],
        [t("quality_latency", "平均延迟"), stats.avg_latency_text, "P95 " + (stats.p95_latency_text || "--")],
        [t("quality_ttft", "首字延迟"), stats.avg_ttft_text, "P95 " + (stats.p95_ttft_text || "--")],
        [t("quality_streak", "当前连续失败"), stats.streak_text],
      ];
      blocks.push(
        '<div class="detail-block"><h3>' +
          esc(t("report_quality", "LLM 服务质量")) +
          '</h3><div class="detail-grid">' +
          pairs
            .map(
              (pair) =>
                '<div class="detail-item"><span>' +
                esc(pair[0]) +
                "</span><strong>" +
                esc(pair[1] || "--") +
                (pair[2] ? '<small class="detail-sub">' + esc(pair[2]) + "</small>" : "") +
                "</strong></div>",
            )
            .join("") +
          "</div>" +
          (quality.top_models && quality.top_models.length
            ? '<div class="table-wrap"><table class="table"><thead><tr><th>' +
              esc(t("table_model", "模型")) +
              "</th><th>" +
              esc(t("table_calls", "调用")) +
              "</th><th>" +
              esc(t("table_errors", "失败")) +
              "</th><th>" +
              esc(t("table_fail_rate", "失败率")) +
              "</th><th>" +
              esc(t("quality_latency", "平均延迟")) +
              "</th></tr></thead><tbody>" +
              quality.top_models
                .map(
                  (model) =>
                    "<tr><td>" +
                    esc(model.name) +
                    "</td><td>" +
                    esc(model.calls_text) +
                    "</td><td>" +
                    esc(model.errors_text) +
                    "</td><td>" +
                    esc(model.fail_rate_text) +
                    "</td><td>" +
                    esc(model.avg_latency_text) +
                    "</td></tr>",
                )
                .join("") +
              "</tbody></table></div>"
            : "") +
          (quality.top_conversations && quality.top_conversations.length
            ? '<div class="table-wrap"><table class="table"><thead><tr><th>' +
              esc(t("table_conversation", "会话")) +
              "</th><th>" +
              esc(t("table_calls", "调用")) +
              "</th><th>" +
              esc(t("table_errors", "失败")) +
              "</th></tr></thead><tbody>" +
              quality.top_conversations
                .map(
                  (conv) =>
                    "<tr><td>" +
                    esc(conv.conversation) +
                    "</td><td>" +
                    esc(conv.calls_text) +
                    "</td><td>" +
                    esc(conv.errors_text) +
                    "</td></tr>",
                )
                .join("") +
              "</tbody></table></div>"
            : "") +
          (quality.note
            ? '<p class="card-note">' +
              esc(
                (quality.note_code
                  ? t(quality.note_code, quality.note)
                  : quality.note) +
                  (Number(stats.aborted_long) > 0
                    ? " " +
                      t(
                        "quality_note_long",
                        "窗口内持续 ≥60 秒的中断 {n} 次，多为上游超时后被取消，建议关注。",
                      ).replace("{n}", String(stats.aborted_long))
                    : "") +
                  (quality.note_truncated
                    ? " " + t("quality_note_truncated", "窗口内请求超过 2 万条，仅统计最近部分，合计可能偏小。")
                    : ""),
              ) +
              "</p>"
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
                esc(localizeHidden(item.name)) +
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

  function updateReportNav() {
    const next = $("report-next");
    if (!next) return;
    const limit = state.reportToday;
    const current = state.reportDayShown;
    // Disable "next day" once the shown report is today's: clicking it did
    // nothing (the bound check returned early), which read as a broken arrow.
    const atLimit = Boolean(
      limit && current && String(current) >= String(limit),
    );
    next.disabled = atLimit;
    next.setAttribute("aria-disabled", atLimit ? "true" : "false");
    // ``showDbUnavailable`` disables both arrows; a later successful load
    // must re-enable the previous-day arrow, or paging back stays dead.
    const prev = $("report-prev");
    if (prev && state.reportDayShown) {
      prev.disabled = false;
      prev.setAttribute("aria-disabled", "false");
    }
  }

  function shiftReportDay(delta) {
    const current = state.reportDay || state.reportDayShown;
    if (!current) return;
    const parts = String(current).split("-").map(Number);
    if (parts.length !== 3 || parts.some((value) => !Number.isFinite(value))) return;
    const parsed = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2]));
    if (Number.isNaN(parsed.getTime())) return;
    parsed.setUTCDate(parsed.getUTCDate() + delta);
    const next = parsed.toISOString().slice(0, 10);
    // The backend bounds the day by the configured timezone's today. Using
    // the browser's UTC yesterday silently disabled the "next day" arrow
    // between 00:00 and 08:00 in Asia/Shanghai (v2.20.8).
    let limit = state.reportToday;
    if (!limit) {
      const now = new Date();
      limit = new Date(
        Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - 1),
      )
        .toISOString()
        .slice(0, 10);
    }
    if (next > limit) return;
    state.reportDay = next;
    loadReports(true).catch((error) => {
      // Only roll back when no newer request has already moved the pointer.
      // A quick second click starts another load; this (older, failed)
      // request used to overwrite the pointer the newer request had set,
      // so the next "previous day" click computed from the wrong base and
      // requested the day AFTER the one being displayed.
      if (state.reportDay === next) {
        state.reportDay = state.reportDayShown || null;
      }
      toast(error.message || t("error_load", "加载失败"));
    });
  }

  async function loadReports(force) {
    const seq = ++state.reportsSeq;
    const params = {};
    if (force) params.fresh = 1;
    if (state.reportDay) params.day = state.reportDay;
    const data = await apiGet(
      "reports",
      Object.keys(params).length ? params : undefined,
    );
    if (seq !== state.reportsSeq) return;
    // The backend serves the previous payload while another build is in
    // flight; remember it so the refresh toast does not claim "数据已更新".
    state.reportsStale = Boolean(data && data.stale);
    if (data && data.today) state.reportToday = String(data.today);
    if (data.db_unavailable) {
      showDbUnavailable("reports");
      toast(
        t("db_unavailable", "数据库暂时不可用，正在自动恢复；当前显示的可能是旧数据"),
      );
      return;
    }
    if (!data || data.ok !== true) {
      throw new Error(
        t(
          (data && data.error_code) || "report_build_failed",
          (data && data.error) || "日报数据构建失败，请稍后重试",
        ),
      );
    }
    $("report-day").textContent = data.day
      ? t("report_data_day", "数据日 {day}", { day: data.day })
      : t("report_no_data", "暂无数据");
    state.reportDayShown = data.day || null;
    updateReportNav();
    // An empty selection used to render as a page of "未开启" values with no
    // explanation of why nothing was built.
    const sectionsEmpty =
      !Array.isArray(data.sections) || data.sections.length === 0;
    if (sectionsEmpty) {
      // The payload's ``sections`` is already filtered to valid names, so an
      // empty result can also mean "every configured name is a typo". Say
      // which one it is instead of always claiming the config is empty
      // (v2.20.17).
      $("report-day").textContent =
        Number(data.sections_configured || 0) > 0
          ? t(
              "report_sections_invalid",
              "日报内容配置里没有有效项，请检查配置页的勾选",
            )
          : t(
              "report_sections_empty",
              "未选择日报内容：daily_report.sections 为空，请在配置页勾选要显示的内容",
            );
    }
    const summary = data.summary || {};
    const notEnabled = t("report_not_enabled", "未开启");
    const scopeLabel = reportScopeLabel(data, summary);
    const anomalyValue =
      summary.anomaly_enabled === false
        ? notEnabled
        : summary.anomaly_count || 0;
    const unresolvedValue =
      summary.anomaly_enabled === false
        ? notEnabled
        : summary.unresolved_count || 0;
    const tokenValue =
      summary.token_enabled === false
        ? notEnabled
        : summary.token_total_code === "no_data"
          ? t("report_no_data", "暂无数据")
          : summary.token_total_text || t("report_no_data", "暂无数据");
    const sentValue =
      summary.qq_enabled === false
        ? notEnabled
        : summary.sent_text || "--";
    const recvValue =
      summary.qq_enabled === false
        ? notEnabled
        : summary.recv_text || "--";
    $("report-summary").innerHTML = [
      metric(
        t("report_anomaly", "异常 / 失败"),
        anomalyValue,
        scopeLabel,
        null,
      ),
      metric(
        t("report_token", "Token"),
        tokenValue,
        scopeLabel,
        null,
      ),
      metric(
        t("report_sent", "发送"),
        sentValue,
        scopeLabel,
        null,
      ),
      metric(
        t("report_recv", "接收"),
        recvValue,
        scopeLabel,
        null,
      ),
      metric(
        t("report_unresolved", "未恢复"),
        unresolvedValue,
        t("report_recent30", "近 30 天"),
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
              '<span class="result ' +
              (run.ok ? "result-ok" : "result-fail") +
              '">' +
              (run.ok
                ? t("result_ok", "成功")
                : t("result_fail", "失败")) +
              "</span></td><td>" +
              esc(methodText(run.detail)) +
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

  async function loadTab(name, force) {
    if (name === "overview") return loadOverview();
    if (name === "history") return loadHistory();
    if (name === "events") return loadEvents();
    if (name === "reports") return loadReports(force);
    return undefined;
  }

  function clearPanelError(name) {
    const panel = $("panel-" + name);
    if (!panel) return;
    const existing = panel.querySelector(".error-state");
    if (existing) existing.remove();
  }

  function showPanelError(name, error) {
    const panel = $("panel-" + name);
    if (!panel) return;
    clearPanelError(name);
    const box = document.createElement("div");
    box.className = "error-state";
    box.innerHTML =
      "<p>" +
      esc(error && error.message ? error.message : String(error || "")) +
      '</p><button type="button" class="ghost">' +
      esc(t("error_retry", "重试")) +
      "</button>";
    box.querySelector("button").addEventListener("click", () => {
      state.loaded.delete(name);
      activateTab(name, true)
        .then(() => {
          clearPanelError(name);
          toast(t("refresh_done", "数据已更新"));
        })
        .catch((retryError) => {
          showPanelError(name, retryError);
          toast(retryError.message || t("error_load", "加载失败"));
        });
    });
    panel.prepend(box);
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
    const lastLoaded = state.loadedAt.get(name) || 0;
    if (
      !force &&
      state.loaded.has(name) &&
      Date.now() - lastLoaded < 5000
    ) {
      return;
    }
    try {
      await loadTab(name, force);
      clearPanelError(name);
      state.loaded.add(name);
      state.loadedAt.set(name, Date.now());
    } catch (error) {
      if (force) throw error;
      showPanelError(name, error);
      toast(error.message || t("error_load", "加载失败"));
    }
  }

  const VERIFY_LABEL = {
    ok: ["verify_state_ok", "成功"],
    uncertain: ["verify_state_uncertain", "未确认（可能未送达）"],
    failed: ["verify_state_failed", "失败"],
  };

  function verifyRow(text, bad) {
    return '<li class="' + (bad ? "verify-bad" : "") + '">' + esc(text) + "</li>";
  }

  function verifyNoteText(data) {
    if (!data) return t("verify_none", "没有可验证的项");
    if (data.note_code && data.note_code.code) {
      const code = data.note_code.code;
      if (code === "monitor_items_incomplete") {
        return t(
          "verify_monitor_items_incomplete",
          "{count} 个监测项配置不完整（缺少 URL/端口等），请在配置页补全后重试",
        ).replace("{count}", String(data.note_code.count || 0));
      }
      if (code === "monitor_items_empty") {
        return t("verify_monitor_items_empty", "没有已启用的监测项");
      }
      if (code === "monitor_disabled") {
        return t(
          "verify_monitor_disabled",
          "服务监测已关闭：本次只是手动探测，后台不会自动检查或告警",
        );
      }
      if (code === "monitor_no_targets") {
        return t(
          "verify_monitor_no_targets",
          "没有可用的服务监测推送目标：后台不会自动检查或告警",
        );
      }
      if (code === "monitor_verify_timeout") {
        return t(
          "monitor_verify_timeout",
          "另有 {count} 个监测项在 80 秒内未完成，已取消",
        ).replace("{count}", String(data.note_code.count || 0));
      }
    }
    return data.note || t("verify_none", "没有可验证的项");
  }

  function renderVerifyResult(kind, data) {
    const box = $("verify-result");
    if (!box) return;
    if (!data || data.ok !== true) {
      // The backend sends a stable error_code so the English page can
      // translate the message; without it the Chinese sentence was shown
      // verbatim (v2.20.14). ``t(code, "")`` falls back to the KEY, so an
      // untranslated (new) code used to print "verify_xxx" in the error box
      // -- treat "translation === key" as missing and use the real message.
      const code = data && data.error_code;
      const coded = code ? t(code, "") : "";
      const errorText =
        (coded && coded !== code ? coded : "") ||
        (data && data.error) ||
        t("verify_failed", "验证失败");
      box.innerHTML =
        '<div class="error-state"><p>' +
        esc(errorText) +
        "</p></div>";
      return;
    }
    if (kind === "webhook") {
      if (!data.listening) {
        // The backend sends a stable message_code; render its translation
        // instead of the hard-coded Chinese sentence (v2.20.20).
        const code = data.message_code;
        const coded = code ? t(code, "") : "";
        const message =
          (coded && coded !== code ? coded : "") ||
          data.message ||
          t("verify_webhook_not_listening", "Webhook 未在监听");
        box.innerHTML =
          '<ul class="verify-list">' + verifyRow(message, true) + "</ul>";
        return;
      }
      const good = Number(data.http_status) === 200;
      // ``http_status: 0`` means the request never got a response. Telling
      // the operator to "check the Token" (the advice for a 403/503) sent
      // them in the wrong direction (v2.20.21).
      const text = good
        ? t(
            "verify_webhook_ok",
            "链路验证通过（HTTP 200）：监听 → 解析 → 推送均正常。",
          )
        : Number(data.http_status) === 0
          ? (data.message || "") +
            " " +
            t(
              "verify_webhook_unreachable",
              "无法连接到 Webhook 监听地址：请确认 host/port 与插件进程状态。",
            )
          : (data.message || "") +
            " " +
            t(
              "verify_webhook_bad",
              "HTTP {status}：请检查 Token 与目标配置。",
              {
                status: data.http_status,
              },
            );
      box.innerHTML = '<ul class="verify-list">' + verifyRow(text, !good) + "</ul>";
      return;
    }
    const results = data.results || [];
    if (!results.length) {
      box.innerHTML =
        '<ul class="verify-list">' +
        verifyRow(verifyNoteText(data), false) +
        "</ul>";
      return;
    }
    // A manual probe can return green rows while the background monitor is
    // disabled or has no push target. Keep that note visible ABOVE the rows
    // instead of dropping it (v2.20.21).
    const noteRow =
      data.note || data.note_code
        ? verifyRow(verifyNoteText(data), false)
        : "";
    const rows = results
      .map((item) => {
        if (kind === "targets") {
          const entry = VERIFY_LABEL[item.state] || VERIFY_LABEL.failed;
          // Full-width punctuation is Chinese-only: the English page showed
          // "target：delivered（error）" (v2.20.20).
          const extra = item.error
            ? (isEnglish() ? " (" : "（") + item.error + (isEnglish() ? ")" : "）")
            : "";
          const retcode =
            item.retcode !== null && item.retcode !== undefined
              ? " · retcode " + item.retcode
              : "";
          return verifyRow(
            item.target +
              (isEnglish() ? ": " : "：") +
              t(entry[0], entry[1]) +
              retcode +
              extra,
            item.state !== "ok",
          );
        }
        const detail = localizeHidden(item.detail || "");
        // HTTP probe details already end with their own elapsed time;
        // appending it again showed the duration twice. Match either the
        // full-width or the ASCII parentheses so the English page does not
        // duplicate it (v2.20.21).
        const openParen = isEnglish() ? " (" : "（";
        const closeParen = isEnglish() ? ")" : "）";
        const elapsed = /[（(]\d+ms[）)]\s*$/.test(detail)
          ? ""
          : openParen + item.elapsed_ms + "ms" + closeParen;
        return verifyRow(
          localizeHidden(item.name || "--") +
            (isEnglish() ? ": " : "：") +
            (item.ok ? "✅" : "❌") +
            " " +
            detail +
            elapsed,
          !item.ok,
        );
      })
      .join("");
    box.innerHTML =
      '<ul class="verify-list">' + noteRow + rows + "</ul>";
  }

  let verifyArmTimer = 0;

  const VERIFY_LABELS = {
    targets: ["verify_targets", "验证推送目标"],
    monitors: ["verify_monitors", "验证监测项"],
    webhook: ["verify_webhook", "验证 Webhook"],
  };

  function verifyLabel(kind) {
    const entry = VERIFY_LABELS[kind] || VERIFY_LABELS.targets;
    return t(entry[0], entry[1]);
  }

  // One shared result box serves all three verify kinds; two concurrent
  // runs used to race and the later response overwrote the earlier one
  // out of order (v2.20.0). Serialize at the UI level.
  let verifyInflight = false;
  async function runVerify(kind, button) {
    if (!button || button.disabled) return;
    if (verifyInflight) {
      const busyBox = $("verify-result");
      if (busyBox) {
        busyBox.innerHTML =
          '<p class="muted">' + esc(t("verifying", "验证中…")) + "</p>";
      }
      return;
    }
    // The dashboard iframe sandbox has no allow-modals, so window.confirm is
    // silently rejected there. Use a two-step arm button instead: the first
    // click arms the button, the second click (within 5s) really sends.
    if (kind === "targets" && button.dataset.armed !== "1") {
      button.dataset.armed = "1";
      button.classList.add("verify-armed");
      button.textContent = t("verify_confirm_again", "再点一次确认发送");
      clearTimeout(verifyArmTimer);
      verifyArmTimer = setTimeout(() => {
        // Only restore this button if it is still armed: another verify
        // button's run clears the shared timer id, and without this check a
        // stale timer never fired while the armed state leaked (the next
        // click skipped the confirmation entirely).
        if (button.dataset.armed === "1") {
          button.dataset.armed = "";
          button.classList.remove("verify-armed");
          button.textContent = verifyLabel(kind);
        }
      }, 5000);
      return;
    }
    if (kind === "targets") {
      // Only the targets flow owns the shared arm timer; clearing it from
      // any other kind's run used to freeze the armed button mid-confirm.
      clearTimeout(verifyArmTimer);
    }
    button.dataset.armed = "";
    button.classList.remove("verify-armed");
    button.disabled = true;
    verifyInflight = true;
    // The other two buttons share the result box; leaving them clickable made
    // the page look stuck (clicks only printed "verifying…" in the box).
    const otherButtons = ["verify-targets", "verify-monitors", "verify-webhook"]
      .map((id) => $(id))
      .filter((element) => element && element !== button);
    otherButtons.forEach((element) => {
      element.disabled = true;
    });
    button.textContent = t("verifying", "验证中…");
    const box = $("verify-result");
    if (box) {
      box.innerHTML = '<p class="muted">' + esc(t("verifying", "验证中…")) + "</p>";
    }
    try {
      const data = await apiPost("verify", { kind });
      renderVerifyResult(kind, data);
    } catch (error) {
      if (box) {
        box.innerHTML =
          '<div class="error-state"><p>' +
          esc(error.message || t("error_load", "加载失败")) +
          "</p></div>";
      }
    } finally {
      verifyInflight = false;
      button.disabled = false;
      otherButtons.forEach((element) => {
        element.disabled = false;
      });
      // Restore this button's own label, not a fixed one: the old code wrote
      // "验证推送目标" for every kind, so after checking monitors the page
      // showed two "push targets" buttons and the second one did not send.
      button.textContent = verifyLabel(kind);
    }
  }

  async function refreshCurrent() {
    const button = $("refresh");
    if (button && button.classList.contains("refreshing")) return;
    state.loaded.delete(state.tab);
    setRefreshState(button, "refreshing");
    try {
      await activateTab(state.tab, true);
      if (state.tab !== "overview") {
        // The header timestamp only comes from the overview payload. Refresh
        // it in the background, otherwise "更新于" stays at the time the page
        // was opened even though the visible tab just reloaded.
        try {
          updateSubtitle(await apiGet("overview"));
        } catch (error) {
          // Keep the old timestamp; the visible tab did refresh successfully.
        }
      }
      setRefreshState(button, "done");
      toast(
        state.reportsStale
          ? t("refresh_stale", "已有一次刷新正在进行，当前显示的是上次数据")
          : t("refresh_done", "数据已更新"),
      );
      state.reportsStale = false;
    } catch (error) {
      setRefreshState(button, "failed");
      showPanelError(state.tab, error);
      toast(error.message || t("error_load", "加载失败"));
    }
  }

  function setRefreshState(button, status) {
    if (!button) return;
    // A "done" state schedules a 2s reset; a new refresh starting inside that
    // window used to leave the timer alive, so it fired mid-flight and put the
    // button back to idle (clickable) while the request was still running.
    clearTimeout(button._resetTimer);
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
    // Older reports: a report that was never delivered (bot offline at 21:00)
    // used to be invisible in the dashboard -- only the newest day existed.
    // The backend accepts ?day= and the arrows walk backwards from it.
    ["report-prev", "report-next"].forEach((id, index) => {
      const button = $(id);
      if (!button) return;
      button.addEventListener("click", () => shiftReportDay(index === 0 ? -1 : 1));
    });
    [
      ["verify-targets", "targets"],
      ["verify-monitors", "monitors"],
      ["verify-webhook", "webhook"],
    ].forEach(([id, kind]) => {
      const button = $(id);
      if (button) {
        button.addEventListener("click", () => runVerify(kind, button));
      }
    });
    $("export-csv").addEventListener("click", () => {
      exportCsv().catch((error) =>
        toast(error.message || t("error_load", "加载失败")),
      );
    });

    document.querySelectorAll("#range-switch button").forEach((button) => {
      button.addEventListener("click", () => {
        const requested = button.dataset.range;
        if (!requested || requested === state.historyRange) return;
        const previous = state.historyRange;
        state.historyRange = requested;
        setRangeActive();
        loadHistory().catch((error) => {
          if (state.historyRange !== requested) return;
          state.historyRange = previous;
          setRangeActive();
          toast(error.message || t("error_load", "加载失败"));
        });
      });
    });

    document.querySelectorAll("#event-switch button").forEach((button) => {
      button.addEventListener("click", () => {
        const requested = button.dataset.days;
        if (!requested || String(requested) === String(state.eventDays)) return;
        const previous = state.eventDays;
        state.eventDays = requested;
        document
          .querySelectorAll("#event-switch button")
          .forEach((item) => item.classList.toggle("active", item === button));
        loadEvents().catch((error) => {
          // Mirror the history range switch: a failed load used to leave the
          // new range highlighted over the old data (2026-11-08 audit).
          if (String(state.eventDays) !== String(requested)) return;
          state.eventDays = previous;
          document
            .querySelectorAll("#event-switch button")
            .forEach((item) =>
              item.classList.toggle(
                "active",
                item.dataset.days === String(previous),
              ),
            );
          toast(error.message || t("error_load", "加载失败"));
        });
      });
    });


  }

  function refreshLocale() {
    const next = currentLocale();
    if (!state.locale) {
      state.locale = next;
      return;
    }
    if (next === state.locale) return;
    state.locale = next;
    applyStaticI18n();
    activateTab(state.tab, true).catch(() => {});
  }

  function applyThemeFallback() {
    // Older bridges never call onContext, so the page used to stay light on
    // a dark system. Follow the OS until the bridge provides a real theme.
    const root = document.documentElement;
    if (root.dataset.theme) return;
    let dark = false;
    try {
      dark = !!(
        window.matchMedia &&
        window.matchMedia("(prefers-color-scheme: dark)").matches
      );
    } catch (error) {
      dark = false;
    }
    root.dataset.theme = dark ? "dark" : "light";
  }

  function bindTheme() {
    const bridge = getBridge();
    if (!bridge || typeof bridge.onContext !== "function") return;
    bridge.onContext((context) => {
      if (!context) return;
      document.documentElement.dataset.theme = context.isDark
        ? "dark"
        : "light";
      refreshLocale();
    });
  }

  async function init() {
    applyThemeFallback();
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
    state.locale = currentLocale();
    applyStaticI18n();
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshLocale();
    });
    await activateTab("overview");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
