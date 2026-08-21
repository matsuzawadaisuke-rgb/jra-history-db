#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import csv, json, re, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import requests
from bs4 import BeautifulSoup

DATA=Path('data/jra_results_20250821_20260820.csv')
ANOM=Path('data/jra_anomalies_20250821_20260820.csv')
SUMMARY=Path('data/build_summary.json')
MAX_WORKERS=4
HEADERS={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36'}
_tls=threading.local()

def ses():
    if not hasattr(_tls,'s'):
        _tls.s=requests.Session(); _tls.s.headers.update(HEADERS)
    return _tls.s

def fetch_meta(row):
    url=row['source_detail']
    for n in range(4):
        try:
            r=ses().get(url,timeout=30); r.raise_for_status()
            html=r.content.decode('euc-jp',errors='replace')
            soup=BeautifulSoup(html,'lxml')
            intro=soup.select_one('.data_intro, .racedata')
            text=intro.get_text(' ',strip=True) if intro else ''
            # Examples include 芝右1800m, 芝左1600m, ダ左1800m, ダート1700m.
            m=re.search(r'(芝|ダート|ダ|障)[^0-9]{0,12}([0-9]{3,4})m',text)
            if not m:
                return row['race_id'],None,None,'course_parse_failed'
            token=m.group(1); dist=int(m.group(2))
            if row.get('class')=='障害' or '障害' in row.get('race_name',''):
                surf='障害'
            elif token.startswith('ダ'):
                surf='ダート'
            elif token.startswith('芝'):
                surf='芝'
            else:
                surf='障害'
            time.sleep(0.15)
            return row['race_id'],surf,dist,''
        except Exception as e:
            if n==3: return row['race_id'],None,None,'course_fetch_failed'
            time.sleep(1.5*(n+1))


def main():
    with DATA.open(encoding='utf-8-sig',newline='') as f:
        rd=csv.DictReader(f); fields=rd.fieldnames; rows=list(rd)
    targets=[r for r in rows if not r.get('surface') or not r.get('distance_m')]
    print(f'[INFO] repair targets={len(targets)}',flush=True)
    fixes={}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs={ex.submit(fetch_meta,r):r['race_id'] for r in targets}
        done=0
        for fut in as_completed(futs):
            rid,surf,dist,err=fut.result(); fixes[rid]=(surf,dist,err); done+=1
            if done%100==0: print(f'[INFO] {done}/{len(targets)} repaired',flush=True)
    unresolved=[]
    for r in rows:
        if r.get('class')=='障害': r['surface']='障害'
        if r['race_id'] in fixes:
            surf,dist,err=fixes[r['race_id']]
            if surf and dist:
                r['surface']=surf; r['distance_m']=str(dist)
                issues=[x for x in r.get('notes','').split(';') if x and x!='course_meta_missing']
            else:
                issues=[x for x in r.get('notes','').split(';') if x]
                if err and err not in issues: issues.append(err)
            r['notes']=';'.join(sorted(set(issues)))
        issues=[x for x in r.get('notes','').split(';') if x]
        r['data_status']='異常候補' if issues else '公開データ取得済'
        r['jra_official_check']='要照合' if issues else '未照合'
        if issues:
            unresolved.append({'race_id':r['race_id'],'date':r['date'],'venue':r['venue'],'race_no':r['race_no'],'issues':';'.join(issues),'source_detail':r['source_detail']})
    with DATA.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    af=['race_id','date','venue','race_no','issues','source_detail']
    with ANOM.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=af); w.writeheader(); w.writerows(unresolved)
    dates=sorted({r['date'] for r in rows})
    summary={'target_start':'2025-08-21','target_end':'2026-08-20','race_rows':len(rows),'race_days':len(dates),'first_date':dates[0] if dates else None,'last_date':dates[-1] if dates else None,'anomalies':len(unresolved),'course_repair_targets':len(targets),'course_repair_success':len(targets)-sum(1 for r in unresolved if 'course_' in r['issues']),'note':'2026-08-15/16 are merged separately from the reconstructed audit/JRA-official dataset in the final workbook.'}
    SUMMARY.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=='__main__': main()
