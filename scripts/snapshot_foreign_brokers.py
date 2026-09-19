import csv, re, requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from pathlib import Path

HEADERS={"User-Agent":"Mozilla/5.0"}
NAMES=["台灣摩根士丹利","摩根大通","美商高盛","美林","新加坡商瑞銀","花旗環球"]
WATCHLIST=["2330","2317","2454","2382","3231","2308","3017","2368","3189","2327","2344","2408","6770","3711","3037","6669","2376","2377","2357","3661"]
OUT=Path("data/foreign_broker_history.csv")
OUT.parent.mkdir(exist_ok=True)

def fetch(symbol):
    url=f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco_{symbol}.djhtm"
    r=requests.get(url,headers=HEADERS,timeout=20); r.raise_for_status()
    r.encoding=r.apparent_encoding
    txt=BeautifulSoup(r.text,"html.parser").get_text(" ",strip=True)
    rows=[]
    for name in NAMES:
        m=re.search(re.escape(name)+r"\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d.]+)%",txt)
        if m:
            buy=int(m.group(1).replace(",","")); sell=int(m.group(2).replace(",",""))
            rows.append((name,buy,sell,buy-sell))
    return rows

tz=timezone(timedelta(hours=8))
today=datetime.now(tz).date().isoformat()
existing=[]
if OUT.exists():
    with OUT.open(encoding="utf-8") as f: existing=list(csv.DictReader(f))
seen={(r["date"],r["symbol"],r["broker"]) for r in existing}
new=[]
for symbol in WATCHLIST:
    try:
        for broker,buy,sell,net in fetch(symbol):
            key=(today,symbol,broker)
            if key not in seen:
                new.append({"date":today,"symbol":symbol,"broker":broker,"buy_lots":buy,"sell_lots":sell,"net_lots":net})
    except Exception as e:
        print(symbol,e)
rows=existing+new
with OUT.open("w",newline="",encoding="utf-8") as f:
    w=csv.DictWriter(f,fieldnames=["date","symbol","broker","buy_lots","sell_lots","net_lots"])
    w.writeheader(); w.writerows(rows)
print(f"added {len(new)} rows")
