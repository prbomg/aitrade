window._uiLockUntil = 0;
document.addEventListener("mousedown", function(e) {
  if (e.target.closest("select, input, .dropdown, [data-dropdown], [onclick*=drawer], [onclick*=Menu], [onclick*=Panel]")) {
    window._uiLockUntil = Date.now() + 6000;
  }
}, true);
document.addEventListener("focusin", function(e) {
  if (e.target && (e.target.tagName === "SELECT" || e.target.tagName === "INPUT")) {
    window._uiLockUntil = Date.now() + 8000;
  }
}, true);

const API_BASE = "";
window._priceMap = {};
window._allLogs = [];
window._logFilter = "ALL";
window._soundEnabled = true;
window._prevAPlusCount = 0;
window._prevPosCount = 0;
window._confirmTimers = {};

function showToast(msg, isErr) {
  const el = document.getElementById("toast");
  el.style.background = isErr ? "#dc2626" : "#0284c7";
  el.textContent = msg;
  el.style.display = "block";
  setTimeout(function() { el.style.display = "none"; }, 4000);
}

// Звуковой синтезатор Web Audio API (без внешних файлов)
function playAlertTone(freq, durationMs) {
  if (!window._soundEnabled) return;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = freq || 880;
    gain.gain.setValueAtTime(0.08, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + (durationMs || 180) / 1000);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + (durationMs || 180) / 1000);
  } catch (e) {}
}

// Двухшаговое подтверждение опасных действий (Double-Tap Confirm без alert)
function requireDoubleTap(btnEl, key, confirmLabel, actionFn) {
  if (window._confirmTimers[key]) {
    clearTimeout(window._confirmTimers[key].timer);
    btnEl.textContent = window._confirmTimers[key].origText;
    btnEl.classList.remove("btn-armed");
    delete window._confirmTimers[key];
    actionFn();
    return;
  }
  const origText = btnEl.textContent;
  btnEl.textContent = confirmLabel;
  btnEl.classList.add("btn-armed");
  const timer = setTimeout(function() {
    btnEl.textContent = origText;
    btnEl.classList.remove("btn-armed");
    delete window._confirmTimers[key];
  }, 3500);
  window._confirmTimers[key] = {timer: timer, origText: origText};
}

window.addEventListener("unhandledrejection", function(event) { console.error(event.reason); });
async function apiPost(path, data) {
  try {
    const r = await fetch(API_BASE + path, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Requested-With": "AItrade"},
      body: JSON.stringify(data || {})
    });
    const result = await r.json();
    if (!r.ok || result.ok === false) throw new Error(result.retMsg || result.error || "Ошибка выполнения запроса");
    return result;
  } catch (e) {
    showToast("Ошибка связи с сервером: " + e, true);
    throw e;
  }
}

document.getElementById("btn-toggle-env").addEventListener("click", function(){document.getElementById("drawer-env").classList.toggle("open");});
document.getElementById("btn-close-env").addEventListener("click", function(){document.getElementById("drawer-env").classList.remove("open");});
document.getElementById("btn-switch-mainnet").disabled = true;
document.getElementById("btn-switch-mainnet").textContent = "Mainnet отключён до валидации";
document.getElementById("btn-save-env-keys").addEventListener("click", async function(){
 const r = await apiPost("/api/env_control",{action:"save_keys",target_mode:document.getElementById("env-key-target").value,api_key:document.getElementById("env-api-key").value,api_secret:document.getElementById("env-api-secret").value});
 document.getElementById("env-api-secret").value="";document.getElementById("env-api-key").value="";showToast(r.ok ? "Ключи сохранены" : r.retMsg,!r.ok);refreshState();
});
document.getElementById("btn-run-checklist").addEventListener("click", async function(){const r=await apiPost("/api/env_control",{action:"run_checklist",target_mode:document.getElementById("env-key-target").value});showToast(r.retMsg || "Проверка завершена",!r.ok);refreshState();});
document.getElementById("btn-switch-demo").addEventListener("click", async function(){const r=await apiPost("/api/env_control",{action:"switch_mode",target_mode:"demo"});showToast(r.retMsg || "Demo",!r.ok);refreshState();});
// Выдвижные панели (Drawers)
document.getElementById("btn-toggle-drawer").addEventListener("click", function() {
  document.getElementById("drawer-settings").classList.toggle("open");
  updateManualCalculator();
});
document.getElementById("btn-close-drawer").addEventListener("click", function() {
  document.getElementById("drawer-settings").classList.remove("open");
});
document.getElementById("btn-toggle-logs").addEventListener("click", function() {
  document.getElementById("drawer-logs").classList.toggle("open");
});
document.getElementById("btn-sound-toggle").addEventListener("click", function() {
  window._soundEnabled = !window._soundEnabled;
  this.textContent = window._soundEnabled ? "🔔 Звук: ВКЛ" : "🔕 Звук: ВЫКЛ";
  if (window._soundEnabled) playAlertTone(740, 140);
});

// Фильтрация логов
document.getElementById("log-filter-bar").addEventListener("click", function(e) {
  const chip = e.target.closest(".log-chip");
  if (!chip) return;
  document.querySelectorAll(".log-chip").forEach(function(c) { c.classList.remove("active"); });
  chip.classList.add("active");
  window._logFilter = chip.getAttribute("data-filter") || "ALL";
  renderLogs();
});

function renderLogs() {
  const box = document.getElementById("log-box");
  const flt = window._logFilter || "ALL";
  const filtered = (window._allLogs || []).filter(function(line) {
    if (flt === "TRADE") return line.indexOf("Открыт") !== -1 || line.indexOf("Booster") !== -1 || line.indexOf("Пирамидинг") !== -1 || line.indexOf("Закрыт") !== -1;
    if (flt === "GUARD") return line.indexOf("Smart Guard") !== -1 || line.indexOf("Безубыток") !== -1 || line.indexOf("TP1") !== -1 || line.indexOf("Trailing") !== -1;
    if (flt === "ALERT") return line.indexOf("🛑") !== -1 || line.indexOf("🚨") !== -1 || line.indexOf("ВКЛЮЧЕН") !== -1 || line.indexOf("ВЫКЛЮЧЕН") !== -1;
    return true;
  });
  box.innerHTML = filtered.length === 0
    ? "<div style=color:var(--muted)>Нет записей по выбранному фильтру.</div>"
    : filtered.map(function(l) { return "<div>" + escapeHtml(l) + "</div>"; }).join("");
}

// Живой калькулятор риска в $ для ручного ордера
function updateManualCalculator() {
  const sym = document.getElementById("manual-symbol").value || "BTCUSDT";
  const qty = Number(document.getElementById("manual-qty").value || 0.02);
  const tpPct = Number(document.getElementById("manual-tp-pct").value || 1.6) / 100.0;
  const slPct = Number(document.getElementById("manual-sl-pct").value || 0.8) / 100.0;
  const lev = Math.max(1, Number(document.getElementById("cfg-leverage").value || 3));
  const px = document.getElementById("manual-type").value === "Limit" ? Number(document.getElementById("manual-limit-price").value || 0) : Number(window._priceMap[sym] || 0);
  document.getElementById("calc-px").textContent = px > 0 ? ("$" + px) : "ожидание...";
  const rawNotional = px * qty;
  const effNotional = px > 0 ? rawNotional : 0;
  const margin = effNotional / lev;
  const riskUsd = effNotional * (slPct + 0.0054);
  const rewardUsd = effNotional * tpPct;
  document.getElementById("calc-notional").textContent = "$" + effNotional.toFixed(2) + (rawNotional < 24 && px > 0 ? " (ниже минимума $24)" : "");
  document.getElementById("calc-margin").textContent = "$" + margin.toFixed(2) + " (" + lev + "x)";
  document.getElementById("calc-risk").textContent = "-$" + riskUsd.toFixed(2);
  document.getElementById("calc-reward").textContent = "+$" + rewardUsd.toFixed(2);
}

["manual-symbol", "manual-type", "manual-limit-price", "manual-qty", "manual-tp-pct", "manual-sl-pct", "cfg-leverage"].forEach(function(id) {
  const el = document.getElementById(id);
  if (el) el.addEventListener("input", updateManualCalculator);
  if (el) el.addEventListener("change", updateManualCalculator);
});

// Переключение вкладок
document.getElementById("tabs-bar").addEventListener("click", function(e) {
  const btn = e.target.closest(".tab-btn");
  if (!btn) return;
  const tabId = btn.getAttribute("data-tab");
  if (!tabId) return;
  document.querySelectorAll(".tab-content").forEach(function(el) { el.classList.remove("active"); });
  document.querySelectorAll(".tab-btn").forEach(function(el) { el.classList.remove("active"); });
  const target = document.getElementById(tabId);
  if (target) target.classList.add("active");
  btn.classList.add("active");
});

// Управление стратегиями
async function sendStrategyControl(mode) {
  const iv = Number(document.getElementById("autopilot-interval").value || 300);
  const cMaxPos = Number(document.getElementById("core-max-pos").value || 3);
  const cMinProb = Number(document.getElementById("core-min-prob").value || 58);
  const res = await apiPost("/api/strategy_control", {
    mode: mode,
    interval_sec: iv,
    core_max_positions: cMaxPos,
    core_min_prob_pct: cMinProb
  });
  await refreshState();
  return res;
}

document.getElementById("btn-toggle-booster").addEventListener("click", async function() {
  const r = await sendStrategyControl("toggle_booster");
  playAlertTone(r.booster_enabled ? 880 : 440, 160);
  showToast(r.booster_enabled ? "🚀 Автопилот РАЗГОНА ($1k→$10k) ВКЛЮЧЕН!" : "⏸ Автопилот Разгона выключен.");
});

document.getElementById("btn-toggle-core").addEventListener("click", async function() {
  const r = await sendStrategyControl("toggle_core");
  playAlertTone(r.core_enabled ? 880 : 440, 160);
  showToast(r.core_enabled ? "🎯 Автопилот ОСНОВНОЙ ИИ-стратегии ВКЛЮЧЕН!" : "⏸ Автопилот Основной ИИ выключен.");
});

document.getElementById("btn-enable-both").addEventListener("click", async function() {
  await sendStrategyControl("enable_both");
  playAlertTone(960, 200);
  showToast("⚡ ОБА автопилота (Разгон + Основная ИИ) ВКЛЮЧЕНЫ!");
});

document.getElementById("btn-stop-all").addEventListener("click", async function() {
  await sendStrategyControl("stop_all");
  showToast("⏸ ВСЕ автопилоты остановлены.");
});

document.getElementById("btn-stop-all-top").addEventListener("click", async function() {
  await sendStrategyControl("stop_all");
  showToast("⏸ ВСЕ автопилоты остановлены.");
});

// Опасные кнопки с защитой Double-Tap Confirm
document.getElementById("btn-emergency-stop").addEventListener("click", function() {
  const btn = this;
  requireDoubleTap(btn, "emg_stop", "⚠️ ТОЧНО ОСТАНОВИТЬ И ЗАКРЫТЬ ВСЁ? (Нажмите еще раз)", async function() {
    showToast("🚨 Выполняется аварийная остановка и закрытие всех позиций...", true);
    await sendStrategyControl("emergency_stop");
    showToast("🚨 Все автопилоты выключены, все позиции и ордера закрыты!");
  });
});

document.getElementById("btn-reset-booster").addEventListener("click", function() {
  const btn = this;
  requireDoubleTap(btn, "rst_booster", "⚠️ Сбросить на $1,000 + снять все лимиты? (Нажмите еще раз)", async function() {
    await apiPost("/api/booster_config", {reset: true, start_equity: 1000.0, target_equity: 10000.0});
    await refreshState();
    showToast("🚀 Разгон перезапущен с $1,000.00! Все стоп-краны и кулдауны очищены.");
  });
});

const btnUnlockLim = document.getElementById("btn-unlock-limits");
if (btnUnlockLim) {
  btnUnlockLim.addEventListener("click", async function() {
    await apiPost("/api/booster_config", {unlock_limits: true});
    await refreshState();
    showToast("🔓 Дневной стоп-кран и 60м кулдауны сняты! Можно торговать дальше.");
  });
}

const btnTrainMode = document.getElementById("btn-training-mode");
if (btnTrainMode) {
  btnTrainMode.addEventListener("click", async function() {
    const nextState = !window._boosterTrainingMode;
    await apiPost("/api/booster_config", {training_mode: nextState});
    await refreshState();
    showToast(nextState ? "🧪 Режим обучения ИИ ВКЛЮЧЕН (торговля 24/7 без стоп-крана дня)" : "🛡 Включен стандартный защитный режим (Kill-Switch активен)");
  });
}

// Обработчики X/News и Adaptive ML Calibrator (исправление 4 кнопок)
const btnRefXnews = document.getElementById("btn-refresh-xnews");
if (btnRefXnews) {
  btnRefXnews.addEventListener("click", async function() {
    showToast("🐦 Обновление ленты X (Twitter) и новостей...");
    await apiPost("/api/x_news", {});
    await refreshState();
    showToast("✅ Радар X/News обновлён");
  });
}
const btnTogXnews = document.getElementById("btn-toggle-xnews");
if (btnTogXnews) {
  btnTogXnews.addEventListener("click", async function() {
    const nextEn = !(window._xNewsEnabled !== false);
    await apiPost("/api/x_news", {enabled: nextEn});
    await refreshState();
    showToast(nextEn ? "🐦 Фильтр X/News ВКЛЮЧЕН" : "⏸ Фильтр X/News ВЫКЛЮЧЕН");
  });
}
const btnRunMlCal = document.getElementById("btn-run-ml-cal");
if (btnRunMlCal) {
  btnRunMlCal.addEventListener("click", async function() {
    showToast("🧠 Пересчёт весов стратегий по Closed PnL...");
    await apiPost("/api/ml_calibrate", {});
    await refreshState();
    showToast("✅ Калибровка ML завершена");
  });
}
const btnTogMlCal = document.getElementById("btn-toggle-ml-cal");
if (btnTogMlCal) {
  btnTogMlCal.addEventListener("click", async function() {
    const nextEn = !(window._mlCalEnabled !== false);
    await apiPost("/api/ml_calibrate", {enabled: nextEn});
    await refreshState();
    showToast(nextEn ? "🧠 Автокалибровка ML ВКЛЮЧЕНА" : "⏸ Автокалибровка ML ВЫКЛЮЧЕНА");
  });
}

function bindCloseAllBtn(btnId, key) {
  const el = document.getElementById(btnId);
  if (!el) return;
  el.addEventListener("click", function() {
    requireDoubleTap(el, key, "⚠️ Подтвердите закрытие ВСЕХ позиций!", async function() {
      showToast("🚨 Закрываем все позиции по рынку...");
      await apiPost("/api/close_all_positions", {});
      await refreshState();
      showToast("✅ Все открытые позиции закрыты.");
    });
  });
}
bindCloseAllBtn("btn-close-all", "cls_all_1");
bindCloseAllBtn("btn-close-all-sticky", "cls_all_2");

document.getElementById("btn-cancel-all").addEventListener("click", function() {
  const btn = this;
  requireDoubleTap(btn, "cnl_all", "⚠️ Отменить все ордера? (Нажмите еще раз)", async function() {
    await apiPost("/api/cancel_all_orders", {});
    await refreshState();
    showToast("Запрос отмены отправлен. Проверьте список активных заявок.");
  });
});

document.getElementById("autopilot-interval").addEventListener("change", async function() {
  await sendStrategyControl("update_params");
  showToast("⏱ Интервал сканирования сохранен");
});

document.getElementById("btn-once-booster").addEventListener("click", async function() {
  showToast("🚀 Скан торговых пар + разовый вход по сетапу Разгона A+...");
  await apiPost("/api/scan", {trade_booster: true, trade_core: false});
  await refreshState();
  showToast("Скан принят в очередь. Ожидайте результат выполнения.");
});

document.getElementById("btn-once-core").addEventListener("click", async function() {
  showToast("🎯 Скан торговых пар + разовый вход по Основной ИИ-стратегии...");
  await apiPost("/api/scan", {trade_booster: false, trade_core: true});
  await refreshState();
  showToast("Скан принят в очередь. Ожидайте результат выполнения.");
});

document.getElementById("btn-scan-only").addEventListener("click", async function() {
  showToast("🔍 Обновляем скан торговых пар и OI Squeeze...");
  await apiPost("/api/scan", {trade_booster: false, trade_core: false});
  await refreshState();
  showToast("Скан поставлен в очередь. Таблицы обновятся после выполнения.");
});

document.getElementById("btn-refresh").addEventListener("click", function() {
  refreshState();
  showToast("🔄 Данные обновлены");
});

document.getElementById("btn-manual-buy").addEventListener("click", function() { submitManual("Buy"); });
document.getElementById("btn-manual-sell").addEventListener("click", function() { submitManual("Sell"); });

async function submitManual(side) {
  const symbol = document.getElementById("manual-symbol").value || "BTCUSDT";
  const order_type = document.getElementById("manual-type").value;
  const qty = Number(document.getElementById("manual-qty").value || 0.02);
  const tp_pct = Number(document.getElementById("manual-tp-pct").value || 1.6) / 100.0;
  const sl_pct = Number(document.getElementById("manual-sl-pct").value || 0.8) / 100.0;
  const leverage = Number(document.getElementById("cfg-leverage").value || 3);
  showToast("Отправка ордера " + symbol + " (" + side + ")...");
  const res = await apiPost("/api/place_manual", {symbol: symbol, side: side, order_type: order_type, qty: qty, price: order_type === "Limit" ? Number(document.getElementById("manual-limit-price").value) : 0, tp_pct: tp_pct, sl_pct: sl_pct, leverage: leverage});
  await refreshState();
  showToast(res.retMsg || "Заявка отправлена. Проверьте исполнение и защитные ордера.", !res.ok);
}

document.getElementById("btn-save-sg").addEventListener("click", async function() {
  await apiPost("/api/smart_guard", {
    enabled: document.getElementById("sg-enabled").value === "1",
    split_tp_enabled: document.getElementById("sg-split").value === "1",
    breakeven_trigger_pct: Number(document.getElementById("sg-be-pct").value || 0.55),
    trailing_step_pct: Number(document.getElementById("sg-trail-pct").value || 0.40),
    max_leverage: Number(document.getElementById("cfg-leverage").value || 3),
    core_max_positions: Number(document.getElementById("core-max-pos").value || 3),
    core_min_prob_pct: Number(document.getElementById("core-min-prob").value || 58),
    btc_guard_enabled: document.getElementById("rg-enabled").value === "1",
    max_same_sector: Number(document.getElementById("rg-max-sector").value || 1),
    max_same_direction_total: Number(document.getElementById("rg-max-dir").value || 2),
    time_stop_enabled: document.getElementById("ts-enabled").value === "1",
    time_stop_minutes: Number(document.getElementById("ts-minutes").value || 45),
    limit_chaser_enabled: document.getElementById("lc-mode").value !== "off",
    limit_chaser_strict: document.getElementById("lc-mode").value === "strict",
    sound_alerts_enabled: window._soundEnabled,
    manual_prefs: {
      symbol: document.getElementById("manual-symbol").value || "BTCUSDT",
      order_type: document.getElementById("manual-type").value || "Market",
      qty: Number(document.getElementById("manual-qty").value || 0.02),
      tp_pct: Number(document.getElementById("manual-tp-pct").value || 1.6),
      sl_pct: Number(document.getElementById("manual-sl-pct").value || 0.8)
    }
  });
  window._prefsHydrated = false;
  await refreshState();
  showToast("💾 Все параметры сохранены на сервер!");
});

document.body.addEventListener("click", async function(e) {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const act = btn.getAttribute("data-action");
  const sym = btn.getAttribute("data-symbol") || "BTCUSDT";
  const side = btn.getAttribute("data-side") || "Buy";
  const size = btn.getAttribute("data-size") || "0.02";
  const avg = Number(btn.getAttribute("data-avg") || 0);
  const mark = Number(btn.getAttribute("data-mark") || avg);
  const tp = Number(btn.getAttribute("data-tp") || 0);
  const sl = Number(btn.getAttribute("data-sl") || 0);
  const idx = Number(btn.getAttribute("data-idx") || 0);
  const oid = btn.getAttribute("data-oid") || "";

  showToast("Выполняется " + act + " для " + sym + "...");
  let res = {ok: false};
  if (act === "split_tp") {
    res = await apiPost("/api/pos_split_tp", {symbol: sym, side: side, size: Number(size), avg_price: avg, take_profit: tp, stop_loss: sl, position_idx: idx});
  } else if (act === "close_half") {
    res = await apiPost("/api/pos_close_half", {symbol: sym, side: side, size: size, position_idx: idx});
  } else if (act === "breakeven") {
    res = await apiPost("/api/pos_breakeven", {symbol: sym, side: side, avg_price: avg, position_idx: idx});
  } else if (act === "trailing") {
    res = await apiPost("/api/pos_trailing", {symbol: sym, mark_price: mark, position_idx: idx});
  } else if (act === "pyramid") {
    res = await apiPost("/api/pos_pyramid", {symbol: sym, side: side, size: Number(size), avg_price: avg, mark_price: mark, position_idx: idx});
  } else if (act === "close_full") {
    res = await apiPost("/api/close_position", {symbol: sym, side: side, size: size, position_idx: idx});
  } else if (act === "cancel_ord") {
    res = await apiPost("/api/cancel_order", {symbol: sym, order_id: oid});
  } else if (act === "exec_sig") {
    res = await apiPost("/api/place_order", {symbol: sym, side: side, order_type: "Market", qty: Number(size), price: avg, take_profit: tp, stop_loss: sl, leverage: 3});
  } else if (act === "booster_exec") {
    res = await apiPost("/api/booster_trade", {symbol: sym, side: side, entry_price: avg, take_profit: tp, stop_loss: sl});
  } else if (act === "ws_quick") {
    const qTp = side === "Buy" ? +(avg * 1.016).toPrecision(5) : +(avg * 0.984).toPrecision(5);
    const qSl = side === "Buy" ? +(avg * 0.992).toPrecision(5) : +(avg * 1.008).toPrecision(5);
    res = await apiPost("/api/booster_trade", {symbol: sym, side: side, entry_price: avg, take_profit: qTp, stop_loss: qSl});
  }
  await refreshState();
  showToast(res.ok ? ((res.status === "PENDING" ? "⏳ Ожидается подтверждение: " : "✅ Выполнено: ") + sym + (res.retMsg ? " — " + res.retMsg : "")) : ("⚠️ " + (res.retMsg || "Отказ Bybit")), !res.ok);
});

// Отрисовка SVG-графика кривой капитала разгона ($1k -> $10k)
function renderEquitySvg(curvePoints, floorUsd, hwmUsd) {
  const svg = document.getElementById("booster-svg-chart");
  if (!svg) return;
  const pts = (curvePoints && curvePoints.length >= 2) ? curvePoints : [1000, 1000];
  const minVal = Math.min.apply(null, pts.concat([floorUsd || 920])) * 0.992;
  const maxVal = Math.max.apply(null, pts.concat([hwmUsd || 1000, (floorUsd || 920) * 1.1])) * 1.008;
  const span = Math.max(1, maxVal - minVal);
  const W = 420, H = 95;
  const toY = function(v) { return Math.round(H - 10 - ((v - minVal) / span) * (H - 20)); };
  const stepX = (W - 20) / Math.max(1, pts.length - 1);
  const polyPts = pts.map(function(v, idx) { return Math.round(10 + idx * stepX) + "," + toY(v); }).join(" ");
  const floorY = toY(floorUsd || 920);
  const hwmY = toY(hwmUsd || 1000);
  document.getElementById("chart-min-max-lbl").textContent = "Дно: $" + Number(floorUsd || 920).toFixed(0) + " | Текущий: $" + Number(pts[pts.length - 1]).toFixed(2);
  svg.innerHTML =
    "<line x1=10 y1=" + floorY + " x2=410 y2=" + floorY + " stroke=#ef4444 stroke-dasharray=4,4 stroke-width=1.3 />" +
    "<line x1=10 y1=" + hwmY + " x2=410 y2=" + hwmY + " stroke=#10b981 stroke-dasharray=3,3 stroke-width=1 />" +
    "<polyline fill=none stroke=#38bdf8 stroke-width=2.5 points=\"" + polyPts + "\" />";
}

function buildPositionsRows(posList) {
  return posList.map(function(p) {
    const u = Number(p.unrealisedPnl || 0);
    const pIdx = Number(p.positionIdx || 0);
    const srcTag = p.source_tag || "🎯 Системный";
    const srcCls = srcTag.indexOf("Booster") !== -1 ? "badge-purple" : (srcTag.indexOf("Ручной") !== -1 ? "badge-amber" : "badge-blue");
    const tp1B = p.tp1_order_price ? ("<span class=\"badge badge-green\">" + p.tp1_order_qty + " @ " + p.tp1_order_price + "</span>") : "<span class=\"badge badge-amber\">Ожидает</span>";
    const stB = (p.is_breakeven ? "<span class=\"badge badge-green\">🛡 БУ</span> " : "") + (p.has_trailing ? ("<span class=\"badge badge-blue\">📈 Trail " + p.trailingStop + "</span>") : "<span class=\"badge badge-amber\">Активна</span>");
    const tsLim = (window._rgCfg && window._rgCfg.time_stop_minutes) ? window._rgCfg.time_stop_minutes : 45;
    const ageM = Number(p.age_min || 0).toFixed(0);
    const secLbl = p.sector ? (" <span style=color:var(--muted);font-size:10px>[" + p.sector + "]</span>") : "";
    const timerBadge = p.is_breakeven ? ("<span class=\"badge badge-green\">" + ageM + "м (в БУ)</span>") : ("<span class=\"badge badge-blue\">⏱ " + ageM + " / " + tsLim + "м</span>");
    return "<tr><td><b>" + p.symbol + "</b> (" + p.leverage + "x)</td><td><span class=\"badge " + srcCls + "\">" + srcTag + "</span>" + secLbl + "</td><td class=" + (p.side === "Buy" ? "pos-green" : "pos-red") + ">" + p.side + "</td><td><b>" + p.size + "</b></td><td>" + p.avgPrice + "</td><td>" + p.markPrice + "</td><td class=" + (u >= 0 ? "pos-green" : "pos-red") + ">" + (u >= 0 ? "+" : "") + u.toFixed(2) + "$</td><td>" + timerBadge + "</td><td>" + tp1B + "</td><td>" + (p.takeProfit || "—") + "</td><td>" + (p.stopLoss || "—") + "</td><td>" + stB + "</td><td style=display:flex;gap:4px;flex-wrap:wrap>" +
      "<button type=button class=\"btn-purple btn-sm\" data-action=split_tp data-symbol=" + p.symbol + " data-side=" + p.side + " data-size=" + p.size + " data-avg=" + p.avgPrice + " data-tp=" + (p.takeProfit || 0) + " data-sl=" + (p.stopLoss || 0) + " data-idx=" + pIdx + ">🎯 TP1 50%</button>" +
      "<button type=button class=\"btn-success btn-sm\" data-action=breakeven data-symbol=" + p.symbol + " data-side=" + p.side + " data-avg=" + p.avgPrice + " data-idx=" + pIdx + ">🛡 В БУ</button>" +
      "<button type=button class=\"btn-primary btn-sm\" data-action=pyramid data-symbol=" + p.symbol + " data-side=" + p.side + " data-size=" + p.size + " data-avg=" + p.avgPrice + " data-mark=" + p.markPrice + " data-idx=" + pIdx + ">➕ Доливка 50%</button>" +
      "<button type=button class=\"btn-primary btn-sm\" data-action=trailing data-symbol=" + p.symbol + " data-mark=" + p.markPrice + " data-idx=" + pIdx + ">📈 Trail</button>" +
      "<button type=button class=\"btn-warning btn-sm\" data-action=close_half data-symbol=" + p.symbol + " data-side=" + p.side + " data-size=" + p.size + " data-idx=" + pIdx + ">✂️ 50%</button>" +
      "<button type=button class=\"btn-danger btn-sm\" data-action=close_full data-symbol=" + p.symbol + " data-side=" + p.side + " data-size=" + p.size + " data-idx=" + pIdx + ">✖</button></td></tr>";
  }).join("");
}

let refreshInFlight = false;
async function refreshState() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  const controller = new AbortController();
  const timer = setTimeout(function(){controller.abort();}, 10000);
  try {
    const r = await fetch(API_BASE + "/api/state", {signal:controller.signal});
    if (!r.ok) throw new Error("HTTP " + r.status);
    const st = await r.json();
    renderState(st);
    const h = st.health || {}, scan = h.scan || {};
    const stale = h.snapshot_age_sec == null || h.snapshot_age_sec > 30 || h.snapshot_error;
    let message = stale ? "Данные устарели: " + (h.snapshot_error || "нет свежего снимка") : "Demo · данные обновлены";
    if (scan.last_error) message += " · Ошибка скана: " + scan.last_error;
    else message += scan.running ? " · выполняется скан" : " · скан завершён";
    if (!h.autopilot_alive) message += " · поток автопилота остановлен";
    if (h.entry_paused) message += " · новые автоматические входы на паузе";
    if ((st.booster || {}).circuit_breaker_tripped) message += " · лимит риска достигнут, новые входы запрещены";
    document.getElementById("runtime-status").textContent = message;
    if (!h.autopilot_alive || scan.last_error) document.getElementById("master-status-badge").textContent = "Ошибка автопилота — см. статус";
  } catch (e) {
    document.getElementById("runtime-status").textContent = "Нет связи с сервисом: " + e.message;
  } finally {clearTimeout(timer);refreshInFlight=false;}
}

function escapeHtml(value) { return String(value == null ? "" : value).replace(/[&<>"']/g, function(c) { return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); }
function renderState(st) {
  document.getElementById("scan-time-label").textContent = "Скан: " + (st.last_scan_ts || "—");
  const envSt = st.env_status || {};
  const isMain = Boolean(envSt.is_mainnet);
  const connB = document.getElementById("conn-badge");
  if (connB) {
    connB.className = isMain ? "badge badge-red btn-armed" : "badge badge-green";
    connB.textContent = isMain ? "💰 BYBIT REAL MAINNET (БОЕВОЙ СЧЕТ)" : "🧪 Bybit Demo LIVE";
  }
  const envModeB = document.getElementById("env-active-mode-badge");
  if (envModeB) {
    envModeB.className = isMain ? "badge badge-red" : "badge badge-green";
    envModeB.textContent = isMain ? "Активен: 💰 REAL MAINNET (api.bybit.com)" : "Активен: 🧪 BYBIT DEMO (api-demo.bybit.com)";
  }
  if (document.getElementById("env-demo-masked")) {
    document.getElementById("env-demo-masked").textContent = envSt.demo_key_masked || "—";
    document.getElementById("env-main-masked").textContent = envSt.mainnet_key_masked || "Не задан";
    const chk = envSt.last_checklist || {};
    document.getElementById("env-chk-time").textContent = "Проверка: " + (chk.checked_at || "—") + " [" + (chk.mode || "mainnet").toUpperCase() + "]";
    const sumEl = document.getElementById("env-chk-summary");
    sumEl.textContent = chk.summary || "Чек-лист еще не запускался";
    sumEl.style.color = chk.passed ? "#34d399" : "#fbbf24";
    const cListEl = document.getElementById("env-chk-list");
    cListEl.innerHTML = (chk.checks || []).map(function(c) {
      const ic = c.ok ? "✅" : "❌";
      const clr = c.ok ? "#e2e8f0" : "#f87171";
      return "<div style=\"padding:4px 7px;background:#111827;border-radius:5px;border-left:3px solid " + (c.ok ? "#10b981" : "#ef4444") + "\">" +
        "<div style=\"font-weight:600;color:" + clr + "\">" + ic + " " + escapeHtml(c.title) + "</div>" +
        "<div style=\"color:var(--muted);font-size:10.5px\">" + escapeHtml(c.detail) + "</div></div>";
    }).join("");
  }
  const acct = st.account || {};
  document.getElementById("kpi-usdt").textContent = "$" + Number(acct.usdt_balance || 0).toLocaleString("en-US", {minimumFractionDigits: 2});
  document.getElementById("kpi-equity").textContent = "Эквити: $" + Number(acct.total_equity || 0).toLocaleString("en-US", {minimumFractionDigits: 2});

  const posList = st.positions || [];
  const ordList = st.open_orders || [];
  document.getElementById("kpi-pos-orders").textContent = posList.length + " поз. / " + ordList.length + " орд.";

  const upnl = Number(acct.unrealised_pnl || 0);
  const uEl = document.getElementById("kpi-upnl");
  uEl.textContent = (upnl >= 0 ? "+$" : "-$") + Math.abs(upnl).toFixed(2);
  uEl.className = "kpi-mini-val " + (upnl >= 0 ? "pos-green" : "pos-red");

  const rpnl = Number(st.realized_pnl_recent || 0);
  const rEl = document.getElementById("kpi-rpnl");
  rEl.textContent = (rpnl >= 0 ? "+$" : "-$") + Math.abs(rpnl).toFixed(2);
  rEl.className = "kpi-mini-val " + (rpnl >= 0 ? "pos-green" : "pos-red");
  document.getElementById("kpi-winrate").textContent = "Win Rate: " + (st.live_win_rate_pct || 0) + "% (" + (st.closed_trades_count || 0) + " сд.)";

  // Обновляем карту цен для живого калькулятора
  const ws = st.ws || {};
  (ws.metrics || []).forEach(function(m) {
    if (m.mid_price > 0) window._priceMap[m.symbol] = m.mid_price;
  });
  const sc = st.scan || {};
  (sc.screener_top || []).forEach(function(r) {
    if (r.last_price > 0) window._priceMap[r.symbol] = r.last_price;
  });
  updateManualCalculator();

  // Отрисовка Deposit Booster ($1k -> $10k) + SVG Curve
  const task=st.manual_task || {};
  if (task.id && window._taskShown !== task.id && (task.status === 'completed' || task.status === 'failed')) {
    window._taskShown=task.id;
    showToast(task.status==='failed' ? 'Ошибка сканирования: '+task.error : 'Сканирование завершено. Результаты обновлены.', task.status==='failed');
  }
  const b = st.booster || {};
  window._boosterTrainingMode = Boolean(b.training_unlimited_mode);
  const trBtn = document.getElementById("btn-training-mode");
  if (trBtn) {
    trBtn.textContent = window._boosterTrainingMode
      ? "🧪 Режим обучения (Без лимита): ВКЛ"
      : "🛡 Режим обучения (Без лимита): ВЫКЛ";
    trBtn.className = window._boosterTrainingMode ? "btn-warning btn-sm" : "btn-purple btn-sm";
  }
  const bLive = Number(b.live_equity || 1000);
  const bVault = Number(b.vault_usd || 0);
  const subAcc = (envSt.subaccounts || {});
  const bAcct = st.booster_account || null;
  const isoLbl = (subAcc.booster && subAcc.booster.isolated) ? ("Изолирован" + (bAcct ? (" · $" + Number(bAcct.usdt_balance || 0).toFixed(0)) : "")) : "Общий Demo";
  document.getElementById("kpi-booster-eq").textContent = "$" + bLive.toLocaleString("en-US", {minimumFractionDigits: 2}) + (bVault > 0 ? (" + $" + bVault.toFixed(0) + " сейф") : "");
  document.getElementById("kpi-booster-floor").textContent = "Порог: $" + Number(b.floor_usd || 920).toFixed(2) + " · Субаккаунт: " + isoLbl;
  document.getElementById("booster-phase-title").textContent = "🚀 " + (b.phase_name || "Фаза 1") + " — Капитал: $" + bLive.toFixed(2) + " | Сейф: $" + bVault.toFixed(2) + " → Цель: $10,000";
  document.getElementById("booster-progress-bar").style.width = Math.max(4, Number(b.progress_pct || 0)) + "%";
  document.getElementById("booster-progress-text").textContent = "Прогресс: $" + (bLive + bVault).toFixed(2) + " из $10,000 (" + (b.progress_pct || 0) + "% | Рост " + (b.growth_multiple || 1) + "x | Сейф: $" + bVault.toFixed(2) + ", след. шаг: $" + Number(b.next_ratchet_equity || 1500).toFixed(0) + ")";
  document.getElementById("b-hwm").textContent = "$" + Number(b.high_water_mark || 1000).toFixed(2);
  document.getElementById("b-floor-status").textContent = b.near_floor_protection ? "⚠️ Демпфер DD активен ($" + b.floor_usd + ")" : ("Порог капитала: $" + Number(b.floor_usd || 920).toFixed(2));
  document.getElementById("b-risk-usd").textContent = "$" + Number(b.effective_risk_usd ?? 0).toFixed(2) + " (" + (b.effective_risk_pct ?? 0) + "%)";
  document.getElementById("b-house-bonus").textContent = "🔐 Сейф-храповик: $" + bVault.toFixed(2) + " (DD: " + Number(b.dd_from_hwm_pct || 0).toFixed(1) + "%)";
  const dPnl = Number(b.realized_today_usd || 0);
  const dEl = document.getElementById("b-daily-pnl");
  dEl.textContent = (dPnl >= 0 ? "+$" : "-$") + Math.abs(dPnl).toFixed(2);
  dEl.className = "kpi-mini-val " + (dPnl >= 0 ? "pos-green" : "pos-red");
  document.getElementById("b-daily-limit").textContent = "Стоп-кран дня: -$" + Number(b.daily_loss_limit_usd || 30).toFixed(2);
  const cbB = document.getElementById("booster-cb-badge");
  cbB.className = b.circuit_breaker_tripped ? "badge badge-red" : "badge badge-green";
  cbB.textContent = b.circuit_breaker_tripped ? "🛑 Дневной стоп-кран СРАБОТАЛ (Пауза)" : "🛡 Дневной стоп-кран: В НОРМЕ";
  const wfaVal = st.wfa_validation || {};
  const wfaBody = document.getElementById("wfa-validation-tbody");
  if (wfaBody) {
    const wfaUpd = document.getElementById("wfa-val-updated");
    if (wfaUpd) wfaUpd.textContent = "Обновлено: " + (wfaVal.updated_at ? String(wfaVal.updated_at).slice(0, 19).replace("T", " ") + " UTC" : "—");
    const stratMap = wfaVal.strategies || {};
    const keys = Object.keys(stratMap);
    wfaBody.innerHTML = keys.length === 0
      ? "<tr><td colspan=7 style=color:var(--muted)>Артефакт wfa_validation.json не найден (fail-closed: новые входы закрыты)</td></tr>"
      : keys.map(function(k) {
          const e = stratMap[k] || {};
          const ok = Boolean(e.wfe_passed);
          const r1 = Number(e.oos_avg_r || 0);
          const r2 = Number(e.oos_2x_avg_r || 0);
          return "<tr><td><b>" + escapeHtml(k) + "</b></td><td><span class=\"badge " + (ok ? "badge-green" : "badge-red") + "\">" + (ok ? "✅ PASSED (В БОЮ)" : "⛔ BLOCKED") + "</span></td><td>" + (e.oos_trades ?? "—") + "</td><td><b>" + (e.oos_pf == null ? "—" : Number(e.oos_pf).toFixed(2)) + "</b></td><td class=" + (r1 > 0 ? "pos-green" : "pos-red") + ">" + (r1 >= 0 ? "+" : "") + r1.toFixed(3) + "R</td><td class=" + (r2 > 0 ? "pos-green" : "pos-red") + ">" + (r2 >= 0 ? "+" : "") + r2.toFixed(3) + "R</td><td>" + (e.wfe == null ? "—" : Number(e.wfe).toFixed(2)) + "</td></tr>";
        }).join("");
  }
  const fnl = st.strategy_funnel_24h || {};
  const fMap = fnl.strategies || {};
  const fnlBody = document.getElementById("strategy-funnel-tbody");
  if (fnlBody) {
    const fnlUpd = document.getElementById("funnel-24h-updated");
    if (fnlUpd) fnlUpd.textContent = "Окно 24ч · обновлено: " + (fnl.updated_at || "—");
    const fKeys = Object.keys(fMap);
    fnlBody.innerHTML = fKeys.length === 0
      ? "<tr><td colspan=8 style=color:var(--muted)>Ожидание первого скана...</td></tr>"
      : fKeys.map(function(k) {
          const r = fMap[k] || {};
          return "<tr><td><b>" + escapeHtml(k) + "</b></td><td>" + (r.scans_eligible ?? 0) + " / " + (r.scans_total ?? 0) + "</td><td>" + (r.candidates ?? 0) + "</td><td>" + (r.blocked_wfa_ml ?? 0) + "</td><td>" + (r.blocked_guard_exchange ?? 0) + "</td><td>" + (r.submitted ?? 0) + "</td><td><b>" + (r.filled_24h ?? 0) + "</b></td><td style=white-space:normal;max-width:420px;color:var(--muted)>" + escapeHtml(r.last_idle_reason || "—") + "</td></tr>";
        }).join("");
  }
  const lsm = st.live_strategy_metrics || {};
  const smMap = lsm.strategies || {};
  const lsmBody = document.getElementById("live-strat-metrics-tbody");
  if (lsmBody) {
    const leg = lsm.legacy_isolated || {};
    const legLbl = document.getElementById("legacy-isolated-lbl");
    if (legLbl) legLbl.textContent = "Legacy изолировано: " + (leg.trades ?? 0) + " сд. (" + (Number(leg.net_pnl_usd || 0) >= 0 ? "+" : "") + Number(leg.net_pnl_usd || 0).toFixed(2) + " USDT)";
    const smKeys = Object.keys(smMap);
    lsmBody.innerHTML = smKeys.length === 0
      ? "<tr><td colspan=8 style=color:var(--muted)>Нет данных раздельного учёта</td></tr>"
      : smKeys.map(function(k) {
          const m = smMap[k] || {};
          const np = Number(m.net_pnl_usd || 0);
          return "<tr><td><b>" + escapeHtml(k) + "</b><div style=font-size:10.5px;color:var(--muted)>" + escapeHtml(m.horizon_label || "") + "</div></td><td>" + (m.trades ?? 0) + " (" + (m.open_trades ?? 0) + " откр.)</td><td class=" + (np >= 0 ? "pos-green" : "pos-red") + ">" + (np >= 0 ? "+" : "") + np.toFixed(2) + " USDT</td><td>" + Number(m.max_drawdown_usd || 0).toFixed(2) + " USDT</td><td>" + (m.avg_hold_hours == null ? "—" : Number(m.avg_hold_hours).toFixed(1) + " ч") + "</td><td>" + (m.avg_realized_r == null ? "—" : (Number(m.avg_realized_r) >= 0 ? "+" : "") + Number(m.avg_realized_r).toFixed(2) + "R") + "</td><td>" + (m.avg_slippage_bps == null ? "—" : Number(m.avg_slippage_bps).toFixed(1) + " bps") + "</td><td>" + Number(m.total_fees_usd || 0).toFixed(3) + " USDT</td></tr>";
        }).join("");
  }
  const shLab = st.shadow_lab || {};
  const shBody = document.getElementById("shadow-lab-tbody");
  if (shBody) {
    const shLbl = document.getElementById("shadow-lab-summary-lbl");
    if (shLbl) shLbl.textContent = "Открыто: " + (shLab.total_open ?? 0) + " · Закрыто: " + (shLab.total_closed ?? 0) + " · Риск: $" + (shLab.risk_per_trade_usd ?? 15) + "/сд.";
    const shList = shLab.leaderboard || [];
    shBody.innerHTML = shList.length === 0
      ? "<tr><td colspan=9 style=color:var(--muted)>Ожидание первых сигналов в Live Shadow...</td></tr>"
      : shList.map(function(r) {
          const np = Number(r.net_pnl_usd || 0);
          const up = Number(r.unrealized_pnl_usd || 0);
          const eq = Number(r.pod_equity_usd || 1000);
          return "<tr><td><b>" + escapeHtml(r.strategy_id) + "</b></td><td class=" + (eq >= 1000 ? "pos-green" : "pos-red") + "><b>$" + eq.toFixed(2) + "</b></td><td>" + (r.open_trades ?? 0) + " / " + (r.closed_trades ?? 0) + "</td><td>" + Number(r.win_rate_pct || 0).toFixed(1) + "%</td><td><b>" + (r.profit_factor == null ? "—" : Number(r.profit_factor).toFixed(2)) + "</b></td><td class=" + (np >= 0 ? "pos-green" : "pos-red") + ">" + (np >= 0 ? "+" : "") + np.toFixed(2) + "$</td><td class=" + (up >= 0 ? "pos-green" : "pos-red") + ">" + (up >= 0 ? "+" : "") + up.toFixed(2) + "$</td><td>" + (r.avg_r == null ? "—" : (Number(r.avg_r) >= 0 ? "+" : "") + Number(r.avg_r).toFixed(2) + "R") + "</td><td>" + Number(r.max_dd_usd || 0).toFixed(2) + "$</td></tr>";
        }).join("");
  }
  // Живая сводка «📍 Что происходит сейчас» над карточками
  const hNow = st.health || {};
  const svcB = document.getElementById("stage-service-badge");
  if (svcB) {
    const isScanRun = Boolean((hNow.scan || {}).running);
    if (!hNow.ok) {
      svcB.className = "badge badge-amber";
      svcB.textContent = isScanRun ? "⏳ Прогрев / Первый скан..." : "⚠️ Требует внимания";
    } else if (hNow.entry_paused) {
      svcB.className = "badge badge-amber";
      svcB.textContent = "⏸ Входы на паузе";
    } else if (isScanRun) {
      svcB.className = "badge badge-blue";
      svcB.textContent = "🔍 Сканирует рынок...";
    } else {
      svcB.className = "badge badge-green";
      svcB.textContent = "🟢 Работает штатно";
    }
  }
  const verB = document.getElementById("stage-version-badge");
  if (verB && hNow.version) verB.textContent = "v" + hNow.version;
  const next4hEl = document.getElementById("stage-next-4h-timer");
  if (next4hEl) {
    const nowU = new Date();
    const utcMinOfDay = nowU.getUTCHours() * 60 + nowU.getUTCMinutes();
    const minSince4h = utcMinOfDay % 240;
    if (minSince4h <= 30) {
      next4hEl.textContent = "🟢 Сейчас активно 30-мин окно 4h T1 (осталось " + (30 - minSince4h) + " мин)";
      next4hEl.style.color = "#34d399";
    } else {
      const leftMin = 240 - minSince4h;
      next4hEl.textContent = "⏳ До след. 4h-окна T1: " + Math.floor(leftMin / 60) + "ч " + (leftMin % 60) + "м";
      next4hEl.style.color = "#38bdf8";
    }
  }
  const t1F = fMap.T1_DONCHIAN_TSMOM || {};
  const t1M = smMap.T1_DONCHIAN_TSMOM || {};
  if (document.getElementById("stage-t1-reason")) {
    const t1Reason = t1F.last_idle_reason || "Ожидание первого скана";
    document.getElementById("stage-t1-reason").textContent = t1Reason;
    const t1B = document.getElementById("stage-t1-badge");
    if (t1B) {
      if ((t1M.open_trades || 0) > 0) { t1B.className = "badge badge-green"; t1B.textContent = "🟢 В позиции (" + t1M.open_trades + ")"; }
      else if (t1Reason.indexOf("Вне 30-мин") !== -1) { t1B.className = "badge badge-blue"; t1B.textContent = "⏳ Ждёт 4h-окно"; }
      else if (t1Reason.indexOf("Guard") !== -1 || t1Reason.indexOf("Отклон") !== -1) { t1B.className = "badge badge-amber"; t1B.textContent = "🛡 Фильтр"; }
      else { t1B.className = "badge badge-green"; t1B.textContent = "🔍 Ищет пробой"; }
    }
    document.getElementById("stage-t1-stats").textContent = "24ч: окон " + (t1F.scans_eligible ?? 0) + "/" + (t1F.scans_total ?? 0) + " · кандидатов " + (t1F.candidates ?? 0) + " · отправлено " + (t1F.submitted ?? 0) + " · чистых сделок T1: " + (t1M.trades ?? 0) + " (" + (Number(t1M.net_pnl_usd || 0) >= 0 ? "+" : "") + Number(t1M.net_pnl_usd || 0).toFixed(2) + "$)";
  }
  const b1F = fMap.B1_SQUEEZE_BREAKOUT || {};
  const b1M = smMap.B1_SQUEEZE_BREAKOUT || {};
  if (document.getElementById("stage-b1-reason")) {
    const b1Reason = b1F.last_idle_reason || "Ожидание первого скана";
    document.getElementById("stage-b1-reason").textContent = b1Reason;
    const b1B = document.getElementById("stage-b1-badge");
    if (b1B) {
      if ((b1M.open_trades || 0) > 0) { b1B.className = "badge badge-green"; b1B.textContent = "🟢 В позиции (" + b1M.open_trades + ")"; }
      else if (b1Reason.indexOf("NoWeekendChop") !== -1) { b1B.className = "badge badge-amber"; b1B.textContent = "🌙 Пауза выходных"; }
      else if (b1Reason.indexOf("Guard") !== -1 || b1Reason.indexOf("Отклон") !== -1) { b1B.className = "badge badge-amber"; b1B.textContent = "🛡 Фильтр"; }
      else { b1B.className = "badge badge-purple"; b1B.textContent = "🔍 Ищет сжатие"; }
    }
    document.getElementById("stage-b1-stats").textContent = "24ч: окон " + (b1F.scans_eligible ?? 0) + "/" + (b1F.scans_total ?? 0) + " · кандидатов " + (b1F.candidates ?? 0) + " · отправлено " + (b1F.submitted ?? 0) + " · чистых сделок B1: " + (b1M.trades ?? 0) + " (" + (Number(b1M.net_pnl_usd || 0) >= 0 ? "+" : "") + Number(b1M.net_pnl_usd || 0).toFixed(2) + "$)";
  }
  const shBadge = document.getElementById("stage-shadow-badge");
  const shReason = document.getElementById("stage-shadow-reason");
  const shStats = document.getElementById("stage-shadow-stats");
  if (shBadge && shReason && shStats) {
    const shOpen = Number(shLab.total_open || 0);
    const shClosed = Number(shLab.total_closed || 0);
    const shBoard = shLab.leaderboard || [];
    shBadge.className = shOpen > 0 ? "badge badge-green" : "badge badge-purple";
    shBadge.textContent = shOpen + " откр. / " + shClosed + " закр.";
    if (shBoard.length > 0) {
      const top = shBoard[0];
      const topPnl = Number(top.net_pnl_usd || 0);
      shReason.textContent = "Лидер: " + top.strategy_id + " (" + (topPnl >= 0 ? "+" : "") + topPnl.toFixed(2) + "$ · WR " + Number(top.win_rate_pct || 0).toFixed(0) + "%)";
    } else {
      shReason.textContent = "Ожидание первых сигналов Shadow Lab...";
    }
    shStats.textContent = "Стратегий в рейтинге: " + shBoard.length + " · Риск: $" + (shLab.risk_per_trade_usd ?? 15) + "/сд. · Без конфликта тикеров";
  }
  const wfa = (st.backtest || {}).rolling_wfa || {};
  if (wfa.status === "ok" && document.getElementById("ml-base-thr")) {
    const wfeStr = wfa.wfe == null ? "—" : Number(wfa.wfe).toFixed(2);
    const dsrStr = (wfa.oos_summary && wfa.oos_summary.deflated_sharpe_ratio != null) ? Number(wfa.oos_summary.deflated_sharpe_ratio).toFixed(2) : "—";
    const s2x = (wfa.oos_stress_2x && wfa.oos_stress_2x.avg_r_multiple != null) ? Number(wfa.oos_stress_2x.avg_r_multiple).toFixed(3) : "—";
    document.getElementById("ml-base-thr").title = "Rolling WFA: WFE=" + wfeStr + " | DSR=" + dsrStr + " | 2x Stress E[R]=" + s2x;
  }
  renderEquitySvg(b.equity_curve || [1000, bLive], b.floor_usd || 920, b.high_water_mark || 1000);

  // Пульт Стратегий
  const ap = st.autopilot || {};
  const bOn = Boolean(ap.booster_enabled);
  const cOn = Boolean(ap.core_enabled);
  document.getElementById("deck-b-eq").textContent = "$" + bLive.toFixed(2);
  document.getElementById("deck-b-phase").textContent = "Фаза " + (b.phase_num || 1) + " (дно $" + Number(b.floor_usd || 920).toFixed(0) + ")";
  document.getElementById("deck-b-risk").textContent = "$" + Number(b.effective_risk_usd ?? 0).toFixed(2) + " (" + (b.effective_risk_pct ?? 0) + "%)";
  document.getElementById("deck-b-cycles").textContent = String(ap.booster_cycles_done || 0);

  document.getElementById("card-booster").className = bOn ? "strat-card active-booster" : "strat-card";
  const badgeB = document.getElementById("badge-booster-state");
  badgeB.className = bOn ? "badge badge-green" : "badge badge-amber";
  badgeB.textContent = bOn ? "ВКЛЮЧЕН" : "ВЫКЛЮЧЕН";
  const btnB = document.getElementById("btn-toggle-booster");
  btnB.className = bOn ? "btn-big-toggle btn-success" : "btn-big-toggle btn-purple";
  btnB.textContent = bOn ? "🟢 АВТОПИЛОТ РАЗГОНА: АКТИВЕН В ФОНЕ" : "🚀 ВКЛЮЧИТЬ АВТОПИЛОТ РАЗГОНА";

  const cfg = st.settings || {};
  document.getElementById("deck-c-lev").textContent = (cfg.max_leverage || 3) + "x (Pure SL/TP · Vol)";
  document.getElementById("deck-c-cycles").textContent = String(ap.core_cycles_done || 0);
  document.getElementById("card-core").className = cOn ? "strat-card active-core" : "strat-card";
  const badgeC = document.getElementById("badge-core-state");
  badgeC.className = cOn ? "badge badge-green" : "badge badge-amber";
  badgeC.textContent = cOn ? "ВКЛЮЧЕН" : "ВЫКЛЮЧЕН";
  const btnC = document.getElementById("btn-toggle-core");
  btnC.className = cOn ? "btn-big-toggle btn-success" : "btn-big-toggle btn-primary";
  btnC.textContent = cOn ? "🟢 ОСНОВНОЙ АВТОПИЛОТ: АКТИВЕН В ФОНЕ" : "🎯 ВКЛЮЧИТЬ ОСНОВНОЙ АВТОПИЛОТ";

  const masterBadge = document.getElementById("master-status-badge");
  const summaryEl = document.getElementById("deck-active-summary");
  if (bOn && cOn) {
    masterBadge.className = "badge badge-green";
    masterBadge.textContent = "🤖 Активны ОБА: Разгон + Основная ИИ";
    summaryEl.style.color = "#34d399";
    summaryEl.textContent = "Работают ОБА: 🚀 Разгон + 🎯 Основная ИИ";
  } else if (bOn) {
    masterBadge.className = "badge badge-purple";
    masterBadge.textContent = "🚀 Активен Автопилот Разгона";
    summaryEl.style.color = "#c4b5fd";
    summaryEl.textContent = "Работает только 🚀 Разгон ($1k→$10k)";
  } else if (cOn) {
    masterBadge.className = "badge badge-blue";
    masterBadge.textContent = "🎯 Активен Основной Автопилот";
    summaryEl.style.color = "#38bdf8";
    summaryEl.textContent = "Работает только 🎯 Основная ИИ";
  } else {
    masterBadge.className = "badge badge-amber";
    masterBadge.textContent = "⏸ Автопилоты: ОСТАНОВЛЕНЫ";
    summaryEl.style.color = "#fbbf24";
    summaryEl.textContent = "Все автопилоты на паузе";
  }
  document.getElementById("deck-next-timer").textContent = (bOn || cOn) ? ((ap.next_in_sec || 0) + " сек") : "На паузе";
  const ivSel = document.getElementById("autopilot-interval");
  if (document.activeElement !== ivSel && ap.interval_sec) ivSel.value = String(ap.interval_sec);

  // Постоянный баннер активных позиций + счетчики вкладок
  const stickyBanner = document.getElementById("sticky-pos-banner");
  if (posList.length > 0) {
    stickyBanner.classList.add("visible");
    document.getElementById("sticky-positions-tbody").innerHTML = buildPositionsRows(posList);
  } else {
    stickyBanner.classList.remove("visible");
  }
  if (posList.length > window._prevPosCount) playAlertTone(920, 220);
  window._prevPosCount = posList.length;

  const sqList = st.oi_squeeze || [];
  const aPlusCount = sqList.filter(function(x) { return x.grade === "A+"; }).length;
  if (aPlusCount > window._prevAPlusCount) playAlertTone(1040, 180);
  window._prevAPlusCount = aPlusCount;

  const allSigs = [].concat(sc.approved_signals || [], sc.rejected_signals || []);
  const cList = st.closed_pnl || [];
  document.getElementById("cnt-booster").textContent = "A+: " + aPlusCount;
  document.getElementById("cnt-signals").textContent = String(allSigs.length);
  document.getElementById("cnt-pos").textContent = posList.length + "/" + ordList.length;
  document.getElementById("cnt-ws").textContent = String(ws.symbols_live || 0);
  document.getElementById("cnt-scr").textContent = String((sc.screener_top || []).length || 0);
  document.getElementById("cnt-hist").textContent = String(cList.length);

  // Таблица OI Squeeze
  const sqBody = document.getElementById("oi-squeeze-tbody");
  if (sqList.length === 0) {
    sqBody.innerHTML = "<tr><td colspan=12 style=text-align:center;color:var(--muted);padding:14px>Нажмите «🔍 Обновить скан»...</td></tr>";
  } else {
    sqBody.innerHTML = sqList.map(function(s) {
      const side = s.side || (s.direction === "LONG" ? "Buy" : "Sell");
      const gCls = s.grade === "A+" ? "badge-green" : (s.grade === "A" ? "badge-blue" : "badge-amber");
      const oiCls = s.oi_delta_45m_pct >= 0 ? "pos-green" : "pos-red";
      return "<tr><td><b>" + s.symbol + "</b></td><td><b>" + s.setup_type + "</b></td><td><span class=\"badge " + gCls + "\">" + s.grade + " (" + s.score + ")</span></td><td class=" + (side === "Buy" ? "pos-green" : "pos-red") + ">" + (s.direction || (side === "Buy" ? "LONG" : "SHORT")) + "</td><td>" + s.entry_price + "</td><td class=" + oiCls + ">" + (s.oi_delta_45m_pct >= 0 ? "+" : "") + s.oi_delta_45m_pct + "% ($" + s.oi_value_m + "M)</td><td>" + s.funding_rate_pct + "%</td><td class=" + (s.obi_top5_pct >= 0 ? "pos-green" : "pos-red") + ">" + (s.obi_top5_pct >= 0 ? "+" : "") + s.obi_top5_pct + "%</td><td>" + s.tp1_price + " / <b>" + (s.take_profit || s.take_profit_2 || s.take_profit_1) + "</b></td><td>" + s.stop_loss + "</td><td style=font-size:11px;color:var(--muted)>" + escapeHtml(s.rationale) + "</td><td><button type=button class=\"btn-success btn-sm\" data-action=booster_exec data-symbol=" + s.symbol + " data-side=" + side + " data-avg=" + s.entry_price + " data-tp=" + (s.take_profit || s.take_profit_2 || s.take_profit_1) + " data-sl=" + s.stop_loss + ">🚀 Вход Booster ($" + Number(b.effective_risk_usd ?? 0).toFixed(0) + ")</button></td></tr>";
    }).join("");
  }

  document.getElementById("kpi-ws").textContent = Number(ws.msg_count || 0).toLocaleString() + " тиков";
  document.getElementById("kpi-ws-sub").textContent = "Активных пар L2: " + (ws.symbols_live || 0) + "/" + (ws.symbols_total || "—");
  const wsB = document.getElementById("ws-badge");
  wsB.className = ws.connected ? "badge badge-blue" : "badge badge-amber";
  wsB.textContent = ws.connected ? ("📡 WebSocket LIVE (" + (ws.symbols_live || 0) + " пар)") : "📡 WebSocket: реконнект...";
  const lc = st.limit_chaser || {};
  const chB = document.getElementById("chaser-badge");
  if (chB) {
    if (!lc.enabled) {
      chB.className = "badge badge-amber";
      chB.textContent = "💎 Maker Chaser: ВЫКЛ";
    } else {
      chB.className = "badge badge-green";
      chB.textContent = "💎 Post-Only Maker: " + (lc.maker_rate_pct == null ? "нет исполнений" : lc.maker_rate_pct + "% заявок");
    }
  }

  const bReg = st.btc_regime || {};
  window._rgCfg = st.regime_guard_config || {};
  const regB = document.getElementById("btc-regime-badge");
  if (regB) {
    regB.textContent = bReg.label || "⚖️ BTC: NEUTRAL";
    regB.title = bReg.reason || "";
    if (bReg.state === "DUMP_GUARD") regB.className = "badge badge-red";
    else if (bReg.state === "PUMP_GUARD" || bReg.state === "BULL_TREND") regB.className = "badge badge-green";
    else if (bReg.state === "BEAR_TREND") regB.className = "badge badge-amber";
    else regB.className = "badge badge-blue";
  }

  const sg = st.smart_guard || {};
  const mp = st.manual_prefs || {};
  const symSel = document.getElementById("manual-symbol");
  if (symSel.options.length === 0 && cfg.core_pairs) {
    cfg.core_pairs.forEach(function(s) {
      const o = document.createElement("option");
      o.value = s;
      o.textContent = s;
      symSel.appendChild(o);
    });
    if (mp.symbol) symSel.value = mp.symbol;
  }
  if (!window._prefsHydrated) {
    window._prefsHydrated = true;
    if (mp.symbol) symSel.value = mp.symbol;
    if (mp.order_type) document.getElementById("manual-type").value = mp.order_type;
    if (mp.qty) document.getElementById("manual-qty").value = mp.qty;
    if (mp.tp_pct) document.getElementById("manual-tp-pct").value = mp.tp_pct;
    if (mp.sl_pct) document.getElementById("manual-sl-pct").value = mp.sl_pct;
    document.getElementById("sg-enabled").value = sg.enabled ? "1" : "0";
    document.getElementById("sg-split").value = sg.split_tp_enabled ? "1" : "0";
    if (sg.breakeven_trigger_pct !== undefined) document.getElementById("sg-be-pct").value = sg.breakeven_trigger_pct;
    if (sg.trailing_step_pct !== undefined) document.getElementById("sg-trail-pct").value = sg.trailing_step_pct;
    if (cfg.max_leverage) document.getElementById("cfg-leverage").value = cfg.max_leverage;
    if (cfg.core_max_positions) document.getElementById("core-max-pos").value = cfg.core_max_positions;
    if (cfg.core_min_prob_pct) document.getElementById("core-min-prob").value = cfg.core_min_prob_pct;
    const rgc = st.regime_guard_config || {};
    if (rgc.enabled !== undefined) document.getElementById("rg-enabled").value = rgc.enabled ? "1" : "0";
    if (rgc.max_same_sector) document.getElementById("rg-max-sector").value = rgc.max_same_sector;
    if (rgc.max_same_direction_total) document.getElementById("rg-max-dir").value = rgc.max_same_direction_total;
    if (rgc.time_stop_enabled !== undefined) document.getElementById("ts-enabled").value = rgc.time_stop_enabled ? "1" : "0";
    if (rgc.time_stop_minutes) document.getElementById("ts-minutes").value = rgc.time_stop_minutes;
    const lcc = st.limit_chaser || {};
    if (document.getElementById("lc-mode")) {
      document.getElementById("lc-mode").value = !lcc.enabled ? "off" : (lcc.strict_post_only ? "strict" : "hybrid");
    }
    updateManualCalculator();
  }

  window._allLogs = st.logs || [];
  renderLogs();

  const pBody = document.getElementById("positions-tbody");
  pBody.innerHTML = posList.length === 0
    ? "<tr><td colspan=12 style=text-align:center;color:var(--muted);padding:16px>Нет открытых позиций.</td></tr>"
    : buildPositionsRows(posList);

  const oBody = document.getElementById("orders-tbody");
  oBody.innerHTML = ordList.length === 0
    ? "<tr><td colspan=8 style=text-align:center;color:var(--muted);padding:14px>Нет активных ордеров.</td></tr>"
    : ordList.map(function(o) {
        return "<tr><td><b>" + o.symbol + "</b></td><td class=" + (o.side === "Buy" ? "pos-green" : "pos-red") + ">" + o.side + "</td><td>" + (o.reduceOnly ? "<span class=\"badge badge-blue\">🎯 TP1 50% (Reduce-Only)</span>" : "<span class=\"badge badge-amber\">Лимитный вход</span>") + "</td><td><b>" + o.qty + "</b></td><td><b>" + o.price + "</b></td><td>" + o.orderStatus + "</td><td style=font-family:monospace>" + (o.orderId || "").slice(0, 10) + "</td><td><button type=button class=\"btn-danger btn-sm\" data-action=cancel_ord data-symbol=" + o.symbol + " data-oid=" + o.orderId + ">🗑 Отменить</button></td></tr>";
      }).join("");

  const wsMetrics = ws.metrics || [];
  document.getElementById("ws-metrics-tbody").innerHTML = wsMetrics.map(function(m) {
    const obiCls = m.obi_top5_pct >= 0 ? "pos-green" : "pos-red";
    const cvdCls = m.cvd_usd >= 0 ? "pos-green" : "pos-red";
    return "<tr><td><b>" + m.symbol + "</b></td><td>" + m.mid_price + " / <span style=color:#38bdf8>" + m.vamp_price + "</span></td><td>" + m.spread_bps + "</td><td class=" + obiCls + ">" + (m.obi_top5_pct >= 0 ? "+" : "") + m.obi_top5_pct + "%</td><td class=" + cvdCls + ">" + (m.cvd_usd >= 0 ? "+$" : "-$") + Math.abs(m.cvd_usd).toLocaleString() + "</td><td>" + m.flow_imbalance_pct + "% (" + m.trades_count + ")</td><td>" + m.largest_wall_age_sec + "с</td><td>" + m.long_liq_usd + "$ / " + m.short_liq_usd + "$</td><td style=display:flex;gap:4px><button type=button class=\"btn-success btn-sm\" data-action=ws_quick data-symbol=" + m.symbol + " data-side=Buy data-avg=" + m.mid_price + ">🚀 Лонг</button><button type=button class=\"btn-danger btn-sm\" data-action=ws_quick data-symbol=" + m.symbol + " data-side=Sell data-avg=" + m.mid_price + ">🔻 Шорт</button></td></tr>";
  }).join("");

  const liqs = ws.recent_liquidations || [];
  document.getElementById("ws-liq-tbody").innerHTML = liqs.length === 0
    ? "<tr><td colspan=5 style=color:var(--muted);text-align:center;padding:14px>Поток allLiquidation активен — ожидаем крупные ликвидации...</td></tr>"
    : liqs.map(function(l) { return "<tr><td>" + l.time + "</td><td><b>" + l.symbol + "</b></td><td class=" + (l.side.includes("LONG") ? "pos-red" : "pos-green") + ">" + l.side + "</td><td>" + l.price + "</td><td><b>$" + Number(l.usd).toLocaleString() + "</b></td></tr>"; }).join("");

  document.getElementById("signals-tbody").innerHTML = allSigs.length === 0
    ? "<tr><td colspan=9 style=text-align:center;color:var(--muted);padding:14px>Нажмите «🔍 Обновить скан»</td></tr>"
    : allSigs.map(function(s) {
        const side = s.side || (s.direction === "LONG" ? "Buy" : "Sell");
        return "<tr><td><b>" + s.symbol + "</b></td><td>" + escapeHtml(s.strategy_name) + "</td><td class=" + (side === "Buy" ? "pos-green" : "pos-red") + ">" + (s.direction || (side === "Buy" ? "LONG" : "SHORT")) + "</td><td>" + Number(s.entry_price).toFixed(4) + "</td><td>" + Number(s.stop_loss).toFixed(4) + "</td><td>" + Number(s.take_profit || s.take_profit_2 || s.take_profit_1).toFixed(4) + "</td><td><b>" + (Number(s.ml_win_probability) * 100).toFixed(1) + "%</b></td><td>" + s.debate_verdict + "</td><td style=display:flex;gap:5px><button type=button class=\"btn-primary btn-sm\" data-action=exec_sig data-symbol=" + s.symbol + " data-side=" + side + " data-size=" + (s.position_size_coins || s.position_qty || 0) + " data-avg=" + s.entry_price + " data-tp=" + (s.take_profit || s.take_profit_2 || s.take_profit_1) + " data-sl=" + s.stop_loss + ">🎯 Стандарт</button><button type=button class=\"btn-purple btn-sm\" data-action=booster_exec data-symbol=" + s.symbol + " data-side=" + side + " data-avg=" + s.entry_price + " data-tp=" + (s.take_profit || s.take_profit_2 || s.take_profit_1) + " data-sl=" + s.stop_loss + ">🚀 Booster</button></td></tr>";
      }).join("");

  const scrList = sc.screener_top || [];
  document.getElementById("screener-tbody").innerHTML = scrList.map(function(r) {
    const px = Number(r.last_price || 0);
    return "<tr><td><b>" + r.symbol + "</b></td><td>" + px + "</td><td><span class=\"badge " + (r.passed ? "badge-green" : "badge-amber") + "\">" + (r.passed ? "IN PLAY" : "WAIT") + "</span></td><td><b>" + Number(r.in_play_score || 0).toFixed(2) + "</b></td><td>" + Number(r.rvol || 0).toFixed(2) + "x</td><td>" + Number(r.natr_pct || 0).toFixed(2) + "%</td><td>" + Number(r.spread_pct || 0).toFixed(4) + "%</td><td>" + (r.mtf_bias || "NEUTRAL") + "</td><td style=display:flex;gap:4px><button type=button class=\"btn-success btn-sm\" data-action=ws_quick data-symbol=" + r.symbol + " data-side=Buy data-avg=" + px + ">🚀 Лонг</button><button type=button class=\"btn-danger btn-sm\" data-action=ws_quick data-symbol=" + r.symbol + " data-side=Sell data-avg=" + px + ">🔻 Шорт</button></td></tr>";
  }).join("");

  // Отрисовка модуля Радар X (Twitter) & Новости
  const xn = st.x_news || {};
  window._xNewsEnabled = xn.enabled !== false;
  const xnB = document.getElementById("x-news-badge");
  if (xnB) {
    xnB.textContent = xn.global_label || "🐦 X/News: NEUTRAL";
    if (xn.global_state === "PANIC") xnB.className = "badge badge-red";
    else if (xn.global_state === "BULLISH") xnB.className = "badge badge-green";
    else if (xn.global_state === "BEARISH") xnB.className = "badge badge-amber";
    else xnB.className = "badge badge-blue";
  }
  const xnItems = xn.items || [];
  const cntXnEl = document.getElementById("cnt-xnews");
  if (cntXnEl) cntXnEl.textContent = String(xnItems.length);

  if (document.getElementById("xn-score")) {
    const gSc = Number(xn.global_score || 0);
    const scEl = document.getElementById("xn-score");
    scEl.textContent = (gSc > 0 ? "+" : "") + gSc.toFixed(1);
    scEl.className = "kpi-mini-val " + (gSc >= 10 ? "pos-green" : (gSc <= -10 ? "pos-red" : ""));
    document.getElementById("xn-state").textContent = xn.global_label || "—";
    document.getElementById("xn-bull").textContent = String(xn.bullish_count || 0);
    document.getElementById("xn-bear").textContent = String(xn.bearish_count || 0);
    document.getElementById("xn-shield").textContent = xn.panic_active ? "🛑 ТРЕВОГА PANIC" : "🛡 В НОРМЕ";
    const pBadge = document.getElementById("xnews-panic-badge");
    if (pBadge) {
      pBadge.className = xn.panic_active ? "badge badge-red" : "badge badge-green";
      pBadge.textContent = xn.panic_active ? ("🛑 PANIC: " + (xn.panic_reason || "Экстренный негатив")) : "🛡 Щит Black-Swan: В НОРМЕ";
    }
    const cMap = xn.coin_sentiment || {};
    const hotKeys = Object.keys(cMap);
    document.getElementById("xn-hot-coins").textContent = hotKeys.length === 0
      ? "Спокойный фон по отслеживаемым монетам"
      : hotKeys.slice(0, 6).map(function(k) {
          const cs = cMap[k].score || 0;
          return k.replace("USDT", "") + " (" + (cs >= 0 ? "+" : "") + cs + ")";
        }).join("  •  ");

    document.getElementById("xnews-tbody").innerHTML = xnItems.length === 0
      ? "<tr><td colspan=7 style=text-align:center;color:var(--muted);padding:14px>Нажмите «🔄 Загрузить свежие твиты сейчас»</td></tr>"
      : xnItems.map(function(it) {
          const sym = it.primary_symbol || "BTCUSDT";
          const px = Number(window._priceMap[sym] || 100);
          const sCls = it.sentiment === "BULLISH" ? "badge-green" : (it.sentiment === "BEARISH" ? "badge-red" : "badge-blue");
          const symBadges = (it.symbols && it.symbols.length > 0)
            ? it.symbols.map(function(s) { return "<span class=\"badge badge-purple\">" + s + "</span>"; }).join(" ")
            : "<span style=color:var(--muted)>РЫНОК (BTC)</span>";
          return "<tr><td>" + it.time + " <span style=color:var(--muted);font-size:10px>(" + it.age_min + "м)</span></td><td><b>" + escapeHtml(it.source) + "</b></td><td>" + symBadges + "</td><td><span class=\"badge " + sCls + "\">" + it.sentiment + " (" + (it.score >= 0 ? "+" : "") + it.score + ")</span></td><td style=white-space:normal;max-width:520px;line-height:1.35>" + escapeHtml(it.headline) + "</td><td style=font-size:11px;color:var(--muted)>" + escapeHtml(it.triggers) + "</td><td style=display:flex;gap:4px><button type=button class=\"btn-success btn-sm\" data-action=ws_quick data-symbol=" + sym + " data-side=Buy data-avg=" + px + ">🚀 Лонг " + sym.replace("USDT", "") + "</button><button type=button class=\"btn-danger btn-sm\" data-action=ws_quick data-symbol=" + sym + " data-side=Sell data-avg=" + px + ">🔻 Шорт</button></td></tr>";
        }).join("");
  }

  // Отрисовка модуля Adaptive ML Calibrator (Шаг 2)
  const mlc = st.ml_calibration || {};
  window._mlCalEnabled = mlc.enabled !== false;
  const pStats = mlc.portfolio_stats || {};
  const sList = mlc.strategy_list || [];
  const recThr = Number(mlc.recommended_ml_threshold_pct || 58.0);
  const cntMlEl = document.getElementById("cnt-ml");
  if (cntMlEl) cntMlEl.textContent = recThr.toFixed(0) + "%";
  const mlBadge = document.getElementById("ml-cal-status-badge");
  if (mlBadge) {
    mlBadge.className = window._mlCalEnabled ? "badge badge-green" : "badge badge-amber";
    mlBadge.textContent = window._mlCalEnabled ? ("🧠 Авто-калибровка: ВКЛ (порог " + recThr.toFixed(1) + "%)") : "⏸ Авто-калибровка: ВЫКЛ";
  }
  if (document.getElementById("ml-rec-thr")) {
    document.getElementById("ml-rec-thr").textContent = recThr.toFixed(1) + "%";
    document.getElementById("ml-base-thr").textContent = "Базовый порог: " + Number(mlc.base_ml_threshold_pct || 58).toFixed(1) + "%";
    document.getElementById("ml-port-pf").textContent = (pStats.profit_factor == null ? "—" : Number(pStats.profit_factor).toFixed(2));
    document.getElementById("ml-port-wr").textContent = Number(pStats.win_rate_pct || 0).toFixed(1) + "%";
    document.getElementById("ml-port-wl").textContent = "Побед: " + (pStats.wins || 0) + " / Стопов: " + (pStats.losses || 0);
    const netP = Number(pStats.net_pnl_usd || 0);
    const netEl = document.getElementById("ml-port-net");
    netEl.textContent = (netP >= 0 ? "+$" : "-$") + Math.abs(netP).toFixed(2);
    netEl.className = "kpi-mini-val " + (netP >= 0 ? "pos-green" : "pos-red");
    document.getElementById("ml-top-strat").textContent = sList.length > 0 ? (sList[0].strategy_name + " (" + sList[0].weight_multiplier + "x)") : "—";
    document.getElementById("ml-last-ts").textContent = mlc.last_calibrated_at || "—";

    document.getElementById("ml-cal-tbody").innerHTML = sList.map(function(sr) {
      const bCls = sr.status === "BOOST" ? "badge-green" : (sr.status === "QUARANTINE" ? "badge-red" : (sr.status === "COOLING" ? "badge-amber" : "badge-blue"));
      const np = Number(sr.net_pnl_usd || 0);
      const padj = Number(sr.prob_adjustment_pct || 0);
      return "<tr><td><b>" + escapeHtml(sr.strategy_name) + "</b></td><td><span class=\"badge " + bCls + "\">" + sr.badge + "</span></td><td>" + sr.trades + " (" + sr.wins + "W / " + sr.losses + "L)</td><td><b>" + sr.win_rate_pct + "%</b></td><td><b>" + (sr.profit_factor == null ? "—" : sr.profit_factor) + "</b></td><td class=" + (sr.streak >= 0 ? "pos-green" : "pos-red") + "><b>" + sr.streak_str + "</b></td><td class=" + (np >= 0 ? "pos-green" : "pos-red") + ">" + (np >= 0 ? "+$" : "-$") + Math.abs(np).toFixed(2) + "</td><td class=" + (padj >= 0 ? "pos-green" : "pos-red") + ">" + (padj >= 0 ? "+" : "") + padj + "%</td></tr>";
    }).join("");
  }

  document.getElementById("closed-pnl-tbody").innerHTML = cList.map(function(c) {
    const p = Number(c.closedPnl || 0);
    const dtStr = c.updatedTime
      ? new Date(Number(c.updatedTime)).toLocaleString("ru-RU", {day:"2-digit", month:"2-digit", year:"numeric", hour:"2-digit", minute:"2-digit", second:"2-digit"})
      : "—";
    return "<tr><td><b>" + c.symbol + "</b></td><td>" + c.side + "</td><td>" + c.qty + "</td><td>" + c.avgEntryPrice + "</td><td>" + c.avgExitPrice + "</td><td class=" + (p >= 0 ? "pos-green" : "pos-red") + ">" + (p >= 0 ? "+" : "") + p.toFixed(2) + " USDT</td><td>" + (c.leverage || "3") + "x</td><td>" + dtStr + "</td></tr>";
  }).join("");
}

refreshState();
setInterval(refreshState, 3500);
