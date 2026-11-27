# -*- coding: utf-8 -*-
from __future__ import annotations
import json, re, threading, time, urllib.request
from datetime import datetime
from typing import Any, Dict, List, Tuple


class XNewsSentimentRadar:
    COIN_ALIASES: Dict[str, List[str]] = {
        "BTCUSDT": ["btc", "bitcoin", "биткоин"],
        "ETHUSDT": ["eth", "ethereum", "vitalik"],
        "SOLUSDT": ["sol", "solana"],
        "XRPUSDT": ["xrp", "ripple", "rlusd"],
        "BNBUSDT": ["bnb", "binance"],
        "TRXUSDT": ["trx", "tron", "justin sun"],
        "SUIUSDT": ["sui"],
        "TONUSDT": ["ton", "toncoin", "telegram", "durov"],
        "ADAUSDT": ["ada", "cardano"],
        "AVAXUSDT": ["avax", "avalanche"],
        "DOTUSDT": ["dot", "polkadot"],
        "NEARUSDT": ["near"],
        "APTUSDT": ["apt", "aptos"],
        "SEIUSDT": ["sei"],
        "TIAUSDT": ["tia", "celestia"],
        "ARBUSDT": ["arb", "arbitrum"],
        "FETUSDT": ["fet", "asi", "fetch"],
        "RENDERUSDT": ["render", "rndr"],
        "TAOUSDT": ["tao", "bittensor"],
        "WLDUSDT": ["wld", "worldcoin", "sam altman", "openai"],
        "INJUSDT": ["inj", "injective"],
        "FILUSDT": ["fil", "filecoin"],
        "ICPUSDT": ["icp", "internet computer"],
        "LINKUSDT": ["link", "chainlink"],
        "AAVEUSDT": ["aave"],
        "UNIUSDT": ["uni", "uniswap"],
        "ONDOUSDT": ["ondo", "rwa", "blackrock"],
        "ENAUSDT": ["ena", "ethena", "usde"],
        "PENDLEUSDT": ["pendle"],
        "JUPUSDT": ["jup", "jupiter"],
        "DOGEUSDT": ["doge", "dogecoin", "elon", "musk", "d.o.g.e"],
        "1000PEPEUSDT": ["pepe"],
        "WIFUSDT": ["wif", "dogwifhat"],
        "1000BONKUSDT": ["bonk"],
        "POPCATUSDT": ["popcat"],
        "ORDIUSDT": ["ordi", "ordinals"],
        "ZECUSDT": ["zec", "zcash"],
        "SOXLUSDT": ["soxl", "semiconductor", "nvidia", "nvda"],
    }

    BULL_WORDS: Dict[str, int] = {
        "listing": 35, "lists": 32, "listed": 28, "upbit": 40, "bithumb": 35, "coinbase": 25,
        "etf inflow*": 40, "inflow*": 25, "etf": 30, "approv*": 32, "partner*": 24,
        "mainnet": 25, "launch*": 20, "buyback*": 30, "burn*": 22,
        "accumulat*": 22, "bought": 20, "bullish": 20, "breakout": 18,
        "grayscale": 25, "blackrock": 28, "microstrategy": 25, "saylor": 22,
        "airdrop*": 18, "upgrade*": 18, "integrat*": 20, "record high": 25, "ath": 22, "all-time high": 25,
        "rate cut*": 35, "fed cut*": 35, "dovish": 25, "stimulus": 25,
        "cpi lower": 30, "cpi cools": 30, "cooling cpi": 30, "cpi below": 28,
        "tariff pause*": 25, "tariffs lifted": 25, "tariffs dropped": 25,
    }

    BEAR_WORDS: Dict[str, int] = {
        "hack*": -55, "exploit*": -55, "breach*": -50, "drain*": -55, "stolen": -45,
        "rugpull*": -55, "rug pull": -55, "compromis*": -45, "phish*": -35, "scam*": -40, "fraud*": -45,
        "delist*": -45, "depeg*": -55, "insolven*": -55, "bankrupt*": -55,
        "withdrawals paused": -50, "withdrawals suspended": -50, "halt*": -40, "frozen": -35,
        "sec sues": -45, "sec charges": -45, "sec lawsuit": -45, "sec probe": -40, "sec investigation": -40,
        "sec": -15, "lawsuit*": -35, "investigat*": -30, "indict*": -45, "arrest*": -40,
        "sanction*": -40, "subpoena*": -35, "crackdown": -35, "fined": -30, "ban": -35, "banned": -35, "reject*": -30,
        "etf outflow*": -45, "outflow*": -28,
        "flash crash": -40, "crash*": -35, "plunge*": -28, "tumble*": -25, "drops to": -20,
        "unlock*": -25, "dump*": -25, "selling": -20, "sold": -18, "bearish": -20, "liquidat*": -20,
        "tariff*": -25, "trade war": -35, "rate hike*": -35, "fed hawkish": -35, "hawkish": -25,
        "hot cpi": -30, "cpi higher": -30, "cpi above": -28, "inflation surge*": -30, "recession": -30,
    }

    # Глобальный PANIC только по системным биржевым инцидентам и крупным стейблкоинам
    PANIC_TRIGGERS = ["insolven*", "bankrupt*", "binance halt", "bybit halt", "exchange halt", "withdrawals suspended", "usdt depeg*", "usdc depeg*", "tether depeg*", "major stablecoin depeg*"]

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.enabled: bool = True
        self.block_on_panic: bool = True
        self.allow_emergency_close: bool = False
        self.items: List[Dict[str, Any]] = []
        self.coin_sentiment: Dict[str, Dict[str, Any]] = {}
        self.global_score: float = 0.0
        self.global_state: str = "NEUTRAL"
        self.global_label: str = "🐦 X/News: ⚖️ NEUTRAL (0)"
        self.panic_active: bool = False
        self.panic_reason: str = ""
        self._last_fetch_ts: float = 0.0

    @staticmethod
    def _curl_json(url: str) -> Any:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.load(r)
        except Exception:
            return None

    def _detect_symbols(self, text: str, raw_coins: List[str]) -> List[str]:
        matched: List[str] = []
        t_low = text.lower()
        for rc in raw_coins:
            rc_clean = re.sub(r"[^A-Z0-9]", "", str(rc).upper())
            if not rc_clean:
                continue
            cand1 = rc_clean if rc_clean.endswith("USDT") else (rc_clean + "USDT")
            cand2 = "1000" + cand1
            if cand1 in self.COIN_ALIASES and cand1 not in matched:
                matched.append(cand1)
            elif cand2 in self.COIN_ALIASES and cand2 not in matched:
                matched.append(cand2)

        for sym, aliases in self.COIN_ALIASES.items():
            if sym in matched:
                continue
            for al in aliases:
                pattern = r"(?:^|[\s\$\#\(\[\.,!?:;])" + re.escape(al) + r"(?:$|[\s\)\]\.,!?:;])"
                if re.search(pattern, t_low):
                    matched.append(sym)
                    break
        return matched[:3]

    NEGATION_PATTERNS = (
        r"\b(?:not|no|never|neither|without|false|fake|rumor|debunked|denied|averted|prevented|mitigated|patched|resolved)\b",
    )

    @staticmethod
    def _term_pattern(term: str) -> str:
        parts = term.split()
        regex_parts = []
        for p in parts:
            if p.endswith("*"):
                regex_parts.append(re.escape(p[:-1]) + r"[a-z]*")
            else:
                regex_parts.append(re.escape(p))
        return r"\b" + r"\s+".join(regex_parts) + r"\b"

    def _is_negated(self, text_low: str, keyword: str) -> bool:
        # Negation belongs to this occurrence and clause, not arbitrary nearby text.
        matches=list(re.finditer(self._term_pattern(keyword),text_low))
        flags=[]
        for m in matches:
            before=re.split(r"[.!?;,]|\b(?:but|however|yet)\b",text_low[:m.start()])[-1]
            after=re.split(r"[.!?;,]",text_low[m.end():])[0]
            prefix=bool(re.search(r"\b(?:not|never|no|false|denies|denied|without)\b(?:\s+[a-z'-]+){0,3}\s*$",before))
            suffix=bool(re.match(r"(?:\s+[a-z'-]+){0,2}\s+(?:(?:was|is|were|are|has been)\s+)?(?:not|prevented|denied|false|debunked|averted|resolved|patched)\b",after))
            flags.append(prefix or suffix)
        return bool(flags) and all(flags)

    def _score_text(self, text: str) -> Tuple[int, str, bool]:
        t_low = text.lower()
        score = 0
        triggers: List[str] = []
        is_panic = False

        has_outflow = bool(re.search(r"\boutflows?\b", t_low))
        has_reject = bool(re.search(r"\breject[a-z]*\b|\bdelay[a-z]*\b", t_low))
        has_approv = bool(re.search(r"\bapprov[a-z]*\b|\bdismiss[a-z]*\b|\bdrops?\s+lawsuit\b", t_low))
        has_tariff_relief = bool(re.search(r"\btariff[a-z]*\s+(?:pause[a-z]*|lifted|dropped|delay[a-z]*)\b", t_low))

        matched_bull: List[str] = []
        for w, val in self.BULL_WORDS.items():
            if w == "etf" and (has_outflow or has_reject):
                continue
            if w == "inflow*" and "etf inflow*" in matched_bull:
                continue
            if re.search(self._term_pattern(w), t_low) and not self._is_negated(t_low, w):
                score += val
                triggers.append("+" + w.rstrip("*"))
                matched_bull.append(w)

        active_bear_words: List[str] = []
        for w, val in self.BEAR_WORDS.items():
            if w == "outflow*" and "etf outflow*" in active_bear_words:
                continue
            if w == "sec" and (has_approv or any(k.startswith("sec ") for k in active_bear_words)):
                continue
            if w == "tariff*" and has_tariff_relief:
                continue
            if w == "crash*" and "flash crash" in active_bear_words:
                continue
            if re.search(self._term_pattern(w), t_low) and not self._is_negated(t_low, w):
                score += val
                triggers.append("-" + w.rstrip("*"))
                active_bear_words.append(w)

        for pw in self.PANIC_TRIGGERS:
            if (pw in active_bear_words or re.search(self._term_pattern(pw), t_low)) and not self._is_negated(t_low, pw) and score <= -35:
                is_panic = True
                break

        score = max(-100, min(100, score))
        trig_str = ", ".join(triggers[:4]) if triggers else "Общий новостной поток"
        return score, trig_str, is_panic

    def refresh_feed(self, force: bool = False) -> Dict[str, Any]:
        now = time.time()
        if not force and (now - self._last_fetch_ts < 20.0) and self.items:
            return self.get_summary()
        self._last_fetch_ts = now

        raw_list = self._curl_json("https://news.treeofalpha.com/api/news?limit=40")
        parsed_items: List[Dict[str, Any]] = []

        if isinstance(raw_list, list) and len(raw_list) > 0:
            for item in raw_list[:40]:
                title = str(item.get("title", "") or item.get("body", "") or "").strip()
                body = str(item.get("body", "") or "").strip()
                full_txt = (title + " " + body).strip()
                if not full_txt:
                    continue
                src_type = str(item.get("source", "") or "X/Twitter")
                info = item.get("info", {}) or {}
                author = str(info.get("twitterId", "") or item.get("icon", "") or src_type)
                ts_ms = int(item.get("time", 0) or (now * 1000))
                age_min = max(0, int((now * 1000 - ts_ms) / 60000))
                time_str = datetime.fromtimestamp(ts_ms / 1000.0).strftime("%H:%M:%S") if ts_ms > 0 else "—"
                url = str(item.get("url", "") or item.get("link", "") or "")

                raw_coins = []
                if isinstance(item.get("coins"), list):
                    for c in item["coins"]:
                        if isinstance(c, dict) and c.get("coin"):
                            raw_coins.append(str(c["coin"]))
                        elif isinstance(c, str):
                            raw_coins.append(c)
                if item.get("coin"):
                    raw_coins.append(str(item.get("coin")))

                syms = self._detect_symbols(full_txt, raw_coins)
                score, trig_str, is_panic = self._score_text(full_txt)

                sentiment = "NEUTRAL"
                if score >= 18:
                    sentiment = "BULLISH"
                elif score <= -18:
                    sentiment = "BEARISH"

                clean_headline = re.sub(r"\s+", " ", title if len(title) > 10 else full_txt)[:190]
                parsed_items.append({
                    "time": time_str,
                    "age_min": age_min,
                    "source": "🐦 X/Twitter" if ("twitter" in src_type.lower() or info.get("isTwitter") or "@" in clean_headline) else ("📰 " + src_type[:16]),
                    "headline": clean_headline,
                    "symbols": syms,
                    "primary_symbol": syms[0] if syms else "BTCUSDT",
                    "score": score,
                    "sentiment": sentiment,
                    "triggers": trig_str,
                    "is_panic": is_panic,
                    "url": url,
                })

        # Резервный источник CryptoCompare News, если Tree of Alpha временно недоступен
        if not parsed_items:
            cc_data = self._curl_json("https://min-api.cryptocompare.com/data/v2/news/?lang=EN")
            cc_list = (cc_data or {}).get("Data", []) if isinstance(cc_data, dict) else []
            for item in (cc_list if isinstance(cc_list, list) else [])[:25]:
                title = str(item.get("title", "") or "").strip()
                cats = str(item.get("categories", "") or "").split("|")
                ts_sec = int(item.get("published_on", int(now)))
                age_min = max(0, int((now - ts_sec) / 60))
                syms = self._detect_symbols(title, cats)
                score, trig_str, is_panic = self._score_text(title)
                sentiment = "BULLISH" if score >= 18 else ("BEARISH" if score <= -18 else "NEUTRAL")
                parsed_items.append({
                    "time": datetime.fromtimestamp(ts_sec).strftime("%H:%M:%S"),
                    "age_min": age_min,
                    "source": "📰 " + str(item.get("source", "CryptoNews"))[:16],
                    "headline": title[:190],
                    "symbols": syms,
                    "primary_symbol": syms[0] if syms else "BTCUSDT",
                    "score": score,
                    "sentiment": sentiment,
                    "triggers": trig_str,
                    "is_panic": is_panic,
                    "url": str(item.get("url", "")),
                })

        top_items = parsed_items[:35]
        # Агрегация сентимента по монетам (за последние 90 минут) и глобального фона
        coin_map: Dict[str, Dict[str, Any]] = {}
        recent_scores: List[int] = []
        panic_found = False
        panic_txt = ""

        for it in top_items:
            if it["age_min"] <= 90:
                if it["score"] != 0:
                    recent_scores.append(it["score"])
                if it["is_panic"] and it["age_min"] <= 35:
                    panic_found = True
                    panic_txt = it["headline"][:90]
                for s in it["symbols"]:
                    entry = coin_map.setdefault(s, {"score": 0, "count": 0, "last_headline": it["headline"], "triggers": it["triggers"]})
                    entry["score"] = max(-100, min(100, entry["score"] + it["score"]))
                    entry["count"] += 1

        avg_sc = round(sum(recent_scores) / len(recent_scores), 1) if recent_scores else 0.0
        with self._lock:
            self.items = top_items
            self.coin_sentiment = coin_map
            self.panic_active = panic_found
            self.panic_reason = panic_txt
            self.global_score = avg_sc

            if not self.enabled:
                self.global_state = "DISABLED"
                self.global_label = "🐦 X/News: ВЫКЛ"
            elif self.panic_active:
                self.global_state = "PANIC"
                self.global_label = "🔴 X/News: PANIC (" + str(avg_sc) + ")"
            elif avg_sc >= 12.0:
                self.global_state = "BULLISH"
                self.global_label = "🟢 X/News: BULLISH (+" + str(avg_sc) + ")"
            elif avg_sc <= -12.0:
                self.global_state = "BEARISH"
                self.global_label = "🟠 X/News: BEARISH (" + str(avg_sc) + ")"
            else:
                self.global_state = "NEUTRAL"
                self.global_label = "🐦 X/News: NEUTRAL (" + ("+" if avg_sc > 0 else "") + str(avg_sc) + ")"

        return self.get_summary()

    def get_coin_modifier(self, symbol: str, direction: str) -> Dict[str, Any]:
        with self._lock:
            enabled = self.enabled
            panic_active = self.panic_active
            block_on_panic = self.block_on_panic
            panic_reason = self.panic_reason
            raw_info = self.coin_sentiment.get(symbol)
            c_info = dict(raw_info) if isinstance(raw_info, dict) else None
        if not enabled:
            return {"score_bonus": 0.0, "allowed": True, "tag": "", "reason": "OK"}

        norm_dir = "LONG" if str(direction).upper() in ("LONG", "BUY") else "SHORT"

        if panic_active and block_on_panic and norm_dir == "LONG":
            return {
                "score_bonus": -25.0,
                "allowed": False,
                "tag": "🔴 NEWS PANIC",
                "reason": "Экстренный негатив в X/News: " + panic_reason,
            }

        if not c_info:
            return {"score_bonus": 0.0, "allowed": True, "tag": "", "reason": "OK"}

        sc = int(c_info.get("score", 0))
        if norm_dir == "LONG":
            if sc <= -30:
                return {
                    "score_bonus": -15.0,
                    "allowed": False,
                    "tag": "🔴 X-FUD (" + str(sc) + ")",
                    "reason": "Медвежий новостной фон по " + symbol + " (" + str(c_info.get("triggers", "")) + ")",
                }
            if sc >= 20:
                return {
                    "score_bonus": 10.0,
                    "allowed": True,
                    "tag": "🐦 X-BULL (+" + str(sc) + ")",
                    "reason": "Подтверждено позитивом в X/News (" + str(c_info.get("triggers", "")) + ")",
                }
        else:
            if sc >= 35:
                return {
                    "score_bonus": -15.0,
                    "allowed": False,
                    "tag": "🟢 X-PUMP (+" + str(sc) + ")",
                    "reason": "Сильный бычий новостной фон по " + symbol + " — запрет шорта против новости",
                }
            if sc <= -20:
                return {
                    "score_bonus": 10.0,
                    "allowed": True,
                    "tag": "🐦 X-BEAR (" + str(sc) + ")",
                    "reason": "Шорт усилен негативом в X/News (" + str(c_info.get("triggers", "")) + ")",
                }

        return {"score_bonus": 0.0, "allowed": True, "tag": "", "reason": "OK"}

    def evaluate_open_position(self, symbol: str, side: str) -> Dict[str, Any]:
        """Оценивает открытую позицию (Buy/Sell) против живого потока X/News для защиты или экстренного выхода."""
        with self._lock:
            enabled = self.enabled
            allow_emergency_close = self.allow_emergency_close
            panic_active = self.panic_active
            block_on_panic = self.block_on_panic
            panic_reason = self.panic_reason
            global_score = self.global_score
            c_info = dict(self.coin_sentiment.get(symbol) or {})
        if not enabled:
            return {"action": "HOLD", "reason": "OK"}

        is_long = str(side).upper() in ("BUY", "LONG")
        sc = int(c_info.get("score", 0) or 0)
        trig = str(c_info.get("triggers", "") or "")
        headline = str(c_info.get("last_headline", "") or panic_reason or "")[:90]

        close_act = "CLOSE_FULL" if allow_emergency_close else "HOLD"
        if is_long:
            if panic_active and block_on_panic:
                return {
                    "action": close_act,
                    "reason": "Наблюдение PANIC (автозакрытие отключено): " + (panic_reason or "экстренный негатив"),
                }
            if sc <= -40:
                return {
                    "action": close_act,
                    "reason": f"Наблюдение негатива по {symbol} ({sc}: {trig}) — {headline}",
                }
            if sc <= -25 or global_score <= -20.0:
                return {
                    "action": "PROTECT",
                    "reason": f"Ухудшение новостного фона для LONG {symbol} (балл {sc})",
                }
        else:
            if sc >= 40:
                return {
                    "action": close_act,
                    "reason": f"Наблюдение бычьего фона против SHORT {symbol} (+{sc}: {trig}) — {headline}",
                }
            if sc >= 25 or global_score >= 20.0:
                return {
                    "action": "PROTECT",
                    "reason": f"Бычий новостной фон против SHORT {symbol} (+{sc})",
                }

        return {"action": "HOLD", "reason": "OK"}

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            items_snap = list(self.items)
            coin_snap = dict(self.coin_sentiment)
            bull_cnt = sum(1 for x in items_snap if x.get("sentiment") == "BULLISH")
            bear_cnt = sum(1 for x in items_snap if x.get("sentiment") == "BEARISH")
            return {
                "enabled": self.enabled,
                "block_on_panic": self.block_on_panic,
                "global_score": self.global_score,
                "global_state": self.global_state,
                "global_label": self.global_label,
                "panic_active": self.panic_active,
                "panic_reason": self.panic_reason,
                "bullish_count": bull_cnt,
                "bearish_count": bear_cnt,
                "total_items": len(items_snap),
                "coin_sentiment": coin_snap,
                "items": items_snap[:30],
            }
