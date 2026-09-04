"""HTML-звіт прогону — той артефакт, який показують людям, а не грепають.

Форма обрана під роботу даних, а не під «хай буде графік»:

* вісім метрик прогону — це вісім окремих чисел-заголовків, а не ряд. Графік із
  восьми стовпчиків різної природи (частка, середнє, кількість обертів) на одній
  осі був би просто неправильним. Тому — плитки з дельтою до попереднього прогону.
* оцінки суддя виставляє в 0.0-1.0 — однорідна величина, п'ять значень, спільна
  шкала. Це смуги: одна серія, підписані прямо на кінці, без легенди.
* покейсові результати — таблиця з розкривним rationale судді. Саме rationale
  і є те, заради чого суддю кликали: число без міркування неперевірне.

Кольори призначені за роботою: одна категоріальна синя для смуг (одна серія),
резервована статусна пара для вердикту гейта і напрямку дельти. Статусні зелений
і червоний нерозрізненні при дейтеранопії (ΔE 4.1) — тому кожен статус несе ще
гліф і підпис, і колір ніколи не є єдиним носієм значення.

Сторінка самодостатня: жодного зовнішнього запиту, тому вона однаково відкривається
з диска, з репо і з проєктора в залі.
"""
from __future__ import annotations

import html
import json
import pathlib
from typing import Any

# Метрики в порядку показу: ключ -> (підпис, чи більше = краще, формат)
DETERMINISTIC = [
    ("root_cause_accuracy", "Клас причини вгадано", "частка кейсів, де агент назвав очікуваний клас"),
    ("tool_recall", "Траєкторія пройдена", "частка кейсів без пропущених очікуваних тулів"),
    ("critic_accept_rate", "Критик прийняв з першого разу",
     "вердикт критика, не обґрунтованість звіту — її міряє суддя (groundedness)"),
    ("self_completed", "Звіт без запобіжника", "частка кейсів, де агент дійшов до висновку сам"),
]

STYLE = """
:root{
  color-scheme:light;
  --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --bar:#2a78d6; --track:#e1e0d9;
  --good:#0ca30c; --critical:#d03b3b; --up:#006300;
}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  color-scheme:dark;
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --bar:#3987e5; --track:#2c2c2a;
  --good:#0ca30c; --critical:#d03b3b; --up:#0ca30c;
}}
:root[data-theme=dark]{
  color-scheme:dark;
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --bar:#3987e5; --track:#2c2c2a;
  --good:#0ca30c; --critical:#d03b3b; --up:#0ca30c;
}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
 font:15px/1.55 ui-sans-serif,-apple-system,'Segoe UI',system-ui,sans-serif;
 font-variant-numeric:tabular-nums}
main{max-width:1000px;margin:0 auto;padding:2.5rem 1.25rem 4rem}
h1{font-size:1.5rem;letter-spacing:-.01em;margin:0 0 .35rem}
h2{font-size:.82rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);
 margin:2.75rem 0 .9rem;font-weight:600}
p{color:var(--ink-2);margin:.4rem 0}
.sub{color:var(--muted);font-size:.85rem}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:10px}

.sample{padding:.75rem 1.1rem;margin:1.5rem 0 0;border-left:4px solid var(--muted);
 font-size:.88rem;color:var(--ink-2)}
.sample b{color:var(--ink)}
.verdict{display:flex;align-items:center;gap:.7rem;padding:.9rem 1.1rem;margin:1.5rem 0 0;
 border-left:4px solid var(--good)}
.verdict.red{border-left-color:var(--critical)}
.verdict .glyph{font-size:1.1rem;line-height:1}
.verdict.green .glyph,.verdict.green .label{color:var(--good)}
.verdict.red .glyph,.verdict.red .label{color:var(--critical)}
.label{font-weight:650}
.verdict ul{margin:.4rem 0 0;padding-left:1.1rem;color:var(--ink-2);font-size:.9rem}

.tiles{display:grid;gap:.75rem;grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.tile{padding:.9rem 1rem}
.tile .name{font-size:.78rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.tile .value{font-size:1.9rem;font-weight:600;letter-spacing:-.02em;margin:.15rem 0 .1rem}
.tile .delta{font-size:.82rem;color:var(--muted)}
.tile .delta.up{color:var(--up)} .tile .delta.down{color:var(--critical)}
.tile .hint{font-size:.78rem;color:var(--muted);margin-top:.35rem;line-height:1.4}

.bars{padding:1.1rem 1.15rem}
.bar-row{display:grid;grid-template-columns:11rem 1fr 3.2rem;align-items:center;
 gap:.75rem;padding:.4rem 0}
.bar-row + .bar-row{border-top:1px solid var(--grid)}
.bar-name{font-size:.9rem;color:var(--ink-2)}
.track{height:9px;background:var(--track);border-radius:4px;overflow:hidden}
.fill{height:100%;background:var(--bar);border-radius:0 4px 4px 0}
.bar-val{text-align:right;font-size:.9rem;font-weight:600}
.bar-na{color:var(--muted);font-size:.82rem;font-weight:400}

table{width:100%;border-collapse:collapse;font-size:.88rem}
th{text-align:left;font-weight:600;color:var(--muted);font-size:.76rem;
 text-transform:uppercase;letter-spacing:.05em;padding:.5rem .6rem;
 border-bottom:1px solid var(--axis)}
td{padding:.5rem .6rem;border-bottom:1px solid var(--grid);vertical-align:top}
tr:last-child td{border-bottom:0}
code{font:.85em ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink-2)}
.ok{color:var(--good);font-weight:600} .miss{color:var(--critical);font-weight:600}
details{margin:.3rem 0 0} summary{cursor:pointer;color:var(--muted);font-size:.82rem}
details p{margin:.4rem 0 .2rem;font-size:.85rem}
.dim{color:var(--muted)}
footer{margin-top:3rem;padding-top:1.2rem;border-top:1px solid var(--grid);
 color:var(--muted);font-size:.83rem}
@media print{body{background:#fff}.card{break-inside:avoid}}
"""


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _tile(name: str, value: Any, previous: Any, hint: str, as_percent: bool = True) -> str:
    shown = _pct(value) if as_percent else f"{value:g}"
    delta = ""
    if isinstance(previous, (int, float)) and isinstance(value, (int, float)):
        change = value - previous
        if abs(change) < 1e-9:
            delta = '<div class="delta">без змін до попереднього</div>'
        else:
            # Гліф ▲/▼ і знак числа несуть напрямок самі — колір лише підсилює.
            arrow, css = ("▲", "up") if change > 0 else ("▼", "down")
            amount = _pct(abs(change)) if as_percent else f"{abs(change):g}"
            delta = f'<div class="delta {css}">{arrow} {amount} до попереднього</div>'
    return (f'<div class="card tile"><div class="name">{html.escape(name)}</div>'
            f'<div class="value">{shown}</div>{delta}'
            f'<div class="hint">{html.escape(hint)}</div></div>')


def _bars(summary: dict, dimensions: dict, na_counts: dict[str, int],
          broken_counts: dict[str, int]) -> str:
    rows = []
    for name in dimensions:
        value = summary.get(name)
        if value is None:
            rows.append(
                f'<div class="bar-row"><div class="bar-name">{html.escape(name)}</div>'
                f'<div class="track"></div>'
                f'<div class="bar-val bar-na" title="вимір не мав знаменника в жодному кейсі">n/a</div>'
                f"</div>")
            continue
        skipped = na_counts.get(name, 0)
        note = f", n/a на {skipped} кейсах" if skipped else ""
        broken = broken_counts.get(name, 0)
        if broken:
            note += f", не виміряно на {broken}"
        rows.append(
            f'<div class="bar-row"><div class="bar-name">{html.escape(name)}</div>'
            f'<div class="track" title="{value:.2f} з 1.00{note}">'
            f'<div class="fill" style="width:{value * 100:.1f}%"></div></div>'
            f'<div class="bar-val">{value:.2f}</div></div>')
    return f'<div class="card bars">{"".join(rows)}</div>'


def _case_rows(rows: list[dict], dimensions: dict) -> str:
    cells = []
    for row in rows:
        matched = row.get("root_cause_match")
        if row.get("error"):
            verdict = ('<span class="miss">✗ не доїхав</span><br>'
                       f'<span class=dim>{html.escape(row["error"][:160])}</span>')
        else:
            verdict = ('<span class="ok">✓ збіг</span>' if matched
                       else '<span class="miss">✗ розбіжність</span>')
        missing = row.get("missing_tools") or []
        tools = ("<span class=dim>усі</span>" if not missing
                 else f'<span class="miss">−{len(missing)}</span> '
                      f'<code>{html.escape(", ".join(missing))}</code>')

        scores, details = [], []
        for name in dimensions:
            entry = (row.get("judge") or {}).get(name, {})
            if "score" in entry:
                scores.append(f"{name} {entry['score']:.2f}")
                details.append(f"<p><b>{html.escape(name)} {entry['score']:.2f}</b> — "
                               f"{html.escape(entry['rationale'])}</p>")
            elif "error" in entry:
                scores.append(f"{name} не виміряно")
                details.append(f"<p><b>{html.escape(name)}</b> — не виміряно: "
                               f"{html.escape(str(entry['error']))}</p>")
            elif "na" in entry and not entry.get("structural"):
                details.append(f"<p><b>{html.escape(name)}</b> — n/a: "
                               f"{html.escape(str(entry['na']))}</p>")
        rationale = (f"<details><summary>міркування судді</summary>{''.join(details)}</details>"
                     if details else "")

        cells.append(
            f"<tr><td><code>{html.escape(row['case_id'])}</code></td>"
            f"<td>{verdict}"
            + ("" if row.get("error") else
               f"<br><span class=dim>{html.escape(str(row.get('root_cause', '—')))}</span>")
            + "</td>"
            f"<td>{tools}</td>"
            f"<td>{html.escape(', '.join(scores)) or '<span class=dim>—</span>'}{rationale}</td>"
            f"<td>{row.get('revisions', 0)}</td></tr>")
    return "".join(cells)


def _na_counts(rows: list[dict], dimensions: dict) -> dict[str, int]:
    """Скільки разів вимір чесно віддав n/a. Структурний n/a не рахуємо — він не подія."""
    return {
        name: sum(1 for r in rows
                  if "na" in (r.get("judge") or {}).get(name, {})
                  and not r["judge"][name].get("structural"))
        for name in dimensions
    }


def _broken_counts(rows: list[dict], dimensions: dict) -> dict[str, int]:
    """Скільки разів вимір не вдалося виміряти. Це не n/a і не нуль — окрема категорія."""
    return {name: sum(1 for r in rows if "error" in (r.get("judge") or {}).get(name, {}))
            for name in dimensions}


def render(report: dict, previous: dict | None) -> str:
    from evals import config

    meta, summary, gate = report["meta"], report["summary"], report["gate"]
    before = (previous or {}).get("summary", {})
    dimensions = config.dimensions()
    rows = report["rows"]

    tiles = "".join(_tile(label, summary[key], before.get(key), hint)
                    for key, label, hint in DETERMINISTIC if key in summary)
    tiles += _tile("Обертів критика", summary["avg_revisions"], before.get("avg_revisions"),
                   "скільки разів у середньому звіт повертали на доопрацювання",
                   as_percent=False)

    verdict_class, glyph, label = (
        ("green", "✓", "Гейт зелений") if gate["passed"]
        else ("red", "✗", "Гейт червоний"))
    failures = ("<ul>" + "".join(f"<li>{html.escape(f)}</li>" for f in gate["failures"]) + "</ul>"
                if gate["failures"] else
                "<span class=sub>усі пороги з evals/eval.toml витримані</span>")

    # Звіт на синтетичних числах виглядає точно як справжній — і саме тому мусить
    # сказати про себе сам. Мовчазний зразок у docs/ рано чи пізно поїде в презентацію
    # як результат прогону.
    sample = ('<div class="card sample"><b>Демонстраційні дані.</b> Це зразок форми звіту, '
              'а не результат прогону: числа синтетичні. Справжній звіт: '
              '<code>make eval-online</code>.</div>') if meta.get("sample") else ""

    # Транспорт — теж частина вимірювального інструмента, і мовчати про нього не можна:
    # через Claude Code CLI tool calling промптовий, а не нативний, тому ці числа
    # порівнюються лише з іншими прогонами того самого транспорту.
    transport_note = ''
    if meta.get("provider") == "claude-code":
        transport_note = (
            '<div class="card sample"><b>Прогін на підписці Claude Code.</b> Виклики '
            'ідуть через <code>claude -p</code>, тому tool calling тут промптовий, а не '
            'нативний: агентський цикл, middleware і guardrails ті самі, механіка '
            'виклику інструментів — інша. Ці оцінки порівнюються лише з іншими '
            'прогонами через той самий транспорт.</div>')

    cost = meta.get("spend")
    cost_note = ''
    if cost and cost.get("calls"):
        per_case = cost["usd"] / max(meta.get("cases") or 1, 1)
        cost_note = (
            f'<div class="card sample">{cost["calls"]} викликів моделі, '
            f'${cost["usd"]:.2f} за прейскурантом (${per_case:.2f} на кейс). На підписці '
            f'це не рахунок, а порядок величини — але саме він відповідає на питання '
            f'«скільки коштує розібраний інцидент».</div>')

    comparison = ("порівняння з попереднім прогоном тієї самої рубрики"
                  if previous else
                  "попереднього прогону цієї рубрики немає — дельти не буде")

    return f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SRE-агент — звіт евалів</title>
<style>{STYLE}</style></head><body><main>

<h1>SRE-агент — звіт евалів</h1>
<p class="sub">{html.escape(meta['stamp'])} · {meta['cases']} кейсів ·
рубрика <code>{html.escape(meta['rubric_version'])}</code> ·
суддя <code>{html.escape(str(meta['judge_model'] or 'не викликався'))}</code> ·
агент <code>{html.escape(str(meta['agent_model']))}</code> ·
транспорт <code>{html.escape(str(meta.get('provider', '—')))}</code></p>
{transport_note}{cost_note}

{sample}
<div class="card verdict {verdict_class}">
  <span class="glyph">{glyph}</span>
  <div><span class="label">{label}</span> {failures}</div>
</div>

<h2>Детерміновані метрики</h2>
<p class="sub">Рахуються без моделі й без стенду — на записаних виводах тулів.
Це та половина, що бігає на кожен коміт.</p>
<div class="tiles">{tiles}</div>

<h2>Оцінки судді, 0.00–1.00</h2>
<p class="sub">Один виклик на вимір, міркування перед оцінкою. Вимір без знаменника
віддає n/a і в середнє не входить — інакше чесна відмова агента каралась би так само,
як безпорадний звіт.</p>
{_bars(summary, dimensions, _na_counts(rows, dimensions), _broken_counts(rows, dimensions))}

<h2>Покейсово</h2>
<div class="card" style="padding:.35rem .5rem">
<table>
<thead><tr><th>кейс</th><th>клас причини</th><th>тули</th>
<th>суддя</th><th>обертів</th></tr></thead>
<tbody>{_case_rows(rows, dimensions)}</tbody>
</table></div>

<footer>
Пороги і виміри оголошені в <code>evals/eval.toml</code>, рубрика — в
<code>evals/prompts/*.md</code>. Кожна оцінка тегована версією рубрики
<code>{html.escape(meta['rubric_version'])}</code>
(<code>{html.escape(meta['prompts_sha'][:12])}…</code>): {comparison}, бо правка
промпта зсуває сам еталон так само, як тиха заміна моделі судді.
</footer>
</main></body></html>
"""


def write_html(report: dict, previous: dict | None, path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(report, previous), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    """Перезібрати HTML з останнього JSON-звіту, нічого не проганяючи заново."""
    import argparse

    from evals.run import DOCS, REPORTS

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--out", type=pathlib.Path, default=DOCS / "eval-report.html")
    args = parser.parse_args(argv)

    reports = sorted(REPORTS.glob("*.json"))
    if not reports:
        print("прогонів ще не було: make eval-online")
        return 2
    latest = json.loads(reports[-1].read_text(encoding="utf-8"))
    # Попередній для дельти — той, що перед останнім, тієї самої рубрики.
    history = [json.loads(p.read_text(encoding="utf-8")) for p in reports[:-1]]
    same = [r for r in history
            if r.get("meta", {}).get("rubric_version") == latest["meta"]["rubric_version"]]
    print(write_html(latest, same[-1] if same else None, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
