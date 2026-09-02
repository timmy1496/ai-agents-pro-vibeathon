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
KB_COLLECTION = os.getenv("KB_COLLECTION", "sre_kb")

# Локальні ONNX-моделі через fastembed: без API-викликів, працює офлайн.
# ponytail: MiniLM-L12 (384d, 0.22GB) замість multilingual-e5-large (1024d, 2.24GB) —
# на корпусі з 20 документів різниці в топ-3 не видно, а перший запуск швидший на порядок.
# Апгрейд: KB_DENSE_MODEL=intfloat/multilingual-e5-large, якщо recall почне провалюватись.
DENSE_MODEL = os.getenv("KB_DENSE_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
SPARSE_MODEL = os.getenv("KB_SPARSE_MODEL", "Qdrant/bm25")

# Каскад моделей: дешева на router/grade/judge, сильна на RCA-синтез.
CHEAP_MODEL = os.getenv("CHEAP_MODEL", "anthropic:claude-haiku-4-5-20251001")
STRONG_MODEL = os.getenv("STRONG_MODEL", "anthropic:claude-sonnet-5")

# ponytail: поріг по RRF-скору, а не по косинусу — RRF не калібрований, тому це грубий
# відсів "нічого не знайшлось", а не міра релевантності. Головний захист від вигадок —
# правило groundedness у промпті + evaluator. Калібрувати, коли KB виросте.
KB_MIN_SCORE = float(os.getenv("KB_MIN_SCORE", "0.02"))
