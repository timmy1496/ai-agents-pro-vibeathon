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

# GRAFANA_URL — голий URL; креденшели окремо, бо їх шлють заголовком Authorization,
# а не в самому URL. scripts/incident.sh раніше очікував тут http://admin:admin@... —
# те саме ім'я змінної означало два різні формати, і один із двох завжди ламався.
# Тепер скрипт складає свій URL сам з GRAFANA_URL + GRAFANA_AUTH.

# Спільний секрет HTTP-входу. Вебхук Alertmanager і, головне, /approve — це кнопка HITL:
# без токена будь-хто в мережі стенду «підтверджує» дію від імені чергового.
# Дефолт демонстраційний і лежить у .env.example навмисно — щоб стенд піднімався з
# коробки; для будь-чого, крім локального демо, змінна має бути задана явно.
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "sre-demo-token")
AGENT_TOKEN_IS_DEFAULT = "AGENT_TOKEN" not in os.environ

# Langfuse: ключі за замовчуванням — ті, що compose створює через LANGFUSE_INIT_*.
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "http://localhost:3001")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "pk-lf-demo")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "sk-lf-demo")
LANGFUSE_ENABLED = os.getenv("LANGFUSE_ENABLED", "1") not in ("0", "false", "")

# Slack. Є токен — пишемо в реальний workspace, немає — у файл-емуляцію.
# Перемикача немає навмисно: зайвий прапорець, який завжди дорівнює "чи є токен".
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "#sre-agent")
# App-level token (xapp-...) для Socket Mode — це окремий токен від бот-токена
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")
KB_COLLECTION = os.getenv("KB_COLLECTION", "sre_kb")

# Куди fastembed кладе ONNX-моделі. Дефолт бібліотеки — $TMPDIR/fastembed_cache, а це
# на macOS тека, яку система періодично прибирає: 2 ГБ e5-large тихо зникають, і
# наступний старт агента стоїть кілька хвилин, качаючи їх заново — зазвичай саме тоді,
# коли цього найменше хочеться. Пінимо в стабільне місце (той самий шлях кешує CI).
os.environ.setdefault("FASTEMBED_CACHE_PATH",
                      str(pathlib.Path.home() / ".cache" / "fastembed"))
FASTEMBED_CACHE = pathlib.Path(os.environ["FASTEMBED_CACHE_PATH"])

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
