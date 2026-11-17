"""Daily interactive report for FunPay WoW: Forever gold snapshots in data/*.csv.

Produces <out>/report_YYYY-MM-DD.html, <out>/index.html (= latest) and <out>/archive.html:
dark-themed pages with interactive charts (Plotly), a server filter, sortable
tables and day-to-day navigation. Also writes report_prices.csv / report_sales.csv.

Estimated sales = stock decreases of the same offer between consecutive
snapshots. It is an approximation: sellers also lower stock by hand, restocks
between snapshots hide sales, and offers that vanish entirely are not counted.

Usage: python funpay_report.py [--data dir | --db sqlite] [--day YYYY-MM-DD] [--out dir] [--banner text]
"""
import argparse
import html as htmlmod
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
STOCK_CAP = 1_000_000  # placeholder stock like 1,000,000,000 is ignored for stock/sales maths
SERVERS = ["(EU) PvE", "(EU) PvP", "(EU) Roleplay PvE", "(EU) Roleplay PvP",
           "(NA) PvE", "(NA) PvP", "(NA) Roleplay PvE", "(NA) Roleplay PvP"]
PLOTLY_CDN = "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"


# ---------------------------------------------------------------- data
def load(data_dir=None, db=None):
    if db:
        df = pd.read_sql("SELECT * FROM offers", sqlite3.connect(db))
    else:
        files = sorted(Path(data_dir).glob("offers_*.csv"))
        if not files:
            raise SystemExit(f"no data files in {data_dir}")
        df = pd.concat((pd.read_csv(f, dtype={"offer_id": str, "user_id": str}) for f in files),
                       ignore_index=True)
    df = df[df["price_eur"] > 0].copy()
    local_tz = datetime.now().astimezone().tzinfo  # show times in the PC's local time
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(local_tz).dt.tz_localize(None)
    df["real_stock"] = df["stock"].where(df["stock"] < STOCK_CAP)
    return df


def price_series(df):
    g = df.groupby(["ts", "server", "side"])
    out = g.agg(offers=("offer_id", "count"),
                real_stock=("real_stock", "sum"),
                min_price=("price_eur", "min"),
                p25_price=("price_eur", lambda s: s.quantile(0.25)),
                median_price=("price_eur", "median"),
                cheapest5_avg=("price_eur", lambda s: s.nsmallest(5).mean()))
    return out.reset_index()


def sales_events(df):
    d = df.sort_values("ts").copy()
    d["prev_stock"] = d.groupby("offer_id")["stock"].shift()
    s = d[(d["prev_stock"] > d["stock"]) & (d["prev_stock"] < STOCK_CAP)].copy()
    s["units"] = s["prev_stock"] - s["stock"]
    s["eur"] = s["units"] * s["price_eur"]
    s["day"] = s["ts"].dt.strftime("%Y-%m-%d")
    return s


def rnd(v, n=4):
    return None if v is None or pd.isna(v) else round(float(v), n)


def snapshot_near(ps, when):
    """Rows of the last snapshot at or before `when` (None if there is none)."""
    older = ps[ps["ts"] <= when]
    return None if older.empty else older[older["ts"] == older["ts"].max()]


def build_payload(df, day):
    ps = price_series(df)
    sales = sales_events(df)
    day_end = datetime.combine(day, datetime.max.time())
    ps_upto = ps[ps["ts"] <= day_end]
    if ps_upto.empty:
        raise SystemExit(f"no snapshots on or before {day}")
    latest_ts = ps_upto["ts"].max()
    latest = ps_upto[ps_upto["ts"] == latest_ts]
    prev = snapshot_near(ps, latest_ts - timedelta(hours=23))
    day_s = day.strftime("%Y-%m-%d")
    yday_s = (day - timedelta(days=1)).strftime("%Y-%m-%d")
    sales_upto = sales[sales["ts"] <= day_end]

    # summary table rows
    today_sales = sales_upto[sales_upto["day"] == day_s].groupby(["server", "side"])[["units", "eur"]].sum()
    prev_idx = prev.set_index(["server", "side"]) if prev is not None else None
    summary = []
    for _, r in latest.iterrows():
        key = (r["server"], r["side"])
        chg = None
        if prev_idx is not None and key in prev_idx.index:
            chg = r["median_price"] / prev_idx.loc[key, "median_price"] - 1
        u = today_sales.loc[key] if key in today_sales.index else None
        summary.append({
            "server": r["server"], "side": r["side"], "offers": int(r["offers"]),
            "stock": rnd(r["real_stock"], 0), "min": rnd(r["min_price"]),
            "p25": rnd(r["p25_price"]), "median": rnd(r["median_price"]),
            "chg": rnd(chg), "units": rnd(u["units"], 0) if u is not None else 0,
            "eur": rnd(u["eur"], 2) if u is not None else 0,
        })

    def kpi(cur, before):
        return {"v": rnd(cur, 3), "d": rnd(cur / before - 1) if before else None}

    def day_tot(d, col):
        return float(sales_upto.loc[sales_upto["day"] == d, col].sum())

    region_min = lambda frame, reg: frame[frame["server"].str.startswith(reg)]["min_price"].min() if frame is not None else None
    kpis = {
        "offers": kpi(latest["offers"].sum(), prev["offers"].sum() if prev is not None else None),
        "stock": kpi(latest["real_stock"].sum(), prev["real_stock"].sum() if prev is not None else None),
        "min_eu": kpi(region_min(latest, "(EU)"), region_min(prev, "(EU)")),
        "min_na": kpi(region_min(latest, "(NA)"), region_min(prev, "(NA)")),
        "units": kpi(day_tot(day_s, "units"), day_tot(yday_s, "units") or None),
        "eur": kpi(day_tot(day_s, "eur"), day_tot(yday_s, "eur") or None),
    }

    last24 = sales_upto[sales_upto["ts"] > latest_ts - timedelta(hours=24)]
    top = (last24.groupby(["seller", "server", "side"])
           .agg(units=("units", "sum"), eur=("eur", "sum"), price=("price_eur", "mean"))
           .reset_index().sort_values("eur", ascending=False).head(20))

    daily = sales_upto.groupby(["day", "server", "side"])[["units", "eur"]].sum().reset_index()
    hourly = sales_upto.assign(hour=sales_upto["ts"].dt.hour,
                               region=sales_upto["server"].str[1:3])
    n_days = max(hourly["day"].nunique(), 1) if not hourly.empty else 1
    hourly = (hourly.groupby(["hour", "region"])["eur"].sum() / n_days).reset_index()

    series = ps_upto.copy()
    series["ts"] = series["ts"].dt.strftime("%Y-%m-%d %H:%M")
    return ps, sales, {
        "day": day_s,
        "latest_ts": latest_ts.strftime("%Y-%m-%d %H:%M"),
        "snapshots": int(ps_upto["ts"].nunique()),
        "first_ts": ps_upto["ts"].min().strftime("%Y-%m-%d"),
        "servers": SERVERS,
        "kpis": kpis,
        "summary": summary,
        "series": {c: series[c].round(4).where(series[c].notna(), None).tolist()
                   if series[c].dtype.kind == "f" else series[c].tolist()
                   for c in ["ts", "server", "side", "offers", "real_stock", "min_price",
                             "median_price", "cheapest5_avg"]},
        "daily": daily.round(2).to_dict("list"),
        "hourly": hourly.round(2).to_dict("list"),
        "top": top.round(4).to_dict("records"),
        "stock_cap": STOCK_CAP,
    }


# ---------------------------------------------------------------- page
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FunPay Gold · __DAY__</title>
<script src="__PLOTLY__"></script>
<style>
:root{--bg:#0b0e14;--panel:#121722;--panel2:#171d2a;--line:#232b3b;--text:#e6e9ef;--muted:#8a93a6;
--accent:#f5b84b;--ali:#4c8dff;--horde:#ff5a5f;--up:#3ecf8e;--down:#ff6b6b;--radius:14px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Segoe UI",Inter,system-ui,sans-serif}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:10;background:rgba(11,14,20,.88);backdrop-filter:blur(10px);
  border-bottom:1px solid var(--line)}
.bar{max-width:1400px;margin:0 auto;padding:12px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.brand{font-weight:700;font-size:16px;display:flex;align-items:center;gap:10px}
.coin{width:22px;height:22px;border-radius:50%;background:radial-gradient(circle at 35% 35%,#ffe29a,#f5b84b 55%,#a8741c);
  box-shadow:0 0 12px rgba(245,184,75,.45)}
.meta{color:var(--muted);font-size:12.5px}
.nav{margin-left:auto;display:flex;gap:6px}
.nav a,.nav span{padding:6px 12px;border:1px solid var(--line);border-radius:999px;font-size:12.5px;color:var(--text);background:var(--panel)}
.nav span{opacity:.35}
main{max-width:1400px;margin:0 auto;padding:20px}
.banner{background:#2a2110;border:1px solid #6b5420;color:#f5d58c;border-radius:var(--radius);padding:10px 16px;margin-bottom:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:18px}
.kpi{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px}
.kpi .l{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.kpi .v{font-size:26px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}
.kpi .d{font-size:12.5px;margin-top:2px;color:var(--muted)}
.up{color:var(--up)} .down{color:var(--down)}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px;align-items:center}
.filters .lbl{color:var(--muted);font-size:12px;margin-right:4px}
.chip{border:1px solid var(--line);background:var(--panel);color:var(--text);padding:6px 12px;border-radius:999px;cursor:pointer;font:inherit;font-size:12.5px}
.chip:hover{border-color:#3a4560}
.chip.on{background:var(--accent);color:#1a1300;border-color:var(--accent);font-weight:600}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:16px 16px 8px;min-width:0}
.card h2{font-size:15px;margin:0 0 2px} .card .sub{color:var(--muted);font-size:12.5px;margin-bottom:6px}
.card-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:999px;overflow:hidden}
.seg button{background:transparent;border:0;color:var(--muted);padding:5px 12px;font:inherit;font-size:12px;cursor:pointer}
.seg button.on{background:var(--panel2);color:var(--text)}
.s12{grid-column:span 12}.s8{grid-column:span 8}.s6{grid-column:span 6}.s4{grid-column:span 4}
@media(max-width:1000px){.s8,.s6,.s4{grid-column:span 12}}
.chart{height:400px}.chart.tall{height:460px}.chart.short{height:340px}
.tablewrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:13px}
th{position:sticky;top:0;background:var(--panel2);color:var(--muted);font-weight:600;text-align:right;padding:8px 10px;
  cursor:pointer;user-select:none;white-space:nowrap;border-bottom:1px solid var(--line)}
th:hover{color:var(--text)} th.sorted::after{content:" ▾"} th.sorted.asc::after{content:" ▴"}
td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th.t,td.t{text-align:left}
tbody tr:hover{background:var(--panel2)}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;font-weight:600}
.pill.Alliance{background:rgba(76,141,255,.15);color:var(--ali)} .pill.Horde{background:rgba(255,90,95,.15);color:var(--horde)}
.bar-cell{position:relative}
.bar-cell i{position:absolute;right:0;top:20%;height:60%;background:rgba(245,184,75,.14);border-radius:4px;z-index:0}
.bar-cell b{position:relative;z-index:1;font-weight:500}
details{margin-top:20px;color:var(--muted);background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:12px 16px}
summary{cursor:pointer;color:var(--text)}
.empty{color:var(--muted);padding:40px 0;text-align:center}
footer{color:var(--muted);font-size:12px;text-align:center;padding:24px}
</style></head>
<body>
<header><div class="bar">
  <div class="brand"><div class="coin"></div>WoW: Forever Gold · FunPay</div>
  <div class="meta">Report <b>__DAY__</b> · latest snapshot __LATEST__ · __SNAPS__ snapshots since __FIRST__ · EUR per unit</div>
  <div class="nav">__NAV__</div>
</div></header>
<main>
__BANNER__
<section class="kpis" id="kpis"></section>

<div class="filters" id="filters"><span class="lbl">Server</span></div>

<div class="grid">
  <div class="card s8"><div class="card-head"><div><h2 id="priceTitle">Price trend</h2>
    <div class="sub">Median (solid) · average of 5 cheapest (dashed) · cheapest (dotted)</div></div></div>
    <div id="priceChart" class="chart tall"></div></div>
  <div class="card s4"><h2>Price ladder</h2><div class="sub">Current median by server — cheapest at top</div>
    <div id="ladderChart" class="chart tall"></div></div>

  <div class="card s12"><div class="card-head"><div><h2>Market summary</h2>
    <div class="sub">Latest snapshot · click a column to sort · sales are for __DAY__</div></div></div>
    <div class="tablewrap"><table id="summary"></table></div></div>

  <div class="card s8"><div class="card-head"><div><h2>Estimated sales per day</h2><div class="sub">Stacked by server</div></div>
    <div class="seg" id="salesMetric"><button data-m="eur" class="on">EUR</button><button data-m="units">Units</button></div></div>
    <div id="salesChart" class="chart"></div></div>
  <div class="card s4"><h2>When do people buy?</h2><div class="sub">Average EUR sold per day, by snapshot time</div>
    <div id="hourChart" class="chart"></div></div>

  <div class="card s6"><h2>Listed stock</h2><div class="sub">Excluding placeholder amounts</div>
    <div id="stockChart" class="chart short"></div></div>
  <div class="card s6"><h2>Number of offers</h2><div class="sub">Competition per server</div>
    <div id="offersChart" class="chart short"></div></div>

  <div class="card s12"><h2>Top sellers · last 24 h</h2><div class="sub">By estimated sales value</div>
    <div class="tablewrap"><table id="top"></table></div></div>
</div>

<details><summary>How are sales estimated?</summary>
<p>FunPay does not publish sales, so they are estimated from stock going down on the same offer between two snapshots
(taken roughly every 3 hours). A seller lowering stock by hand counts as a sale, restocks between snapshots hide sales,
and offers removed entirely are not counted. Stock listed at __CAP__ or more is treated as a placeholder and ignored.
Treat the figures as a trend indicator rather than exact revenue.</p></details>
</main>
<footer>Generated __GEN__ · data from funpay.com/en/chips/258/</footer>

<script>
const D = __DATA__;
const C = {text:'#e6e9ef', muted:'#8a93a6', line:'#232b3b', ali:'#4c8dff', horde:'#ff5a5f'};
const SC = ['#4c8dff','#f5b84b','#3ecf8e','#c084fc','#ff5a5f','#22d3ee','#fb923c','#a3e635'];
const serverColor = s => SC[D.servers.indexOf(s) % SC.length];
const sideColor = s => s === 'Alliance' ? C.ali : C.horde;
const fmt = (v, d=0) => v == null ? '–' : Number(v).toLocaleString('en-US', {minimumFractionDigits:d, maximumFractionDigits:d});
const eur = (v, d=2) => v == null ? '–' : '€' + fmt(v, d);
const pct = v => v == null ? '–' : (v > 0 ? '+' : '') + (v*100).toFixed(1) + '%';
const cls = v => v == null || Math.abs(v) < 0.0005 ? '' : (v > 0 ? 'up' : 'down');
let server = 'All';

const layout = extra => Object.assign({
  paper_bgcolor:'rgba(0,0,0,0)', plot_bgcolor:'rgba(0,0,0,0)',
  font:{color:C.text, family:'Segoe UI, Inter, system-ui, sans-serif', size:12},
  margin:{l:52, r:14, t:58, b:36}, hovermode:'x unified',
  hoverlabel:{bgcolor:'#171d2a', bordercolor:C.line, font:{color:C.text}},
  xaxis:{gridcolor:C.line, linecolor:C.line, zeroline:false, tickformat:'%b %d', dtick:86400000},
  yaxis:{gridcolor:C.line, linecolor:C.line, zeroline:false},
  legend:{orientation:'h', x:0, y:1.02, yanchor:'bottom', font:{size:10.5, color:C.muted}},
}, extra || {});
const cfg = {displaylogo:false, responsive:true, modeBarButtonsToRemove:['lasso2d','select2d','autoScale2d']};
const plot = (id, data, lay) => window.Plotly ? Plotly.react(id, data, layout(lay), cfg)
  : (document.getElementById(id).innerHTML = '<div class="empty">Charts need an internet connection (plotly.js).</div>');
const empty = (id, msg) => { if (window.Plotly) Plotly.purge(id); document.getElementById(id).innerHTML = '<div class="empty">'+msg+'</div>'; };

// ---------- rows helper
const S = D.series, N = S.ts.length;
const rows = Array.from({length:N}, (_, i) => Object.fromEntries(Object.keys(S).map(k => [k, S[k][i]])));
const inServer = s => server === 'All' || s === server;

// ---------- KPIs
function kpis(){
  const k = D.kpis, el = document.getElementById('kpis');
  const card = (label, val, d, invert) => {
    const c = d == null ? '' : ((invert ? -d : d) > 0.0005 ? 'up' : ((invert ? -d : d) < -0.0005 ? 'down' : ''));
    return `<div class="kpi"><div class="l">${label}</div><div class="v">${val}</div>
      <div class="d ${c}">${d == null ? 'no comparison yet' : pct(d) + ' vs 24 h ago'}</div></div>`;
  };
  el.innerHTML = card('Offers listed', fmt(k.offers.v), k.offers.d)
    + card('Listed stock', fmt(k.stock.v), k.stock.d)
    + card('Cheapest · EU', eur(k.min_eu.v, 3), k.min_eu.d, true)
    + card('Cheapest · NA', eur(k.min_na.v, 3), k.min_na.d, true)
    + card('Est. units sold · day', fmt(k.units.v), k.units.d)
    + card('Est. sales value · day', eur(k.eur.v, 0), k.eur.d);
}

// ---------- filters
function filters(){
  const f = document.getElementById('filters');
  ['All', ...D.servers].forEach(s => {
    const b = document.createElement('button');
    b.className = 'chip' + (s === server ? ' on' : ''); b.textContent = s;
    b.onclick = () => { server = s; f.querySelectorAll('.chip').forEach(x => x.classList.toggle('on', x === b)); render(); };
    f.appendChild(b);
  });
}

// ---------- charts
function priceChart(){
  const traces = [];
  if (server === 'All') {
    document.getElementById('priceTitle').textContent = 'Price trend · all servers (avg of 5 cheapest)';
    D.servers.forEach(s => ['Alliance','Horde'].forEach(side => {
      const r = rows.filter(x => x.server === s && x.side === side);
      traces.push({x:r.map(x=>x.ts), y:r.map(x=>x.cheapest5_avg), name:`${s} ${side[0]}`, mode:'lines',
        line:{color:serverColor(s), width:1.6, dash: side === 'Horde' ? 'dot' : 'solid'},
        hovertemplate:'€%{y:.3f}<extra>%{fullData.name}</extra>'});
    }));
  } else {
    document.getElementById('priceTitle').textContent = 'Price trend · ' + server;
    ['Alliance','Horde'].forEach(side => {
      const r = rows.filter(x => x.server === server && x.side === side), col = sideColor(side);
      const t = (y, name, dash, w, show) => ({x:r.map(x=>x.ts), y:r.map(y), name:`${side} · ${name}`, mode:'lines',
        line:{color:col, width:w, dash, shape:'spline', smoothing:0.4}, legendgroup:side, showlegend:show,
        hovertemplate:'€%{y:.3f}<extra>%{fullData.name}</extra>'});
      traces.push(t(x=>x.median_price, 'median', 'solid', 2.4, true),
                  t(x=>x.cheapest5_avg, '5 cheapest', 'dash', 1.4, true),
                  t(x=>x.min_price, 'cheapest', 'dot', 1.2, true));
    });
  }
  plot('priceChart', traces, {yaxis:{gridcolor:C.line, tickprefix:'€', zeroline:false},
    xaxis:{gridcolor:C.line, tickformat:'%b %d', dtick:86400000,
      rangeslider:{visible:N > 40, bgcolor:'#0e131c', bordercolor:C.line, thickness:0.06}},
    margin:{l:52, r:14, t: server === 'All' ? 78 : 40, b:30}});
}

function ladderChart(){
  const latest = D.summary;  // always show every server; highlight the selected one
  const traces = ['Alliance','Horde'].map(side => {
    const r = latest.filter(x => x.side === side);
    return {type:'bar', orientation:'h', name:side, y:r.map(x=>x.server), x:r.map(x=>x.median),
      marker:{color:sideColor(side), line:{width:0}, opacity:r.map(x => inServer(x.server) ? 1 : 0.22)},
      hovertemplate:'%{y}<br>€%{x:.3f}<extra>'+side+'</extra>'};
  });
  const order = [...new Set(latest.map(x=>x.server))].sort((a,b) =>
    Math.min(...latest.filter(x=>x.server===b).map(x=>x.median)) - Math.min(...latest.filter(x=>x.server===a).map(x=>x.median)));
  plot('ladderChart', traces, {barmode:'group', bargap:0.25, hovermode:'closest', margin:{l:120,r:14,t:40,b:36},
    xaxis:{gridcolor:C.line, tickprefix:'€', zeroline:false}, yaxis:{categoryorder:'array', categoryarray:order}});
}

let salesMetric = 'eur';
function salesChart(){
  const d = D.daily, n = (d.day || []).length;
  if (!n) return empty('salesChart', 'Sales appear after the scraper has run at least twice.');
  const days = [...new Set(d.day)].sort();
  const traces = D.servers.filter(inServer).map(s => ({type:'bar', name:s, x:days,
    y:days.map(day => d.day.reduce((a, _, i) => a + (d.day[i]===day && d.server[i]===s ? d[salesMetric][i] : 0), 0)),
    marker:{color:serverColor(s)},
    hovertemplate: (salesMetric==='eur' ? '€%{y:,.0f}' : '%{y:,.0f} units') + '<extra>'+s+'</extra>'}));
  plot('salesChart', traces, {barmode:'stack', bargap:0.3, margin:{l:60,r:14,t: server==='All' ? 78 : 40, b:36},
    xaxis:{type:'category', gridcolor:C.line},
    yaxis:{gridcolor:C.line, tickprefix: salesMetric==='eur' ? '€' : '', zeroline:false}});
}

function hourChart(){
  const h = D.hourly;
  if (!(h.hour || []).length) return empty('hourChart', 'Not enough data yet.');
  const regions = server === 'All' ? ['EU','NA'] : [server.slice(1,3)];
  const traces = regions.map((r, j) => {
    const hrs = [...Array(24).keys()];
    return {type:'bar', name:r, x:hrs.map(x => String(x).padStart(2,'0')+':00'),
      y:hrs.map(x => h.hour.reduce((a,_,i) => a + (h.hour[i]===x && h.region[i]===r ? h.eur[i] : 0), 0)),
      marker:{color: j ? '#c084fc' : '#22d3ee'}, hovertemplate:'€%{y:,.0f}<extra>'+r+'</extra>'};
  });
  plot('hourChart', traces, {barmode:'group', margin:{l:56,r:14,t:40,b:56},
    xaxis:{type:'category', gridcolor:C.line, tickangle:-45},
    yaxis:{gridcolor:C.line, tickprefix:'€', zeroline:false}});
}

function supplyCharts(){
  const ts = [...new Set(S.ts)].sort();
  const sum = (s, field) => ts.map(t => rows.filter(x => x.ts===t && x.server===s).reduce((a,x)=>a+(x[field]||0),0));
  const mk = field => D.servers.filter(inServer).map(s => ({x:ts, y:sum(s, field), name:s, mode:'lines',
    line:{color:serverColor(s), width:1.8}, hovertemplate:'%{y:,.0f}<extra>'+s+'</extra>'}));
  const lay = {showlegend: server==='All', margin:{l:52, r:14, t: server==='All' ? 78 : 16, b:36}};
  plot('stockChart', mk('real_stock'), lay);
  plot('offersChart', mk('offers'), lay);
}

// ---------- tables
function sortable(table, cols, data, render, initial){
  let key = initial, asc = false;
  const draw = () => {
    const rowsSorted = [...data].sort((a,b) => {
      const x = a[key], y = b[key];
      const r = (x==null) - (y==null) || (typeof x === 'string' ? x.localeCompare(y) : x - y);
      return asc ? r : -r;
    });
    table.innerHTML = '<thead><tr>' + cols.map(c =>
      `<th data-k="${c.k}" class="${c.t?'t':''} ${c.k===key?'sorted'+(asc?' asc':''):''}">${c.h}</th>`).join('')
      + '</tr></thead><tbody>' + (rowsSorted.length ? rowsSorted.map(render).join('')
      : `<tr><td class="t" colspan="${cols.length}" style="color:var(--muted)">No data yet</td></tr>`) + '</tbody>';
    table.querySelectorAll('th').forEach(th => th.onclick = () => {
      if (key === th.dataset.k) asc = !asc; else { key = th.dataset.k; asc = !!cols.find(c=>c.k===key).t; }
      draw();
    });
  };
  draw();
}

function summaryTable(){
  const data = D.summary.filter(r => inServer(r.server));
  const maxE = Math.max(1, ...data.map(r => r.eur || 0));
  sortable(document.getElementById('summary'), [
    {k:'server',h:'Server',t:1},{k:'side',h:'Faction',t:1},{k:'offers',h:'Offers'},{k:'stock',h:'Stock'},
    {k:'min',h:'Cheapest'},{k:'p25',h:'25th pct'},{k:'median',h:'Median'},{k:'chg',h:'Median 24 h'},
    {k:'units',h:'Est. units'},{k:'eur',h:'Est. €'}], data, r => `<tr>
      <td class="t">${r.server}</td><td class="t"><span class="pill ${r.side}">${r.side}</span></td>
      <td>${fmt(r.offers)}</td><td>${fmt(r.stock)}</td><td>${eur(r.min,3)}</td><td>${eur(r.p25,3)}</td>
      <td><b>${eur(r.median,3)}</b></td><td class="${cls(r.chg)}">${pct(r.chg)}</td><td>${fmt(r.units)}</td>
      <td class="bar-cell"><i style="width:${(r.eur||0)/maxE*100}%"></i><b>${eur(r.eur,0)}</b></td></tr>`, 'eur');
}

function topTable(){
  const data = D.top.filter(r => inServer(r.server));
  sortable(document.getElementById('top'), [
    {k:'seller',h:'Seller',t:1},{k:'server',h:'Server',t:1},{k:'side',h:'Faction',t:1},
    {k:'units',h:'Est. units'},{k:'price',h:'Avg price'},{k:'eur',h:'Est. €'}], data, r => `<tr>
      <td class="t">${r.seller}</td><td class="t">${r.server}</td><td class="t"><span class="pill ${r.side}">${r.side}</span></td>
      <td>${fmt(r.units)}</td><td>${eur(r.price,3)}</td><td><b>${eur(r.eur,0)}</b></td></tr>`, 'eur');
}

document.getElementById('salesMetric').querySelectorAll('button').forEach(b => b.onclick = () => {
  salesMetric = b.dataset.m;
  b.parentNode.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
  salesChart();
});

function render(){ priceChart(); ladderChart(); summaryTable(); salesChart(); hourChart(); supplyCharts(); topTable(); }
kpis(); filters(); render();
</script></body></html>
"""

INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>FunPay Gold Reports</title>
<style>body{margin:0;background:#0b0e14;color:#e6e9ef;font:15px/1.5 "Segoe UI",system-ui,sans-serif}
main{max-width:720px;margin:0 auto;padding:40px 20px}h1{font-size:22px}p{color:#8a93a6}
a{display:flex;justify-content:space-between;padding:14px 18px;margin:8px 0;background:#121722;border:1px solid #232b3b;
border-radius:12px;color:#e6e9ef;text-decoration:none}a:hover{border-color:#f5b84b}a span{color:#8a93a6}</style></head>
<body><main><h1>WoW: Forever Gold · daily reports</h1><p>FunPay market snapshots, newest first.</p>__LIST__</main></body></html>"""


def write_archive(out_dir):
    days = sorted((p.stem.replace("report_", "") for p in out_dir.glob("report_*.html")), reverse=True)
    items = "".join(f'<a href="report_{d}.html"><b>{d}</b><span>{datetime.strptime(d, "%Y-%m-%d"):%A}</span></a>'
                    for d in days)
    (out_dir / "archive.html").write_text(INDEX.replace("__LIST__", items or "<p>No reports yet.</p>"), encoding="utf-8")
    return days


def build(data_dir, db, day, out_dir, banner=None):
    df = load(data_dir, db)
    if df.empty:
        raise SystemExit("no data in database yet")
    ps, sales, data = build_payload(df, day)

    # CSV exports (full history)
    out_dir.mkdir(parents=True, exist_ok=True)
    ps.to_csv(out_dir / "report_prices.csv", index=False)
    sales.groupby(["day", "server", "side"])[["units", "eur"]].sum().reset_index() \
        .to_csv(out_dir / "report_sales.csv", index=False)

    out_dir.mkdir(parents=True, exist_ok=True)
    day_s = data["day"]
    path = out_dir / f"report_{day_s}.html"
    path.write_text("", encoding="utf-8")  # so the index/nav knows this day exists
    days = sorted(write_archive(out_dir))
    i = days.index(day_s)
    prev_link = f'<a href="report_{days[i-1]}.html">← {days[i-1][5:]}</a>' if i > 0 else "<span>←</span>"
    next_link = f'<a href="report_{days[i+1]}.html">{days[i+1][5:]} →</a>' if i < len(days) - 1 else "<span>→</span>"
    nav = f'{prev_link}<a href="index.html">Latest</a><a href="archive.html">All reports</a>{next_link}'

    page = (PAGE.replace("__PLOTLY__", PLOTLY_CDN)
                .replace("__DAY__", day_s)
                .replace("__LATEST__", data["latest_ts"])
                .replace("__SNAPS__", str(data["snapshots"]))
                .replace("__FIRST__", data["first_ts"])
                .replace("__NAV__", nav)
                .replace("__CAP__", f"{STOCK_CAP:,}")
                .replace("__GEN__", datetime.now().strftime("%Y-%m-%d %H:%M"))
                .replace("__BANNER__", f'<div class="banner">{htmlmod.escape(banner)}</div>' if banner else "")
                .replace("__DATA__", json.dumps(data, default=str).replace("</", "<\\/")))
    path.write_text(page, encoding="utf-8")
    if i > 0:  # give the previous day's report a "next" link to this one
        prev_path = out_dir / f"report_{days[i-1]}.html"
        txt = prev_path.read_text(encoding="utf-8")
        if "<span>→</span>" in txt:
            prev_path.write_text(txt.replace("<span>→</span>",
                                             f'<a href="report_{day_s}.html">{day_s[5:]} →</a>', 1), encoding="utf-8")
    if i == len(days) - 1:
        (out_dir / "index.html").write_text(page, encoding="utf-8")  # site front page = latest
    print(f"wrote {path}")
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(HERE / "data"))
    ap.add_argument("--db", default=None, help="read an SQLite file instead of --data")
    ap.add_argument("--day", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--out", default=str(HERE / "docs"))
    ap.add_argument("--banner", default=None)
    a = ap.parse_args()
    build(a.data, a.db, datetime.strptime(a.day, "%Y-%m-%d").date(), Path(a.out), a.banner)
