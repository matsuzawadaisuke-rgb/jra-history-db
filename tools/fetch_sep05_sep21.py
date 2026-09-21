#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import csv,re,time,json
from datetime import date,timedelta
from pathlib import Path
import requests
from bs4 import BeautifulSoup
START=date(2026,9,5); END=date(2026,9,21)
OUT=Path("data/jra_results_20260905_20260921.csv")
SUMMARY=Path("data/jra_results_20260905_20260921_summary.json")
HEADERS={"User-Agent":"Mozilla/5.0 Chrome/131 Safari/537.36"}
VENUE_CODES={"01":"札幌","02":"函館","03":"福島","04":"新潟","05":"東京","06":"中山","07":"中京","08":"京都","09":"阪神","10":"小倉"}
FIELDS=["race_id","date","venue","race_no","race_name","surface","distance_m","class","field_size","weather","going","first_no","first_name","first_popularity","first_odds","second_no","second_name","second_popularity","second_odds","third_no","third_name","third_popularity","third_odds","win_payout","quinella_payout","wide_payouts","trio_payout","trifecta_payout","source_bulk","source_detail","jra_official_check","data_status","notes"]
S=requests.Session();S.headers.update(HEADERS)
def get(url,enc="euc-jp"):
    for i in range(4):
        try:
            r=S.get(url,timeout=30);r.raise_for_status();time.sleep(.12);return r.content.decode(enc,errors="replace")
        except Exception:
            if i==3:return None
            time.sleep(1+i)
def digits(x):return re.sub(r"[^0-9]","",str(x or ""))
def norm(x):return re.sub(r"\s+","",str(x or ""))
def cls(name,info):
    t=f"{name} {info}"
    for p,v in [(r"新馬","新馬"),(r"未勝利","未勝利"),(r"障害","障害"),(r"G1|Ｇ１|GⅠ|GI\\b","G1"),(r"G2|Ｇ２|GⅡ|GII\\b","G2"),(r"G3|Ｇ３|GⅢ|GIII\\b","G3"),(r"リステッド|\\(L\\)|\\bL\\b","L"),(r"1勝|500万","1勝"),(r"2勝|1000万","2勝"),(r"3勝|1600万","3勝"),(r"オープン|OP","OP")]:
        if re.search(p,t,re.I):return v
    return "その他"
def pays(soup):
    out={"単勝":"","馬連":"","ワイド":"","三連複":"","三連単":""}
    for table in soup.select("table.pay_table_01,table.pay_table_02"):
        for tr in table.select("tr"):
            th=tr.select_one("th");tds=tr.select("td")
            if not th or len(tds)<2:continue
            typ=norm(th.get_text(" ",strip=True));comb=[x.strip() for x in tds[0].stripped_strings if x.strip()];ps=[digits(x) for x in tds[1].stripped_strings if digits(x)]
            pairs=[f"{a}:{b}" for a,b in zip(comb,ps)];first=ps[0] if ps else ""
            if "単勝" in typ:out["単勝"]=first
            elif typ=="馬連":out["馬連"]=first
            elif "ワイド" in typ:out["ワイド"]=" / ".join(pairs)
            elif "三連複" in typ:out["三連複"]=first
            elif "三連単" in typ:out["三連単"]=first
    return out
def ids_for(d):
    ds=d.strftime("%Y%m%d");html=get(f"https://db.netkeiba.com/race/list/{ds}/")
    if not html:return []
    ids=sorted(set(re.findall(r"/race/(20\\d{10})/?",html)))
    return [r for r in ids if r[:4]==str(d.year) and r[4:6] in VENUE_CODES]
def parse(rid,d):
    url=f"https://db.netkeiba.com/race/{rid}/";html=get(url);issues=[]
    if not html:return None,["fetch_failed"]
    soup=BeautifulSoup(html,"lxml");intro=soup.select_one(".data_intro,.racedata");info=intro.get_text(" ",strip=True) if intro else ""
    h1=soup.select_one(".data_intro h1,.racedata h1");name=h1.get_text(" ",strip=True) if h1 else "";surface="";distance="";weather="";going=""
    m=re.search(r"(芝|ダート|障害)[^0-9]{0,12}([0-9]{3,4})m",info)
    if m:surface=m.group(1);distance=int(m.group(2))
    wm=re.search(r"天候\\s*[:：]\\s*([^\\s/]+)",info)
    if wm:weather=wm.group(1)
    gm=re.search(r"(?:芝|ダート)\\s*[:：]\\s*([^\\s/]+)",info)
    if gm:going=gm.group(1)
    runners=[];table=soup.select_one("table.race_table_01")
    if table:
        trs=table.select("tr");hdr=[norm(x.get_text(" ",strip=True)) for x in trs[0].select("th")] if trs else []
        for tr in trs[1:]:
            cells=tr.select("td")
            if not cells:continue
            vals=[c.get_text(" ",strip=True) for c in cells];row={hdr[i]:vals[i] for i in range(min(len(hdr),len(vals)))} if hdr else {};mm=re.match(r"^\\d+",str(row.get("着順",vals[0] if vals else "")))
            if not mm:continue
            runners.append({"rank":int(mm.group()),"no":digits(row.get("馬番","")),"name":row.get("馬名",""),"pop":digits(row.get("人気","")),"odds":row.get("単勝","")})
    runners.sort(key=lambda x:x["rank"]);top=runners[:3];p=pays(soup)
    if len(top)<3:issues.append("top3_missing")
    if not surface or not distance:issues.append("course_meta_missing")
    if not weather or (surface!="障害" and not going):issues.append("weather_going_missing")
    if not p["馬連"]:issues.append("quinella_missing")
    if not p["三連複"]:issues.append("trio_missing")
    if not p["ワイド"]:issues.append("wide_missing")
    def v(i,k):return top[i].get(k,"") if len(top)>i else ""
    return {"race_id":rid,"date":d.isoformat(),"venue":VENUE_CODES.get(rid[4:6],""),"race_no":int(rid[-2:]),"race_name":name,"surface":surface,"distance_m":distance,"class":cls(name,info),"field_size":len(runners),"weather":weather,"going":going,"first_no":v(0,"no"),"first_name":v(0,"name"),"first_popularity":v(0,"pop"),"first_odds":v(0,"odds"),"second_no":v(1,"no"),"second_name":v(1,"name"),"second_popularity":v(1,"pop"),"second_odds":v(1,"odds"),"third_no":v(2,"no"),"third_name":v(2,"name"),"third_popularity":v(2,"pop"),"third_odds":v(2,"odds"),"win_payout":p["単勝"],"quinella_payout":p["馬連"],"wide_payouts":p["ワイド"],"trio_payout":p["三連複"],"trifecta_payout":p["三連単"],"source_bulk":f"https://db.netkeiba.com/race/list/{d.strftime('%Y%m%d')}/","source_detail":url,"jra_official_check":"未照合","data_status":"公開データ取得済" if not issues else "異常候補","notes":";".join(sorted(set(issues)))},issues
def main():
    OUT.parent.mkdir(exist_ok=True);rows=[];day_counts={};bad=[];d=START
    while d<=END:
        ids=ids_for(d)
        if ids:
            print(d.isoformat(),len(ids),flush=True);day_counts[d.isoformat()]=len(ids)
            for rid in ids:
                row,iss=parse(rid,d)
                if row:rows.append(row)
                if iss:bad.append({"race_id":rid,"issues":iss})
        d+=timedelta(days=1)
    rows.sort(key=lambda r:(r["date"],r["venue"],int(r["race_no"])))
    with OUT.open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
    summary={"start":START.isoformat(),"end":END.isoformat(),"rows":len(rows),"days":day_counts,"unique_ids":len({r["race_id"] for r in rows}),"issue_rows":len(bad)}
    SUMMARY.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8");print(json.dumps(summary,ensure_ascii=False),flush=True)
if __name__=="__main__":main()
