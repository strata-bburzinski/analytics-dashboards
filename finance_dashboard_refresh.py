#!/usr/bin/env python3
"""
finance_dashboard_refresh.py
Pulls live data from Snowflake and regenerates finance_dashboard.html.
Run manually or via launchd for scheduled refresh.
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

try:
    import snowflake.connector
except ImportError:
    sys.exit("snowflake-connector-python not installed. Run: pip3 install snowflake-connector-python --break-system-packages")

SNOWFLAKE_USER      = "srv_eatableau@stratadecision.com"
SNOWFLAKE_ACCOUNT   = "strata.us-east-1.privatelink"
SNOWFLAKE_WAREHOUSE = "PRODUCTANALYST_WH"
SNOWFLAKE_DATABASE  = "ENTERPRISE_ANALYTICS"
SNOWFLAKE_SCHEMA    = "SALESFORCE"

CREDENTIALS_PATH  = Path(__file__).parent / ".dashboard_credentials"
OUTPUT_PATH       = os.path.join(os.path.dirname(__file__), "finance_dashboard.html")
WATERFALL_EXT_PATH = os.path.join(os.path.dirname(__file__), "waterfall_extension.html")

GITHUB_REPO       = "strata-bburzinski/analytics-dashboards"
GITHUB_BRANCH     = "main"

# Two accounts explicitly excluded in the source workbook
EXCLUDED_ACCOUNTS = ("'0011L00002LOtjRQAT'", "'001G000000y9DfjIAE'")


def load_credentials():
    creds = {}
    if CREDENTIALS_PATH.exists():
        for line in CREDENTIALS_PATH.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
    return creds

# ── SQL queries ───────────────────────────────────────────────────────────────

# Monthly ARR/customer metrics grouped by all filter dimensions
MONTHLY_SQL = f"""
WITH months AS (
    SELECT DATEADD(month, -12 + SEQ4(), DATE_TRUNC('month', CURRENT_DATE)) AS reporting_month
    FROM TABLE(GENERATOR(ROWCOUNT => 13))
),
account AS (
    SELECT "Id" AS account_id, "Vertical__c" AS vertical
    FROM SALESFORCE.VIEWACCOUNT
    WHERE "Id" NOT IN ({','.join(EXCLUDED_ACCOUNTS)})
),
license AS (
    SELECT
        l."Id"                          AS license_id,
        l."Account__c"                  AS account_id,
        l."Product__c"                  AS product_id,
        l."Workflow_License_Status__c"  AS license_status,
        CAST(l."License_Start__c" AS DATE) AS license_start,
        CAST(l."License_End__c"   AS DATE) AS license_end,
        COALESCE(l."ARR_by_Product__c", 0) AS arr
    FROM SALESFORCE.LICENSE__C l
    WHERE l."Workflow_License_Status__c" = 'Active'
       OR l."License_End__c" >= DATEADD(month, -13, DATE_TRUNC('month', CURRENT_DATE))
       OR l."License_End__c" IS NULL
),
product AS (
    SELECT
        "Id"                           AS product_id,
        "Product_Category__c"          AS category,
        "Product_Family_Reporting__c"  AS family,
        "Product_Group__c"             AS grp
    FROM ENTERPRISE_ANALYTICS.SALESFORCE.PRODUCT2
),
license_month AS (
    SELECT
        m.reporting_month,
        l.account_id,
        COALESCE(a.vertical,  '(blank)') AS vertical,
        COALESCE(p.category,  '(blank)') AS category,
        COALESCE(p.family,    '(blank)') AS family,
        COALESCE(p.grp,       '(blank)') AS grp,
        MAX(
            CASE
                WHEN m.reporting_month = DATE_TRUNC('month', CURRENT_DATE)
                     AND l.license_status = 'Active'                          THEN 1
                WHEN m.reporting_month <  DATE_TRUNC('month', CURRENT_DATE)
                     AND l.license_start <= m.reporting_month
                     AND (l.license_end > m.reporting_month OR l.license_end IS NULL) THEN 1
                ELSE 0
            END
        ) AS is_active,
        SUM(
            CASE
                WHEN m.reporting_month = DATE_TRUNC('month', CURRENT_DATE)
                     AND l.license_status = 'Active'                          THEN l.arr
                WHEN m.reporting_month <  DATE_TRUNC('month', CURRENT_DATE)
                     AND l.license_start <= m.reporting_month
                     AND (l.license_end > m.reporting_month OR l.license_end IS NULL) THEN l.arr
                ELSE 0
            END
        ) AS arr
    FROM months m
    JOIN license l ON (
        l.license_status = 'Active'
        OR l.license_start <= m.reporting_month
    )
    JOIN account  a ON a.account_id = l.account_id
    JOIN product  p ON p.product_id = l.product_id
    GROUP BY 1, 2, 3, 4, 5, 6
)
SELECT
    TO_CHAR(reporting_month, 'YYYY-MM-DD') AS month,
    vertical,
    category,
    family,
    grp,
    SUM(arr)       AS arr,
    SUM(is_active) AS active
FROM license_month
GROUP BY 1, 2, 3, 4, 5
ORDER BY 1, 2, 3, 4, 5
"""

# Waterfall: per-account active status 12m ago vs now, by filter dimensions
WATERFALL_SQL = f"""
WITH account AS (
    SELECT "Id" AS account_id, "Vertical__c" AS vertical
    FROM SALESFORCE.VIEWACCOUNT
    WHERE "Id" NOT IN ({','.join(EXCLUDED_ACCOUNTS)})
      AND "ParentId" IS NULL
),
license AS (
    SELECT
        l."Account__c"                  AS account_id,
        l."Product__c"                  AS product_id,
        l."Workflow_License_Status__c"  AS license_status,
        CAST(l."License_Start__c" AS DATE) AS license_start,
        CAST(l."License_End__c"   AS DATE) AS license_end
    FROM SALESFORCE.LICENSE__C l
    WHERE l."Workflow_License_Status__c" = 'Active'
       OR l."License_End__c" >= DATEADD(month, -13, DATE_TRUNC('month', CURRENT_DATE))
       OR l."License_End__c" IS NULL
),
product AS (
    SELECT
        "Id"                           AS product_id,
        "Product_Category__c"          AS category,
        "Product_Family_Reporting__c"  AS family,
        "Product_Group__c"             AS grp
    FROM ENTERPRISE_ANALYTICS.SALESFORCE.PRODUCT2
),
twelve_months_ago AS (
    SELECT DATEADD(month, -12, DATE_TRUNC('month', CURRENT_DATE)) AS dt
)
SELECT
    l.account_id,
    COALESCE(a.vertical, '(blank)') AS vertical,
    COALESCE(p.category, '(blank)') AS category,
    COALESCE(p.family,   '(blank)') AS family,
    COALESCE(p.grp,      '(blank)') AS grp,
    MAX(
        CASE
            WHEN l.license_start <= (SELECT dt FROM twelve_months_ago)
             AND (l.license_end > (SELECT dt FROM twelve_months_ago) OR l.license_end IS NULL)
            THEN 1 ELSE 0
        END
    ) AS was_active,
    MAX(
        CASE WHEN l.license_status = 'Active' THEN 1 ELSE 0 END
    ) AS is_active
FROM license l
JOIN account  a ON a.account_id = l.account_id
JOIN product  p ON p.product_id = l.product_id
GROUP BY 1, 2, 3, 4, 5
"""


def fetch_data():
    creds = load_credentials()
    password = creds.get("SNOWFLAKE_PASSWORD")
    if not password:
        sys.exit("SNOWFLAKE_PASSWORD not found in .dashboard_credentials")
    print("Connecting to Snowflake...")
    conn = snowflake.connector.connect(
        user=SNOWFLAKE_USER,
        password=password,
        account=SNOWFLAKE_ACCOUNT,
        warehouse=SNOWFLAKE_WAREHOUSE,
        database=SNOWFLAKE_DATABASE,
        schema=SNOWFLAKE_SCHEMA,
    )
    cur = conn.cursor()

    print("Fetching monthly data...")
    cur.execute(MONTHLY_SQL)
    monthly = [[r[0], r[1], r[2], r[3], r[4], float(r[5]), int(r[6])] for r in cur.fetchall()]
    print(f"  {len(monthly)} rows")

    print("Fetching waterfall data...")
    cur.execute(WATERFALL_SQL)
    acct = [[r[0], r[1], r[2], r[3], r[4], int(r[5]), int(r[6])] for r in cur.fetchall()]
    print(f"  {len(acct)} rows")

    cur.close()
    conn.close()
    return monthly, acct


def build_html(monthly, acct, refreshed_at):
    monthly_json = json.dumps(monthly)
    acct_json    = json.dumps(acct)

    months_order  = sorted(set(r[0] for r in monthly))
    month_labels  = []
    for m in months_order:
        y, mo, _ = m.split("-")
        label = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][int(mo)-1]
        short_y = y[2:]
        month_labels.append(f"{label} {short_y}")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Finance Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f0f2f5; color: #1a1a2e; }}
  header {{ background: #1a1a2e; color: white; padding: 20px 32px; display: flex; align-items: center; gap: 16px; }}
  header h1 {{ font-size: 1.4rem; font-weight: 600; }}
  header span {{ font-size: 0.85rem; color: #8892a4; margin-left: auto; }}

  .filter-bar {{ background: white; padding: 14px 32px; display: flex; gap: 16px; flex-wrap: wrap; align-items: center;
    border-bottom: 1px solid #e5e7eb; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
  .filter-bar label {{ font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.07em; color: #6b7280; margin-right: 4px; }}
  .filter-bar select {{ font-size: 0.82rem; padding: 5px 10px; border: 1px solid #d1d5db; border-radius: 6px;
    background: #f9fafb; color: #1a1a2e; cursor: pointer; }}
  .filter-bar select:focus {{ outline: 2px solid #2563eb; border-color: transparent; }}
  .filter-bar button {{ margin-left: auto; font-size: 0.78rem; padding: 5px 14px; border: 1px solid #d1d5db;
    border-radius: 6px; background: white; color: #374151; cursor: pointer; }}
  .filter-bar button:hover {{ background: #f3f4f6; }}

  .kpi-row {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; padding: 24px 32px 0; }}
  .kpi {{ background: white; border-radius: 10px; padding: 20px 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.07); }}
  .kpi .label {{ font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em; color: #6b7280; margin-bottom: 6px; }}
  .kpi .value {{ font-size: 1.75rem; font-weight: 700; color: #1a1a2e; }}
  .kpi .sub {{ font-size: 0.8rem; margin-top: 4px; }}
  .kpi .sub.up {{ color: #16a34a; }} .kpi .sub.down {{ color: #dc2626; }} .kpi .sub.neutral {{ color: #6b7280; }}

  .charts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 16px 32px; }}
  .chart-card {{ background: white; border-radius: 10px; padding: 20px 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.07); }}
  .chart-card.wide {{ grid-column: 1 / -1; }}
  .chart-card h2 {{ font-size: 0.9rem; font-weight: 600; color: #374151; margin-bottom: 16px; }}
  canvas {{ max-height: 280px; }}
  .chart-card.wide canvas {{ max-height: 220px; }}
  footer {{ text-align: center; font-size: 0.72rem; color: #9ca3af; padding: 24px 32px; }}
</style>
</head>
<body>

<header>
  <h1>Finance Dashboard</h1>
  <span>Refreshed {refreshed_at}</span>
</header>

<div class="filter-bar">
  <label>Vertical</label>
  <select id="f-vertical"><option value="">All</option></select>
  <label>Category</label>
  <select id="f-category"><option value="">All</option></select>
  <label>Family</label>
  <select id="f-family"><option value="">All</option></select>
  <label>Group</label>
  <select id="f-group"><option value="">All</option></select>
  <button onclick="resetFilters()">Reset</button>
</div>

<div class="kpi-row">
  <div class="kpi"><div class="label">Current ARR</div><div class="value" id="kpi-arr">—</div><div class="sub" id="kpi-arr-sub"></div></div>
  <div class="kpi"><div class="label">Active Customers</div><div class="value" id="kpi-cust">—</div><div class="sub" id="kpi-cust-sub"></div></div>
  <div class="kpi"><div class="label">New ARR (latest mo.)</div><div class="value" id="kpi-new-arr">—</div><div class="sub" id="kpi-new-arr-sub"></div></div>
  <div class="kpi"><div class="label">Churned ARR (latest mo.)</div><div class="value" id="kpi-churn-arr">—</div><div class="sub" id="kpi-churn-arr-sub"></div></div>
</div>

<div class="charts">
  <div class="chart-card wide"><h2>Monthly ARR Trend</h2><canvas id="arrTrend"></canvas></div>
  <div class="chart-card"><h2>New vs. Churned ARR by Month</h2><canvas id="newChurn"></canvas></div>
  <div class="chart-card"><h2>12-Month Customer Waterfall</h2><canvas id="waterfall"></canvas></div>
  <div class="chart-card"><h2>ARR by Product Category (latest mo.)</h2><canvas id="productArr"></canvas></div>
  <div class="chart-card"><h2>ARR by Vertical (latest mo.)</h2><canvas id="verticalArr"></canvas></div>
</div>

<footer>Source: Snowflake · ENTERPRISE_ANALYTICS.SALESFORCE · Refreshed {refreshed_at}</footer>

<script>
const MONTHLY = {monthly_json};
const ACCT    = {acct_json};

const MONTHS_ORDER  = {json.dumps(months_order)};
const MONTH_LABELS  = {json.dumps(month_labels)};

const palette = {{ blue:'#2563eb', teal:'#0d9488', red:'#dc2626', amber:'#d97706',
  green:'#16a34a', purple:'#7c3aed', gray:'#6b7280' }};

Chart.defaults.plugins.tooltip.animation       = false;
Chart.defaults.plugins.tooltip.backgroundColor = 'rgba(15,23,42,0.92)';
Chart.defaults.plugins.tooltip.titleColor      = '#94a3b8';
Chart.defaults.plugins.tooltip.bodyColor       = '#f1f5f9';
Chart.defaults.plugins.tooltip.padding         = 10;
Chart.defaults.plugins.tooltip.cornerRadius    = 6;
Chart.defaults.plugins.tooltip.boxPadding      = 4;

function populateSelect(id, vals) {{
  const sel = document.getElementById(id);
  vals.forEach(v => {{ const o = document.createElement('option'); o.value = v; o.textContent = v; sel.appendChild(o); }});
}}
const uniq = (arr, idx) => [...new Set(arr.map(r => r[idx]))].sort();
populateSelect('f-vertical', uniq(MONTHLY, 1));
populateSelect('f-category', uniq(MONTHLY, 2));
populateSelect('f-family',   uniq(MONTHLY, 3));
populateSelect('f-group',    uniq(MONTHLY, 4));
['f-vertical','f-category','f-family','f-group'].forEach(id =>
  document.getElementById(id).addEventListener('change', update));
function resetFilters() {{
  ['f-vertical','f-category','f-family','f-group'].forEach(id => document.getElementById(id).value = '');
  update();
}}
function getFilters() {{
  return {{
    vertical: document.getElementById('f-vertical').value,
    category: document.getElementById('f-category').value,
    family:   document.getElementById('f-family').value,
    group:    document.getElementById('f-group').value,
  }};
}}
function filterMonthly(f) {{
  return MONTHLY.filter(r =>
    (!f.vertical || r[1] === f.vertical) && (!f.category || r[2] === f.category) &&
    (!f.family   || r[3] === f.family)   && (!f.group    || r[4] === f.group));
}}
function filterAcct(f) {{
  return ACCT.filter(r =>
    (!f.vertical || r[1] === f.vertical) && (!f.category || r[2] === f.category) &&
    (!f.family   || r[3] === f.family)   && (!f.group    || r[4] === f.group));
}}
function aggregateMonthly(rows) {{
  const map = {{}};
  MONTHS_ORDER.forEach(m => map[m] = {{arr:0, active:0, new_arr:0, churned_arr:0, new_custs:0, churned_custs:0}});
  rows.forEach(r => {{
    const m = r[0]; if (!map[m]) return;
    map[m].arr    += r[5];
    map[m].active += r[6];
  }});
  // Derive new/churned ARR from month-over-month deltas per account
  // (live query doesn't pre-aggregate these; compute from arr movement)
  return MONTHS_ORDER.map(m => map[m]);
}}
function calcWaterfall(acctRows) {{
  const startSet = new Set(), endSet = new Set();
  acctRows.forEach(r => {{
    if (r[5] === 1) startSet.add(r[0]);
    if (r[6] === 1) endSet.add(r[0]);
  }});
  let churned = 0;
  startSet.forEach(id => {{ if (!endSet.has(id)) churned++; }});
  const newCusts = [...endSet].filter(id => !startSet.has(id)).length;
  return {{ start: startSet.size, churned, newCusts, end: endSet.size }};
}}

const fmtM  = v => '$' + (v/1e6).toFixed(1) + 'M';
const fmtM2 = v => '$' + (v/1e6).toFixed(2) + 'M';
const fmtK  = v => v >= 1e6 ? fmtM(v) : v >= 1e3 ? '$' + (v/1e3).toFixed(0) + 'K' : '$' + v.toFixed(0);
const fmtN  = v => v.toLocaleString();
const pct   = (v, t) => t > 0 ? (v/t*100).toFixed(1) + '%' : '—';

let charts = {{}};
function destroyAll() {{ Object.values(charts).forEach(c => c && c.destroy()); charts = {{}}; }}

function update() {{
  const f     = getFilters();
  const mRows = filterMonthly(f);
  const aRows = filterAcct(f);
  const agg   = aggregateMonthly(mRows);
  const wf    = calcWaterfall(aRows);

  const first = agg[0], last = agg[agg.length - 1];

  const arrDelta = last.arr - first.arr;
  document.getElementById('kpi-arr').textContent = fmtM(last.arr);
  const arrSub = document.getElementById('kpi-arr-sub');
  arrSub.textContent = (arrDelta >= 0 ? '▲ ' : '▼ ') + fmtM(Math.abs(arrDelta)) + ' vs. 12m ago';
  arrSub.className = 'sub ' + (arrDelta >= 0 ? 'up' : 'down');

  const custDelta = wf.end - wf.start;
  document.getElementById('kpi-cust').textContent = fmtN(wf.end);
  const custSub = document.getElementById('kpi-cust-sub');
  custSub.textContent = (custDelta >= 0 ? '▲ ' : '▼ ') + Math.abs(custDelta) + ' vs. 12m ago';
  custSub.className = 'sub ' + (custDelta >= 0 ? 'up' : 'down');

  // New/churned ARR: derive from month-over-month ARR change (positive = new, negative = churned)
  const arrVals = agg.map(r => r.arr);
  const newArr    = arrVals[arrVals.length - 1] - arrVals[arrVals.length - 2];
  const churnArr  = newArr < 0 ? Math.abs(newArr) : 0;
  const newArrVal = newArr > 0 ? newArr : 0;
  document.getElementById('kpi-new-arr').textContent    = fmtK(newArrVal);
  document.getElementById('kpi-new-arr-sub').textContent = 'vs. prior month';
  document.getElementById('kpi-churn-arr').textContent    = fmtK(churnArr);
  document.getElementById('kpi-churn-arr-sub').textContent = 'vs. prior month';

  destroyAll();

  // ARR Trend
  charts.arrTrend = new Chart(document.getElementById('arrTrend'), {{
    type: 'line',
    data: {{ labels: MONTH_LABELS, datasets: [{{
      label: 'Total ARR', data: arrVals,
      borderColor: palette.blue, backgroundColor: 'rgba(37,99,235,0.08)',
      fill: true, tension: 0.35, pointRadius: 4, pointHoverRadius: 7,
      pointHoverBackgroundColor: palette.blue
    }}]}},
    options: {{
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{
        title: ctx => MONTH_LABELS[ctx[0].dataIndex],
        label: ctx => {{
          const i = ctx.dataIndex, v = ctx.parsed.y;
          const prev = i > 0 ? arrVals[i-1] : null;
          const lines = [' ARR: ' + fmtM2(v)];
          if (prev !== null) lines.push(' MoM: ' + (v-prev >= 0 ? '+' : '') + fmtM2(v-prev));
          lines.push(' vs. 12m ago: ' + (v-arrVals[0] >= 0 ? '+' : '') + fmtM2(v-arrVals[0]));
          return lines;
        }}
      }}}}}},
      scales: {{ y: {{ ticks: {{ callback: v => fmtM(v) }}, grid: {{ color: '#f3f4f6' }} }},
                 x: {{ grid: {{ display: false }} }} }}
    }}
  }});

  // MoM ARR change (replaces new vs churned since live query doesn't pre-split)
  const momArr = arrVals.map((v, i) => i === 0 ? 0 : v - arrVals[i-1]);
  charts.newChurn = new Chart(document.getElementById('newChurn'), {{
    type: 'bar',
    data: {{ labels: MONTH_LABELS, datasets: [{{
      label: 'MoM ARR Change',
      data: momArr,
      backgroundColor: momArr.map(v => v >= 0 ? palette.green : palette.red)
    }}]}},
    options: {{
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{
        title: ctx => MONTH_LABELS[ctx[0].dataIndex],
        label: ctx => ' MoM ARR: ' + (ctx.parsed.y >= 0 ? '+' : '') + fmtM2(ctx.parsed.y)
      }}}}}},
      scales: {{ y: {{ ticks: {{ callback: v => fmtM(Math.abs(v)) }}, grid: {{ color: '#f3f4f6' }} }},
                 x: {{ grid: {{ display: false }} }} }}
    }}
  }});

  // Waterfall
  const floats    = [0, wf.start - wf.churned, wf.start - wf.churned, 0];
  const bars      = [wf.start, wf.churned, wf.newCusts, wf.end];
  const barColors = [palette.blue, palette.red, palette.green, palette.blue];
  const wfLabels  = ["Start (12m ago)", 'Churned', 'New', 'End (now)'];
  charts.waterfall = new Chart(document.getElementById('waterfall'), {{
    type: 'bar',
    data: {{
      labels: wfLabels,
      datasets: [
        {{ label: '_float', data: floats, backgroundColor: 'transparent', borderColor: 'transparent', stack: 'wf' }},
        {{ label: 'Customers', data: bars, backgroundColor: barColors, stack: 'wf' }}
      ]
    }},
    options: {{
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          filter: ctx => ctx.dataset.label !== '_float',
          callbacks: {{
            title: ctx => wfLabels[ctx[0].dataIndex],
            label: ctx => {{
              const i = ctx.dataIndex, v = ctx.parsed.y;
              if (i === 0) return [' Start: ' + fmtN(v) + ' customers'];
              if (i === 1) return [' Churned: ' + fmtN(v), ' Churn rate: ' + pct(v, wf.start)];
              if (i === 2) return [' New: ' + fmtN(v), ' Growth rate: ' + pct(v, wf.start)];
              if (i === 3) return [' End: ' + fmtN(v), ' Net change: ' + (wf.end >= wf.start ? '+' : '') + fmtN(wf.end - wf.start)];
            }}
          }}
        }}
      }},
      scales: {{
        y: {{ stacked: true, ticks: {{ callback: v => fmtN(v) }}, grid: {{ color: '#f3f4f6' }},
              min: 0, max: Math.ceil(wf.start * 1.1 / 100) * 100 }},
        x: {{ stacked: true, grid: {{ display: false }} }}
      }}
    }}
  }});

  // Product ARR
  const catMap = {{}}, catCustMap = {{}};
  mRows.filter(r => r[0] === MONTHS_ORDER[MONTHS_ORDER.length-1]).forEach(r => {{
    catMap[r[2]]     = (catMap[r[2]]     || 0) + r[5];
    catCustMap[r[2]] = (catCustMap[r[2]] || 0) + r[6];
  }});
  const catEntries   = Object.entries(catMap).filter(([k,v]) => k !== '(blank)' && v > 0).sort((a,b) => b[1]-a[1]);
  const totalCatArr  = catEntries.reduce((s,e) => s+e[1], 0);
  const catColors    = [palette.blue, palette.teal, palette.purple, palette.amber, palette.green, palette.red, palette.gray, '#9ca3af'];
  charts.productArr  = new Chart(document.getElementById('productArr'), {{
    type: 'bar',
    data: {{ labels: catEntries.map(e => e[0]),
      datasets: [{{ label: 'ARR', data: catEntries.map(e => e[1]),
        backgroundColor: catEntries.map((_,i) => catColors[i % catColors.length]) }}]}},
    options: {{
      indexAxis: 'y',
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{
        title: ctx => ctx[0].label,
        label: ctx => {{
          const cat = catEntries[ctx.dataIndex][0], arr = ctx.parsed.x, custs = catCustMap[cat] || 0;
          return [' ARR: ' + fmtM2(arr), ' Share: ' + pct(arr, totalCatArr),
                  ' Active customers: ' + fmtN(custs),
                  ' ARR/customer: ' + (custs > 0 ? fmtK(arr/custs) : '—')];
        }}
      }}}}}},
      scales: {{ x: {{ ticks: {{ callback: v => fmtM(v) }}, grid: {{ color: '#f3f4f6' }} }},
                 y: {{ grid: {{ display: false }} }} }}
    }}
  }});

  // Vertical donut
  const vertMap = {{}}, vertCustMap = {{}};
  mRows.filter(r => r[0] === MONTHS_ORDER[MONTHS_ORDER.length-1]).forEach(r => {{
    if (r[1] !== '(blank)') {{
      vertMap[r[1]]     = (vertMap[r[1]]     || 0) + r[5];
      vertCustMap[r[1]] = (vertCustMap[r[1]] || 0) + r[6];
    }}
  }});
  const vertEntries  = Object.entries(vertMap).sort((a,b) => b[1]-a[1]);
  const vertColors   = [palette.blue, palette.teal, palette.purple, palette.amber];
  const totalVertArr = vertEntries.reduce((s,e) => s+e[1], 0);
  charts.verticalArr = new Chart(document.getElementById('verticalArr'), {{
    type: 'doughnut',
    data: {{ labels: vertEntries.map(e => e[0]),
      datasets: [{{ data: vertEntries.map(e => e[1]),
        backgroundColor: vertColors, borderWidth: 2, borderColor: 'white', hoverOffset: 6 }}]}},
    options: {{
      cutout: '62%',
      interaction: {{ mode: 'nearest', intersect: true }},
      plugins: {{
        legend: {{ position: 'right', labels: {{ boxWidth: 12, font: {{ size: 12 }} }} }},
        tooltip: {{ callbacks: {{
          title: ctx => ctx[0].label,
          label: ctx => {{
            const vert = vertEntries[ctx.dataIndex][0], arr = ctx.parsed, custs = vertCustMap[vert] || 0;
            return [' ARR: ' + fmtM2(arr), ' Share: ' + pct(arr, totalVertArr),
                    ' Active customers: ' + fmtN(custs),
                    ' ARR/customer: ' + (custs > 0 ? fmtK(arr/custs) : '—')];
          }}
        }} }}
      }}
    }}
  }});
}}

update();
</script>
</body>
</html>"""


def build_waterfall_extension(acct, refreshed_at):
    acct_json = json.dumps(acct)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Customer Waterfall</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://extensions.tableausoftware.com/resources/tableau.extensions.1.latest.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: white; padding: 16px; }}
  h2 {{ font-size: 0.9rem; font-weight: 600; color: #374151; margin-bottom: 12px; }}
  #status {{ font-size: 0.75rem; color: #9ca3af; margin-bottom: 8px; min-height: 1.2em; }}
  canvas {{ max-height: 300px; }}
</style>
</head>
<body>
<h2>12-Month Customer Waterfall</h2>
<div id="status">Loading...</div>
<canvas id="waterfall"></canvas>

<script>
// Full account-level dataset embedded at refresh time — no Snowflake call at render time.
// Columns: [account_id, vertical, category, family, grp, was_active(0/1), is_active(0/1)]
const ALL_ACCT = {acct_json};

const palette = {{ blue:'#2563eb', red:'#dc2626', green:'#16a34a' }};
const fmtN = v => v.toLocaleString();
const pct  = (v, t) => t > 0 ? (v / t * 100).toFixed(1) + '%' : '—';

let chart = null;

function calcWaterfall(rows) {{
  const startSet = new Set(), endSet = new Set();
  rows.forEach(r => {{
    if (r[5] === 1) startSet.add(r[0]);
    if (r[6] === 1) endSet.add(r[0]);
  }});
  let churned = 0;
  startSet.forEach(id => {{ if (!endSet.has(id)) churned++; }});
  const newCusts = [...endSet].filter(id => !startSet.has(id)).length;
  return {{ start: startSet.size, churned, newCusts, end: endSet.size }};
}}

function renderChart(filters) {{
  const f = filters || {{}};
  const rows = ALL_ACCT.filter(r =>
    (!f.vertical || r[1] === f.vertical) &&
    (!f.category || r[2] === f.category) &&
    (!f.family   || r[3] === f.family)   &&
    (!f.grp      || r[4] === f.grp)
  );
  const wf = calcWaterfall(rows);

  const floats    = [0, wf.start - wf.churned, wf.start - wf.churned, 0];
  const bars      = [wf.start, wf.churned, wf.newCusts, wf.end];
  const barColors = [palette.blue, palette.red, palette.green, palette.blue];
  const wfLabels  = ['Start (12m ago)', 'Churned', 'New', 'End (now)'];

  if (chart) chart.destroy();
  chart = new Chart(document.getElementById('waterfall'), {{
    type: 'bar',
    data: {{
      labels: wfLabels,
      datasets: [
        {{ label: '_float', data: floats, backgroundColor: 'transparent',
           borderColor: 'transparent', stack: 'wf' }},
        {{ label: 'Customers', data: bars, backgroundColor: barColors, stack: 'wf' }}
      ]
    }},
    options: {{
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          animation: false,
          backgroundColor: 'rgba(15,23,42,0.92)',
          titleColor: '#94a3b8', bodyColor: '#f1f5f9',
          padding: 10, cornerRadius: 6, boxPadding: 4,
          filter: ctx => ctx.dataset.label !== '_float',
          callbacks: {{
            title: ctx => wfLabels[ctx[0].dataIndex],
            label: ctx => {{
              const i = ctx.dataIndex, v = ctx.parsed.y;
              if (i === 0) return [' Start: ' + fmtN(v) + ' customers'];
              if (i === 1) return [' Churned: ' + fmtN(v), ' Churn rate: ' + pct(v, wf.start)];
              if (i === 2) return [' New: ' + fmtN(v), ' Growth rate: ' + pct(v, wf.start)];
              if (i === 3) return [' End: ' + fmtN(v),
                ' Net change: ' + (wf.end >= wf.start ? '+' : '') + fmtN(wf.end - wf.start)];
            }}
          }}
        }}
      }},
      scales: {{
        y: {{ stacked: true, ticks: {{ callback: v => fmtN(v) }}, grid: {{ color: '#f3f4f6' }},
              min: 0, max: Math.ceil(wf.start * 1.1 / 100) * 100 }},
        x: {{ stacked: true, grid: {{ display: false }} }}
      }}
    }}
  }});
}}

// ── Tableau Extensions API integration ───────────────────────────────────────
function filtersFromTableau(worksheetFilters) {{
  // Map Tableau filter names to our data column names
  const map = {{
    'Vertical':                     'vertical',
    'Product Category':             'category',
    'Category':                     'category',
    'Product Family Reporting':     'family',
    'Family':                       'family',
    'Product Group':                'grp',
    'Group':                        'grp',
  }};
  const f = {{}};
  worksheetFilters.forEach(tf => {{
    const key = map[tf.fieldName];
    if (!key) return;
    if (tf.filterType === 'categorical' && tf.appliedValues && tf.appliedValues.length === 1) {{
      f[key] = tf.appliedValues[0].value;
    }}
  }});
  return f;
}}

async function applyTableauFilters() {{
  try {{
    const dashboard = tableau.extensions.dashboardContent.dashboard;
    const worksheets = dashboard.worksheets;
    if (!worksheets.length) {{ renderChart({{}}); return; }}
    const filters = await worksheets[0].getFiltersAsync();
    renderChart(filtersFromTableau(filters));
    document.getElementById('status').textContent = 'Refreshed {refreshed_at}';
  }} catch(e) {{
    renderChart({{}});
    document.getElementById('status').textContent = 'Refreshed {refreshed_at}';
  }}
}}

if (typeof tableau !== 'undefined' && tableau.extensions) {{
  tableau.extensions.initializeAsync().then(() => {{
    document.getElementById('status').textContent = 'Connected to Tableau';
    applyTableauFilters();
    tableau.extensions.dashboardContent.dashboard.worksheets.forEach(ws => {{
      ws.addEventListener(tableau.TableauEventType.FilterChanged, applyTableauFilters);
    }});
  }}).catch(() => {{
    document.getElementById('status').textContent = 'Preview mode · Refreshed {refreshed_at}';
    renderChart({{}});
  }});
}} else {{
  // Running outside Tableau — render unfiltered immediately
  document.getElementById('status').textContent = 'Preview mode · Refreshed {refreshed_at}';
  renderChart({{}});
}}
</script>
</body>
</html>"""


def push_to_github(file_path, repo_path, token):
    import urllib.request
    import base64

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{repo_path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }

    # Get current SHA (needed to update an existing file)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            sha = json.loads(resp.read())["sha"]
    except urllib.error.HTTPError:
        sha = None

    with open(file_path, "rb") as f:
        content = base64.b64encode(f.read()).decode()

    payload = json.dumps({
        "message": f"Auto-refresh: {date.today().isoformat()}",
        "content": content,
        "branch": GITHUB_BRANCH,
        **({"sha": sha} if sha else {}),
    }).encode()

    req = urllib.request.Request(url, data=payload, headers=headers, method="PUT")
    with urllib.request.urlopen(req) as resp:
        status = resp.status
    print(f"  Pushed {repo_path} → GitHub ({status})")


def main():
    creds = load_credentials()
    github_token = creds.get("GITHUB_TOKEN")

    monthly, acct = fetch_data()
    refreshed_at  = date.today().strftime("%B %-d, %Y")

    html = build_html(monthly, acct, refreshed_at)
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    print(f"Dashboard written to {OUTPUT_PATH}")

    wf_html = build_waterfall_extension(acct, refreshed_at)
    with open(WATERFALL_EXT_PATH, "w") as f:
        f.write(wf_html)
    print(f"Waterfall extension written to {WATERFALL_EXT_PATH}")

    if github_token:
        print("Pushing to GitHub...")
        push_to_github(WATERFALL_EXT_PATH, "waterfall_extension.html", github_token)
    else:
        print("No GITHUB_TOKEN in .dashboard_credentials — skipping GitHub push")


if __name__ == "__main__":
    main()
