# -*- coding: utf-8 -*-
from __future__ import annotations
import json, re, subprocess, time
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
        "listing": 35, "lists": 32, "upbit": 40, "bithumb": 35, "coinbase": 25,
        "etf": 30, "approved": 35, "approval": 30, "partnership": 25, "partners": 22,
        "mainnet": 25, "launch": 20, "launches": 20, "buyback": 30, "burn": 25,
        "accumulating": 22, "bought": 20, "bullish": 20, "breakout": 18,
        "grayscale": 25, "blackrock": 28, "microstrategy": 25, "saylor": 22,
        "airdrop": 18, "upgrade": 18, "integration": 20, "record high": 25, "ath": 22,
    }

    BEAR_WORDS: Dict[str, int] = {
        "hack": -55, "hacked": -55, "exploit": -55, "exploited": -55, "drained": -55,
        "delist": -45, "delisting": -45, "sec sues": -45, "lawsuit": -35, "investigation": -30,
        "halt": -40, "halted": -40, "insolvency": -55, "bankrupt": -55, "bankruptcy": -55,
        "unlock": -25, "dump": -25, "selling": -20, "sold": -18, "bearish": -20,
        "crash": -35, "ban": -35, "banned": -35, "reject": -30, "rejected": -30,
        "outflow": -22, "liquidated": -20, "phishing": -35, "stolen": -45,
    }

    PANIC_TRIGGERS = ["hack", "hacked", "exploit", "drained", "insolvency", "bankrupt", "binance halt", "bybit halt"]

    def __init__(self) -> None:
        self.enabled: bool = True
        self.block_on_panic: bool = True
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
            out = subprocess.check_output(
                ["curl", "-sS", "-m", "5", "-A", "Mozilla/5.0", url],
                text=True,
            )
            return json.loads(out)
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

    def _is_negated(self, text_low: str, keyword: str) -> bool:
        # Negation belongs to this occurrence and clause, not arbitrary nearby text.
        matches=list(re.finditer(r"\b"+re.escape(keyword)+r"\b",text_low))
        flags=[]
        for m in matches:
            before=re.split(r"[.!?;,]|\b(?:but|however|yet)\b",text_low[:m.start()])[-1]
            after=re.split(r"[.!?;,]",text_low[m.end():])[0]
            prefix=bool(re.search(r"\b(?:not|never|no|false|denies|denied|without)\b(?:\s+[a-z'-]+){0,3}\s*$",before))
            suffix=bool(re.match(r"\s+(?:(?:was|is|were|has been)\s+)?(?:prevented|denied|false|debunked)\b",after))
            flags.append(prefix or suffix)
        return bool(flags) and all(flags)

    def _score_text(self, text: str) -> Tuple[int, str, bool]:
        t_low = text.lower()
        score = 0
        triggers: List[str] = []
        is_panic = False

        for w, val in self.BULL_WORDS.items():
            if re.search(r"\b" + re.escape(w) + r"\b", t_low) and not self._is_negated(t_low, w):
                score += val
                triggers.append("+" + w)

        active_bear_words: List[str] = []
        for w, val in self.BEAR_WORDS.items():
            if re.search(r"\b" + re.escape(w) + r"\b", t_low) and not self._is_negated(t_low, w):
                score += val
                triggers.append("-" + w)
                active_bear_words.append(w)

        for pw in self.PANIC_TRIGGERS:
            if pw in active_bear_words and score <= -35:
                is_panic = True
                break

        score = max(-100, min(100, score))
        trig_str = ", ".join(triggers[:3]) if triggers else "Общий новостной поток"
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

        self.items = parsed_items[:35]

        # Агрегация сентимента по монетам (за последние 90 минут) и глобального фона
        coin_map: Dict[str, Dict[str, Any]] = {}
        recent_scores: List[int] = []
        panic_found = False
        panic_txt = ""

        for it in self.items:
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

        self.coin_sentiment = coin_map
        self.panic_active = panic_found
        self.panic_reason = panic_txt

        avg_sc = round(sum(recent_scores) / len(recent_scores), 1) if recent_scores else 0.0
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
        if not self.enabled:
            return {"score_bonus": 0.0, "allowed": True, "tag": "", "reason": "OK"}

        norm_dir = "LONG" if str(direction).upper() in ("LONG", "BUY") else "SHORT"

        if self.panic_active and self.block_on_panic and norm_dir == "LONG":
            return {
                "score_bonus": -25.0,
                "allowed": False,
                "tag": "🔴 NEWS PANIC",
                "reason": "Экстренный негатив в X/News: " + self.panic_reason,
            }

        c_info = self.coin_sentiment.get(symbol)
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
        if not self.enabled:
            return {"action": "HOLD", "reason": "OK"}

        is_long = str(side).upper() in ("BUY", "LONG")
        c_info = self.coin_sentiment.get(symbol) or {}
        sc = int(c_info.get("score", 0) or 0)
        trig = str(c_info.get("triggers", "") or "")
        headline = str(c_info.get("last_headline", "") or self.panic_reason or "")[:90]

        if is_long:
            if self.panic_active and self.block_on_panic:
                return {
                    "action": "CLOSE_FULL",
                    "reason": "Щит Black-Swan (PANIC): " + (self.panic_reason or "экстренный негатив"),
                }
            if sc <= -40:
                return {
                    "action": "CLOSE_FULL",
                    "reason": f"Критический негатив по {symbol} ({sc}: {trig}) — {headline}",
                }
            if sc <= -25 or self.global_score <= -20.0:
                return {
                    "action": "PROTECT",
                    "reason": f"Ухудшение новостного фона для LONG {symbol} (балл {sc})",
                }
        else:
            if sc >= 40:
                return {
                    "action": "CLOSE_FULL",
                    "reason": f"Сильный бычий импульс против SHORT {symbol} (+{sc}: {trig}) — {headline}",
                }
            if sc >= 25 or self.global_score >= 20.0:
                return {
                    "action": "PROTECT",
                    "reason": f"Бычий новостной фон против SHORT {symbol} (+{sc})",
                }

        return {"action": "HOLD", "reason": "OK"}

    def get_summary(self) -> Dict[str, Any]:
        bull_cnt = sum(1 for x in self.items if x.get("sentiment") == "BULLISH")
        bear_cnt = sum(1 for x in self.items if x.get("sentiment") == "BEARISH")
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
            "total_items": len(self.items),
            "coin_sentiment": self.coin_sentiment,
            "items": self.items[:30],
        }
