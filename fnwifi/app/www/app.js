/* ==========================================================================
 * fnwifi 前端逻辑
 *
 * 说明（重构后）：
 *   - 已移除国际化（i18n），所有文案直接使用中文；
 *   - 已移除夜间模式 / 主题跟随逻辑（UI 固定浅色）；
 *   - 已移除国家码自定义选项（后端固定 CN）；
 *   - 概览页与设置页的密码都支持小眼睛切换明文显示。
 * ========================================================================== */

const API = "/app/fnwifi/api";

const state = {
  config: null,
  status: null,
  channels: { bg: [], a: [] },
  clients: [],
  running: false,
  loaded: false,
  busy: false,
  polling: false,
  showPassword: false,          // 概览页密码明文开关
  showSettingsPassword: false,  // 设置页密码明文开关
  speedSample: null,            // { time, byMac: { mac: {rx, tx} } }
  maxRate: 1024 * 1024,         // 自适应网速条上限 (1 MB/s 起步)
  speedHistory: [],             // [{ down, up }] 最近 60 个采样点,用于折线图
};

/* ---------------------------- 页面元素引用 ---------------------------- */
const els = {
  summary: document.getElementById("statusSummary"),
  toggle: document.getElementById("toggleBtn"),
  save: document.getElementById("saveBtn"),
  form: document.getElementById("configForm"),
  iface: document.getElementById("ifaceSelect"),
  uplink: document.getElementById("uplinkSelect"),
  channel: document.getElementById("channelSelect"),
  clients: document.getElementById("clients"),
  clientCount: document.getElementById("clientCount"),
  toast: document.getElementById("toast"),
  modal: document.getElementById("modal"),
  modalTitle: document.getElementById("modalTitle"),
  modalBody: document.getElementById("modalBody"),
  modalOk: document.getElementById("modalOk"),
  modalCancel: document.getElementById("modalCancel"),
  // 概览页
  ovSsid: document.getElementById("ovSsid"),
  ovPassword: document.getElementById("ovPassword"),
  togglePwdBtn: document.getElementById("togglePwdBtn"),
  ovClientCount: document.getElementById("ovClientCount"),
  speedDown: document.getElementById("speedDown"),
  speedUp: document.getElementById("speedUp"),
  speedChart: document.getElementById("speedChart"),
  lineDown: document.getElementById("lineDown"),
  lineUp: document.getElementById("lineUp"),
  chartGrid: document.querySelector(".grid-lines"),
  wiBand: document.getElementById("wiBand"),
  wiChannel: document.getElementById("wiChannel"),
  wiWidth: document.getElementById("wiWidth"),
  wiIp: document.getElementById("wiIp"),
  wiTxPower: document.getElementById("wiTxPower"),
  wiHotspotIface: document.getElementById("wiHotspotIface"),
  wiStaAp: document.getElementById("wiStaAp"),
  wiIsolation: document.getElementById("wiIsolation"),
  wiAllowPorts: document.getElementById("wiAllowPorts"),
  // 设置页
  settingsPwd: document.getElementById("settingsPwd"),
  toggleSettingsPwdBtn: document.getElementById("toggleSettingsPwdBtn"),
  portPolicySelect: document.getElementById("portPolicySelect"),
  allowPortsField: document.getElementById("allowPortsField"),
  // 导航
  navStatus: document.getElementById("navStatus"),
  navClients: document.getElementById("navClients"),
  navSettings: document.getElementById("navSettings"),
  pageStatus: document.getElementById("page-status"),
  pageClients: document.getElementById("page-clients"),
  pageSettings: document.getElementById("page-settings"),
};

/* ------------------------------ 基础工具 ------------------------------ */
function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
}

function apiUrl(action) {
  return `${API}/${encodeURIComponent(String(action).split("?")[0])}?${String(
    action,
  ).includes("?") ? String(action).split("?")[1] : ""}`;
}

async function api(action, { method = "GET", data = null } = {}) {
  const options = { method, cache: "no-store" };
  if (data) {
    options.method = method === "GET" ? "POST" : method;
    options.headers = { "Content-Type": "application/x-www-form-urlencoded" };
    options.body = new URLSearchParams(data);
  }
  const response = await fetch(apiUrl(action), options);
  const result = await response.json();
  if (!response.ok || result.ok === false) {
    throw new Error(
      result.error || result.message || `HTTP ${response.status}`,
    );
  }
  return result;
}

function setOptions(select, values, selected, firstLabel = "") {
  const options = [];
  if (firstLabel)
    options.push(`<option value="">${escapeHtml(firstLabel)}</option>`);
  values.forEach((value) => {
    const label = Array.isArray(value) ? value[1] : value;
    const optionValue = Array.isArray(value) ? value[0] : value;
    options.push(
      `<option value="${escapeHtml(optionValue)}">${escapeHtml(label)}</option>`,
    );
  });
  select.innerHTML = options.join("");
  select.value = selected || "";
}

function channelOptions() {
  const band = els.form.elements.band.value || "bg";
  const raw = state.channels[band] || [];
  const parsed = raw.map((item) => {
    const [channel, freq, support] = String(item).split(":");
    return [
      channel,
      `${channel} (${freq} MHz${support === "disabled" ? ", 不可用" : ""})`,
    ];
  });
  // 后端拿不到信道列表时的兜底选项
  if (!parsed.length) {
    return band === "a"
      ? [
          ["36", "36"],
          ["40", "40"],
          ["44", "44"],
          ["48", "48"],
          ["149", "149"],
        ]
      : [
          ["1", "1"],
          ["6", "6"],
          ["11", "11"],
        ];
  }
  return parsed;
}

function applyPortPolicyUI() {
  const policy = els.portPolicySelect ? els.portPolicySelect.value : "custom";
  if (els.allowPortsField) {
    els.allowPortsField.style.display = policy === "custom" ? "" : "none";
  }
}

function fillForm() {
  const cfg = state.config || {};
  els.form.ssid.value = cfg.ssid || "";
  els.form.password.value = cfg.password || "";
  els.form.ipCidr.value = cfg.ipCidr || "";
  els.form.allowPorts.value =
    cfg.allowPorts && cfg.allowPorts !== "*" ? cfg.allowPorts : "";
  els.form.band.value = cfg.band || "bg";
  els.form.channelWidth.value = cfg.channelWidth || "20";
  els.form.isolation.value = cfg.isolation === "0" ? "0" : "1";
  // 端口策略三态:* = 全部放行,空 = 全部拦截,其他 = 部分端口
  const allowPorts = (cfg.allowPorts || "").trim();
  const policy =
    allowPorts === "*" ? "allow" : allowPorts === "" ? "deny" : "custom";
  if (els.portPolicySelect) els.portPolicySelect.value = policy;
  applyPortPolicyUI();
  setOptions(els.channel, channelOptions(), cfg.channel || "");
}

function collectForm() {
  const policy = els.portPolicySelect ? els.portPolicySelect.value : "custom";
  // 按策略生成 ALLOW_PORTS:* = 全部放行,空 = 全部拦截,自定义 = 端口列表
  let allowPorts = "";
  if (policy === "allow") {
    allowPorts = "*";
  } else if (policy === "custom") {
    allowPorts = (els.form.allowPorts.value || "").trim();
  }
  return {
    iface: els.form.iface.value,
    uplinkIface: els.form.uplinkIface.value,
    ssid: els.form.ssid.value,
    password: els.form.password.value,
    ipCidr: els.form.ipCidr.value,
    allowPorts,
    band: els.form.band.value,
    channel: els.form.channel.value,
    channelWidth: els.form.channelWidth.value,
    isolation: els.form.isolation.value || "1",
  };
}

function formatBytes(value) {
  const n = Number(value || 0);
  if (!n) return "-";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function formatSpeed(bytesPerSec) {
  const n = Number(bytesPerSec || 0);
  if (n <= 0) return "0 B/s";
  if (n < 1024) return `${n.toFixed(0)} B/s`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB/s`;
  return `${(n / 1024 / 1024).toFixed(2)} MB/s`;
}

function formatDuration(seconds) {
  const s = Number(seconds || 0);
  if (!s) return "-";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
}

function bandLabel(band) {
  return band === "a" ? "5G" : "2.4G";
}

/* ========================= 实时速率计算 ========================= */
// 后端 clients 返回的是累计字节数,轮询两次做差值得到速率
function computeRates(clients) {
  const now = Date.now();
  const prev = state.speedSample;
  const deltaSec = prev ? (now - prev.time) / 1000 : 0;
  const byMac = {};
  let totalDown = 0;
  let totalUp = 0;
  for (const client of clients) {
    const mac = client.mac;
    // 注意:iw station dump 的 rx/tx 是 AP 视角——
    // rx bytes = AP 收到(客户端上传),tx bytes = AP 发出(客户端下载)
    const rx = Number(client.rxBytes || 0); // 上传字节
    const tx = Number(client.txBytes || 0); // 下载字节
    const prevEntry = prev?.byMac?.[mac];
    let downloadRate = 0;
    let uploadRate = 0;
    if (prevEntry && deltaSec > 0) {
      downloadRate = Math.max(0, (tx - prevEntry.tx) / deltaSec);
      uploadRate = Math.max(0, (rx - prevEntry.rx) / deltaSec);
    }
    client.downloadRate = downloadRate;
    client.uploadRate = uploadRate;
    byMac[mac] = { rx, tx };
    totalDown += downloadRate;
    totalUp += uploadRate;
  }
  state.speedSample = { time: now, byMac };
  state.totalDown = totalDown;
  state.totalUp = totalUp;
  // 折线图历史(最多 60 个点 ≈ 2 分钟)
  state.speedHistory.push({ down: totalDown, up: totalUp });
  if (state.speedHistory.length > 60) {
    state.speedHistory.shift();
  }
  const peak = Math.max(totalDown, totalUp, 64 * 1024);
  if (peak > state.maxRate) state.maxRate = peak;
  else state.maxRate = Math.max(64 * 1024, state.maxRate * 0.98);
}

function chartPoints() {
  const W = 600;
  const H = 160;
  const PAD = 4;
  const data = state.speedHistory;
  if (!data.length) {
    return { down: "", up: "", max: 0, grid: "" };
  }
  // 自适应峰值:取历史最大值(含 padding),至少 64KB/s
  let max = Math.max(...data.map((p) => Math.max(p.down, p.up)), 64 * 1024);
  max *= 1.15;
  const n = data.length;
  // 固定步长(按 60 点满宽计算),最新点在右侧,历史向左展开——曲线从右侧"起点"出现
  const MAX_POINTS = 60;
  const stepX = (W - PAD * 2) / Math.max(MAX_POINTS - 1, 1);
  const toY = (v) => H - PAD - (v / max) * (H - PAD * 2);
  const toPoints = (key) =>
    data.map((p, i) => ({ x: W - PAD - (n - 1 - i) * stepX, y: toY(p[key]) }));
  // 中点二次贝塞尔平滑:曲线经过相邻点中点,控制点为数据点本身,
  // 曲线被限制在数据点之间,不会 overshoot 到 0 线以下
  const smoothPath = (pts) => {
    if (pts.length < 2) return "";
    const mid = (a, b) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
    const first = mid(pts[0], pts[1]);
    let d = `M ${first.x.toFixed(1)},${first.y.toFixed(1)}`;
    for (let i = 1; i < pts.length - 1; i++) {
      const m = mid(pts[i], pts[i + 1]);
      d += ` Q ${pts[i].x.toFixed(1)},${pts[i].y.toFixed(1)} ${m.x.toFixed(1)},${m.y.toFixed(1)}`;
    }
    const last = pts[pts.length - 1];
    d += ` L ${last.x.toFixed(1)},${last.y.toFixed(1)}`;
    return d;
  };
  const downPath = smoothPath(toPoints("down"));
  const upPath = smoothPath(toPoints("up"));
  // 网格线:3 条水平虚线
  const grid = [0.25, 0.5, 0.75]
    .map(
      (f) =>
        `<line x1="0" y1="${toY(max * f).toFixed(1)}" x2="${W}" y2="${toY(max * f).toFixed(1)}"></line>`,
    )
    .join("");
  return { down: downPath, up: upPath, max, grid };
}

function renderChart() {
  const pts = chartPoints();
  if (!els.lineDown || !els.lineUp) return;
  els.lineDown.setAttribute("d", pts.down);
  els.lineUp.setAttribute("d", pts.up);
  if (els.chartGrid) {
    els.chartGrid.innerHTML = pts.grid;
  }
}

/* ============================== 渲染 ============================== */
function render() {
  const status = state.status || {};
  const cfg = state.config || {};
  state.running = Boolean(status.running);

  // 侧栏开关
  els.summary.textContent = state.running
    ? `运行中 · ${status.hotspotIface || "-"}`
    : "已停止";
  els.toggle.textContent = state.running ? "关闭热点" : "开启热点";

  // 总览 SSID / 密码（默认掩码，点小眼睛切换明文）
  els.ovSsid.textContent = cfg.ssid || "-";
  if (cfg.password) {
    els.ovPassword.textContent = state.showPassword
      ? cfg.password
      : "•".repeat(Math.min(cfg.password.length, 12));
  } else {
    els.ovPassword.textContent = "开放（无密码）";
  }
  if (els.togglePwdBtn) {
    els.togglePwdBtn.classList.toggle("eye-active", state.showPassword);
  }
  // 终端数量:直接取已拉取的客户端列表长度
  els.ovClientCount.textContent = String((state.clients || []).length);

  // 实时网速(折线图)
  els.speedDown.textContent = formatSpeed(state.totalDown);
  els.speedUp.textContent = formatSpeed(state.totalUp);
  renderChart();

  // WiFi 信息（国家码已固定 CN，不再展示该项）
  els.wiBand.textContent = bandLabel(cfg.band || "bg");
  els.wiChannel.textContent = cfg.channel || "-";
  els.wiWidth.textContent = cfg.channelWidth ? `${cfg.channelWidth} MHz` : "-";
  els.wiIp.textContent = status.ip ? `${status.ip}` : "-";
  els.wiTxPower.textContent = status.txPowerDbm ? `${status.txPowerDbm} dBm` : "-";
  els.wiHotspotIface.textContent = status.hotspotIface || status.iface || "-";
  els.wiStaAp.textContent = status.staApConcurrent ? "支持" : "不支持";
  els.wiStaAp.className = status.staApConcurrent ? "ok" : "bad";
  const isolated = cfg.isolation !== "0";
  els.wiIsolation.textContent = isolated ? "开启" : "关闭";
  els.wiIsolation.className = isolated ? "ok" : "bad";
  els.wiAllowPorts.textContent =
    cfg.allowPorts === "*"
      ? "全部放行"
      : (cfg.allowPorts || "").trim() === ""
        ? "全部拦截"
        : cfg.allowPorts || "-";
}

function renderClients(clients) {
  els.clientCount.textContent = String(clients.length);
  if (!clients.length) {
    els.clients.innerHTML = `<div class="empty">暂无客户端</div>`;
    return;
  }
  els.clients.innerHTML = clients
    .map(
      (client) => `
    <div class="client-row">
      <strong title="${escapeHtml(client.mac || "")}">${escapeHtml(
        client.hostname || client.mac || "-",
      )}</strong>
      <span>${escapeHtml(client.ip || "-")}</span>
      <span>${client.signalDbm == null ? "-" : `${client.signalDbm} dBm`}</span>
      <span>${formatDuration(client.connectedSeconds)}</span>
      <span class="num">${formatSpeed(client.downloadRate)}</span>
      <span class="num">${formatSpeed(client.uploadRate)}</span>
      <button class="danger-btn" type="button" data-kick="${escapeHtml(
        client.mac || "",
      )}">下线</button>
    </div>
  `,
    )
    .join("");
}

/* ============================ 页面切换 ============================ */
const NAV_PAGES = ["status", "clients", "settings"];
const NAV_EL_IDS = {
  status: "navStatus",
  clients: "navClients",
  settings: "navSettings",
};
const PAGE_EL_IDS = {
  status: "pageStatus",
  clients: "pageClients",
  settings: "pageSettings",
};

function switchPage(name) {
  NAV_PAGES.forEach((page) => {
    const navEl = els[NAV_EL_IDS[page]];
    const pageEl = els[PAGE_EL_IDS[page]];
    if (navEl) navEl.classList.toggle("active", page === name);
    if (pageEl) pageEl.classList.toggle("active", page === name);
  });
}

/* ============================ 数据加载 ============================ */
function setBusy(busy) {
  state.busy = Boolean(busy);
  els.save.disabled = state.busy || !state.loaded;
  els.toggle.disabled = state.busy || !state.loaded;
}

async function loadAll() {
  setBusy(true);
  try {
    const [config, ifaces, uplinks, status, clients] = await Promise.all([
      api("config_get"),
      api("ifaces"),
      api("uplinks"),
      api("status"),
      api("clients"),
    ]);
    state.config = config.config || {};
    state.channels = config.channelOptions || { bg: [], a: [] };
    state.status = status.status || {};
    state.clients = clients.clients || [];
    // 热点网卡:默认选中第一张无线网卡;共享网卡:默认选中系统默认网口
    const ifaceList = ifaces.ifaces || [];
    const uplinkList = uplinks.uplinks || [];
    const defaultUplink =
      state.status.effectiveUplinkIface ||
      state.status.uplinkIface ||
      uplinkList[0] ||
      "";
    setOptions(els.iface, ifaceList, state.config.iface || ifaceList[0] || "");
    setOptions(
      els.uplink,
      uplinkList,
      state.config.uplinkIface || defaultUplink,
    );
    fillForm();
    state.speedSample = null; // 重新开始采样
    computeRates(state.clients);
    state.loaded = true;
    render();
    renderClients(state.clients);
  } finally {
    setBusy(false);
  }
}

async function refreshLiveData({ silent = true } = {}) {
  if (!state.loaded || state.busy || state.polling) return;
  state.polling = true;
  try {
    const [status, clients] = await Promise.all([
      api("status"),
      api("clients"),
    ]);
    state.status = status.status || {};
    state.clients = clients.clients || [];
    computeRates(state.clients);
    render();
    renderClients(state.clients);
  } catch (error) {
    if (!silent) showToast(error.message, true);
  } finally {
    state.polling = false;
  }
}

/* ============================ 用户操作 ============================ */
function showToast(message, error = false) {
  els.toast.textContent = message;
  els.toast.classList.toggle("error", error);
  els.toast.classList.remove("hidden");
  clearTimeout(els.toast._timer);
  els.toast._timer = setTimeout(() => els.toast.classList.add("hidden"), 2600);
}

function confirmDialog(title, body) {
  return new Promise((resolve) => {
    els.modalTitle.textContent = title;
    els.modalBody.textContent = body;
    els.modal.classList.remove("hidden");
    const done = (value) => {
      els.modal.classList.add("hidden");
      els.modalOk.onclick = null;
      els.modalCancel.onclick = null;
      resolve(value);
    };
    els.modalOk.onclick = () => done(true);
    els.modalCancel.onclick = () => done(false);
  });
}

async function saveConfig() {
  if (!state.loaded) return;
  const shouldRestart = state.running;
  setBusy(true);
  els.save.textContent = "保存中...";
  try {
    await api("config_set", { method: "POST", data: collectForm() });
    if (shouldRestart) {
      els.save.textContent = "重启中...";
      await api("stop");
      await api("start");
      showToast("已保存并重启热点");
    } else {
      showToast("已保存");
    }
    await loadAll();
  } finally {
    setBusy(false);
    els.save.textContent = "保存";
  }
}

async function toggleHotspot() {
  if (!state.loaded) return;
  setBusy(true);
  try {
    if (state.running) {
      await api("stop");
      showToast("热点已关闭");
    } else {
      await api("config_set", { method: "POST", data: collectForm() });
      const pre = await api("stpre");
      if (pre.abort) throw new Error(pre.error || "start aborted");
      if (Array.isArray(pre.warnings) && pre.warnings.length) {
        const ok = await confirmDialog("开启前确认", pre.warnings.join("\n"));
        if (!ok) return;
      }
      await api("start");
      showToast("热点已开启");
    }
    await loadAll();
  } finally {
    setBusy(false);
  }
}

/* ============================ 事件绑定 ============================ */
els.navStatus.addEventListener("click", () => switchPage("status"));
els.navClients.addEventListener("click", () => switchPage("clients"));
els.navSettings.addEventListener("click", () => switchPage("settings"));
els.togglePwdBtn.addEventListener("click", () => {
  state.showPassword = !state.showPassword;
  render();
});
els.toggleSettingsPwdBtn.addEventListener("click", () => {
  state.showSettingsPassword = !state.showSettingsPassword;
  els.settingsPwd.type = state.showSettingsPassword ? "text" : "password";
  els.toggleSettingsPwdBtn.classList.toggle(
    "eye-active",
    state.showSettingsPassword,
  );
});
els.save.addEventListener("click", () =>
  saveConfig().catch((error) => showToast(error.message, true)),
);
els.toggle.addEventListener("click", () =>
  toggleHotspot().catch((error) => showToast(error.message, true)),
);
document.addEventListener("click", (event) => {
  if (event.target === els.modal) {
    els.modal.classList.add("hidden");
    return;
  }
});
els.form.elements.band.addEventListener("change", () =>
  setOptions(els.channel, channelOptions(), ""),
);
els.portPolicySelect?.addEventListener("change", () => applyPortPolicyUI());
els.clients.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-kick]");
  if (!button) return;
  const mac = button.dataset.kick;
  const ok = await confirmDialog("确认下线", `确定要让客户端下线？\n${mac}`);
  if (!ok) return;
  await api(`kick?mac=${encodeURIComponent(mac)}`);
  showToast("已下线");
  await loadAll();
});

/* ============================ 初始化 ============================ */
setInterval(() => refreshLiveData(), 2000); // 2s 轮询,保证网速实时
setBusy(true);
loadAll().catch((error) => {
  state.loaded = false;
  setBusy(false);
  showToast(error.message, true);
});
