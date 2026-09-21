#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import asyncio,csv,json,re
from datetime import date,timedelta
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

START=date(2026,9,5); END=date(2026,9,21)
OUT=Path("data/jra_results_20260905_20260921.csv")
SUMMARY=Path("data/jra_results_20260905_20260921_summary.json")
VENUE_CODES={"01":"札幌","02":"函館","03":"福島","04":"新潟","05":"東京","06":"中山","07":"中京","08":"京都","09":"阪神","10":"小倉"}
MEETINGS={
    "2026-09-05":["2026060401","2026090401","2026010205"],
    "2026-09-06":["2026060402","2026090402","2026010206"],
    "2026-09-12":["2026060403","2026090403"],
    "2026-09-13":["2026060404","2026090404"],
    "2026-09-19":["2026060405","2026090405"],
    "2026-09-20":["2026060406","2026090406"],
    "2026-09-21":["2026060407","2026090407"],
}
FIELDS=["race_id","date","venue","race_no","race_name","surface","distance_m","class","field_size","weather","going",
"first_no","first_name","first_popularity","first_odds","second_no","second_name","second_popularity","second_odds",
"third_no","third_name","third_popularity","third_odds","win_payout","quinella_payout","wide_payouts","trio_payout",
"trifecta_payout","source_bulk","source_detail","jra_official_check","data_status","notes"]

def clean(s): return re.sub(r"\s+","",str(s or ""))
def yen(s):
    m=re.search(r"([0-9,]+)円",str(s or ""))
    return m.group(1).replace(",","") if m else ""
def normalize_class(name,extra):
    t=f"{name} {extra}"
    for p,v in [(r"新馬","新馬"),(r"未勝利","未勝利"),(r"障害","障害"),(r"G1|Ｇ１|GⅠ|GI\b","G1"),
                (r"G2|Ｇ２|GⅡ|GII\b","G2"),(r"G3|Ｇ３|GⅢ|GIII\b","G3"),(r"リステッド|\(L\)|\bL\b","L"),
                (r"1勝|500万","1勝"),(r"2勝|1000万","2勝"),(r"3勝|1600万","3勝"),(r"オープン|OP","OP")]:
        if re.search(p,t,re.I): return v
    return "その他"
def parse_course(info):
    txt=info.replace("\n"," ")
    surface="";distance="";weather="";going=""
    m=re.search(r"(芝|ダート|ダ|障害|障)[^0-9]{0,30}([0-9]{3,4})m",txt)
    if m:
        s=m.group(1); surface="芝" if s=="芝" else ("ダート" if "ダ" in s else "障害"); distance=int(m.group(2))
    wm=re.search(r"天候[:：]?\s*([晴曇雨雪]+)",txt)
    if wm: weather=wm.group(1)
    gm=re.search(r"(?:馬場|芝|ダート)[:：]?\s*([良稍重不]+)",txt)
    if gm: going=gm.group(1)
    return surface,distance,weather,going
def parse_payout_rows(rows):
    out={"単勝":"","馬連":"","ワイド":"","3連複":"","3連単":""}
    for cells in rows:
        if len(cells)<3: continue
        label=clean(cells[0])
        if label=="単勝": out["単勝"]=yen(cells[2])
        elif label=="馬連": out["馬連"]=yen(cells[2])
        elif label=="3連複": out["3連複"]=yen(cells[2])
        elif label=="3連単": out["3連単"]=yen(cells[2])
        elif label=="ワイド":
            combos=[x.strip() for x in str(cells[1]).split("\n") if x.strip()]
            pays=[x.replace(",","") for x in re.findall(r"[0-9,]+(?=円)",str(cells[2]))]
            out["ワイド"]=" / ".join(f"{re.sub(r'\\s+','-',c)}:{p}" for c,p in zip(combos,pays))
    return out

async def scrape_one(browser,rid,d,sem):
    url=f"https://race.netkeiba.com/race/result.html?race_id={rid}"
    issues=[]
    async with sem:
        page=await browser.new_page()
        try:
            await page.goto(url,wait_until="domcontentloaded",timeout=60000)
            await page.wait_for_selector("table.RaceTable01 tr.HorseList",timeout=25000)
            name=clean(await page.locator(".RaceName").first.inner_text()) if await page.locator(".RaceName").count() else ""
            info=await page.locator(".RaceData01").first.inner_text() if await page.locator(".RaceData01").count() else ""
            extra=await page.locator(".RaceData02").first.inner_text() if await page.locator(".RaceData02").count() else ""
            surface,distance,weather,going=parse_course(info)
            rowloc=page.locator("table.RaceTable01 tr.HorseList")
            field_size=await rowloc.count()
            tops=[]
            for i in range(min(3,field_size)):
                vals=await rowloc.nth(i).locator("td").all_inner_texts()
                vals=[v.strip() for v in vals]
                if len(vals)>=15:
                    tops.append({"no":vals[2],"name":vals[3],"pop":vals[9],"odds":vals[10]})
            if len(tops)<3: issues.append("top3_missing")
            p_rows=[]
            pt=page.locator("table.Payout_Detail_Table tr")
            for i in range(await pt.count()):
                vals=await pt.nth(i).locator("th,td").all_inner_texts()
                if vals:p_rows.append(vals)
            p=parse_payout_rows(p_rows)
            if not surface or not distance: issues.append("course_meta_missing")
            if not weather: issues.append("weather_missing")
            if surface!="障害" and not going: issues.append("going_missing")
            for k,tag in [("馬連","quinella_missing"),("ワイド","wide_missing"),("3連複","trio_missing"),("3連単","trifecta_missing")]:
                if not p[k]: issues.append(tag)
            def v(i,k):return tops[i].get(k,"") if len(tops)>i else ""
            row={"race_id":rid,"date":d.isoformat(),"venue":VENUE_CODES.get(rid[4:6],""),"race_no":int(rid[-2:]),
                 "race_name":name,"surface":surface,"distance_m":distance,"class":normalize_class(name,extra),"field_size":field_size,
                 "weather":weather,"going":going,
                 "first_no":v(0,"no"),"first_name":v(0,"name"),"first_popularity":v(0,"pop"),"first_odds":v(0,"odds"),
                 "second_no":v(1,"no"),"second_name":v(1,"name"),"second_popularity":v(1,"pop"),"second_odds":v(1,"odds"),
                 "third_no":v(2,"no"),"third_name":v(2,"name"),"third_popularity":v(2,"pop"),"third_odds":v(2,"odds"),
                 "win_payout":p["単勝"],"quinella_payout":p["馬連"],"wide_payouts":p["ワイド"],"trio_payout":p["3連複"],
                 "trifecta_payout":p["3連単"],"source_bulk":f"https://race.netkeiba.com/top/race_list.html?kaisai_date={d.strftime('%Y%m%d')}",
                 "source_detail":url,"jra_official_check":"未照合","data_status":"公開データ取得済" if not issues else "異常候補",
                 "notes":";".join(sorted(set(issues)))}
            return row,issues
        except PlaywrightTimeoutError:
            return None,["timeout"]
        except Exception as e:
            return None,[f"exception:{type(e).__name__}:{str(e)[:100]}"]
        finally:
            await page.close()

async def amain():
    OUT.parent.mkdir(exist_ok=True)
    pairs=[];day_counts={};d=START
    while d<=END:
        bases=MEETINGS.get(d.isoformat(),[])
        if bases:
            ids=[f"{base}{r:02d}" for base in bases for r in range(1,13)]
            day_counts[d.isoformat()]=len(ids);pairs.extend((rid,d) for rid in ids)
        d+=timedelta(days=1)
    rows=[];bad=[];sem=asyncio.Semaphore(8)
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=["--disable-dev-shm-usage"])
        tasks=[asyncio.create_task(scrape_one(browser,rid,d,sem)) for rid,d in pairs]
        done=0
        for task in asyncio.as_completed(tasks):
            row,iss=await task
            if row:rows.append(row)
            if iss and row:bad.append({"race_id":row["race_id"],"issues":iss})
            elif iss:bad.append({"race_id":"unknown","issues":iss})
            done+=1
            if done%24==0:print("processed",done,"/",len(tasks),flush=True)
        await browser.close()
    rows.sort(key=lambda r:(r["date"],r["venue"],int(r["race_no"])))
    with OUT.open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
    summary={"start":START.isoformat(),"end":END.isoformat(),"rows":len(rows),"days":day_counts,
             "unique_ids":len({r["race_id"] for r in rows}),"issue_rows":len(bad),"issues":bad[:50]}
    SUMMARY.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=="__main__": asyncio.run(amain())
