# AItrade — Гибридный ИИ-комбайн для интрадей-торговли на Bybit V5 (Demo & Live)

Полнофункциональная реализация трехуровневого ИИ-комбайна из документа **«Интрадей-стратегии и ИИ-комбайн для криптотрейдинга»**, оптимизированная для получения **3–10 снайперских сигналов в день с максимальным винрейтом (65%–75%+)** и безопасной отработки на **Bybit Unified Demo Trading (`https://api-demo.bybit.com`)**.

---

## Быстрый старт (без установки внешних библиотек)

Ядро `AItrade` спроектировано так, чтобы работать сразу на стандартном Python 3.9+ на вашем Mac (Zero-Dependency Mode) и при этом включает готовый адаптер и конфигурацию для контейнера **Freqtrade + FreqAI**.

```bash
cd /Users/sergejrubcov/AItrade

# 1. Просканировать 20 пар Bybit V5, вывести снайперские сигналы и сгенерировать HTML-дашборд
python3 run_combine.py scan

# 2. Просканировать рынок и автоматически исполнить топ-сигналы на демо-счете Bybit / в локальном SQLite-трекере
python3 run_combine.py demo-trade

# 3. Запустить сравнительный Walk-Forward бэктест (Наивный режим vs Снайперский ИИ-комбайн)
python3 run_combine.py backtest

# 4. Посмотреть журнал открытых демо-сделок
python3 run_combine.py journal

# 5. Запустить автотесты всех математических формул, стратегий и риск-движка
python3 -m unittest discover -s tests -v
```

После вызова `scan`, `demo-trade` или `report` в папке автоматически обновляется наглядный интерактивный терминал [`dashboard_report.html`](file:///Users/sergejrubcov/AItrade/dashboard_report.html), который можно открыть в любом браузере.

---

## Как подключить демо-счет Bybit (Bybit Unified Demo Trading)

1. Зайдите в свой аккаунт на [Bybit](https://www.bybit.com), в правом верхнем меню нажмите **«Демо-трейдинг» (Demo Trading)**.
2. Перейдите в **API Management** внутри демо-аккаунта и создайте ключ с правами **Read-Write (Чтение и Торговля ордерами/позициями)** без права вывода средств.
3. Скопируйте `.env.example` в `.env` и вставьте ключи:
   ```bash
   cp .env.example .env
   ```
   ```ini
   BYBIT_DEMO_API_KEY=ваш_ключ_демо
   BYBIT_DEMO_API_SECRET=ваш_секрет_демо
   AITRADE_MODE=demo
   ```
4. Пока ключи не указаны в `.env`, комбайн автоматически забирает рыночные данные и ведет виртуальный демо-счет ($10,000 USDT) в локальной базе `data/demo_journal.sqlite3`. Как только вы пропишете ключи, ордера `Limit PostOnly` со стоп-лоссом и тейк-профитом будут параллельно выставляться напрямую на `https://api-demo.bybit.com/v5/order/create`.

---

## Из чего состоит воронка ИИ-комбайна

1. **Пре-скринер «Монеты в игре» ([`aitrade/screener/coins_in_play.py`](file:///Users/sergejrubcov/AItrade/aitrade/screener/coins_in_play.py))**:
   - Проверяет пул из 20 ликвидных фьючерсных пар Bybit (`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `XRPUSDT`, `ADAUSDT`, `SUIUSDT` и др.).
   - Фильтрует относительный объем `RVOL`, нормализованную волатильность `NATR %`, защитное отношение `ATR(14)/ATR_baseline(20) <= 1.5`, бид-аск спред `< 0.10%` и определяет старший Bias на `1H` по вееру `EMA 8/21/50` и `ADX(14)` ([Kryptonim](https://www.kryptonim.com/blog/find-volatile-coins-crypto-scalping)).
2. **Микроструктура стакана L2 и деривативы ([`aitrade/features/microstructure.py`](file:///Users/sergejrubcov/AItrade/aitrade/features/microstructure.py))**:
   - Считает `VAMP` (Volume Adjusted Mid Price), взвешенный дисбаланс топ-5 уровней стакана `OBI` с весами `[1.0, 0.5, 0.25, 0.125, 0.0625]`, сглаженный полиномиальным **фильтром Савицкого — Голея (Savitzky-Golay)** ([arXiv 2506.05764v2](https://arxiv.org/html/2506.05764v2)).
   - Вычисляет токсичный поток ордеров `VPIN (TOF)` ([The Kingfisher](https://thekingfisher.io/blogs/the_kingfisher_your_scalping_toolbox)), свечную и тиковую кумулятивную дельту `CVD` и **4-ступенчатый фильтр защиты от спуфинга** (`Liquidity Behavior Index — LBI`, отсекающий `Vanishing Walls` и `Layering`) ([ChainVol](https://chainvol.com/features/spoofing)).
3. **Пять рабочих интрадей-архетипов ([`aitrade/strategies/`](file:///Users/sergejrubcov/AItrade/aitrade/strategies/))**:
   - **Архетип 1 (`vwap_keltner_mr.py`)**: Снайперский возврат к среднему от границ канала Кельтнера (`EMA20 ± 2×ATR`) и сессионного `VWAP ±2σ` при флэтовом `ADX` (базовый Win Rate `65%–75%`), а также трендовый `VWAP Reclaim` ([Quantum Algo](https://www.quantum-algo.com/blog/vwap-intraday-trading-strategy/)).
   - **Архетип 2 (`liquidity_sweep_fvg.py`)**: Ложный пробой пулов ликвидности `BSL / SSL` за 1–3 свечи (`Liquidity Sweep`) + лимитный ретест 50% середины `15m Fair Value Gap (FVG)` с подтверждением дивергенции `CVD` ([ACY SMC Guide](https://acy.com/en/market-news/education/confirmation-model-ob-fvg-liquidity-sweep-j-o-20251112-094218/)).
   - **Архетип 3 (`oi_cvd_squeeze.py`)**: Разворот после каскадной ликвидации лонгов (`Long Flush` при падении `OI`) и истощение шорт-сквиза (`Short Squeeze Exhaustion` у верхней границы зоны стоимости `VAH`).
   - **Архетип 4 (`orb_breakout.py`)**: Пробой диапазона открытия Европейской и Американской сессии (`Session ORB`) при высоком `RVOL` с правилом 2 баров (`2-Bar Stall Rule`) ([QuantifiedStrategies](https://www.quantifiedstrategies.com/stocks-in-play-trading-strategy-day-trading/)).
   - **Архетип 5 (`lead_lag_density.py`)**: Межрыночный импульс запаздывания `Lead-Lag (BTC → Altcoins, lag=3)` ([ResearchGate Lead-Lag](https://www.researchgate.net/publication/401861798_Price_Transmission_from_Bitcoin_to_Altcoins_High-Frequency_Evidence_and_Implications_for_Trading_Strategy)) и вход от верифицированных крупных плотностей стакана L2 (`LBI >= 0.75`).
4. **Табличный ML-валидатор и Совет ИИ-агентов ([`aitrade/ml/meta_labeler.py`](file:///Users/sergejrubcov/AItrade/aitrade/ml/meta_labeler.py), [`aitrade/agents/debate_council.py`](file:///Users/sergejrubcov/AItrade/aitrade/agents/debate_council.py))**:
   - Оценивает вероятность успеха на скользящем окне с экспоненциальными весами свежести, проверяет **Индекс несходства (`Dissimilarity Index <= 1.0`)** ([FreqAI](https://www.freqtrade.io/en/stable/freqai-configuration/)) и **Cost-Aware Execution Filter ($|\hat{r}| > 2.0 \cdot c_{total}$)** ([Bitcoin Trading Under Transaction Costs](https://www.researchgate.net/publication/405440975_Machine_Learning-Based_Bitcoin_Trading_Under_Transaction_Costs_Evidence_From_Walk-Forward_Forecasting)).
   - Проводит раунд дебатов Бычьего и Медвежьего агентов-исследователей ([TradingAgents](https://tradingagents-ai.github.io/)).
5. **Математический риск-движок ([`aitrade/risk/risk_engine.py`](file:///Users/sergejrubcov/AItrade/aitrade/risk/risk_engine.py))**:
   - Рассчитывает объем позиции от фиксированного риска `0.5%` капитала (`Leverage as an Output`, максимум `4x`), проверяет **правило 5-кратной дистанции до цены ликвидации (`LiqDistance >= 5 × Stop`)** и оценивает **динамическое проскальзывание по 10 уровням стакана L2** в долларовом эквиваленте ([Imbalance Labs](https://imbalancelabs.com/research)).
