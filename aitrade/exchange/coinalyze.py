"""
Опциональный клиент Coinalyze API (https://api.coinalyze.net/v1/) для агрегированного
кросс-биржевого открытого интереса (Binance + Bybit + OKX), ставок финансирования и ликвидаций.
Бесплатный лимит: 40 запросов в минуту (до 20 тикеров в одном вызове).
Если ключ COINALYZE_API_KEY не указан в .env, система прозрачно использует прямые потоки Bybit V5.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


class CoinalyzeClient:
    BASE_URL = "https://api.coinalyze.net/v1"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key.strip()

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _get(self, endpoint: str, params: Dict[str, Any], timeout: float = 6.0) -> Any:
        if not self.is_configured:
            return None
        params = dict(params)
        params["api_key"] = self.api_key
        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "AItrade-Combine/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_aggregated_open_interest(self, symbols: List[str]) -> Optional[List[Dict[str, Any]]]:
        """Запрашивает текущий открытый интерес по списку символов (до 20 за 1 вызов)."""
        if not self.is_configured or not symbols:
            return None
        coinalyze_syms = [f"{s}_PERP.A" for s in symbols[:20]]
        try:
            return self._get("open-interest", {"symbols": ",".join(coinalyze_syms)})
        except Exception:
            return None
