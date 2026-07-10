#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
상권레이더 - 가공 스크립트

store_stat(소상공인 개폐업 데이터)을 기본으로 하되,
raw_data/ 에 백업된 국토부 실거래가·서울 생활인구 원본을 함께 읽어
"3개 공공데이터 소스를 융합한" 리스크 지표를 산출한다.

지표 로직 (주의: 검증된 예측 모델이 아니라 "탐색·비교용 지표"임을 README/화면에 명시할 것):
  1) 변화 신호 (region x industry): (폐업수-개업수)/(점포수+1), winsorize 후 0~100 정규화
  2) 포화도 신호: 그 동네 전체 상가 중 이 업종 비중, winsorize 후 0~100 정규화
     base = 0.5 * 변화신호 + 0.5 * 포화도신호
  3) 부동산 조정치 (구 단위): 이번달 실거래 건수가 적을수록 리스크 가산 (-15~+15)
  4) 생활인구 조정치 (구 단위): 생활인구가 적을수록 리스크 가산 (-15~+15)
  최종 = base + 부동산조정 + 생활인구조정 (0~100 클리핑)

각 구성요소를 risk_score 테이블에 별도 컬럼으로 저장해서, 화면에서 "왜 이 점수가
나왔는지" 투명하게 breakdown을 보여줄 수 있게 한다 (블랙박스 점수 하나만 던지지 않음).

cron 등록 예시 (매일 새벽 2시 실행):
  0 2 * * * cd /home/opc/sangkwon-radar/scripts && /usr/bin/python3 process.py >> ../logs/process.log 2>&1
"""

import os
import re
import json
import glob
import xml.etree.ElementTree as ET

import oracledb

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "raw_data")
ADJ_MAX = 15.0


def get_db_connection():
    user = os.environ.get("ORACLE_USER", "sangkwon")
    pw = os.environ.get("ORACLE_PW")
    dsn = os.environ.get("ORACLE_DSN", "localhost/XEPDB1")
    return oracledb.connect(user=user, password=pw, dsn=dsn)


def log_run(conn, job_name, status, message):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_log (job_name, status, message) VALUES (:1, :2, :3)",
            [job_name, status, message[:500]],
        )
    conn.commit()


def ensure_breakdown_columns(conn):
    """risk_score에 breakdown 컬럼이 없으면 추가 (최초 1회만 실제로 ALTER 실행됨)."""
    needed = {
        "CHANGE_SCORE": "NUMBER(6,2)",
        "SATURATION_SCORE": "NUMBER(6,2)",
        "REALESTATE_ADJ": "NUMBER(6,2)",
        "POPULATION_ADJ": "NUMBER(6,2)",
    }
    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM user_tab_columns WHERE table_name = 'RISK_SCORE'
        """)
        existing = {r[0] for r in cur.fetchall()}
        for col, coltype in needed.items():
            if col not in existing:
                cur.execute(f"ALTER TABLE risk_score ADD ({col} {coltype})")
                print(f"[INFO] risk_score.{col} 컬럼 추가함")
    conn.commit()


def ensure_risk_history_table(conn):
    """
    risk_score_history가 없으면 생성 (append-only 시계열).
    store_stat_history와 달리 이건 process.py 최초 실행 시 자동으로 만들어진다
    (schema.sql을 다시 실행하지 않아도 되게 하기 위함).
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM user_tables WHERE table_name = 'RISK_SCORE_HISTORY'
        """)
        exists = cur.fetchone()[0] > 0
        if not exists:
            cur.execute("""
                CREATE TABLE risk_score_history (
                    id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    region_cd VARCHAR2(10) NOT NULL,
                    ind_cd VARCHAR2(10) NOT NULL,
                    ym VARCHAR2(6) NOT NULL,
                    risk_value NUMBER(5,2),
                    run_at DATE DEFAULT SYSDATE
                )
            """)
            print("[INFO] risk_score_history 테이블 생성함 (최초 1회)")
    conn.commit()


def insert_risk_history(conn, final_rows):
    """이번 실행의 리스크 점수를 시계열 히스토리에 append (절대 삭제하지 않음)."""
    if not final_rows:
        return
    with conn.cursor() as cur:
        for region_cd, ind_cd, ym, final, *_ in final_rows:
            cur.execute(
                """
                INSERT INTO risk_score_history (region_cd, ind_cd, ym, risk_value)
                VALUES (:1, :2, :3, :4)
                """,
                [region_cd, ind_cd, ym, final],
            )
    conn.commit()
    print(f"[OK] risk_score_history 누적 저장: {len(final_rows)}건")


def latest_files(pattern):
    return sorted(glob.glob(os.path.join(RAW_DIR, pattern)))


def load_realestate_signal():
    """raw_data/realestate_<LAWD_CD>_<YYYYMM>_*.xml -> {lawd_cd: 이번달 실거래 건수}"""
    signal = {}
    files = latest_files("realestate_*.xml")
    latest_per_lawd = {}
    for f in files:
        m = re.search(r"realestate_(\d{5})_(\d{6})_", os.path.basename(f))
        if not m:
            continue
        latest_per_lawd[m.group(1)] = f
    for lawd_cd, f in latest_per_lawd.items():
        try:
            with open(f, encoding="utf-8") as fh:
                root = ET.fromstring(fh.read())
            signal[lawd_cd] = len(root.findall(".//item"))
        except Exception as e:
            print(f"[WARN] 실거래가 파일 파싱 실패 ({f}): {e}")
    return signal


def load_population_signal():
    """raw_data/population_real_*.json (서울 생활인구) -> {gu_key: 생활인구 합계}"""
    files = latest_files("population_real_*.json")
    if not files:
        return {}
    latest = files[-1]
    try:
        with open(latest, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:
        print(f"[WARN] 생활인구 파일 파싱 실패 ({latest}): {e}")
        return {}

    rows = None
    for key in data:
        if isinstance(data[key], dict) and "row" in data[key]:
            rows = data[key]["row"]
            break
    if not rows:
        print("[WARN] 생활인구 응답에서 row 목록을 찾지 못함 -> 조정치 0으로 처리")
        return {}

    signal = {}
    for row in rows:
        gu_key = row.get("SIGNGU_CODE_SE") or row.get("SIGNGU_NM") or row.get("signgu_code_se")
        pop_val = row.get("TOT_LVPOP_CO") or row.get("tot_lvpop_co")
        if gu_key is None or pop_val is None:
            continue
        try:
            pop_val = float(pop_val)
        except (TypeError, ValueError):
            continue
        signal[str(gu_key)] = signal.get(str(gu_key), 0.0) + pop_val
    if not signal:
        print("[WARN] 생활인구 데이터에서 유효한 (구코드, 인구수) 쌍을 못 찾음 -> 조정치 0으로 처리")
    return signal


def normalize_adjustment(value, all_values, invert=True):
    if value is None or not all_values or len(set(all_values)) < 2:
        return 0.0
    lo, hi = min(all_values), max(all_values)
    ratio = (value - lo) / (hi - lo)
    if invert:
        ratio = 1 - ratio
    return round((ratio * 2 - 1) * ADJ_MAX, 2)


def winsorized_normalize(vals):
    s = sorted(vals)
    n = len(s)

    def pct(p):
        idx = min(n - 1, max(0, int(round(p * (n - 1)))))
        return s[idx]

    lo, hi = pct(0.05), pct(0.95)
    if lo == hi:
        lo, hi = (s[0], s[-1]) if s else (0, 1)
    span = (hi - lo) or 1.0

    def norm(v):
        clipped = max(lo, min(hi, v))
        return (clipped - lo) / span * 100
    return norm


def compute_and_upsert_risk(conn):
    ensure_breakdown_columns(conn)
    ensure_risk_history_table(conn)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT region_cd, ind_cd, ym, store_cnt, open_cnt, close_cnt
            FROM store_stat
        """)
        rows = cur.fetchall()

    if not rows:
        return 0, "no_data"

    raw_scores = []
    region_totals = {}
    for region_cd, ind_cd, ym, store_cnt, open_cnt, close_cnt in rows:
        store_cnt = store_cnt or 0
        open_cnt = open_cnt or 0
        close_cnt = close_cnt or 0
        change_signal = (close_cnt - open_cnt) / (store_cnt + 1)
        raw_scores.append((region_cd, ind_cd, ym, change_signal, store_cnt))
        region_totals[region_cd] = region_totals.get(region_cd, 0) + store_cnt

    change_norm = winsorized_normalize([r[3] for r in raw_scores])

    saturation_vals = []
    for region_cd, ind_cd, ym, store_cnt, open_cnt, close_cnt in rows:
        total = region_totals.get(region_cd, 0)
        ratio = (store_cnt or 0) / total if total else 0.0
        saturation_vals.append(ratio)
    saturation_norm = winsorized_normalize(saturation_vals)

    base_detail = {}  # key -> (base, change_score, saturation_score)
    for i, r in enumerate(raw_scores):
        region_cd, ind_cd, ym = r[0], r[1], r[2]
        change_score = round(change_norm(r[3]), 2)
        saturation_score = round(saturation_norm(saturation_vals[i]), 2)
        base = round(0.5 * change_score + 0.5 * saturation_score, 2)
        base_detail[(region_cd, ind_cd, ym)] = (base, change_score, saturation_score)

    low_sample = [(r[0], r[1]) for r in raw_scores if r[4] < 5]
    if low_sample:
        shown = low_sample[:10]
        more = f" 외 {len(low_sample)-10}건 더" if len(low_sample) > 10 else ""
        print(f"[WARN] 점포수 5개 미만 저표본 조합 {len(low_sample)}건 "
              f"(리스크 지표 신뢰도 낮음, 참고용): {shown}{more}")

    realestate_signal = load_realestate_signal()
    realestate_values = list(realestate_signal.values())
    population_signal = load_population_signal()
    population_values = list(population_signal.values())

    print(f"[INFO] 부동산 신호(구별 거래건수): {realestate_signal}")
    print(f"[INFO] 생활인구 신호(구별 합계, 상위 5개만 표시): "
          f"{dict(list(population_signal.items())[:5])}")

    final_rows = []
    for (region_cd, ind_cd, ym), (base, change_score, saturation_score) in base_detail.items():
        lawd_cd = region_cd[:5]
        re_val = realestate_signal.get(lawd_cd)
        re_adj = normalize_adjustment(re_val, realestate_values, invert=True)
        pop_val = population_signal.get(lawd_cd)
        pop_adj = normalize_adjustment(pop_val, population_values, invert=True)

        final = max(0.0, min(100.0, base + re_adj + pop_adj))
        final_rows.append((region_cd, ind_cd, ym, round(final, 2),
                            change_score, saturation_score, re_adj, pop_adj))

    with conn.cursor() as cur:
        cur.execute("DELETE FROM risk_score")
        for region_cd, ind_cd, ym, final, change_score, saturation_score, re_adj, pop_adj in final_rows:
            cur.execute(
                """
                INSERT INTO risk_score
                    (region_cd, ind_cd, ym, risk_value,
                     change_score, saturation_score, realestate_adj, population_adj)
                VALUES (:1, :2, :3, :4, :5, :6, :7, :8)
                """,
                [region_cd, ind_cd, ym, final, change_score, saturation_score, re_adj, pop_adj],
            )
    conn.commit()
    insert_risk_history(conn, final_rows)

    sample = final_rows[:3]
    detail = ", ".join(
        f"{r}/{i}: change={cs} sat={ss} re={ra:+} pop={pa:+} => {f}"
        for r, i, _, f, cs, ss, ra, pa in sample
    )
    print(f"[INFO] 총 {len(final_rows)}건 산출, 예시 3건: {detail}")

    used_sources = ["소상공인(필수)"]
    if realestate_values:
        used_sources.append("국토부실거래가")
    if population_values:
        used_sources.append("서울생활인구")
    return len(final_rows), "+".join(used_sources)


def main():
    conn = get_db_connection()
    try:
        n, sources = compute_and_upsert_risk(conn)
        log_run(conn, "process", "SUCCESS", f"{n}건 리스크 지표 산출 (반영 소스: {sources})")
        print(f"[OK] risk_score 적재 완료: {n}건 (반영 소스: {sources})")
    except Exception as e:
        log_run(conn, "process", "FAIL", str(e))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
