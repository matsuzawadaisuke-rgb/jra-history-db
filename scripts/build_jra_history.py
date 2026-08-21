#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a one-year JRA race-result database.

Primary bulk source for race IDs: public uma-logic result JSON files.
Detailed race metadata / result / payout: db.netkeiba.com result pages.
The resulting CSV keeps source URLs and a verification flag so anomalies can
later be checked against JRA official results without silently guessing.
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

START = date(2025, 8, 21)
END = date(2026, 8, 20)
SOURCE_REPO = "https://github.com/uma-logic-user/uma-logic.git"
SOURCE_DIR = Path(".cache/uma-logic")
OUT_DIR = Path("data")
OUT_CSV = OUT_DIR / "jra_results_20250821_20260820.csv"
ANOMALY_CSV = OUT_DIR / "jra_anomalies_20250821_20260820.csv"
SUMMARY_JSON = OUT_DIR / "build_summary.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"}
SLEEP_SEC = 0.75
MAX_RETRIES = 4
TIMEOUT = 30

VENUE_CODES = {"01":"札幌","02":"函館","03":"福島","04":"新潟","05":"東京","06":"中山","07":"中京","08":"京都","09":"阪神","10":"小倉"}

FIELDS = [
    "race_id","date","venue","race_no","race_name","surface","distance_m","class","field_size","weather","going",
    "first_no","first_name","first_popularity","first_odds",
    "second_no","second_name","second_popularity","second_odds",
    "third_no","third_name","third_popularity","third_odds",
    "win_payout","quinella_payout","wide_payouts","trio_payout","trifecta_payout",
    "source_bulk","source_detail","jra_official_check","data_status","notes"
]


def http_get(url: str, encoding: str = "euc-jp") -> Optional[str]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.content.decode(encoding, errors="replace")
        except Exception as e:
            if attempt == MAX_RETRIES:
                print(f"[ERROR] {url}: {e}", flush=True)
                return None
            time.sleep(attempt * 2)
    return None


def clone_source() -> None:
    if SOURCE_DIR.exists():
        shutil.rmtree(SOURCE_DIR)
    SOURCE_DIR.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git","clone","--depth","1",SOURCE_REPO,str(SOURCE_DIR)], check=True)


def load_race_ids() -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for p in sorted((SOURCE_DIR / "data").glob("results_*.json")):
        m = re.fullmatch(r"results_(\d{8})\.json", p.name)
        if not m:
            continue
        d = datetime.strptime(m.group(1), "%Y%m%d").date()
        if not (START <= d <= END):
            continue
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for r in obj.get("races", []):
            rid = str(r.get("race_id", ""))
            if len(rid) == 12 and rid[4:6] in VENUE_CODES:
                out.append((rid, p.name))
    # dedupe preserving order
    seen=set(); dedup=[]
    for x in out:
        if x[0] not in seen:
            seen.add(x[0]); dedup.append(x)
    return dedup


def normalize_class(race_name: str, info: str) -> str:
    t = f"{race_name} {info}"
    rules = [
        (r"新馬", "新馬"),(r"未勝利", "未勝利"),(r"障害", "障害"),
        (r"1勝|500万", "1勝"),(r"2勝|1000万", "2勝"),(r"3勝|1600万", "3勝"),
        (r"G1|Ｇ１|GI\b", "G1"),(r"G2|Ｇ２|GII\b", "G2"),(r"G3|Ｇ３|GIII\b", "G3"),
        (r"リステッド|\(L\)|\bL\b", "L"),(r"オープン|OP", "OP"),
    ]
    for pat, val in rules:
        if re.search(pat, t, re.I): return val
    return "その他"


def clean_money(s: str) -> str:
    return re.sub(r"[^0-9]", "", s or "")


def parse_payouts(soup: BeautifulSoup) -> Dict[str,str]:
    result = {"単勝":"","馬連":"","ワイド":"","三連複":"","三連単":""}
    for table in soup.select("table.pay_table_01, table.pay_table_02"):
        for tr in table.select("tr"):
            th = tr.select_one("th")
            if not th: continue
            typ = th.get_text(" ", strip=True)
            tds = tr.select("td")
            if not tds: continue
            # netkeiba payout rows generally alternate combination and payout cells.
            texts = [td.get_text(" ", strip=True) for td in tds]
            pairs=[]
            for i in range(0, len(texts)-1, 2):
                combo=texts[i]; pay=clean_money(texts[i+1])
                if combo and pay: pairs.append(f"{combo}:{pay}")
            if "単勝" in typ and pairs: result["単勝"] = pairs[0].split(":")[-1]
            elif "馬連" in typ and pairs: result["馬連"] = pairs[0].split(":")[-1]
            elif "ワイド" in typ and pairs: result["ワイド"] = " / ".join(pairs)
            elif "三連複" in typ and pairs: result["三連複"] = pairs[0].split(":")[-1]
            elif "三連単" in typ and pairs: result["三連単"] = pairs[0].split(":")[-1]
    return result


def parse_result(race_id: str, bulk_file: str) -> Tuple[Optional[Dict], List[str]]:
    url = f"https://db.netkeiba.com/race/{race_id}/"
    html = http_get(url)
    if not html:
        return None, ["detail_fetch_failed"]
    soup = BeautifulSoup(html, "lxml")
    intro = soup.select_one(".data_intro, .racedata")
    intro_text = intro.get_text(" ", strip=True) if intro else ""
    h1 = soup.select_one(".data_intro h1, .racedata h1")
    race_name = h1.get_text(" ", strip=True) if h1 else ""

    date_str = race_id[:4] + "-" + race_id[4:6] + "-" + race_id[6:8]
    # race_id date is not calendar date for JRA format; overwrite from page when available
    dm = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", soup.get_text(" ", strip=True))
    if dm:
        date_str = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"

    venue = ""
    for v in VENUE_CODES.values():
        if v in intro_text:
            venue=v; break
    if not venue: venue = VENUE_CODES.get(race_id[4:6], "")

    sm = re.search(r"(芝|ダート|障害)\s*([0-9]{3,4})m", intro_text)
    surface = sm.group(1) if sm else ""
    distance = int(sm.group(2)) if sm else ""
    wm = re.search(r"天候\s*[:：]\s*([^\s/]+)", intro_text)
    weather = wm.group(1) if wm else ""
    gm = re.search(r"(?:芝|ダート)\s*[:：]\s*([^\s/]+)", intro_text)
    going = gm.group(1) if gm else ""

    table = soup.select_one("table.race_table_01")
    anomalies=[]
    runners=[]
    if table:
        header_cells = table.select("tr th")
        headers=[x.get_text(" ", strip=True) for x in header_cells]
        for tr in table.select("tr")[1:]:
            cells=tr.select("td")
            if not cells: continue
            vals=[c.get_text(" ", strip=True) for c in cells]
            row={}
            for i,v in enumerate(vals):
                key=headers[i] if i < len(headers) else str(i)
                row[key]=v
            rank_txt=row.get("着順", vals[0] if vals else "")
            mm=re.search(r"\d+", rank_txt)
            if not mm: continue
            rank=int(mm.group())
            def gv(label, fallback=""):
                return row.get(label, fallback)
            horse_no=re.sub(r"\D", "", gv("馬番", vals[2] if len(vals)>2 else ""))
            horse_name=gv("馬名", vals[3] if len(vals)>3 else "")
            odds=gv("単勝", "")
            pop=gv("人気", "")
            runners.append({"rank":rank,"no":horse_no,"name":horse_name,"odds":odds,"pop":pop})
    if len(runners) < 3:
        anomalies.append("result_rows_lt_3")
    runners.sort(key=lambda x:x["rank"])
    top3=runners[:3]
    payouts=parse_payouts(soup)
    if not payouts["馬連"]: anomalies.append("quinella_missing")
    if not payouts["三連複"] and len(runners)>=3: anomalies.append("trio_missing")

    def top(i, key):
        return top3[i].get(key, "") if len(top3)>i else ""
    row = {
        "race_id":race_id,"date":date_str,"venue":venue,"race_no":int(race_id[-2:]),"race_name":race_name,
        "surface":surface,"distance_m":distance,"class":normalize_class(race_name,intro_text),"field_size":len(runners),
        "weather":weather,"going":going,
        "first_no":top(0,"no"),"first_name":top(0,"name"),"first_popularity":top(0,"pop"),"first_odds":top(0,"odds"),
        "second_no":top(1,"no"),"second_name":top(1,"name"),"second_popularity":top(1,"pop"),"second_odds":top(1,"odds"),
        "third_no":top(2,"no"),"third_name":top(2,"name"),"third_popularity":top(2,"pop"),"third_odds":top(2,"odds"),
        "win_payout":payouts["単勝"],"quinella_payout":payouts["馬連"],"wide_payouts":payouts["ワイド"],
        "trio_payout":payouts["三連複"],"trifecta_payout":payouts["三連単"],
        "source_bulk":f"https://github.com/uma-logic-user/uma-logic/blob/main/data/{bulk_file}",
        "source_detail":url,"jra_official_check":"要照合" if anomalies else "未照合",
        "data_status":"異常候補" if anomalies else "公開データ取得済","notes":";".join(anomalies)
    }
    return row, anomalies


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    clone_source()
    ids=load_race_ids()
    print(f"[INFO] race ids: {len(ids)}", flush=True)
    rows=[]; anomaly_rows=[]
    for idx,(rid,bfile) in enumerate(ids,1):
        row, anomalies=parse_result(rid,bfile)
        if row:
            # Enforce calendar target using parsed date.
            try:
                d=datetime.strptime(row["date"], "%Y-%m-%d").date()
            except Exception:
                d=None
            if d is not None and START <= d <= END:
                rows.append(row)
                if anomalies:
                    anomaly_rows.append({"race_id":rid,"date":row["date"],"venue":row["venue"],"race_no":row["race_no"],"issues":";".join(anomalies),"source_detail":row["source_detail"]})
        if idx % 50 == 0:
            print(f"[INFO] {idx}/{len(ids)} processed, {len(rows)} kept", flush=True)
        time.sleep(SLEEP_SEC)

    # Missing latest weekend (2026-08-15/16) is intentionally not fabricated here.
    # It is merged later from the already reconstructed audit workbook / official verification.
    rows.sort(key=lambda r:(r["date"],r["venue"],int(r["race_no"])))
    with OUT_CSV.open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS); w.writeheader(); w.writerows(rows)
    with ANOMALY_CSV.open("w",newline="",encoding="utf-8-sig") as f:
        af=["race_id","date","venue","race_no","issues","source_detail"]
        w=csv.DictWriter(f,fieldnames=af); w.writeheader(); w.writerows(anomaly_rows)
    dates=sorted({r["date"] for r in rows})
    summary={"target_start":START.isoformat(),"target_end":END.isoformat(),"race_rows":len(rows),"race_days":len(dates),"first_date":dates[0] if dates else None,"last_date":dates[-1] if dates else None,"anomalies":len(anomaly_rows),"note":"2026-08-15/16 are merged separately from the reconstructed audit/JRA-official dataset if absent from bulk source."}
    SUMMARY_JSON.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False), flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
