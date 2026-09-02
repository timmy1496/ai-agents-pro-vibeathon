"""Єдине місце для env-налаштувань. Без класів конфігу — їх тут нема що інкапсулювати."""
import os
import pathlib

from dotenv import load_dotenv

load_dotenv()

ROOT = pathlib.Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "kb"
CATALOG_FILE = ROOT / "catalog" / "services.yaml"
DATA_DIR = ROOT / "data"

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000")
GRAFANA_AUTH = os.getenv("GRAFANA_AUTH", "admin:admin")
ALERTMANAGER_URL = os.getenv("ALERTMANAGER_URL", "http://localhost:9093")
KB_COLLECTION = os.getenv("KB_COLLECTION", "sre_kb")

# Локальні ONNX-моделі через fastembed: без API-викликів, працює офлайн.
# e5-large (1024d, 2.24GB) а не MiniLM (384d, 0.22GB) — виміряно, не за відчуттям:
# на MiniLM "інциденти з OOMKilled" дає косинус 0.208, а "політика відпусток" 0.238,
# тобто релевантне і стороннє не розділяються порогом узагалі. На e5-large ті самі
# запити: 0.87 проти 0.79. Без цього розділення fail-closed неможливий у принципі.
# Префікси "query:"/"passage:" fastembed підставляє сам — вручну нічого не додаємо.
DENSE_MODEL = os.getenv("KB_DENSE_MODEL", "intfloat/multilingual-e5-large")
SPARSE_MODEL = os.getenv("KB_SPARSE_MODEL", "Qdrant/bm25")

# Провайдер визначається за ключем: sk-or-... це OpenRouter, він говорить
# OpenAI-сумісним протоколом, а не Anthropic-нативним, тому клієнт інший.
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY") or (
    os.getenv("ANTHROPIC_API_KEY", "") if os.getenv("ANTHROPIC_API_KEY", "").startswith("sk-or-") else "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# Каскад моделей: дешева на router/grade/judge, сильна на RCA-синтез.
CHEAP_MODEL = os.getenv("CHEAP_MODEL",
                        "anthropic/claude-haiku-4.5" if OPENROUTER_KEY
                        else "anthropic:claude-haiku-4-5-20251001")
STRONG_MODEL = os.getenv("STRONG_MODEL",
                         "anthropic/claude-sonnet-5" if OPENROUTER_KEY
                         else "anthropic:claude-sonnet-5")

# Грубий відсів хвоста по dense-косинусу. НЕ повноцінний fail-closed, і ось чому:
# заміряно на 13 запитах — релевантні лягли в 0.782..0.866, сторонні в 0.752..0.805.
# Діапазони перетинаються ("деплой" 0.782 нижче, ніж "як налаштувати принтер" 0.805),
# бо однослівні запити тягнуть косинус униз незалежно від того, чи є відповідь у базі.
# Тому 0.78 ріже лише очевидно стороннє (погода, чемпіонат), а справжній fail-closed —
# grade-крок: A1 сам оцінює знайдене перед відповіддю (див. SYSTEM_PROMPT).
# Перекалібрувати: прогнати kb-кейси датасету і подивитись, чи не з'явилась щілина.
KB_MIN_DENSE_SCORE = float(os.getenv("KB_MIN_DENSE_SCORE", "0.78"))
