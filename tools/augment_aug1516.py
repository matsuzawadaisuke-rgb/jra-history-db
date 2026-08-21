#!/usr/bin/env python3
from __future__ import annotations
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.build_jra_history import parse_result, FIELDS

OUT=ROOT/'data'/'jra_results_20250821_20260820.csv'
ANOM=ROOT/'data'/'jra_anomalies_20250821_20260820.csv'
VENUES=[('01','札幌','01'),('04','新潟','02'),('07','中京','02')]
DATES=[('2026-08-15','07'),('2026-08-16','08')]

def direct_items():
    items=[]
    for d,day in DATES:
        for vcode,vname,kai in VENUES:
            for r in range(1,13):
                rid=f"2026{vcode}{kai}{day}{r:02d}"
                items.append((rid,d,'direct_aug1516',{'race_name':'','venue':vname}))
    return items

def main():
    existing=[]
    if OUT.exists():
        with OUT.open(encoding='utf-8-sig',newline='') as f: existing=list(csv.DictReader(f))
    byid={r['race_id']:r for r in existing}
    issues=[]
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs={ex.submit(parse_result,it):it[0] for it in direct_items()}
        for fut in as_completed(futs):
            rid=futs[fut]
            row,errs=fut.result()
            row['source_bulk']='direct_race_id_for_2026-08-15_16'
            byid[rid]=row
            if errs:
                issues.append({'race_id':rid,'date':row['date'],'venue':row['venue'],'race_no':row['race_no'],'issues':';'.join(sorted(set(errs))),'source_detail':row['source_detail']})
    rows=sorted(byid.values(),key=lambda r:(r['date'],r['venue'],int(r['race_no'])))
    with OUT.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
    # Preserve any prior anomalies except the already JRA-officially repaired 2025-12-06 Nakayama 11; append aug issues.
    prior=[]
    if ANOM.exists():
        with ANOM.open(encoding='utf-8-sig',newline='') as f:
            prior=[r for r in csv.DictReader(f) if r.get('race_id')!='202506050111']
    merged={r['race_id']:r for r in prior+issues}
    with ANOM.open('w',encoding='utf-8-sig',newline='') as f:
        af=['race_id','date','venue','race_no','issues','source_detail']
        w=csv.DictWriter(f,fieldnames=af);w.writeheader();w.writerows(sorted(merged.values(),key=lambda r:r['race_id']))
    print(f'rows={len(rows)} aug_issues={len(issues)} total_anomalies={len(merged)}')

if __name__=='__main__': main()
