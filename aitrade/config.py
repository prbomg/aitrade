"""
Загрузчик конфигурации и переменных окружения (.env + config/settings.json).
"""

import json
import os
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "settings.json"
ENV_PATH = BASE_DIR / ".env"


def load_dotenv_file(env_path: Path = ENV_PATH) -> None:
    """Читает файл .env без внешних зависимостей и загружает переменные в os.environ."""
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def load_settings(config_path: Path = CONFIG_PATH) -> Dict[str, Any]:
    """Загружает настройки из JSON и дополняет их переменными окружения."""
    load_dotenv_file()
    with open(config_path, "r", encoding="utf-8") as f:
        settings: Dict[str, Any] = json.load(f)

    mode = os.environ.get("AITRADE_MODE", settings["exchange"].get("mode", "demo")).lower()
    settings["exchange"]["mode"] = mode
    settings["exchange"]["demo_api_key"] = os.environ.get("BYBIT_DEMO_API_KEY", "")
    settings["exchange"]["demo_api_secret"] = os.environ.get("BYBIT_DEMO_API_SECRET", "")
    settings["exchange"]["live_api_key"] = os.environ.get("BYBIT_LIVE_API_KEY", "")
    settings["exchange"]["live_api_secret"] = os.environ.get("BYBIT_LIVE_API_SECRET", "")
    settings["coinalyze_api_key"] = os.environ.get("COINALYZE_API_KEY", "")
    settings["openai_api_key"] = os.environ.get("OPENAI_API_KEY", "")
    settings["gemini_api_key"] = os.environ.get("GEMINI_API_KEY", "")
    settings["llm_model"] = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    settings["telegram_bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    settings["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", "")
    return settings
