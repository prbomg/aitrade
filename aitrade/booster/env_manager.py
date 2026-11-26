# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from aitrade.exchange.transport import request
from aitrade.storage import atomic_json


class BybitEnvManager:
    """
    Менеджер контуров и изолированных субаккаунтов Bybit V5:
    - Поддерживает отдельные пары ключей для субаккаунтов `booster` и `autotrade`
      (как в `demo`, так и в `mainnet`).
    - Fallback на общий demo-ключ допускается только при `shared_demo_account=True`.
    """

    URLS = {
        "demo": "https://api-demo.bybit.com",
        "mainnet": "https://api.bybit.com",
    }
    ACCOUNTS = ("booster", "autotrade")

    def __init__(self, data_dir: Path, default_demo_key: str = "", default_demo_secret: str = "") -> None:
        self._lock = threading.RLock()
        self.vault_path = data_dir / "env_vault.json"
        self.state = self._load(default_demo_key, default_demo_secret)

    def _default_state(self, d_key: str, d_sec: str) -> Dict[str, Any]:
        return {
            "active_mode": "demo",
            "shared_demo_account": True,
            "demo": {
                "api_key": d_key,
                "api_secret": d_sec,
                "base_url": self.URLS["demo"],
            },
            "mainnet": {
                "api_key": "",
                "api_secret": "",
                "base_url": self.URLS["mainnet"],
            },
            "subaccounts": {
                "booster": {
                    "demo": {"api_key": "", "api_secret": ""},
                    "mainnet": {"api_key": "", "api_secret": ""},
                },
                "autotrade": {
                    "demo": {"api_key": "", "api_secret": ""},
                    "mainnet": {"api_key": "", "api_secret": ""},
                },
            },
            "last_checklist": {
                "mode": "mainnet",
                "passed": False,
                "checked_at": "—",
                "checks": [],
                "summary": "Предторговая проверка Real Mainnet еще не запускалась",
            },
        }

    def _load(self, d_key: str, d_sec: str) -> Dict[str, Any]:
        st = self._default_state(d_key, d_sec)
        if self.vault_path.exists():
            try:
                raw = json.loads(self.vault_path.read_text(encoding="utf-8"))
                st.update(raw)
                st.setdefault("subaccounts", self._default_state("", "")["subaccounts"])
                for acc in self.ACCOUNTS:
                    st["subaccounts"].setdefault(
                        acc, {"demo": {"api_key": "", "api_secret": ""}, "mainnet": {"api_key": "", "api_secret": ""}}
                    )
                if not st.get("demo", {}).get("api_key") and d_key:
                    st["demo"]["api_key"] = d_key
                    st["demo"]["api_secret"] = d_sec
            except Exception:
                pass
        self._save(st)
        return st

    def _save(self, st: Dict[str, Any], raise_on_error: bool = False) -> None:
        try:
            atomic_json(self.vault_path, st)
            os.chmod(self.vault_path, 0o600)
        except Exception:
            if raise_on_error:
                raise

    @staticmethod
    def mask_key(k: str) -> str:
        k = str(k or "").strip()
        if len(k) <= 6:
            return "Не задан" if not k else ("***" + k[-2:])
        return k[:4] + "••••" + k[-4:]

    def get_active_credentials(self) -> Dict[str, str]:
        with self._lock:
            mode = self.state.get("active_mode", "demo")
            if mode not in ("demo", "mainnet"):
                mode = "demo"
            block = dict(self.state.get(mode, {}))
        return {
            "mode": mode,
            "api_key": str(block.get("api_key", "") or "").strip(),
            "api_secret": str(block.get("api_secret", "") or "").strip(),
            "base_url": self.URLS.get(mode, self.URLS["demo"]),
        }

    def get_account_credentials(self, account: str = "autotrade") -> Dict[str, Any]:
        """
        Возвращает ключи конкретного субаккаунта (`booster` или `autotrade`).
        Если отдельные ключи субаккаунта не заданы и активен `demo` с `shared_demo_account=True`,
        выполняется явный fallback на общий demo-ключ с пометкой `isolated=False`.
        """
        acc = account if account in self.ACCOUNTS else "autotrade"
        with self._lock:
            mode = self.state.get("active_mode", "demo")
            if mode not in ("demo", "mainnet"):
                mode = "demo"
            sub = dict(((self.state.get("subaccounts") or {}).get(acc) or {}).get(mode) or {})
            shared_demo = bool(self.state.get("shared_demo_account", True))
        sk = str(sub.get("api_key", "") or "").strip()
        ss = str(sub.get("api_secret", "") or "").strip()
        if sk and ss:
            return {
                "account": acc,
                "mode": mode,
                "api_key": sk,
                "api_secret": ss,
                "base_url": self.URLS[mode],
                "isolated": True,
            }
        if mode == "demo" and shared_demo:
            base = self.get_active_credentials()
            return {
                "account": acc,
                "mode": mode,
                "api_key": base["api_key"],
                "api_secret": base["api_secret"],
                "base_url": base["base_url"],
                "isolated": False,
            }
        return {
            "account": acc,
            "mode": mode,
            "api_key": "",
            "api_secret": "",
            "base_url": self.URLS[mode],
            "isolated": False,
        }

    def save_subaccount_keys(
        self, account: str, target_mode: str, api_key: str, api_secret: str
    ) -> Dict[str, Any]:
        if account not in self.ACCOUNTS or target_mode not in ("demo", "mainnet"):
            return {"ok": False, "retMsg": "Некорректный субаккаунт или контур"}
        with self._lock:
            sub = self.state.setdefault("subaccounts", {}).setdefault(
                account, {"demo": {"api_key": "", "api_secret": ""}, "mainnet": {"api_key": "", "api_secret": ""}}
            )
            cur = sub.setdefault(target_mode, {})
            if api_key and "••••" not in api_key:
                cur["api_key"] = api_key.strip()
            if api_secret and "••••" not in api_secret:
                cur["api_secret"] = api_secret.strip()
            try:
                self._save(self.state, raise_on_error=True)
            except Exception as exc:
                return {"ok": False, "retMsg": "Ошибка записи env_vault.json: " + str(exc)[:140]}
            masked = self.mask_key(cur.get("api_key", ""))
        return {
            "ok": True,
            "account": account,
            "mode": target_mode,
            "masked_key": masked,
        }

    def save_keys(self, target_mode: str, api_key: str, api_secret: str) -> Dict[str, Any]:
        if target_mode not in ("demo", "mainnet"):
            return {"ok": False, "retMsg": "Некорректный контур"}
        with self._lock:
            cur = self.state.setdefault(target_mode, {})
            if api_key and "••••" not in api_key:
                cur["api_key"] = api_key.strip()
            if api_secret and "••••" not in api_secret:
                cur["api_secret"] = api_secret.strip()
            cur["base_url"] = self.URLS[target_mode]
            try:
                self._save(self.state, raise_on_error=True)
            except Exception as exc:
                return {"ok": False, "retMsg": "Ошибка записи env_vault.json: " + str(exc)[:140]}
            masked = self.mask_key(cur.get("api_key", ""))
        return {"ok": True, "mode": target_mode, "masked_key": masked}

    @staticmethod
    def _signed_get(base_url: str, api_key: str, api_secret: str, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return request(base_url, api_key, api_secret, "GET", endpoint, params)
        except Exception as exc:
            return {"retCode": -1, "retMsg": str(exc)}

    def run_preflight_checklist(self, target_mode: str = "mainnet", account: str = "shared") -> Dict[str, Any]:
        acc = str(account or "shared").strip().lower()
        with self._lock:
            if acc in self.ACCOUNTS:
                block = dict(((self.state.get("subaccounts") or {}).get(acc) or {}).get(target_mode) or {})
                scope_label = f"{acc.upper()}/{target_mode.upper()}"
            else:
                acc = "shared"
                block = dict(self.state.get(target_mode, {}) or {})
                scope_label = target_mode.upper()
        api_key = str(block.get("api_key", "") or "").strip()
        api_secret = str(block.get("api_secret", "") or "").strip()
        base_url = self.URLS.get(target_mode, self.URLS["mainnet"])

        checks = []
        all_passed = True

        has_keys = bool(api_key and api_secret)
        checks.append({
            "id": "keys_present",
            "title": "1. Наличие API-ключей в защищенном хранилище (" + scope_label + ")",
            "ok": has_keys,
            "detail": ("Ключ " + self.mask_key(api_key) + " загружен") if has_keys else "Ключи для выбранного контура/субаккаунта не введены",
        })
        if not has_keys:
            res = {
                "mode": target_mode,
                "account": acc,
                "passed": False,
                "checked_at": datetime.now().strftime("%H:%M:%S"),
                "checks": checks,
                "summary": "❌ Проверка не пройдена: сначала введите и сохраните API Key и Secret для " + scope_label,
            }
            with self._lock:
                self.state["last_checklist"] = res
                self._save(self.state)
            return res

        key_info_resp = self._signed_get(base_url, api_key, api_secret, "/v5/user/query-api", {})
        ret_code = key_info_resp.get("retCode", -1)
        k_res = key_info_resp.get("result", {}) or {}
        perms = k_res.get("permissions", {}) or {}

        auth_ok = (ret_code == 0)
        checks.append({
            "id": "auth_valid",
            "title": "2. Подлинность подписи и связь с Bybit (" + base_url.replace("https://", "") + ")",
            "ok": auth_ok,
            "detail": ("Связь установлена, UserID: " + str(k_res.get("userID", "OK")) + (", ReadOnly=Нет" if k_res.get("readOnly") == 0 else "")) if auth_ok else ("Ошибка API: " + str(key_info_resp.get("retMsg", "Неверный ключ"))),
        })
        if not auth_ok:
            all_passed = False

        contract_perms = perms.get("ContractTrade", []) or []
        derivatives_perms = perms.get("Derivatives", []) or []
        read_only = int(k_res.get("readOnly", 0) or 0)
        can_trade_futures = auth_ok and (read_only == 0) and (len(contract_perms) > 0 or len(derivatives_perms) > 0)
        checks.append({
            "id": "futures_perm",
            "title": "3. Разрешение на торговлю фьючерсами (ContractTrade: Order/Position)",
            "ok": can_trade_futures,
            "detail": ("Права активны: " + ", ".join(contract_perms or derivatives_perms)) if can_trade_futures else "У ключа нет прав на фьючерсы или включен режим Только-Чтение",
        })
        if not can_trade_futures:
            all_passed = False

        wallet_perms = perms.get("Wallet", []) or []
        has_withdraw = any("withdraw" in str(p).lower() for p in wallet_perms)
        no_withdraw_ok = auth_ok and (not has_withdraw)
        checks.append({
            "id": "no_withdraw",
            "title": "4. Безопасность средств: запрет на вывод (No Withdraw Permission)",
            "ok": no_withdraw_ok,
            "detail": "Безопасно: право на вывод средств (Withdraw) отключено" if no_withdraw_ok else "⚠️ ОПАСНО: у ключа включено право Withdraw! Отключите его в настройках Bybit",
        })
        if not no_withdraw_ok:
            all_passed = False

        bal_resp = self._signed_get(base_url, api_key, api_secret, "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        bal_ok = (bal_resp.get("retCode") == 0)
        lst = bal_resp.get("result", {}).get("list", []) if bal_ok else []
        total_eq = float(lst[0].get("totalEquity", 0) or 0) if lst else 0.0
        min_eq_ok = bal_ok and (total_eq >= 20.0)
        checks.append({
            "id": "uta_balance",
            "title": "5. Единый торговый аккаунт (UTA UNIFIED) и депозит >= $20",
            "ok": min_eq_ok,
            "detail": ("UTA активен, доступный капитал: $" + "{:,.2f}".format(total_eq)) if bal_ok else ("Не удалось получить баланс UTA: " + str(bal_resp.get("retMsg", ""))),
        })
        if not min_eq_ok:
            all_passed = False

        summary_txt = (
            "✅ ВСЕ 5 ПРОВЕРОК ПРОЙДЕНЫ! Контур " + scope_label + " полностью готов к работе ($" + "{:,.2f}".format(total_eq) + ")."
            if all_passed
            else "🛑 Предторговый чек-лист (" + scope_label + ") не пройден. Исправьте отмеченные пункты перед включением боевого режима."
        )
        res = {
            "mode": target_mode,
            "account": acc,
            "passed": all_passed,
            "checked_at": datetime.now().strftime("%H:%M:%S"),
            "equity_usd": round(total_eq, 2),
            "checks": checks,
            "summary": summary_txt,
        }
        with self._lock:
            self.state["last_checklist"] = res
            self._save(self.state)
        return res

    def switch_environment(self, target_mode: str, force_demo: bool = False) -> Dict[str, Any]:
        if target_mode not in ("demo", "mainnet"):
            return {"ok": False, "retMsg": "Неизвестный контур"}
        if target_mode == "demo" or force_demo:
            with self._lock:
                self.state["active_mode"] = "demo"
                self._save(self.state)
            return {"ok": True, "active_mode": "demo", "retMsg": "Переключено на учебный контур 🧪 Bybit Demo"}

        chk = self.run_preflight_checklist("mainnet")
        if not chk.get("passed"):
            return {
                "ok": False,
                "active_mode": self.state.get("active_mode", "demo"),
                "checklist": chk,
                "retMsg": chk.get("summary", "Чек-лист не пройден"),
            }
        with self._lock:
            self.state["active_mode"] = "mainnet"
            self._save(self.state)
        return {
            "ok": True,
            "active_mode": "mainnet",
            "checklist": chk,
            "retMsg": "💰 БОЕВОЙ КОНТУР BYBIT REAL MAINNET АКТИВИРОВАН!",
        }

    def get_public_status(self) -> Dict[str, Any]:
        with self._lock:
            act = self.state.get("active_mode", "demo")
            b_creds = self.get_account_credentials("booster")
            a_creds = self.get_account_credentials("autotrade")
            return {
                "active_mode": act,
                "is_mainnet": act == "mainnet",
                "shared_demo_account": bool(self.state.get("shared_demo_account", True)),
                "active_url": self.URLS.get(act, self.URLS["demo"]),
                "demo_key_masked": self.mask_key(self.state.get("demo", {}).get("api_key", "")),
                "mainnet_key_masked": self.mask_key(self.state.get("mainnet", {}).get("api_key", "")),
                "mainnet_configured": bool(self.state.get("mainnet", {}).get("api_key")),
                "subaccounts": {
                    "booster": {
                        "isolated": b_creds["isolated"],
                        "key_masked": self.mask_key(b_creds["api_key"]),
                    },
                    "autotrade": {
                        "isolated": a_creds["isolated"],
                        "key_masked": self.mask_key(a_creds["api_key"]),
                    },
                },
                "last_checklist": dict(self.state.get("last_checklist", {})),
            }
