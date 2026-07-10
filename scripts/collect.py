#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
상권레이더 - 데이터 수집 스크립트 (v2: 구 단위 자동 전체 수집)

기존에는 행정동 코드를 하나하나 손으로 추측/검증해서 하드코딩했으나(4개 동 한계),
이번 버전은 시군구코드(signguCd)만 주면 그 구에 속한 "모든 행정동"의 상가업소를
API가 알아서 다 돌려주는 방식으로 바꿔서, 코드 하드코딩 없이 커버리지를 크게 넓혔다.
새로 발견되는 행정동은 region 테이블에 자동으로 채워진다(MERGE).

3개 공공 API를 사용합니다:
  1) 소상공인시장진흥공단 상가(상권)정보 API  -> store_stat 테이블에 실제 적재 (핵심 데이터)
  2) 국토교통부 상업업무용 부동산 실거래가 API -> raw_data/ 에 원본 백업 (보조 데이터)
  3) 서울 열린데이터광장 생활인구 API          -> raw_data/ 에 원본 백업 (보조 데이터)

API 키가 없거나 --sample 옵션을 주면 스키마와 동일한 형태의 샘플 데이터로 동작합니다.

사용법:
  python3 collect.py --sample          # 샘플 데이터로 즉시 테스트
  python3 collect.py                   # .env의 키가 있으면 실제 API 호출 (구 전체, 몇 분 걸릴 수 있음)
  python3 collect.py --skip-extra      # 소상공인 API만 (국토부/서울 생략)

환경변수 (.env 또는 export로 설정):
  DATA_GO_KR_KEY   : 공공데이터포털 인증키 (소상공인 상권정보 + 국토부 실거래가 공용)
  SEOUL_API_KEY    : 서울 열린데이터광장 인증키 (생활인구)
  ORACLE_USER      : Oracle 계정 (예: sangkwon)
  ORACLE_PW        : Oracle 비밀번호
  ORACLE_DSN       : 예) localhost/XEPDB1
"""

import os
import sys
import json
import glob
import random
import argparse
import datetime
import xml.etree.ElementTree as ET

import requests
import oracledb

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "raw_data")
os.makedirs(RAW_DIR, exist_ok=True)

# 시군구코드(signguCd) 단위로 수집한다. 이 구 안의 모든 행정동은 API가 알려주는 대로 자동 발견된다.
SIGNGUS = [
    ("11110", "종로구"), ("11140", "중구"), ("11170", "용산구"),
    ("11200", "성동구"), ("11215", "광진구"), ("11230", "동대문구"),
    ("11260", "중랑구"), ("11290", "성북구"), ("11305", "강북구"),
    ("11320", "도봉구"), ("11350", "노원구"), ("11380", "은평구"),
    ("11410", "서대문구"), ("11440", "마포구"), ("11470", "양천구"),
    ("11500", "강서구"), ("11530", "구로구"), ("11545", "금천구"),
    ("11560", "영등포구"), ("11590", "동작구"), ("11620", "관악구"),
    ("11650", "서초구"), ("11680", "강남구"), ("11710", "송파구"),
    ("11740", "강동구"),
]
INDUSTRIES = ["I1", "I2", "G2", "F1", "S1", "E1"]

# 구 하나당 최대로 가져올 페이지 수 (numOfRows x MAX_PAGES = 구별 최대 수집 상가 수).
# 강남구 같은 큰 구는 상가가 수천~수만 개라 전량 수집은 시간이 너무 오래 걸려서,
# "충분히 넓은 표본"을 목표로 상한을 둔다. (필요시 늘릴 수 있음)
ROWS_PER_PAGE = 500
MAX_PAGES = 4


def match_industry(item):
    """상권업종명(중/소분류)을 우리 스키마 업종코드로 매칭.
    I1=한식 I2=카페 G2=편의점 F1=치킨/피자/분식(간편식) S1=미용/네일 E1=학원/교육
    """
    mcls = (item.get("indsMclsNm") or "")
    scls = (item.get("indsSclsNm") or "")
    if "한식" in mcls or "한식" in scls:
        return "I1"
    if "커피" in mcls or "카페" in scls or "커피" in scls:
        return "I2"
    if "편의점" in scls:
        return "G2"
    if "치킨" in scls or "피자" in scls or "분식" in scls or "김밥" in scls:
        return "F1"
    if "미용실" in scls or "네일" in scls or "피부관리" in scls:
        return "S1"
    if "학원" in mcls or "교습" in scls or "학원" in scls:
        return "E1"
    return None


def get_db_connection():
    user = os.environ.get("ORACLE_USER", "sangkwon")
    pw = os.environ.get("ORACLE_PW")
    dsn = os.environ.get("ORACLE_DSN", "localhost/XEPDB1")
    if not pw:
        raise RuntimeError("ORACLE_PW 환경변수가 설정되지 않았습니다. .env를 확인하세요.")
    return oracledb.connect(user=user, password=pw, dsn=dsn)


def log_run(conn, job_name, status, message):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_log (job_name, status, message) VALUES (:1, :2, :3)",
            [job_name, status, message[:500]],
        )
    conn.commit()


def save_raw_backup(payload, tag, ext="json"):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(RAW_DIR, f"{tag}_{ts}.{ext}")
    with open(path, "w", encoding="utf-8") as f:
        if ext == "json":
            json.dump(payload, f, ensure_ascii=False, indent=2)
        else:
            f.write(payload)
    print(f"[OK] 원본 백업 저장: {path}")
    return path


# ------------------------------------------------------------------
# 1) 소상공인시장진흥공단 상가(상권)정보 API (핵심 데이터 -> store_stat, region)
# ------------------------------------------------------------------
def fetch_gu_items(api_key, signgu_cd, max_retries=2):
    """
    구(signguCd) 단위로 그 구에 속한 모든 상가업소를 페이지네이션으로 수집.
    각 페이지 요청은 네트워크 흔들림(타임아웃 등)에 대비해 최대 max_retries회 재시도한다.
    (재시도 없이 첫 페이지가 실패하면 그 구 전체가 0건으로 소실되는 문제가 있었음)
    MAX_PAGES에 도달하거나 더 이상 데이터가 없으면 종료.
    """
    url = "https://apis.data.go.kr/B553077/api/open/sdsc2/storeListInDong"
    all_items = []
    for page in range(1, MAX_PAGES + 1):
        params = {
            "serviceKey": api_key,
            "divId": "signguCd",
            "key": signgu_cd,
            "type": "json",
            "numOfRows": ROWS_PER_PAGE,
            "pageNo": page,
        }
        items = None
        last_err = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.get(url, params=params, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                items = data.get("body", {}).get("items", [])
                if isinstance(items, dict):
                    items = items.get("item", [])
                break  # 성공
            except Exception as e:
                last_err = e
                if attempt < max_retries:
                    print(f"[WARN] {signgu_cd} {page}페이지 {attempt}차 시도 실패, 재시도합니다: {e}",
                          file=sys.stderr)
                else:
                    print(f"[WARN] {signgu_cd} {page}페이지 {attempt}차 시도까지 모두 실패, 포기: {e}",
                          file=sys.stderr)
        if items is None:
            break
        if not items:
            break
        all_items.extend(items)
        print(f"[INFO] {signgu_cd} {page}페이지: {len(items)}건 (누적 {len(all_items)}건)")
        if len(items) < ROWS_PER_PAGE:
            break  # 마지막 페이지
    return all_items


def load_previous_snapshot():
    """
    직전 sangkwon_real_*.json 백업을 {signgu_cd: [items]} 형태로 반환.
    없으면 None (최초 실행, 비교 기준 없음).
    """
    files = sorted(glob.glob(os.path.join(RAW_DIR, "sangkwon_real_*.json")))
    if not files:
        return None
    latest = files[-1]
    try:
        with open(latest, encoding="utf-8") as f:
            data = json.load(f)
        # 구버전(region_cd 키) 백업과의 호환을 위해 signgu_cd 키가 있는 것만 사용
        if data and "signgu_cd" not in data[0]:
            print("[INFO] 이전 버전(동 단위) 백업 파일이라 비교 기준으로 사용하지 않음.")
            return None
        return {r["signgu_cd"]: r["items"] for r in data}
    except Exception as e:
        print(f"[WARN] 직전 스냅샷 로드 실패({latest}): {e}", file=sys.stderr)
        return None


def build_store_stat_rows(api_key):
    """
    구 단위로 전체 상가를 수집한 뒤, 각 상가의 adongCd(행정동)로 그룹핑해서
    region x industry 단위 store_cnt/open_cnt/close_cnt를 계산한다.
    개업/폐업은 이번 스냅샷과 직전 스냅샷의 bizesId 집합 비교로 산출한다(실측 기반).
    반환값: (rows, discovered_regions)
      discovered_regions: {adongCd: (adongNm, signguNm)} - region 테이블에 자동 반영할 목록
    """
    ym = datetime.datetime.now().strftime("%Y%m")
    prev_snapshot = load_previous_snapshot()  # {signgu_cd: [items]} or None

    rows = []
    all_raw = []
    discovered_regions = {}

    for signgu_cd, gu_label in SIGNGUS:
        print(f"[INFO] {gu_label}({signgu_cd}) 수집 시작...")
        items = fetch_gu_items(api_key, signgu_cd)
        all_raw.append({"signgu_cd": signgu_cd, "signgu_nm": gu_label, "items": items})
        print(f"[OK] {gu_label} 수집 완료: 총 {len(items)}건")

        prev_items = (prev_snapshot or {}).get(signgu_cd, [])

        # 행정동별로 그룹핑
        cur_by_dong = {}
        for it in items:
            adong = it.get("adongCd")
            if not adong:
                continue
            cur_by_dong.setdefault(adong, []).append(it)
            discovered_regions[adong] = (it.get("adongNm") or adong, it.get("signguNm") or gu_label)

        prev_by_dong = {}
        for it in prev_items:
            adong = it.get("adongCd")
            if not adong:
                continue
            prev_by_dong.setdefault(adong, []).append(it)

        for adong, dong_items in cur_by_dong.items():
            for ind_cd in INDUSTRIES:
                cur_matched = [it for it in dong_items if match_industry(it) == ind_cd]
                cur_ids = {it.get("bizesId") for it in cur_matched if it.get("bizesId")}
                store_cnt = len(cur_ids)
                if store_cnt == 0:
                    continue  # 그 동에 그 업종이 아예 없으면 행 자체를 만들지 않음 (불필요한 0행 방지)

                if prev_snapshot is not None:
                    prev_dong_items = prev_by_dong.get(adong, [])
                    prev_matched = [it for it in prev_dong_items if match_industry(it) == ind_cd]
                    prev_ids = {it.get("bizesId") for it in prev_matched if it.get("bizesId")}
                    open_cnt = len(cur_ids - prev_ids)
                    close_cnt = len(prev_ids - cur_ids)
                else:
                    open_cnt = 0
                    close_cnt = 0

                rows.append({
                    "region_cd": adong,
                    "ind_cd": ind_cd,
                    "ym": ym,
                    "store_cnt": store_cnt,
                    "open_cnt": open_cnt,
                    "close_cnt": close_cnt,
                })

    save_raw_backup(all_raw, "sangkwon_real")
    if prev_snapshot is None:
        print("[INFO] 직전 스냅샷이 없어 이번 실행은 store_cnt만 채움 "
              "(open/close는 다음 실행부터 비교 가능, cron 누적으로 점점 정확해짐)")
    print(f"[INFO] 발견된 행정동 수: {len(discovered_regions)}개, 생성된 (지역x업종) 조합: {len(rows)}개")
    return rows, discovered_regions


# ------------------------------------------------------------------
# 2) 국토교통부 상업업무용 부동산 매매 실거래가 API (보조 데이터 -> raw_data 백업)
# ------------------------------------------------------------------
def fetch_realestate_raw(api_key):
    url = "https://apis.data.go.kr/1613000/RTMSDataSvcNrgTrade/getRTMSDataSvcNrgTrade"
    ym = datetime.datetime.now().strftime("%Y%m")
    total_count = 0
    for signgu_cd, gu_label in SIGNGUS:
        params = {
            "serviceKey": api_key,
            "LAWD_CD": signgu_cd,
            "DEAL_YMD": ym,
            "numOfRows": 100,
            "pageNo": 1,
        }
        try:
            resp = requests.get(url, params=params, timeout=60)
            resp.raise_for_status()
            save_raw_backup(resp.text, f"realestate_{signgu_cd}_{ym}", ext="xml")
            try:
                root = ET.fromstring(resp.text)
                items = root.findall(".//item")
                total_count += len(items)
            except ET.ParseError:
                pass
        except Exception as e:
            print(f"[WARN] 실거래가 {gu_label}({signgu_cd}) 수집 실패: {e}", file=sys.stderr)
    print(f"[OK] 실거래가 원본 백업 완료 (파싱된 건수: {total_count}건)")
    return total_count


# ------------------------------------------------------------------
# 3) 서울 열린데이터광장 생활인구 API (보조 데이터 -> raw_data 백업)
# ------------------------------------------------------------------
def fetch_population_raw(api_key):
    url = f"http://openapi.seoul.go.kr:8088/{api_key}/json/SPOP_DAILYSUM_JACHI/1/100/"
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        save_raw_backup(data, "population_real")
        row_count = len(data.get("SPOP_DAILYSUM_JACHI", {}).get("row", []))
        print(f"[OK] 생활인구 원본 백업 완료 ({row_count}건)")
        return row_count
    except Exception as e:
        print(f"[WARN] 생활인구 수집 실패: {e}", file=sys.stderr)
        return 0


def generate_sample_data():
    ym = datetime.datetime.now().strftime("%Y%m")
    sample_regions = ["1168060000", "1168064000", "1150053000", "1174060000"]
    rows = []
    for region_cd in sample_regions:
        for ind_cd in INDUSTRIES:
            rows.append({
                "region_cd": region_cd,
                "ind_cd": ind_cd,
                "ym": ym,
                "store_cnt": random.randint(20, 120),
                "open_cnt": random.randint(0, 8),
                "close_cnt": random.randint(0, 10),
            })
    discovered = {
        "1168060000": ("샘플동A", "강남구"),
        "1168064000": ("샘플동B", "강남구"),
        "1150053000": ("샘플동C", "강서구"),
        "1174060000": ("샘플동D", "강동구"),
    }
    return rows, discovered


def upsert_regions(conn, discovered_regions):
    """새로 발견된 행정동을 region 테이블에 반영 (있으면 이름 갱신, 없으면 추가)."""
    if not discovered_regions:
        return
    with conn.cursor() as cur:
        for region_cd, (region_nm, gu_nm) in discovered_regions.items():
            cur.execute(
                """
                MERGE INTO region r
                USING (SELECT :region_cd AS region_cd FROM dual) src
                ON (r.region_cd = src.region_cd)
                WHEN MATCHED THEN UPDATE SET region_nm = :region_nm, gu_nm = :gu_nm
                WHEN NOT MATCHED THEN INSERT (region_cd, region_nm, gu_nm)
                    VALUES (:region_cd, :region_nm, :gu_nm)
                """,
                {"region_cd": region_cd, "region_nm": region_nm, "gu_nm": gu_nm},
            )
    conn.commit()
    print(f"[OK] region 테이블 갱신: {len(discovered_regions)}개 행정동 반영")


def insert_history(conn, rows):
    """
    store_stat_history에 이번 실행 결과를 append (절대 삭제하지 않음).
    이게 cron으로 매일 쌓이면서 진짜 시계열 추세 데이터가 된다.
    """
    if not rows:
        return
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO store_stat_history (region_cd, ind_cd, ym, store_cnt, open_cnt, close_cnt)
                VALUES (:region_cd, :ind_cd, :ym, :store_cnt, :open_cnt, :close_cnt)
                """,
                r,
            )
    conn.commit()
    print(f"[OK] store_stat_history 누적 저장: {len(rows)}건 (시계열 데이터로 계속 쌓임)")


def upsert_store_stat(conn, rows):
    if not rows:
        return
    ym = rows[0]["ym"]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM store_stat WHERE ym = :ym", {"ym": ym})
        for r in rows:
            cur.execute(
                """
                INSERT INTO store_stat (region_cd, ind_cd, ym, store_cnt, open_cnt, close_cnt)
                VALUES (:region_cd, :ind_cd, :ym, :store_cnt, :open_cnt, :close_cnt)
                """,
                r,
            )
    conn.commit()
    print(f"[OK] store_stat 적재 완료: {len(rows)}건 (ym={ym} 기존 데이터 교체)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", action="store_true", help="샘플 데이터로 실행 (API 키 불필요)")
    parser.add_argument("--skip-extra", action="store_true", help="국토부/서울 API 호출 생략 (소상공인만)")
    args = parser.parse_args()

    data_go_kr_key = os.environ.get("DATA_GO_KR_KEY")
    seoul_key = os.environ.get("SEOUL_API_KEY")
    use_sample = args.sample or not data_go_kr_key

    conn = get_db_connection()
    try:
        if use_sample:
            print("[INFO] 샘플 모드로 실행합니다 (DATA_GO_KR_KEY 미설정 또는 --sample 지정).")
            rows, discovered_regions = generate_sample_data()
        else:
            print("[INFO] 소상공인 상권정보 실제 API를 구 단위로 호출합니다 (몇 분 걸릴 수 있어요).")
            rows, discovered_regions = build_store_stat_rows(data_go_kr_key)

            if not args.skip_extra:
                print("[INFO] 국토부 실거래가 API를 호출합니다 (보조 데이터, raw_data 백업).")
                fetch_realestate_raw(data_go_kr_key)

                if seoul_key:
                    print("[INFO] 서울 생활인구 API를 호출합니다 (보조 데이터, raw_data 백업).")
                    fetch_population_raw(seoul_key)
                else:
                    print("[INFO] SEOUL_API_KEY 미설정 -> 생활인구 수집 생략.")

        upsert_regions(conn, discovered_regions)
        upsert_store_stat(conn, rows)
        insert_history(conn, rows)
        log_run(conn, "collect", "SUCCESS",
                f"{len(rows)}건 수집, {len(discovered_regions)}개 행정동 (sample={use_sample})")
    except Exception as e:
        log_run(conn, "collect", "FAIL", str(e))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
