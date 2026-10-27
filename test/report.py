"""Self-contained HTML report, with optional calls to a local Python server."""

from datetime import date
from decimal import Decimal
from html import escape
import json
import math

from calculation_run.files import encode, read_json
from wheat_curve.domain.tenors import tenor_to_date


def money(value):
    return "нет данных" if value is None else f"{Decimal(str(value)):,.2f}".replace(",", " ")


def chart(points):
    if not points:
        return "<p>Кривая недоступна.</p>"
    values = [float(p["price"]) for p in points if p["price"] is not None]
    if not values:
        return "<p>Нет цен для графика.</p>"
    start, end = date.fromisoformat(points[0]["date"]), date.fromisoformat(points[-1]["date"])
    low = math.floor(min(values) / 100) * 100 - 100
    high = math.ceil(max(values) / 100) * 100 + 100
    tick_count = (high - low) // 100 + 1
    plot_height = max(320, (tick_count - 1) * 18)
    width, height, left, top = 1000, plot_height + 75, 85, 22
    def coordinates(point):
        x = left + (date.fromisoformat(point["date"]) - start).days / max((end - start).days, 1) * 875
        y = top + (high - float(point["price"])) / (high - low) * plot_height
        return f"{x:.2f},{y:.2f}"
    parts, segment = [], []
    for point in points:
        if point["price"] is None:
            if segment:
                parts.append(' '.join(segment))
                segment = []
        else:
            segment.append(coordinates(point))
    if segment:
        parts.append(' '.join(segment))
    lines = ''.join(f'<polyline points="{p}" fill="none" stroke="#2864ce" stroke-width="2.5"/>' for p in parts)
    ticks = ''
    for value in range(low, high + 1, 100):
        y = top + (high - value) / (high - low) * plot_height
        ticks += f'<line x1="85" x2="960" y1="{y}" y2="{y}" stroke="#e5e9f0"/>'
        label = f"{value:,}".replace(",", " ")
        ticks += f'<text x="73" y="{y+5}" text-anchor="end">{label}</text>'
    return f'''<svg viewBox="0 0 {width} {height}" role="img" aria-label="Сегодняшняя кривая, линейная шкала цен, шаг 100 руб./т">
    {ticks}{lines}<text x="85" y="{height-15}">{start}</text><text x="960" y="{height-15}" text-anchor="end">{end}</text></svg>'''


def write_report(run):
    interpolation = run.folder / "results" / "interpolated_curve.json"
    points = read_json(interpolation) if interpolation.exists() else []
    modules = ''.join(f'<tr><td>{escape(name)}</td><td>{escape(value["status"])}</td>'
                      f'<td>{escape(value.get("message", ""))}</td></tr>' for name, value in run.info["modules"].items())
    sources = ''.join(f'<tr><td>{escape(name)}</td><td>{escape(value.get("source", ""))}</td></tr>'
                      for name, value in run.info["sources"].items())
    rows = ''.join(f'<tr><td>{escape(p["tenor"])}</td><td>{escape(p["maturity_date"])}</td>'
                   f'<td>{money(p["price"])}</td><td>{escape(p["source"])}</td></tr>'
                   for p in (run.curve or {}).get("curve", []))
    seed = json.dumps(run.portfolio, ensure_ascii=False, default=encode).replace("<", "\\u003c")
    example_day = date.fromisoformat(run.info.get("calculation_date", date.today().isoformat()))
    example = [{"trade_id": "buy-1", "quantity": "100", "strike": "21000", "side": 1,
                "fixing_date": tenor_to_date(example_day, "6M").isoformat(),
                "payment_date": tenor_to_date(example_day, "7M").isoformat()},
               {"trade_id": "sell-1", "quantity": "50", "strike": "21500", "side": -1,
                "fixing_date": tenor_to_date(example_day, "12M").isoformat(),
                "payment_date": tenor_to_date(example_day, "13M").isoformat()}]
    example_json = escape(json.dumps(example, ensure_ascii=False, indent=2))
    warnings = escape(json.dumps(run.info["warnings"], ensure_ascii=False))
    changes = escape(json.dumps(run.info["changes"], ensure_ascii=False))
    go = money(run.margin["portfolio"]["go"]) if run.margin else "недоступно"
    npv = money(run.npv["portfolio"]) if run.npv else "недоступно"
    html = '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Расчёт кривой пшеницы</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f5f8;color:#233044;font:16px/1.5 system-ui,sans-serif}
main{max-width:1150px;margin:36px auto;padding:0 22px}h1{font-size:30px;margin-bottom:6px}h2{font-size:21px;margin-top:0}
section{background:white;padding:24px;margin:20px 0;border-radius:12px;border:1px solid #dfe5ee}
.muted{color:#64748b}.warning{background:#fff8e5;padding:14px;overflow-wrap:anywhere}
svg{width:100%;font:14px system-ui;fill:#64748b}table{border-collapse:collapse;width:100%;font-size:14px}
td,th{padding:10px 12px;border-bottom:1px solid #e6ebf2;text-align:left}th{color:#64748b}td:nth-child(3){font-variant-numeric:tabular-nums}
input,select,button,textarea{font:inherit;padding:9px;border:1px solid #cbd5e1;border-radius:6px}button{background:#245ac0;color:white;cursor:pointer}
button:disabled{opacity:.45;cursor:not-allowed}.fields{display:flex;gap:12px;flex-wrap:wrap}label{display:grid;font-size:14px;gap:5px}
textarea{width:100%;min-height:200px;font-family:monospace;font-size:14px}pre{white-space:pre-wrap;overflow-wrap:anywhere}
.scroll{overflow:auto}.cards{display:flex;gap:32px;flex-wrap:wrap}.cards strong{font-size:24px}a{color:#245ac0}
</style><main>'''
    html += f'<h1>Кривая пшеницы</h1><div class="muted">Дата расчёта: {escape(run.info.get("calculation_date", "не определена"))}<br>Запуск: {escape(run.folder.name)}, время Московское</div>'
    if run.info["synthetic"]:
        html += '<p class="warning">Синтетические данные. Это демонстрация, не оценка реального рынка.</p>'
    html += f'<section><h2>Сегодняшняя кривая</h2><p class="muted">Log-linear интерполяция. Ось цен линейная, шаг 100 руб./т. Пропуски не соединяются.</p>{chart(points)}'
    if run.curve:
        html += '<a href="excel/today_curve.xlsx">Скачать сегодняшнюю кривую в Excel</a>'
        html += '<details><summary>39 узлов</summary><div class="scroll"><table><tr><th>Срок</th><th>Экспирация</th><th>Цена, руб./т</th><th>Источник</th></tr>' + rows + '</table></div></details>'
    html += f'</section><section><h2>Сохранённые результаты</h2><div class="cards"><div>NPV, руб.<br><strong>{npv}</strong></div><div>ГО портфеля, руб.<br><strong>{go}</strong></div></div><p class="muted">ГО модельное, не официальный лимит НКЦ.</p></section>'
    html += '''<section><h2>NPV сделки</h2><p class="muted" id="connection">Без локального сервера доступен только просмотр сохранённых результатов.</p>
<form id="npv-form"><div class="fields">
<label>Сторона<select name="side"><option value="1">Покупка</option><option value="-1">Продажа</option></select></label>
<label>Объём, т<input name="quantity" type="number" step="any" min="0" required></label>
<label>Цена, руб./т<input name="strike" type="number" step="any" min="0" required></label>
<label>Фиксация<input name="fixing_date" type="date" required></label>
<label>Оплата<input name="payment_date" type="date" required></label>
</div><p><button disabled id="npv-button">Рассчитать NPV</button></p></form><pre id="npv-result"></pre></section>
<section><h2>ГО портфеля</h2><p>Вставь JSON-массив сделок: <code>[{...}, {...}]</code>, без внешнего объекта <code>trades</code>.</p>
<p><code>trade_id</code>: уникальный идентификатор; <code>quantity</code>: объём в тоннах;
<code>strike</code>: договорная цена в руб./т; <code>side</code>: 1 для покупки, -1 для продажи.
Объём и цена задаются положительными десятичными строками, сторона числом.
<code>fixing_date</code>: дата фиксации; <code>payment_date</code>: дата оплаты. Формат дат: YYYY-MM-DD.
Фиксация должна быть строго после конца горизонта риска, оплата не раньше фиксации; обе даты в пределах доступных кривых.</p>
<p>Пример формата, не исходный портфель запуска:</p>'''
    html += f'<pre id="portfolio-example"><code>{example_json}</code></pre>'
    html += '''<label for="portfolio">Портфель для расчёта ГО (JSON)</label>
<textarea id="portfolio" aria-label="Портфель JSON" spellcheck="false"></textarea>
<p><button disabled id="margin-button">Рассчитать ГО на подготовленных сценариях</button></p><pre id="margin-result"></pre>
<p class="muted">Изменения сделок и результаты интерактивного пересчёта не сохраняются на диск.</p></section>'''
    html += f'<section><h2>Источники и подстановки</h2><p class="warning">{warnings}</p><p>{changes}</p><table><tr><th>Блок</th><th>Источник</th></tr>{sources}</table></section>'
    html += f'<section><h2>Выполнение модулей</h2><div class="scroll"><table><tr><th>Модуль</th><th>Результат</th><th>Ошибка</th></tr>{modules}</table></div></section>'
    html += r'''<script>
const initialPortfolio = SEED;
document.getElementById('portfolio').value=JSON.stringify(initialPortfolio,null,2);
const form=document.getElementById('npv-form');
if(initialPortfolio.length){for(const [k,v] of Object.entries(initialPortfolio[0])){if(form.elements[k])form.elements[k].value=v;}}
let token=null;
async function connect(){if(!['http:','https:'].includes(location.protocol))return;
try{const response=await fetch('api/session',{cache:'no-store'});if(!response.ok)return;const data=await response.json();token=data.token;
document.getElementById('npv-button').disabled=!data.npv;document.getElementById('margin-button').disabled=!data.margin;
document.getElementById('connection').textContent='Локальный Python-сервер подключён. Рыночные данные и параметры расчёта зафиксированы.';
}catch(error){document.getElementById('connection').textContent='Локальный сервер недоступен. Сохранённые результаты доступны.';}}
function format(value){if(typeof value==='string'&&/^-?\d+(\.\d+)?$/.test(value)){return Number(value).toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2});}return String(value);}
async function calculate(kind,trades,target){target.textContent='Расчёт...';try{
const response=await fetch('api/'+kind,{method:'POST',headers:{'Content-Type':'application/json','X-Run-Token':token},body:JSON.stringify({trades})});
const data=await response.json();if(!response.ok)throw Error(data.error||'Ошибка расчёта');
if(kind==='npv'){target.textContent='NPV, руб.: '+format(data.portfolio);}
else{target.textContent=Object.entries(data.portfolio).map(([k,v])=>k+': '+format(v)+' руб.').join('\n');}
}catch(error){target.textContent=error.message;}}
form.addEventListener('submit',event=>{event.preventDefault();const values=Object.fromEntries(new FormData(form));values.trade_id='interactive';values.side=Number(values.side);calculate('npv',[values],document.getElementById('npv-result'));});
document.getElementById('margin-button').addEventListener('click',()=>{const target=document.getElementById('margin-result');try{calculate('margin',JSON.parse(document.getElementById('portfolio').value),target);}catch(error){target.textContent=error.message;}});
connect();</script></main></html>'''.replace('SEED', seed)
    (run.folder / "report.html").write_text(html, encoding="utf-8")
