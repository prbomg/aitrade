# Критический анализ подхода и архитектура ИИ-комбайна AItrade под Bybit V5 (Demo & Live)

На основе детального разбора вашего документа **«Интрадей-стратегии и ИИ-комбайн для криптотрейдинга»** ниже приведена инженерная и алгоритмическая экспертиза подхода, выявленные узкие места при реальной торговле на **Bybit V5** и архитектурные улучшения, заложенные в код проекта `AItrade`.

---

## 1. Сильные стороны выбранной концепции

1. **Отказ от HFT-гонки в пользу горизонта 15m – 4h (Удержание 1–8 часов, 3–10 сделок в день)**:
   - На горизонте секунд и тикового OBI (Order Book Imbalance) валовое движение цены составляет менее 10 б.п. (0.10%), что полностью съедается тейкерской комиссией Bybit Perpetuals (`0.055%` тейкер / `0.020%` мейкер) ([Bitsgap Blog](https://bitsgap.com/blog/bybit-binance-okx-where-your-bot-pays-less-in-2026), [Imbalance Labs](https://imbalancelabs.com/research)).
   - Переход к снайперским лимитным входам (Maker `0.02%`) на таймфреймах `15m / 1h` с целевым движением `0.9% – 3.5%+` делает систему нечувствительной к сетевому пингу 150–250 мс с домашнего Mac или базового VPS и устраняет вымывание депозита комиссиями ([Machine Learning-Based Bitcoin Trading Under Transaction Costs](https://www.researchgate.net/publication/405440975_Machine_Learning-Based_Bitcoin_Trading_Under_Transaction_Costs_Evidence_From_Walk-Forward_Forecasting)).

2. **Смена фокуса с пробоев на возврат к среднему (Mean Reversion) и ловлю ложных выносов (Liquidity Sweep + FVG) для максимального Win Rate**:
   - Классические пробойные стратегии имеют базовый винрейт `35%–48%` и требуют редких трендовых хвостов, тогда как возврат от экстремумов `VWAP ±2σ` / каналов Кельтнера (`EMA 20 ± 2×ATR`) при фильтрации силы тренда `ADX(14)` и подтвержденный ложный пробой (`Liquidity Sweep + 15m FVG + дивергенция CVD`) дают исторический винрейт **65%–75%** при профит-факторе **1.5–2.5** ([WisdomChain Top 50 Strategies](https://insights.wisdomchain.com/top-50-algorithmic-trading-strategies-futures-forex-comprehensive-review/), [Quantum Algo VWAP](https://www.quantum-algo.com/blog/vwap-intraday-trading-strategy/)).

3. **Трехуровневая защита сигнала от шума**:
   - **Пре-скринер «Монеты в игре» (Coins in Play)**: отсекает неликвидные пары со спредом `≥ 0.10%`, низким относительным объемом (`RVOL < 1.5`) или хаотичным расширением волатильности (`ATR_14 / ATR_20 > 1.5`) ([Kryptonim](https://www.kryptonim.com/blog/find-volatile-coins-crypto-scalping), [QuantifiedStrategies](https://www.quantifiedstrategies.com/stocks-in-play-trading-strategy-day-trading/)).
   - **Табличный ML Meta-Labeling + Cost-Aware Execution Filter ($\lambda = 2.0$)**: модель оценивает вероятность отработки и ожидаемую амплитуду $|\hat{r}| > \lambda \cdot c_{total}$, отсекая ~85% шумовых сделок, а индекс несходства (**Dissimilarity Index — DI**) и детектор выбросов блокируют торговлю в незнакомых рыночных шоках ([FreqAI Documentation](https://www.freqtrade.io/en/stable/freqai-configuration/)).
   - **Мультиагентный дебат (Bull vs. Bear Researcher + Risk Judge)**: устраняет предвзятость подтверждения (confirmation bias) и накладывает фильтр макрорежима (`Risk-On`, `Risk-Off`, `Squeeze Alert`, `Range`) ([TradingAgents](https://tradingagents-ai.github.io/)).

---

## 2. Критический разбор узких мест и доработки специально под Bybit Demo Trading

При переносе описанной в документе теории на практику работы с **Bybit V5** есть **5 критических нюансов**, которые мы исправили и улучшили в архитектуре `AItrade`:

| Узкое место в исходном подходе | В чем риск на практике | Как это решено в нашем комбайне `AItrade` |
| :--- | :--- | :--- |
| **1. Путаница между Bybit Testnet и Bybit Demo Trading** | На классическом `api-testnet.bybit.com` искусственный стакан с пустыми спредами и фейковыми прострелами — обучение ML и тест лимитных стратегий там бесполезны. | В коннекторе `BybitV5Client` реализован **нативный режим Bybit Unified Demo Trading (`https://api-demo.bybit.com`)**, где котировки и ликвидность на 100% совпадают с реальным рынком Bybit (`api.bybit.com`), а сделки совершаются на виртуальном демо-счете USDT. |
| **2. Зависимость от сторонних API для деривативов (Coinalyze / Hyblock)** | Бесплатный Coinalyze имеет лимиты и задержки агрегации, а Hyblock стоит $4,788/год ([Hyblock API](https://hyblockcapital.com/api)). Без API-ключа система бы встала. | Нативный **Bybit V5 Market API** бесплатно и без авторизации отдает исторический и текущий **Open Interest (`/v5/market/open-interest`)**, **Funding Rate (`/v5/market/funding/history`)**, **Long/Short Ratio (`/v5/market/account-ratio`)**, глубокий стакан **Level-2 (`/v5/market/orderbook`, до 500 уровней)** и ленту принтов. Мы забираем их напрямую с Bybit V5, а Coinalyze оставили как опциональный расширитель. |
| **3. Конфликт стратегий возврата к среднему и пробоя (Mean Reversion vs ORB)** | Если одновременно запустить контртрендовый `VWAP ±2σ` и пробойный `5m/15m ORB` на открытии США (13:30 UTC), они будут открывать встречные позы на одной свече. | Внедрен **переключатель рыночного режима (`Regime Router`)** по вееру `EMA 8/21/50`, `ADX(14)` на 1H и времени сессии UTC: при `ADX < 25` приоритет у `VWAP/Keltner Mean Reversion` и `Liquidity Sweep + FVG`; при `ADX ≥ 25` контртренд блокируется и активируются `VWAP Trend Pullback` и направленный `Session ORB`. |
| **4. Холодный старт ML-валидатора (Cold Start Problem)** | В чистом FreqAI пока модель не накопит разметку или если не установлены тяжелые C++ библиотеки, вы не получите ни одного сигнала. | Реализован **двухконтурный ансамбль (`GradientBoosting / Walk-Forward Meta-Labeler` + калиброванный `Quant Scoring Ensemble`)** с расчетом **Savitzky-Golay сглаживания**, **VAMP (Volume Adjusted Mid Price)**, **Dissimilarity Index (DI)** и **Cost-Aware Filter ($\lambda = 2.0$)**. Он обучается «на лету» на скачанных свечах Bybit за секунды и сразу выдает калиброванный Winrate-скор. |
| **5. Пересчет размера позиции и дистанции до ликвидации** | В документе указана формула размера позиции от риска $0.5\%$ и правило дистанции до ликвидации $\ge 5 \times \text{Stop}$, но без учета минимального шага лота (`qtyStep`) и `Dynamic L2 Slippage` биржа отклонит ордер. | Модуль `RiskEngine` автоматически сверяет шаг цены/лота с Bybit, «прометает» 10 уровней реального стакана L2 (`sweep_l2_slippage_bps`), вычитает комиссии и проверяет условие `LiqDistance >= 5 * StopDistance` при максимальном плече `4x`. |

---

## 3. Архитектура реализованной системы в папке `/Users/sergejrubcov/AItrade`

```text
AItrade/
├── README.md                        # Полное руководство по запуску, настройке Bybit Demo и стратегиям
├── ARCHITECTURE_ANALYSIS.md         # Данный аналитический разбор и математическая спецификация
├── .env.example                     # Шаблон ключей Bybit Demo Trading, Coinalyze, LLM и Telegram
├── requirements.txt                 # Зависимости (опциональные пакеты + работа на чистом Python 3)
├── config/
│   ├── settings.json                # Конфигурация пула 20 монет, риск-менеджмента, ML и сессий
│   └── freqtrade_freqai_bybit.json  # Готовый конфиг для интеграции с Freqtrade + FreqAI на Bybit Demo
├── aitrade/
│   ├── __init__.py
│   ├── config.py                    # Загрузчик настроек и параметров из settings.json / .env
│   ├── exchange/
│   │   ├── bybit_v5.py              # Клиент Bybit V5 REST (Mainnet Data + Demo Trading Execution + Paper Sim)
│   │   └── coinalyze.py             # Опциональный коннектор Coinalyze API для кросс-биржевого OI/CVD
│   ├── features/
│   │   ├── indicators.py            # Расчет EMA 8/21/50, ATR/NATR, ADX, RSI, Bollinger, Keltner, Session VWAP ±1..3σ
│   │   └── microstructure.py        # L2 VAMP, Weighted OBI, Savitzky-Golay фильтр, VPIN (TOF), CVD, Anti-Spoofing LBI
│   ├── screener/
│   │   └── coins_in_play.py         # 4-ступенчатый скринер: RVOL ≥ 1.5, NATR, ATR ratio ≤ 1.5, Spread < 0.10%, MTF Bias
│   ├── strategies/
│   │   ├── base.py                  # Базовая модель сигнала TradeSignal со спецификацией входа, SL, TP1/TP2, R:R
│   │   ├── vwap_keltner_mr.py       # Архетип 1: Возврат к среднему Keltner / Bollinger / VWAP ±2σ и VWAP Reclaim
│   │   ├── liquidity_sweep_fvg.py   # Архетип 2: Снятие ликвидности (Sweep 1-3 бара) + 15m FVG + дивергенция CVD
│   │   ├── oi_cvd_squeeze.py        # Архетип 3: Каскады ликвидаций, дельта OI, токсичный поток VPIN и истощение сквиза
│   │   ├── orb_breakout.py          # Архетип 4: Пробой утреннего диапазона сессии (EU/US Session ORB + 2-Bar Stall Rule)
│   │   └── lead_lag_density.py      # Архетип 5: Статистический Lead-Lag (BTC → Altcoins) и скальпинг от L2-плотностей
│   ├── ml/
│   │   └── meta_labeler.py          # Walk-Forward ML-валидатор, Dissimilarity Index (DI ≤ 1.0), Cost-Aware Filter (λ = 2.0)
│   ├── agents/
│   │   └── debate_council.py        # Мультиагентный совет (Bull vs Bear Researcher + Risk Judge + LLM/Rule-Based гибрид)
│   ├── risk/
│   │   └── risk_engine.py           # Leverage as Output, 5x Stop-to-Liquidation Rule, Dynamic 10-Level L2 Slippage
│   ├── execution/
│   │   ├── demo_trader.py           # Исполнитель сделок на Bybit Demo Trading + локальный трекер демо-портфеля SQLite
│   │   └── freqai_strategy.py       # Готовый класс стратегии IStrategy для прямого запуска внутри Freqtrade + FreqAI
│   ├── backtest/
│   │   └── walk_forward.py          # Walk-Forward бэктестер с учетом комиссий Bybit, L2-проскальзывания и Deflated Sharpe
│   └── dashboard/
│       └── server.py                # Интерактивный локальный веб-терминал аналитики, живых сигналов и демо-сделок
├── main.py                          # Единая точка входа CLI (scan, backtest, trade-demo, dashboard)
└── tests/
    └── test_combine.py              # Автотесты всех математических формул, стратегий, риск-движка и пайплайна
```
