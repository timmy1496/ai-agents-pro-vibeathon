#!/usr/bin/env bash
# Наскрізний сценарій демо: усі п'ять агентів у тому порядку, у якому їх варто
# показувати. Кожна дія друкує СПОЧАТКУ команду, а потім результат — глядач має бачити,
# що це справжній виклик, а не заготовлений текст.
#
#   ./scripts/demo.sh          з паузами: після кожного акту чекає Enter
#   ./scripts/demo.sh --auto   без пауз (запис відео, прогін перед виступом)
#   ./scripts/demo.sh 3        лише акт 3
#
# Перед першим прогоном: make doctor-warm. Він скаже, чого бракує, і прогріє ембеддер —
# інакше перший RCA стоїть кілька хвилин, качаючи модель, і це виглядає як зависання.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENT="${AGENT_URL:-http://localhost:8000}"
TOKEN="${AGENT_TOKEN:-sre-demo-token}"
PY="$ROOT/.venv/bin/python"

AUTO=0
ONLY=""
for arg in "$@"; do
  case "$arg" in
    --auto) AUTO=1 ;;
    [0-9]) ONLY="$arg" ;;
    *) echo "usage: $0 [--auto] [номер акту]" >&2; exit 1 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
dim()  { printf '\033[2m%s\033[0m\n' "$1"; }

act() {
  [ -n "$ONLY" ] && [ "$ONLY" != "$1" ] && return 1
  echo
  bold "── Акт $1 · $2"
  dim  "$3"
  echo
  return 0
}

pause() {
  [ "$AUTO" = 1 ] && return 0
  [ -n "$ONLY" ] && return 0
  printf '\033[2m   ↵ далі\033[0m'; read -r _ </dev/tty; echo
}

# Показує команду, потім виконує. Порядок важливий: спершу видно, ЩО викликають.
run() { dim "\$ $*"; eval "$@"; }

sre() {
  run "curl -s -X POST '$AGENT/sre' -H 'Authorization: Bearer $TOKEN'" \
      "-H 'Content-Type: application/json' -d '$1' | $PY -m json.tool --no-ensure-ascii"
}

# --- 0 · чи взагалі поїде ------------------------------------------------------
if act 0 "Готовність стенду" "Те, що ламається тихо: правила не завантажились, KB порожня, токен розійшовся."; then
  run "$PY -m scripts.doctor" || {
    echo; bold "Стенд не готовий — демо зупинено."; exit 1; }
  pause
fi

# --- 1 · A1 Knowledge ----------------------------------------------------------
if act 1 "A1 · Знання" "Гібридний пошук по KB: BM25 ловить ідентифікатори, e5 — сенс. Джерела в кожній відповіді."; then
  sre '{"text":"Що робити при OOMKilled рестартах і хто власник payment-gateway?","thread_id":"demo-kb"}'
  pause
fi

# --- 2 · A2 Incident -----------------------------------------------------------
if act 2 "A2 · Інцидент від алерта до RCA" "Chaos -> метрики -> Prometheus -> алерт -> Alertmanager -> вебхук -> розслідування. Нічого не підставлено."; then
  run "make -C '$ROOT' incident-1"
  echo
  dim "Алерт спрацює за ~40-60 с (scrape 5s + for 30s + group_wait 10s)."
  dim "Дивись: $AGENT (треди) і http://localhost:3000/d/sre-agent-golden (метрики + анотації агента)."
  pause
fi

# --- 3 · HITL ------------------------------------------------------------------
if act 3 "Межа агента" "Дві різні речі, які легко сплутати: що агент пропонує людині, і що політика не пускає навіть до неї."; then
  dim "Безпечна ремедіація — чекає людину:"
  run "$PY -c \"
from agents.tools.actions import propose_action
import json; print(json.dumps(propose_action.invoke({'service':'demo-chaos-svc','action':'відкотити на 1.4.2','reason':'регресія релізу','command':'kubectl rollout undo deploy/demo-chaos-svc'}), ensure_ascii=False, indent=2))\""
  echo
  dim "Деструктив — не доходить ні до тула, ні до людини:"
  run "$PY -c \"
from agents.tools.actions import propose_action
import json; print(json.dumps(propose_action.invoke({'service':'demo-chaos-svc','action':'прибрати под','reason':'так швидше','command':'kubectl delete pod demo-chaos-svc-abc --force --grace-period=0'}), ensure_ascii=False, indent=2))\""
  echo
  dim "Це не перевірка всередині тула: у графі агента блокування спрацьовує РАНІШЕ, ніж"
  dim "HITL встигає перервати граф — тобто людині цю пропозицію не показують узагалі."
  run "$PY -m pytest tests/test_guardrails.py -q"
  pause
fi

# --- 4 · A3 Reviewer -----------------------------------------------------------
if act 4 "A3 · Ревізія сервісу" "Чекліст детермінований і живе в коді: «у вас немає алерту на latency» не має залежати від настрою моделі."; then
  sre '{"text":"/sre review demo-chaos-svc","thread_id":"demo-review"}'
  pause
fi

# --- 5 · A4 Release Monitor ----------------------------------------------------
if act 5 "A4 · Метрики після релізу" "Пороги залежать від tier і рахуються без LLM. Модель формулює вердикт, але не вирішує його."; then
  sre '{"text":"/sre release demo-chaos-svc","thread_id":"demo-release"}'
  pause
fi

# --- 6 · евали і тиха деградація ----------------------------------------------
if act 6 "Гейт евалів і тиха деградація" "Найцікавіше для суддів: поломка, якої не видно очима."; then
  run "make -C '$ROOT' eval"
  echo
  dim "А тепер сервіс «просто змінив формат логів»: msg -> message. Тули не падають,"
  dim "тести на тули зелені, агент відповідає. Червоніє саме гейт — бо докази"
  dim "перестали нести сигнал."
  run "make -C '$ROOT' demo-degradation"
  pause
fi

# --- 7 · прибрати за собою -----------------------------------------------------
if act 7 "Скидання" "Chaos лишається увімкненим після демо — тут його знімають."; then
  run "make -C '$ROOT' reset"
fi

echo
bold "Демо завершено."
dim "Звіт евалів для слайдів: make eval-online (потрібен ключ) -> docs/eval-report.html"
