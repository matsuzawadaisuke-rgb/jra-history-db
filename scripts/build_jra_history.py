#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a one-year JRA race-result database.

Bulk race IDs / fallback results come from the public uma-logic repository.
Race metadata, official finishing order fields and payouts are re-read from
netkeiba result pages. Every row carries source URLs and an audit status; we do
not guess missing values. JRA official confirmation is applied later to rows
flagged as anomalies and to the reconstructed 2026-08-15/16 audit data.
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
MAX_RETRIES = 4
TIMEOUT = 30
MAX_WORKERS = 4
REQUEST_PAUSE = 0.20

VENUE_CODES = {"01":"札幌","02":"函館","03":"福島","04":"新潟","05":"東京","06":"中山","07":"中京","08":"京都","09":"阪神","10":"小倉"}
FIELDS = [
    "race_id","date","venue","race_no","race_name","surface","distance_m","class","field_size","weather","going",
    "first_no","first_name","first_popularity","first_odds",
    "second_no","second_name","second_popularity","second_odds",
    "third_no","third_name","third_popularity","third_odds",
    "win_payout","quinella_payout","wide_payouts","trio_payout","trifecta_payout",
    "source_bulk","source_detail","jra_official_check","data_status","notes"
]

_thread_local = threading.local()

def session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        s=requests.Session(); s.headers.update(HEADERS); _thread_local.session=s
    return _thread_local.session


def http_get(url: str, encoding: str = "euc-jp") -> Optional[str]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r=session().get(url, timeout=TIMEOUT)
            r.raise_for_status()
            time.sleep(REQUEST_PAUSE)
            return r.content.decode(encoding, errors="replace")
        except Exception as e:
            if attempt == MAX_RETRIES:
                print(f"[ERROR] {url}: {e}", flush=True)
                return None
            time.sleep(attempt * 1.5)
    return None


def clone_source() -> None:
    if SOURCE_DIR.exists(): shutil.rmtree(SOURCE_DIR)
    SOURCE_DIR.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git","clone","--depth","1",SOURCE_REPO,str(SOURCE_DIR)], check=True)


def load_bulk() -> List[Tuple[str, str, str, Dict]]:
    out=[]
    for p in sorted((SOURCE_DIR / "data").glob("results_*.json")):
        m=re.fullmatch(r"results_(\d{8})\.json", p.name)
        if not m: continue
        d=datetime.strptime(m.group(1), "%Y%m%d").date()
        if not (START <= d <= END): continue
        try: obj=json.loads(p.read_text(encoding="utf-8"))
        except Exception: continue
        calendar_date=d.isoformat()
        for r in obj.get("races", []):
            rid=str(r.get("race_id", ""))
            if len(rid)==12 and rid[4:6] in VENUE_CODES:
                out.append((rid, calendar_date, p.name, r))
    seen=set(); dedup=[]
    for x in out:
        if x[0] not in seen:
            seen.add(x[0]); dedup.append(x)
    return dedup


def normalize_class(race_name: str, info: str) -> str:
    t=f"{race_name} {info}"
    rules=[
        (r"新馬","新馬"),(r"未勝利","未勝利"),(r"障害","障害"),
        (r"G1|Ｇ１|GⅠ|GI\b","G1"),(r"G2|Ｇ２|GⅡ|GII\b","G2"),(r"G3|Ｇ３|GⅢ|GIII\b","G3"),
        (r"リステッド|\(L\)|\bL\b","L"),(r"1勝|500万","1勝"),(r"2勝|1000万","2勝"),(r"3勝|1600万","3勝"),
        (r"オープン|OP","OP"),
    ]
    for pat,val in rules:
        if re.search(pat,t,re.I): return val
    return "その他"


def digits(s: str) -> str:
    return re.sub(r"[^0-9]", "", s or "")


def norm_header(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def parse_payouts(soup: BeautifulSoup) -> Dict[str,str]:
    result={"単勝":"","馬連":"","ワイド":"","三連複":"","三連単":""}
    for table in soup.select("table.pay_table_01, table.pay_table_02"):
        for tr in table.select("tr"):
            th=tr.select_one("th")
            if not th: continue
            typ=norm_header(th.get_text(" ", strip=True))
            tds=tr.select("td")
            if len(tds)<2: continue
            combos=[x.strip() for x in tds[0].stripped_strings if x.strip()]
            pays=[digits(x) for x in tds[1].stripped_strings if digits(x)]
            pairs=[f"{c}:{p}" for c,p in zip(combos,pays)]
            first=pays[0] if pays else ""
            if "単勝" in typ: result["単勝"]=first
            elif typ=="馬連": result["馬連"]=first
            elif "ワイド" in typ: result["ワイド"]=" / ".join(pairs)
            elif "三連複" in typ: result["三連複"]=first
            elif "三連単" in typ: result["三連単"]=first
    return result


def fallback_top3(bulk: Dict) -> List[Dict]:
    arr=bulk.get("top3") or []
    out=[]
    for h in arr[:3]:
        out.append({
            "rank":h.get("着順", ""),"no":h.get("馬番", ""),"name":h.get("馬名", ""),
            "pop":"","odds":h.get("オッズ", "")
        })
    return out


def parse_result(item: Tuple[str,str,str,Dict]) -> Tuple[Dict,List[str]]:
    race_id, calendar_date, bulk_file, bulk = item
    url=f"https://db.netkeiba.com/race/{race_id}/"
    html=http_get(url)
    anomalies=[]
    intro_text=""; race_name=str(bulk.get("race_name", "")); runners=[]; payouts={"単勝":"","馬連":"","ワイド":"","三連複":"","三連単":""}
    surface=""; distance=""; weather=""; going=""; venue=str(bulk.get("venue", "")) or VENUE_CODES.get(race_id[4:6], "")

    if html:
        soup=BeautifulSoup(html,"lxml")
        intro=soup.select_one(".data_intro, .racedata")
        intro_text=intro.get_text(" ", strip=True) if intro else ""
        h1=soup.select_one(".data_intro h1, .racedata h1")
        if h1: race_name=h1.get_text(" ", strip=True)
        for v in VENUE_CODES.values():
            if v in intro_text: venue=v; break
        sm=re.search(r"(芝|ダート|障害)[^0-9]{0,10}([0-9]{3,4})m", intro_text)
        if sm: surface=sm.group(1); distance=int(sm.group(2))
        wm=re.search(r"天候\s*[:：]\s*([^\s/]+)", intro_text)
        if wm: weather=wm.group(1)
        gm=re.search(r"(?:芝|ダート)\s*[:：]\s*([^\s/]+)", intro_text)
        if gm: going=gm.group(1)

        table=soup.select_one("table.race_table_01")
        if table:
            trs=table.select("tr")
            headers=[]
            if trs:
                headers=[norm_header(x.get_text(" ",strip=True)) for x in trs[0].select("th")]
            for tr in trs[1:]:
                cells=tr.select("td")
                if not cells: continue
                vals=[c.get_text(" ",strip=True) for c in cells]
                row={headers[i]:vals[i] for i in range(min(len(headers),len(vals)))} if headers else {}
                rank_txt=row.get("着順", vals[0] if vals else "")
                mm=re.search(r"^\d+", str(rank_txt))
                if not mm: continue
                rank=int(mm.group())
                horse_no=digits(row.get("馬番", vals[2] if len(vals)>2 else ""))
                horse_name=row.get("馬名", vals[3] if len(vals)>3 else "")
                runners.append({"rank":rank,"no":horse_no,"name":horse_name,"odds":row.get("単勝", ""),"pop":digits(row.get("人気", ""))})
        payouts=parse_payouts(soup)
    else:
        anomalies.append("detail_fetch_failed")

    runners.sort(key=lambda x:x.get("rank",999))
    top3=runners[:3] if len(runners)>=3 else fallback_top3(bulk)
    if len(top3)<3: anomalies.append("top3_missing")
    field_size=len(runners) if runners else len(bulk.get("all_results") or [])

    # Fallback payout values are retained only when single-valued; concatenated multi-value rows remain blank and are flagged.
    bp=bulk.get("payouts") or {}
    if not payouts["単勝"] and bp.get("単勝"): payouts["単勝"]=str(bp.get("単勝"))
    if not payouts["馬連"] and bp.get("馬連"): payouts["馬連"]=str(bp.get("馬連"))
    if not payouts["三連複"] and bp.get("三連複"): payouts["三連複"]=str(bp.get("三連複"))
    if not payouts["三連単"] and bp.get("三連単"): payouts["三連単"]=str(bp.get("三連単"))
    if not payouts["ワイド"]: anomalies.append("wide_missing")
    if not payouts["馬連"]: anomalies.append("quinella_missing")
    if not payouts["三連複"]: anomalies.append("trio_missing")
    if not surface or not distance: anomalies.append("course_meta_missing")
    if not weather or not going: anomalies.append("weather_going_missing")

    def top(i,key): return top3[i].get(key,"") if len(top3)>i else ""
    row={
        "race_id":race_id,"date":calendar_date,"venue":venue,"race_no":int(race_id[-2:]),"race_name":race_name,
        "surface":surface,"distance_m":distance,"class":normalize_class(race_name,intro_text),"field_size":field_size,"weather":weather,"going":going,
        "first_no":top(0,"no"),"first_name":top(0,"name"),"first_popularity":top(0,"pop"),"first_odds":top(0,"odds"),
        "second_no":top(1,"no"),"second_name":top(1,"name"),"second_popularity":top(1,"pop"),"second_odds":top(1,"odds"),
        "third_no":top(2,"no"),"third_name":top(2,"name"),"third_popularity":top(2,"pop"),"third_odds":top(2,"odds"),
        "win_payout":payouts["単勝"],"quinella_payout":payouts["馬連"],"wide_payouts":payouts["ワイド"],"trio_payout":payouts["三連複"],"trifecta_payout":payouts["三連単"],
        "source_bulk":f"https://github.com/uma-logic-user/uma-logic/blob/main/data/{bulk_file}","source_detail":url,
        "jra_official_check":"要照合" if anomalies else "未照合","data_status":"異常候補" if anomalies else "公開データ取得済","notes":";".join(sorted(set(anomalies)))
    }
    return row, anomalies


def main() -> int:
    OUT_DIR.mkdir(parents=True,exist_ok=True)
    clone_source()
    items=load_bulk()
    print(f"[INFO] race ids: {len(items)}",flush=True)
    rows=[]; anomaly_rows=[]; done=0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures={ex.submit(parse_result,item):item[0] for item in items}
        for fut in as_completed(futures):
            rid=futures[fut]
            try: row,issues=fut.result()
            except Exception as e:
                row=None; issues=[f"exception:{type(e).__name__}"]
                print(f"[ERROR] {rid}: {e}",flush=True)
            if row:
                rows.append(row)
                if issues:
                    anomaly_rows.append({"race_id":rid,"date":row["date"],"venue":row["venue"],"race_no":row["race_no"],"issues":";".join(sorted(set(issues))),"source_detail":row["source_detail"]})
            done+=1
            if done%100==0: print(f"[INFO] {done}/{len(items)} processed",flush=True)

    rows.sort(key=lambda r:(r["date"],r["venue"],int(r["race_no"])))
    with OUT_CSV.open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS); w.writeheader(); w.writerows(rows)
    with ANOMALY_CSV.open("w",newline="",encoding="utf-8-sig") as f:
        af=["race_id","date","venue","race_no","issues","source_detail"]
        w=csv.DictWriter(f,fieldnames=af); w.writeheader(); w.writerows(anomaly_rows)
    dates=sorted({r["date"] for r in rows})
    summary={
        "target_start":START.isoformat(),"target_end":END.isoformat(),"race_rows":len(rows),"race_days":len(dates),
        "first_date":dates[0] if dates else None,"last_date":dates[-1] if dates else None,"anomalies":len(anomaly_rows),
        "note":"Bulk source currently ends before 2026-08-15/16; those dates are merged from the reconstructed audit/JRA-official dataset in the final workbook."
    }
    SUMMARY_JSON.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    return 0

if __name__=="__main__": sys.exit(main())
