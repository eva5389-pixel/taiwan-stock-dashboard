import csv, re, requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from pathlib import Path

HEADERS={"User-Agent":"Mozilla/5.0"}
NAMES=["台灣摩根士丹利","摩根大通","美商高盛","美林","新加坡商瑞銀","花旗環球"]
CORE_WATCHLIST=["2330","2317","2454","2382","3231","2308","3017","2368","3189","2327","2344","2408","6770","3711","3037","6669","2376","2377","2357","3661",
                "2409","3481","2883","1314","6116","8150","2371","2881","2855","2882"]

def fetch_foreign_rank(limit=30, date=None):
    """取得 TWSE 指定日外資買超排行；同時保存候選日期、名稱、排名與淨買超。"""
    url="https://www.twse.com.tw/rwd/zh/fund/T86?selectType=ALL&response=json"
    if date: url+=f"&date={date.strftime('%Y%m%d')}"
    try:
        j=requests.get(url,headers=HEADERS,timeout=20).json()
        fields=j.get("fields",[]); data=j.get("data",[])
        code_i=fields.index("證券代號")
        name_i=fields.index("證券名稱")
        net_i=fields.index("外陸資買賣超股數(不含外資自營商)")
        ranked=[]
        for row in data:
            code=str(row[code_i]).strip()
            if not re.fullmatch(r"\d{4}",code): continue
            try: net=int(str(row[net_i]).replace(",","").replace("+",""))
            except Exception: continue
            if net>0: ranked.append((net,code,str(row[name_i]).strip()))
        ranked=sorted(ranked,reverse=True)[:int(limit)]
        raw_report_date=str(j.get("date","")).strip()
        try:
            report_date=datetime.strptime(raw_report_date,"%Y%m%d").date().isoformat()
        except ValueError:
            report_date=(date or datetime.now(tz).date()).isoformat()
        return [{"date":report_date,"symbol":code,"name":name,"rank":rank,
                 "foreign_net_lots":round(net/1000,3)}
                for rank,(net,code,name) in enumerate(ranked,1)]
    except Exception as e:
        print("TWSE top foreign ranking unavailable:",e)
        return []

OUT=Path("data/foreign_broker_history.csv")
OUT.parent.mkdir(exist_ok=True)
CANDIDATE_OUT=Path("data/foreign_candidate_history.csv")

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

BROKER_IDS={"台灣摩根士丹利":"1470","摩根大通":"8440","美商高盛":"1480","美林":"1440","新加坡商瑞銀":"1650","花旗環球":"1590"}

def fetch_broker_history(symbol, broker, broker_id):
    """抓單一分點歷史頁。公開頁日期格式為 YYYY/MM/DD。"""
    # 富邦 eBrokerDJ 自設區間：C=1, D=起日, E=迄日, ver=V3。
    # 往前抓 400 個日曆日，目標涵蓋約 240 個交易日。
    end=datetime.now(tz).date()
    start=end-timedelta(days=400)
    url=(f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco0/zco0.djhtm"
         f"?a={symbol}&BHID={broker_id}&b={broker_id}&C=1&D={start.isoformat()}&E={end.isoformat()}&ver=V3")
    r=requests.get(url,headers=HEADERS,timeout=20); r.raise_for_status()
    r.encoding=r.apparent_encoding
    txt=BeautifulSoup(r.text,"html.parser").get_text(" ",strip=True)
    # 日期 買進 賣出 買賣總額 買賣超
    pat=re.compile(r"(?<!\d)(20\d{2}/\d{2}/\d{2})\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+(-?[\d,]+)")
    rows=[]
    for ds,buy_s,sell_s,total_s,net_s in pat.findall(txt):
        try: dt=datetime.strptime(ds,"%Y/%m/%d").date().isoformat()
        except ValueError: continue
        buy=int(buy_s.replace(",","")); sell=int(sell_s.replace(",",""))
        net=int(net_s.replace(",",""))
        # 保守驗證，避免欄位錯位。
        if net != buy-sell: net=buy-sell
        rows.append({"date":dt,"symbol":symbol,"broker":broker,"buy_lots":buy,"sell_lots":sell,"net_lots":net})
    return rows


today=datetime.now(tz).date().isoformat()

# 每次排程回補最近5個有資料的交易日，讓昨日候選即使今天未入榜仍能繼續追蹤。
candidate_existing=[]
if CANDIDATE_OUT.exists():
    with CANDIDATE_OUT.open(encoding="utf-8") as f: candidate_existing=list(csv.DictReader(f))
candidate_seen={(r["date"],r["symbol"]) for r in candidate_existing}
candidate_new=[]
trading_days=[]
for offset in range(0,15):
    day=datetime.now(tz).date()-timedelta(days=offset)
    rows_for_day=fetch_foreign_rank(30,day)
    if not rows_for_day: continue
    report_date=rows_for_day[0]["date"]
    if report_date not in trading_days: trading_days.append(report_date)
    for row in rows_for_day:
        key=(row["date"],row["symbol"])
        if key not in candidate_seen:
            candidate_new.append(row); candidate_seen.add(key)
    if len(trading_days)>=5: break

candidate_rows=candidate_existing+candidate_new
with CANDIDATE_OUT.open("w",newline="",encoding="utf-8") as f:
    w=csv.DictWriter(f,fieldnames=["date","symbol","name","rank","foreign_net_lots"])
    w.writeheader(); w.writerows(candidate_rows)

recent_candidate_symbols=[r["symbol"] for r in candidate_rows if r["date"] in set(trading_days)]
WATCHLIST=sorted(set(CORE_WATCHLIST+recent_candidate_symbols))
existing=[]
if OUT.exists():
    with OUT.open(encoding="utf-8") as f: existing=list(csv.DictReader(f))
seen={(r["date"],r["symbol"],r["broker"]) for r in existing}
new=[]
existing_symbols={str(r.get("symbol","")).zfill(4) for r in existing}
existing_dates={}
for r in existing:
    sym=str(r.get("symbol","")).zfill(4)
    existing_dates.setdefault(sym,set()).add(r.get("date",""))

# 新加入的股票先回補近400日；已有至少20日資料者不必每天重抓完整歷史。
for symbol in WATCHLIST:
    if symbol in existing_symbols and len(existing_dates.get(symbol,set()))>=20:
        continue
    for broker,broker_id in BROKER_IDS.items():
        try:
            for row in fetch_broker_history(symbol,broker,broker_id):
                key=(row["date"],row["symbol"],row["broker"])
                if key not in seen:
                    new.append(row); seen.add(key)
        except Exception as e:
            print("history",symbol,broker,e)

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
print(f"tracking {len(WATCHLIST)} symbols; added {len(new)} broker rows; "
      f"added {len(candidate_new)} candidate rows; dynamic top30={len(set(WATCHLIST)-set(CORE_WATCHLIST))}")
