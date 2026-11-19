# Мастер-промпт для Gemini на переделку AItrade

Скопируйте текст ниже в сессию Gemini (вместе с доступом к репозиторию и файлами `01_RESEARCH_AND_MATH_2026.md`, `02_CODEBASE_AUDIT.md`, `03_REFACTOR_SPEC.md`):

---

Ты — senior quant developer и архитектор алгоритмических торговых систем на Python (Bybit V5).
Изучи три документа в папке `docs/refactor_2026/`:
1. `01_RESEARCH_AND_MATH_2026.md` — эмпирическое исследование стратегий и математика 2026 года.
2. `02_CODEBASE_AUDIT.md` — перечень критических багов в текущей кодовой базе `AItrade`.
3. `03_REFACTOR_SPEC.md` — детальное техническое задание на пересборку комбайна под два изолированных инструмента: «Разгон» ($1,000 → $10,000) и «Автотрейдинг» (высокий винрейт + стабильный плюс).

### Строгие инженерные ограничения
1. **НЕ ломай существующий контур безопасности:** сохрани инварианты из `aitrade/storage.py` (`Ledger`, `atomic_json`), `aitrade/exchange/transport.py` и все тесты в `tests/test_safety.py` и `tests/test_intraday_release.py`.
2. **Никакого look-ahead bias и фейкового бэктеста:** решения принимаются строго на закрытых свечах; запрещено подменять сетевые ошибки синтетическими свечами в продакшене и бэктесте; запрещено завышать ML-вероятность захардкоженными константами (`base_winrate_est`).
3. **Никакого мартингейла и отключения риск-лимитов:** `training_unlimited_mode` по умолчанию строго `False`.
4. **После каждого этапа** запускай `python3 -m unittest discover -s tests -v`, проверяй отсутствие регрессий, обновляй/добавляй юнит-тесты для новой логики и показывай краткий diff + вывод тестов.

### Поэтапный план работы (выполняй строго по шагам, останавливаясь для проверки после каждого этапа)

- **Этап 1. Многомесячный WFA-бэктестер и исправление индикаторов/режимов:**
  - Доработай `aitrade/features/indicators.py` (`calc_atr_percentile`, `calc_hurst_exponent`, `calc_supertrend`).
  - Создай `aitrade/strategies/regime_router.py` (тройной гейт `ADX + ATR% + Hurst` + опциональный `GaussianHMM` в `aitrade/ml/regime_hmm.py`).
  - Перепиши `aitrade/backtest/walk_forward.py` на загрузку 180+ дней свечей Bybit с пагинацией и локальным кэшем, Rolling WFA (6+ циклов), честный Sharpe/DSR, Монте-Карло перестановку сделок и стресс-тест 2x издержек.

- **Этап 2. Торговые стратегии обоих контуров:**
  - Исправь `aitrade/strategies/regime_s1_s3.py` (честный `S3_VWAP_RANGE` с `R:R >= 1.15`, `SL >= 0.80%`, `PostOnly` Limit; исправленная цена ретеста `S1` ниже рынка; проверка гэпов только на последних 60 барах).
  - Создай `aitrade/strategies/booster_breakout.py` (Volatility Squeeze + Supertrend Breakout, `SL >= max(1.2%, 1.5*ATR)`).
  - Создай `aitrade/strategies/momentum_funding.py` (Рукав B Автотрейдинга) и `aitrade/strategies/funding_carry.py` (Рукав C Автотрейдинга).
  - Прогони все направленные стратегии через новый WFA-бэктестер из Этапа 1 и покажи таблицу метрик OOS (`WR`, `PF`, `WFE`, `Max DD`, `2x-cost E[R]`).

- **Этап 3. Реальный ML Meta-Labeler и исправление Калибратора:**
  - Перепиши `aitrade/ml/meta_labeler.py` на `LightGBM` / `scikit-learn` с Triple Barrier разметкой, Purged CV + embargo, вероятностной калибровкой и честным Cost-Aware фильтром (удали `0.68 * base_winrate_est` и сломанный `min_samples`).
  - Исправь `aitrade/booster/ml_calibrator.py`: убери вечный `QUARANTINE` по 3 стопам, добавь порог выборки `>= 20–25` сделок и автовыход по таймеру/теневым сделкам.

- **Этап 4. Изоляция контуров «Разгон» и «Автотрейдинг», Сейф-храповик и Risk/Execution:**
  - Раздели субаккаунты, API-ключи, слоты и `Ledger` в `env_manager.py` и `live_server.py`.
  - В `smart_position_manager.py` добавь переключение в `Isolated Margin`, волатильностный трейлинг (`1.8 * ATR`) для Разгона и принудительный `PostOnly` / `ReduceOnly`.
  - Перепиши `aitrade/booster/deposit_booster.py`: убери потолок `$1,000`, реализуй дробный Келли `3.5%–4.5%`, демпфер `-20%` и «Сейф-храповик» (`Ratchet Vault`).
  - Исправь `OISqueezeScanner` (отключи от автоторговли, поправь пороги и закрытые свечи, вынеси из-под глобального лока) и убери паразитные вызовы `calibrate_from_closed_pnl()` / `refresh_feed()` из `get_full_state()`.

- **Этап 5. UI, конфигурация и финальные тесты:**
  - Обнови `config/settings.json` (раздельные списки `autotrade_pairs` и `booster_pairs`, торговые часы UTC).
  - Добавь `last_price` в `ScreenerResult` (`coins_in_play.py`).
  - Почини 4 неработающие кнопки, гидратацию `training_unlimited_mode`, отображение субаккаунтов, «Сейфа-храповика», режимного гейта и WFA-метрик в `dashboard_report.html`.
  - Напиши юнит-тесты на все новые модули, прогони весь набор тестов и обнови `release-manifest.json`.

Начинай с **Этапа 1**. Перед правкой кода кратко перечисли, какие файлы и функции ты изменишь на этом этапе.