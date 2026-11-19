# Технический аудит текущего репозитория AItrade

## 1. Что написано хорошо и ОБЯЗАТЕЛЬНО нужно сохранить

Не переписывать и не ломать следующие модули и инварианты:
- `aitrade/storage.py`: класс `Ledger` (SQLite с `busy_timeout=10000`, `chmod 0o600`, аудит-лог `events`, строгий контроль жизненного цикла позиций через `_execution_lifecycles()` и сохранение количества без эвристик по символу/времени) и `atomic_json()`.
- `aitrade/exchange/transport.py`: `request()` с валидацией TLS, отсутствием секретов в argv и `ExchangeError(uncertain=...)` без слепых ретраев `POST`.
- `aitrade/exchange/ws_stream.py`: потоковый `BybitV5WebSocketHub` с `get_bbo(max_age=5)`, временной дедупликацией сделок/ликвидаций и окнами CVD 1m/5m/15m.
- `tests/test_safety.py` и `tests/test_intraday_release.py`: все инварианты безопасности (идемпотентность `orderLinkId`, запрет повторного закрытия при `PENDING`, проверка `CSRF/Origin`, запрет синтетического fallback в продакшене).

---

## 2. Критические торговые и математические дефекты (причины убытков)

### Дефект 1: Отрицательное матожидание в `S3_VWAP_RANGE`
- **Файлы:** `aitrade/strategies/regime_s1_s3.py`, `aitrade/risk/risk_engine.py`, `aitrade/dashboard/live_server.py`.
- **Суть бага:**
  - Для `S3_VWAP_RANGE` разрешён `min_rr_s3 = 0.50` и минимальный стоп `MIN_STOP_FRAC = 0.0055` (0.55%), а `order_type = "Market"`.
  - Условие входа `c_sig.close > prev_bar.high` (Buy) / `c_sig.close < prev_bar.low` (Sell) ждёт подтверждения слишком поздно, съедая половину расстояния до VWAP и сжимая R:R до `0.50–0.75`.
  - При издержках Bybit Taker + проскальзывание (`0.17%` на круг) и стопе `0.55%` стоимость входа/выхода равна `0.31R`. При `R:R = 0.50` для безубыточности нужен винрейт `87.3%`; при 56% WR матожидание составляет `-0.469R` на сделку.

### Дефект 2: Сломанный режимный роутер и Taker-пробой в `S1_TREND_RETEST`
- **Файл:** `aitrade/strategies/regime_s1_s3.py`.
- **Суть багов:**
  1. `classify_regime_1h` использует `er20 >= 0.22` (порог снижен в `apply_aitrade_fixes.py`). На чистом случайном блуждании без тренда средний `ER(20) = 0.225`, и `44.1%` чистого шума классифицируется как тренд — `S1` торгует пробои во флэте.
  2. `retest_px = box_high + 0.25 * atr15` (для лонга) и `box_low - 0.25 * atr15` (для шорта). Если пробойная свеча закрылась на `+0.10 * atr15` выше `box_high`, лимитная цена покупки оказывается **выше рынка**. Поскольку в `live_server.py` для `Limit` используется `timeInForce="GTC"` (а не `PostOnly`), ордер немедленно исполняется по рынку как Taker.
  3. Проверка гэпов `any(b.timestamp - a.timestamp != 900_000 for a, b in zip(closed_15m, closed_15m[1:]))` проверяет все 600 свечей (~6.25 дней). Один пропущенный бар 5 дней назад полностью блокирует стратегию на паре.

### Дефект 3: Неработающий ML-фильтр в `WalkForwardMetaLabeler`
- **Файл:** `aitrade/ml/meta_labeler.py`.
- **Суть багов:**
  1. `blended_prob = 0.68 * signal.base_winrate_est + 0.32 * empirical_prob + micro_bonus` на 68% состоит из захардкоженной константы (`0.48` / `0.56`), а `micro_bonus` добавляет до `+0.145`. Даже стратегия с реальным эмпирическим WR `25%–35%` получает скор `53%–61%` и проходит фильтр.
  2. Баг в проверке минимальной выборки: `min_samples = min(8, len(X_train)) if len(X_train) > 0 else 0`, затем `len(X_train) >= min_samples` — это условие **всегда `True`** (даже при `0` или `1` сэмпле).
  3. Переменная `empirical_ret` рассчитывается, но нигде не используется: `expected_return_pct` вычисляется из раздутой `blended_prob`, делая Cost-Aware Filter фиктивным.

### Дефект 4: Вечный deadlock (`QUARANTINE`) в `AdaptiveMLCalibrator`
- **Файлы:** `aitrade/booster/ml_calibrator.py`, `aitrade/dashboard/live_server.py`.
- **Суть бага:** при `streak <= -3` (3 стопа подряд) стратегия получает статус `QUARANTINE`, а `live_server.py` полностью запрещает ей новые входы. Так как новых сделок нет, `streak` никогда не обновляется — стратегия умирает навсегда. При рабочем WR `45%` шанс поймать 3 стопа подряд за 15 сделок равен `77.4%` (за 30 сделок — `95.8%`).

### Дефект 5: «Разгон» математически заблокирован и не изолирован от «Автотрейдинга»
- **Файлы:** `aitrade/booster/deposit_booster.py`, `aitrade/dashboard/live_server.py`, `aitrade/execution/smart_position_manager.py`.
- **Суть багов:**
  1. Таблица `PHASES` (риски `1.0%–1.35%`) и `house_money_bonus_usd` не работают: ниже в `compute_metrics` захардкожено `base_risk_pct = 0.5`, `house_money_bonus_usd = 0.0` и потолок `min(1000., current_eq)`. В `_place_checked` также стоит `capital = min(1000., usdt_balance)` и `risk_frac <= 0.007`. Максимальный риск заморожен на `$5–$7` навсегда (для роста `+$9,000` при `+0.25R` потребуется `7,200` сделок).
  2. Оба контура (`BOOSTER` и `CORE`) работают на **одном счёте** и делят 3 общих слота (`len(busy) >= 3`). При этом `trade_booster` идёт первым и съедает слоты `trade_core`.
  3. Строка `if booster_slots > 0 and not trade_core:` отключает трендовый `S1` для Разгона, когда включены оба автопилота.
  4. Нигде в коде не вызывается переключение режима маржи в `Isolated` (`/v5/position/switch-isolated`).

### Дефект 6: Невалидированный `OISqueezeScanner` и удушающий трейлинг
- **Файлы:** `aitrade/booster/oi_squeeze_scanner.py`, `aitrade/execution/smart_position_manager.py`, `aitrade/booster/regime_correlation_guard.py`.
- **Суть багов:**
  1. `OISqueezeScanner` не подключён к бэктестеру и не проходил историческую валидацию.
  2. Пороги `short_liq > 200` ($200), `long_liq > 500` ($500), `oi_delta_pct >= 0.45%`, `funding_rate_pct >= 0.012%` срабатывают на обычном рыночном шуме.
  3. ATR и проверка «Анти-Хай» считаются по **незакрытой свече `k_list[0]`**.
  4. В `SmartPositionManager` для `BOOSTER:*` стоит фиксированный трейлинг `0.85%` после `+1.3R` (при стопах у альтов `1.5%–2.6%`) и частичное закрытие `30%` на `+1.0R`, что обрезает трендовые хвосты.
  5. `RegimeAndCorrelationGuard` блокирует входы по 5-минутному движению BTC (`±0.35%`), что часто отсекает лучшие откатные входы и пропускает входы на пиках.

### Дефект 7: Инженерные, инфраструктурные и UI-баги
- **Отключённая защита на VPS:** `apply_full_upgrade.py` устанавливает `training_unlimited_mode = True` в `data/booster_state.json`, что отключает дневной стоп-кран `-3%`, лимит сделок/день, блок плавающей просадки `-1%` и кулдауны.
- **Бесполезный бэктест длиной 2.9 дня:** `walk_forward.py` качает `limit=500` 15m свечей (5.2 дня), тратит 220 на прогрев и тестирует всего ~2.9 дня.
- **Паразитные записи на диск и блокировки:** `get_full_state()` (каждые 5с) вызывает `ml_calibrator.calibrate_from_closed_pnl()` (перезапись JSON + `fsync` ~17 000 раз/сутки) и `x_radar.refresh_feed()` (гонка с `_news_loop`). `OISqueezeScanner` делает 33 последовательных HTTP-запроса под глобальным `self._lock`.
- **Баги UI (`dashboard_report.html`) и скринера:**
  - Кнопки `#btn-refresh-xnews`, `#btn-toggle-xnews`, `#btn-run-ml-cal`, `#btn-toggle-ml-cal` не имеют JS-обработчиков.
  - `window._boosterTrainingMode` не синхронизируется с `st.booster.training_unlimited_mode` в `renderState()`.
  - В `ScreenerResult` (`aitrade/screener/coins_in_play.py`) отсутствует поле `last_price`, из-за чего UI показывает `0` и падают быстрые ордера из вкладки «Скринер».
  - В `config/settings.json` смешаны 35 пар (мейджоры и тонкие мемкоины `POPCAT`, `WIF`, `1000BONK`) без разделения ликвидности и профилей риска.