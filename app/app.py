#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
상권레이더 - 서비스 제공(Serve) 계층
Flask로 REST API + 간단한 대시보드 페이지를 제공한다.

실행:
  export ORACLE_USER=sangkwon ORACLE_PW=... ORACLE_DSN=localhost/XEPDB1
  python3 app.py
  -> http://<VM 공인 IP>:8080 접속
"""

import os
import oracledb
from flask import Flask, jsonify, render_template_string

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # requirements.txt에 python-dotenv 없으면 export로 환경변수 설정해서 사용

app = Flask(__name__)


def get_db_connection():
    user = os.environ.get("ORACLE_USER", "sangkwon")
    pw = os.environ.get("ORACLE_PW")
    dsn = os.environ.get("ORACLE_DSN", "localhost/XEPDB1")
    return oracledb.connect(user=user, password=pw, dsn=dsn)


@app.route("/api/risk")
def api_risk():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.region_cd, r.region_nm, r.gu_nm, s.ind_cd, i.ind_nm, s.ym, s.risk_value,
                       s.change_score, s.saturation_score, s.realestate_adj, s.population_adj,
                       st.store_cnt, st.open_cnt, st.close_cnt
                FROM risk_score s
                JOIN region r ON r.region_cd = s.region_cd
                JOIN industry i ON i.ind_cd = s.ind_cd
                LEFT JOIN store_stat st
                       ON st.region_cd = s.region_cd AND st.ind_cd = s.ind_cd AND st.ym = s.ym
                ORDER BY s.risk_value DESC
            """)
            rows = cur.fetchall()
        data = [
            {
                "region_cd": r[0], "region": r[1], "gu": r[2], "ind_cd": r[3], "industry": r[4], "ym": r[5],
                "risk": float(r[6]) if r[6] is not None else None,
                "change_score": float(r[7]) if r[7] is not None else None,
                "saturation_score": float(r[8]) if r[8] is not None else None,
                "realestate_adj": float(r[9]) if r[9] is not None else None,
                "population_adj": float(r[10]) if r[10] is not None else None,
                "store_cnt": r[11], "open_cnt": r[12], "close_cnt": r[13],
            }
            for r in rows
        ]
        return jsonify(data)
    finally:
        conn.close()


@app.route("/api/trend/<region_cd>/<ind_cd>")
def api_trend(region_cd, ind_cd):
    """
    특정 지역x업종의 리스크 지표 시계열 추세를 반환 (cron 누적될수록 풍부해짐).
    risk_score_history를 기준으로 직접 조회한다.
    (이전 버전은 store_stat_history와 분 단위 타임스탬프를 매칭하려 했으나,
    collect.py/process.py가 서로 다른 시각에 실행되어 타임스탬프가 어긋나
    risk 값이 계속 null로 나오는 문제가 있어 수정함.)
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT TO_CHAR(run_at, 'YYYY-MM-DD HH24:MI'), risk_value
                    FROM risk_score_history
                    WHERE region_cd = :1 AND ind_cd = :2
                    ORDER BY run_at
                """, [region_cd, ind_cd])
                risk_rows = cur.fetchall()
            except oracledb.DatabaseError:
                risk_rows = []  # risk_score_history가 아직 없는 구버전 DB

        return jsonify([
            {"run_at": r[0], "risk": float(r[1]) if r[1] is not None else None}
            for r in risk_rows
        ])
    finally:
        conn.close()


@app.route("/api/health")
def health():
    try:
        conn = get_db_connection()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


PAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>상권레이더 - 창업 리스크 대시보드</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  :root { --brand: #9C1F4A; --brand2: #C74634; --bg: #f7f7f9; }
  * { box-sizing: border-box; }
  body { font-family: 'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif; margin: 0; padding: 32px; background: var(--bg); color: #222; }
  h1 { color: var(--brand); margin-bottom: 4px; }
  .subtitle { color: #555; margin-top: 0; }
  .disclaimer { background: #fff8e1; border-left: 4px solid #f0ad4e; padding: 12px 16px; border-radius: 4px; margin: 16px 0; font-size: 14px; line-height: 1.6; }
  .card { background: white; padding: 20px; border-radius: 8px; margin-top: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
  .filters { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
  select, button { padding: 8px 12px; border-radius: 6px; border: 1px solid #ccc; font-size: 14px; }
  button { background: var(--brand); color: white; border: none; cursor: pointer; }
  button:hover { opacity: 0.9; }
  table { border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 13px; }
  th, td { border: 1px solid #eee; padding: 8px 10px; text-align: left; white-space: nowrap; }
  th { background: var(--brand); color: white; cursor: pointer; position: sticky; top: 0; }
  tr:nth-child(even) { background: #fafafa; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; color: white; }
  .compare-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  .compare-col { background: #fafafa; border-radius: 8px; padding: 16px; }
  .compare-col h3 { margin-top: 0; color: var(--brand); }
  .metric-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #eee; }
  .metric-label { color: #666; }
  .table-wrap { max-height: 500px; overflow-y: auto; }
  @media (max-width: 800px) { .compare-grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
  <h1>상권레이더 — 지역×업종 창업 지표 대시보드</h1>
  <p class="subtitle">Oracle Cloud VM 위에서 수집 → 저장(Oracle XE) → 가공 → 제공되는 데이터입니다. (포트 3000)</p>

  <div class="disclaimer">
    ⚠️ <b>이 점수는 검증된 "폐업 예측"이 아니라 탐색·비교용 참고 지표입니다.</b>
    변화신호(최근 개폐업 추세) + 포화도(업종 밀집도) + 부동산 거래량 + 생활인구를 임의 가중치로 합산한 값이며,
    실제 과거 폐업 이력으로 검증되지 않았습니다. 절대적인 판단 근거가 아니라 여러 지역을 비교하는 참고 자료로 활용하세요.
  </div>

  <div class="card">
    <h3 style="margin-top:0; color:var(--brand);">한눈에 보기</h3>
    <div class="compare-grid">
      <div>
        <h4 style="color:#27ae60;">✅ 지표상 가장 안전한 Top 5</h4>
        <div id="safeList"></div>
      </div>
      <div>
        <h4 style="color:#c0392b;">⚠️ 지표상 가장 주의 필요한 Top 5</h4>
        <div id="riskyList"></div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="filters">
      <label>구 필터: <select id="guFilter"><option value="">전체</option></select></label>
      <label>업종 필터: <select id="indFilter"><option value="">전체</option></select></label>
      <span id="countLabel" style="color:#666; font-size:13px;"></span>
    </div>
    <canvas id="riskChart" height="90"></canvas>
    <p style="font-size:12px; color:#888;">* 상위 20개 조합만 표시 (필터 적용 시 필터된 결과 기준)</p>
  </div>

  <div class="card">
    <h3 style="margin-top:0; color:var(--brand);">지역 비교</h3>
    <div class="filters">
      <select id="cmpA"></select>
      <span>vs</span>
      <select id="cmpB"></select>
      <button onclick="renderCompare()">비교하기</button>
    </div>
    <div class="compare-grid" id="compareArea"></div>
  </div>

  <div class="card">
    <h3 style="margin-top:0; color:var(--brand);">시계열 추세 (cron이 매일 쌓일수록 풍부해짐)</h3>
    <div class="filters">
      <select id="trendSelect"></select>
      <button onclick="renderTrend()">추세 보기</button>
    </div>
    <canvas id="trendChart" height="80"></canvas>
    <p style="font-size:12px; color:#888;">* 지금은 데이터 포인트가 적을 수 있어요. 매일 자동 수집(cron)이 쌓일수록 실제 추세가 보입니다.</p>
  </div>

  <div class="card">
    <h3 style="margin-top:0; color:var(--brand);">전체 데이터</h3>
    <div class="table-wrap">
      <table id="riskTable">
        <thead><tr>
          <th onclick="sortBy('region')">지역</th>
          <th onclick="sortBy('gu')">구</th>
          <th onclick="sortBy('industry')">업종</th>
          <th onclick="sortBy('store_cnt')">점포수</th>
          <th onclick="sortBy('change_score')">변화신호</th>
          <th onclick="sortBy('saturation_score')">포화도</th>
          <th onclick="sortBy('realestate_adj')">부동산조정</th>
          <th onclick="sortBy('population_adj')">생활인구조정</th>
          <th onclick="sortBy('risk')">종합 지표</th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

<script>
let ALL_DATA = [];
let sortKey = 'risk', sortDir = -1;

function riskColor(v) {
  if (v >= 70) return '#c0392b';
  if (v >= 45) return '#e67e22';
  return '#27ae60';
}

function applyFilters(data) {
  const gu = document.getElementById('guFilter').value;
  const ind = document.getElementById('indFilter').value;
  return data.filter(d => (!gu || d.gu === gu) && (!ind || d.industry === ind));
}

let chartInstance = null;
function renderChart(data) {
  const top = [...data].sort((a,b) => b.risk - a.risk).slice(0, 20);
  const labels = top.map(d => d.region + '·' + d.industry);
  const values = top.map(d => d.risk);
  const colors = top.map(d => riskColor(d.risk));
  if (chartInstance) chartInstance.destroy();
  chartInstance = new Chart(document.getElementById('riskChart'), {
    type: 'bar',
    data: { labels, datasets: [{ label: '종합 지표 (0~100, 높을수록 주의)', data: values, backgroundColor: colors }] },
    options: { responsive: true, plugins: { legend: { display: false } } }
  });
}

function renderTable(data) {
  const sorted = [...data].sort((a,b) => {
    const av = a[sortKey], bv = b[sortKey];
    if (typeof av === 'string') return sortDir * av.localeCompare(bv);
    return sortDir * ((av ?? 0) - (bv ?? 0));
  });
  const tbody = document.querySelector('#riskTable tbody');
  tbody.innerHTML = '';
  sorted.forEach(d => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${d.region}</td><td>${d.gu}</td><td>${d.industry}</td>
      <td>${d.store_cnt ?? '-'}</td>
      <td>${d.change_score ?? '-'}</td>
      <td>${d.saturation_score ?? '-'}</td>
      <td>${d.realestate_adj ?? '-'}</td>
      <td>${d.population_adj ?? '-'}</td>
      <td><span class="badge" style="background:${riskColor(d.risk)}">${d.risk}</span></td>`;
    tbody.appendChild(tr);
  });
  document.getElementById('countLabel').textContent = `${data.length}개 조합 표시 중`;
}

function sortBy(key) {
  if (sortKey === key) sortDir *= -1; else { sortKey = key; sortDir = -1; }
  renderTable(applyFilters(ALL_DATA));
}

function renderTopLists(data) {
  const valid = data.filter(d => d.risk !== null);
  const safe = [...valid].sort((a,b) => a.risk - b.risk).slice(0, 5);
  const risky = [...valid].sort((a,b) => b.risk - a.risk).slice(0, 5);
  const fmt = d => `<div class="metric-row"><span>${d.region}(${d.gu}) · ${d.industry}</span>
    <span class="badge" style="background:${riskColor(d.risk)}">${d.risk}</span></div>`;
  document.getElementById('safeList').innerHTML = safe.map(fmt).join('') || '<p>데이터 없음</p>';
  document.getElementById('riskyList').innerHTML = risky.map(fmt).join('') || '<p>데이터 없음</p>';
}

let trendChartInstance = null;
function populateTrendSelect(data) {
  const sel = document.getElementById('trendSelect');
  const seen = new Set();
  data.forEach(d => {
    const key = d.region_cd + '|' + d.ind_cd;
    if (seen.has(key) || !d.region_cd) return;
    seen.add(key);
    const o = document.createElement('option');
    o.value = key;
    o.textContent = `${d.region}(${d.gu}) · ${d.industry}`;
    sel.appendChild(o);
  });
}

function renderTrend() {
  const [regionCd, indCd] = document.getElementById('trendSelect').value.split('|');
  if (!regionCd) return;
  fetch(`/api/trend/${regionCd}/${indCd}`)
    .then(res => res.json())
    .then(points => {
      const labels = points.map(p => p.run_at);
      const storeCnts = points.map(p => p.store_cnt);
      if (trendChartInstance) trendChartInstance.destroy();
      trendChartInstance = new Chart(document.getElementById('trendChart'), {
        type: 'line',
        data: { labels, datasets: [{ label: '점포 수 추이', data: storeCnts, borderColor: '#9C1F4A', tension: 0.2 }] },
        options: { responsive: true }
      });
    });
}

function populateFilters(data) {
  const gus = [...new Set(data.map(d => d.gu))].sort();
  const inds = [...new Set(data.map(d => d.industry))].sort();
  const guSel = document.getElementById('guFilter');
  const indSel = document.getElementById('indFilter');
  gus.forEach(g => { const o = document.createElement('option'); o.value = g; o.textContent = g; guSel.appendChild(o); });
  inds.forEach(i => { const o = document.createElement('option'); o.value = i; o.textContent = i; indSel.appendChild(o); });

  const cmpA = document.getElementById('cmpA');
  const cmpB = document.getElementById('cmpB');
  data.forEach((d, idx) => {
    const label = `${d.region}(${d.gu}) · ${d.industry}`;
    const oa = document.createElement('option'); oa.value = idx; oa.textContent = label; cmpA.appendChild(oa);
    const ob = document.createElement('option'); ob.value = idx; ob.textContent = label; cmpB.appendChild(ob);
  });
  if (data.length > 1) cmpB.selectedIndex = 1;
}

function metricCard(d) {
  if (!d) return '<p>선택 안 됨</p>';
  return `
    <h3>${d.region} (${d.gu}) · ${d.industry}</h3>
    <div class="metric-row"><span class="metric-label">점포수</span><span>${d.store_cnt ?? '-'}</span></div>
    <div class="metric-row"><span class="metric-label">개업/폐업(최근 대비)</span><span>${d.open_cnt ?? 0} / ${d.close_cnt ?? 0}</span></div>
    <div class="metric-row"><span class="metric-label">변화신호</span><span>${d.change_score ?? '-'}</span></div>
    <div class="metric-row"><span class="metric-label">포화도</span><span>${d.saturation_score ?? '-'}</span></div>
    <div class="metric-row"><span class="metric-label">부동산 조정</span><span>${d.realestate_adj ?? '-'}</span></div>
    <div class="metric-row"><span class="metric-label">생활인구 조정</span><span>${d.population_adj ?? '-'}</span></div>
    <div class="metric-row" style="font-weight:bold; border-top:2px solid var(--brand); margin-top:6px; padding-top:10px;">
      <span>종합 지표</span><span style="color:${riskColor(d.risk)}">${d.risk}</span>
    </div>
  `;
}

function renderCompare() {
  const a = ALL_DATA[document.getElementById('cmpA').value];
  const b = ALL_DATA[document.getElementById('cmpB').value];
  document.getElementById('compareArea').innerHTML =
    `<div class="compare-col">${metricCard(a)}</div><div class="compare-col">${metricCard(b)}</div>`;
}

fetch('/api/risk')
  .then(res => res.json())
  .then(data => {
    ALL_DATA = data;
    populateFilters(data);
    populateTrendSelect(data);
    renderTopLists(data);
    renderChart(data);
    renderTable(data);
    renderCompare();
    document.getElementById('guFilter').addEventListener('change', () => {
      const f = applyFilters(ALL_DATA); renderChart(f); renderTable(f);
    });
    document.getElementById('indFilter').addEventListener('change', () => {
      const f = applyFilters(ALL_DATA); renderChart(f); renderTable(f);
    });
  })
  .catch(err => {
    document.body.innerHTML += '<p style="color:red">데이터를 불러오지 못했습니다: ' + err + '</p>';
  });
</script>
</body>
</html>
"""


@app.route("/")
def index():
    # Claude Design으로 만든 번들형 대시보드를 그대로 서빙한다.
    # render_template_string(Jinja)을 쓰면 번들 안의 JS 코드에 있는 { } 문법과
    # 충돌할 위험이 있어서, 파일을 그대로 읽어 raw HTML로 반환한다.
    dashboard_path = os.path.join(os.path.dirname(__file__), "static", "dashboard.html")
    with open(dashboard_path, encoding="utf-8") as f:
        html = f.read()
    return html


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
