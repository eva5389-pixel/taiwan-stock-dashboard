import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
from io import StringIO
from bs4 import BeautifulSoup
from datetime import datetime

st.set_page_config(page_title="台股成本儀表板",page_icon="📊",layout="wide")
st.title("📊 台股成本・籌碼儀表板")
st.caption("輸入股票代號自動更新行情與可取得的分點資料；成本/訊號為研究估算，不構成投資建議。")

HEADERS={"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Safari/537.36"}

@st.cache_data(ttl=300)
def stock_data(symbol):
    # 直接使用 Yahoo Finance chart endpoint，避免 Streamlit Cloud 額外 yfinance 套件依賴
    last_err=None
    for suffix in [".TW",".TWO"]:
        ticker=symbol+suffix
        try:
            url=f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            params={"range":"6mo","interval":"1d","events":"history","includeAdjustedClose":"true"}
            res=requests.get(url,params=params,headers=HEADERS,timeout=15)
            res.raise_for_status()
            obj=res.json()["chart"]["result"]
            if not obj: continue
            x=obj[0]; q=x["indicators"]["quote"][0]
            h=pd.DataFrame({"Open":q.get("open"),"High":q.get("high"),"Low":q.get("low"),"Close":q.get("close"),"Volume":q.get("volume")},
                index=pd.to_datetime(x["timestamp"],unit="s"))
            h=h.dropna(subset=["Close"])
            if not h.empty:
                return ticker,h,float(h["Close"].iloc[-1]),None
        except Exception as e:
            last_err=str(e)
    return None,pd.DataFrame(),np.nan,last_err or "查無行情"

@st.cache_data(ttl=900)
def wantgoo_branch(symbol):
    # WantGoo 個股分點頁。公開 HTML 若因會員權限遮蔽數值，會回傳狀態而非假資料。
    urls=[
      f"https://www.wantgoo.com/stock/{symbol}/major-investors/branch-buysell",
      f"https://www.wantgoo.com/stock/etf/{symbol}/major-investors/branch-buysell"
    ]
    for url in urls:
        try:
            r=requests.get(url,headers=HEADERS,timeout=15); r.raise_for_status()
            tables=pd.read_html(StringIO(r.text))
            candidates=[]
            for x in tables:
                cols=" ".join(map(str,x.columns))
                if "券商" in cols and ("買" in cols or "賣" in cols):
                    candidates.append(x)
            if candidates:
                d=max(candidates,key=len).copy()
                d.columns=[str(c[-1] if isinstance(c,tuple) else c).strip() for c in d.columns]
                return d,url,("WantGoo 公開頁會遮蔽部分買賣張數；目前僅顯示公開可讀欄位。" if d.astype(str).apply(lambda c: c.str.contains(r"\\*\\*\\*",regex=True).any()).any() else None)
            if "登入" in r.text or "會員" in r.text:
                return pd.DataFrame(),url,"WantGoo 此個股完整分點數值需要會員登入，公開頁無法取得完整數字。"
        except Exception as e:last=str(e)
    return pd.DataFrame(),urls[0],locals().get("last","無法讀取 WantGoo")

@st.cache_data(ttl=900)
def fubon_stock_brokers(symbol, period=1):
    """富邦 eBrokerDJ / MoneyDJ 個股主力進出公開頁；純 BeautifulSoup，不依賴 lxml。"""
    # eBrokerDJ 個股主力頁可確認的期間頁面先限 1/5 日；不要把無效 suffix 回傳的預設頁誤標成 20/60 日。
    suffix={1:"",5:"_5"}.get(int(period))
    if suffix is None:
        return "", "", f"{period}日分點頁目前無法由公開來源可靠取得，避免把重複的單日資料誤當成{period}日。"
    url=f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco_{symbol}{suffix}.djhtm"
    try:
        r=requests.get(url,headers=HEADERS,timeout=15)
        r.raise_for_status()
        r.encoding=r.apparent_encoding
        txt=BeautifulSoup(r.text,"html.parser").get_text(" ",strip=True)
        return txt,url,None
    except Exception as e:
        return "",url,str(e)

def parse_fubon_brokers(text):
    names=["台灣摩根士丹利","摩根大通","美商高盛","美林","新加坡商瑞銀","花旗環球"]
    out=[]
    # MoneyDJ text sequence: broker buy sell net ratio. Capture signed/unsigned integer fields.
    for name in names:
        m=re.search(re.escape(name)+r"\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d.]+)%",text)
        if m:
            buy=int(m.group(1).replace(",","")); sell=int(m.group(2).replace(",","")); shown=int(m.group(3).replace(",",""))
            net=buy-sell
            out.append({"主要券商":name,"買進張數":buy,"賣出張數":sell,"淨買超":net,"成交占比%":float(m.group(4)),
                        "隔日沖判斷":daytrade_flag(buy,sell,net)})
        else:
            out.append({"主要券商":name,"買進張數":np.nan,"賣出張數":np.nan,"淨買超":np.nan,"成交占比%":np.nan,"隔日沖判斷":"本期未進榜"})
    return pd.DataFrame(out)

@st.cache_data(ttl=300)
def foreign_history(symbol):
    """讀取 GitHub Actions 每日累積的六大外資分點歷史。"""
    url="https://raw.githubusercontent.com/eva5389-pixel/taiwan-stock-dashboard/main/data/foreign_broker_history.csv"
    try:
        d=pd.read_csv(url)
        if d.empty: return pd.DataFrame(),url,None
        d["symbol"]=d["symbol"].astype(str).str.replace(".0","",regex=False).str.zfill(4)
        d=d[d["symbol"]==str(symbol).zfill(4)].copy()
        d["date"]=pd.to_datetime(d["date"],errors="coerce")
        return d.sort_values("date"),url,None
    except Exception as e: return pd.DataFrame(),url,str(e)

def remaining_inventory_cost(history,h,days):
    """用逐日、逐分點買賣，以移動平均法推估可追蹤的剩餘庫存與成本。"""
    empty=pd.DataFrame(columns=["分點","推估剩餘庫存張數","推估持倉成本","現價損益%","超出可追蹤庫存賣出","資料狀態"])
    if history.empty or h.empty: return np.nan,0,0,0,empty
    dates=history[["date"]].dropna().drop_duplicates().sort_values("date").tail(int(days))["date"]
    if dates.empty: return np.nan,0,0,0,empty
    raw=history[history["date"].isin(dates)].copy()
    px=h.copy().reset_index()
    px=px.rename(columns={px.columns[0]:"date"})
    px["date"]=pd.to_datetime(px["date"],errors="coerce").dt.normalize()
    px["est_price"]=(px["High"]+px["Low"]+px["Close"])/3
    raw["date"]=pd.to_datetime(raw["date"],errors="coerce").dt.normalize()
    m=raw.merge(px[["date","est_price"]],on="date",how="inner")
    m=m.dropna(subset=["date","est_price"]).sort_values(["broker","date"])
    if m.empty: return np.nan,0,0,0,empty

    rows=[]
    for broker,g in m.groupby("broker",sort=False):
        inventory=0.0
        average_cost=np.nan
        unknown_sell=0.0
        for _,r in g.iterrows():
            buy=max(float(r.get("buy_lots",0) or 0),0)
            sell=max(float(r.get("sell_lots",0) or 0),0)
            price=float(r["est_price"])
            if buy>0:
                total_cost=(inventory*(average_cost if pd.notna(average_cost) else 0))+(buy*price)
                inventory+=buy
                average_cost=total_cost/inventory if inventory>0 else np.nan
            if sell>0:
                if sell>=inventory:
                    unknown_sell+=max(sell-inventory,0)
                    inventory=0.0
                    average_cost=np.nan
                else:
                    inventory-=sell
        status="可追蹤庫存" if inventory>0 else ("期初庫存不明／區間淨賣出" if unknown_sell>0 else "無可追蹤庫存")
        rows.append({"分點":broker,"推估剩餘庫存張數":inventory,
                     "推估持倉成本":average_cost,
                     "現價損益%":np.nan,
                     "超出可追蹤庫存賣出":unknown_sell,
                     "資料狀態":status})
    detail=pd.DataFrame(rows)
    current=float(h["Close"].dropna().iloc[-1]) if h["Close"].notna().any() else np.nan
    valid=detail[(detail["推估剩餘庫存張數"]>0)&detail["推估持倉成本"].notna()].copy()
    if not valid.empty and pd.notna(current):
        detail.loc[valid.index,"現價損益%"]=(current/detail.loc[valid.index,"推估持倉成本"]-1)*100
    tracked=float(valid["推估剩餘庫存張數"].sum()) if not valid.empty else 0.0
    composite=float(np.average(valid["推估持倉成本"],weights=valid["推估剩餘庫存張數"])) if tracked>0 else np.nan
    unknown=float(detail["超出可追蹤庫存賣出"].sum()) if not detail.empty else 0.0
    used=int(m["date"].nunique())
    return composite,tracked,unknown,used,detail

def market_costs(h):
    out={}
    for n in [5,10,20,60]:
        d=h.tail(n)
        out[n]=np.average(d["Close"],weights=d["Volume"]) if len(d) and d["Volume"].sum()>0 else np.nan
    return out

@st.cache_data(ttl=900)
def fubon_period_summary(symbol, suffix):
    """讀取 eBrokerDJ 公開期間主力頁的合計買/賣超與平均成本。"""
    url=f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco_{symbol}_{suffix}.djhtm"
    try:
        r=requests.get(url,headers=HEADERS,timeout=15); r.raise_for_status()
        r.encoding=r.apparent_encoding
        txt=BeautifulSoup(r.text,"html.parser").get_text(" ",strip=True)
        mb=re.search(r"平均買超成本\s*([\d,]+(?:\.\d+)?)",txt)
        ms=re.search(r"平均賣超成本\s*([\d,]+(?:\.\d+)?)",txt)
        buy=float(mb.group(1).replace(",","")) if mb else np.nan
        sell=float(ms.group(1).replace(",","")) if ms else np.nan
        return buy,sell,url,None
    except Exception as e:
        return np.nan,np.nan,url,str(e)

def parse_rank_average_cost(text, label, current_price=np.nan):
    """解析公開排行平均成本；異常值直接視為無可靠資料，避免把排名數字誤認成股價。"""
    if not text: return np.nan
    m=re.search(re.escape(label)+r"[^0-9]{0,20}([0-9]+(?:\\.[0-9]+)?)",text)
    if not m: return np.nan
    try: v=float(m.group(1))
    except: return np.nan
    if pd.notna(current_price) and current_price>0:
        # 排行平均成交成本不應與當期股價差數個數量級；寬鬆保留 20%~500% 區間。
        if v < current_price*0.20 or v > current_price*5: return np.nan
    return v

def foreign_broker_cost_estimate(h, broker_df, days):
    """用區間分點買賣張數 + 日線價格估計各外資分點的累積持倉成本。"""
    if h.empty or broker_df.empty: return broker_df.copy(), np.nan
    d=h.tail(int(days)).copy()
    if d.empty: return broker_df.copy(), np.nan
    typical=(d["High"]+d["Low"]+d["Close"])/3 if all(c in d.columns for c in ["High","Low","Close"]) else d["Close"]
    market_px=np.average(typical,weights=d["Volume"]) if d["Volume"].sum()>0 else float(typical.mean())
    out=broker_df.copy()
    # 沒有逐筆分點成交價時，以區間市場成交重心為基準；依各分點淨買賣強度做小幅價格重心調整，
    # 讓各分點估值可比較，但仍明確標示為模型估算而非真實庫存成本。
    total=(out["買進張數"].fillna(0)+out["賣出張數"].fillna(0)).replace(0,np.nan)
    pressure=(out["淨買超"].fillna(0)/total).clip(-1,1).fillna(0)
    daily_range=((d["High"]-d["Low"])/d["Close"]).replace([np.inf,-np.inf],np.nan).mean() if all(c in d.columns for c in ["High","Low","Close"]) else 0
    adj=(daily_range if pd.notna(daily_range) else 0)*0.25
    out["分點估算買進成本"]=np.where(out["買進張數"].fillna(0)>0,market_px*(1+pressure*adj),np.nan)
    out["估算持倉張數"]=out["淨買超"].clip(lower=0)
    weights=out["估算持倉張數"].fillna(0)
    composite=np.average(out.loc[weights>0,"分點估算買進成本"],weights=weights[weights>0]) if (weights>0).any() else market_px
    return out,composite

def find_col(cols,keys):
    for c in cols:
        if any(k in str(c) for k in keys): return c
    return None

def clean_num(s):
    return pd.to_numeric(s.astype(str).str.replace(",","",regex=False).str.replace("*","",regex=False),errors="coerce")


def daytrade_flag(buy,sell,net):
    """僅以當期分點買賣結構判斷疑似隔日沖，不宣稱實際交易策略。"""
    if pd.isna(buy) or pd.isna(sell): return "資料不足"
    total=buy+sell
    if total<=0: return "—"
    turnover=min(buy,sell)/max(buy,sell) if max(buy,sell)>0 else 0
    net_ratio=abs(net)/total
    if total>=100 and turnover>=0.80 and net_ratio<=0.10:
        return "🔴 高疑似隔日沖"
    if total>=50 and turnover>=0.60 and net_ratio<=0.25:
        return "🟠 疑似短線/隔日沖"
    return "⚪ 未見明顯隔日沖特徵"

@st.cache_data(ttl=300)
def taiex_spot():
    url="https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX"
    try:
        r=requests.get(url,headers=HEADERS,timeout=15); r.raise_for_status()
        d=r.json()
        # Search all returned rows for TAIEX/發行量加權股價指數 and a plausible index value.
        for row in d:
            txt=" ".join(map(str,row.values()))
            if "發行量加權股價指數" in txt or "TAIEX" in txt:
                nums=[]
                for v in row.values():
                    try: nums.append(float(str(v).replace(",","")))
                    except: pass
                nums=[x for x in nums if x>1000]
                if nums: return nums[-1],url,None
        return np.nan,url,"找不到加權指數欄位"
    except Exception as e: return np.nan,url,str(e)

@st.cache_data(ttl=900)
def taifex_options():
    """TXO 行情：先試 OpenAPI，若非 JSON 則改抓 TAIFEX 官方每日行情 HTML。"""
    api="https://openapi.taifex.com.tw/v1/DailyMarketReportOfOptions"
    try:
        r=requests.get(api,headers=HEADERS,timeout=20)
        if r.ok and "json" in r.headers.get("content-type","").lower() and r.text.strip():
            obj=r.json()
            if obj: return pd.DataFrame(obj),api,None
    except Exception:
        pass
    url="https://www.taifex.com.tw/enl/eng3/optDailyMarketReport"
    try:
        r=requests.get(url,headers=HEADERS,timeout=20); r.raise_for_status()
        soup=BeautifulSoup(r.text,"html.parser")
        rows=[]
        for tr in soup.select("table tr"):
            cells=[c.get_text(" ",strip=True) for c in tr.select("th,td")]
            if len(cells)>=10 and cells[0]=="TXO" and cells[4] in ["Call","Put"]:
                rows.append(cells[:18])
        if rows:
            names=["Contract","Contract Month","Contract Date","Strike Price","Call/Put","Open","High","Low","Last Traded Price","Settlement Price","Change","Change%","Volume","Open Interest","Best Bid","Best Ask","Historical High","Historical Low"]
            width=min(len(names),min(map(len,rows)))
            return pd.DataFrame([x[:width] for x in rows],columns=names[:width]),url,None
        return pd.DataFrame(),url,"官方頁面目前沒有可解析的 TXO 行情列"
    except Exception as e:
        return pd.DataFrame(),url,str(e)

def option_value_split(spot,strike,premium,cp):
    intrinsic=max(spot-strike,0) if str(cp).upper().startswith(("C","買權")) else max(strike-spot,0)
    time_value=max(premium-intrinsic,0)
    return intrinsic,time_value

@st.cache_data(ttl=900)
def twse_foreign_buy_rank():
    """TWSE 三大法人 T86（rwd 報表 API）：上市個股外陸資買進／賣出／淨買賣。"""
    url="https://www.twse.com.tw/rwd/zh/fund/T86?selectType=ALL&response=json"
    try:
        r=requests.get(url,headers=HEADERS,timeout=20); r.raise_for_status()
        j=r.json()
        fields=j.get("fields",[])
        data=j.get("data",[])
        if not fields or not data:
            return pd.DataFrame(),url,"TWSE T86 回傳無 fields/data："+str(j.get("stat",""))
        d=pd.DataFrame(data,columns=fields)
        code="證券代號"; name="證券名稱"
        buy="外陸資買進股數(不含外資自營商)"
        sell="外陸資賣出股數(不含外資自營商)"
        net="外陸資買賣超股數(不含外資自營商)"
        missing=[c for c in [code,name,buy,sell,net] if c not in d.columns]
        if missing:
            return pd.DataFrame(),url,"TWSE T86 缺欄位："+",".join(missing)+"｜實際欄位："+"｜".join(map(str,d.columns))
        out=pd.DataFrame({"代號":d[code].astype(str).str.strip(),"名稱":d[name].astype(str).str.strip()})
        def num(c):
            return pd.to_numeric(d[c].astype(str).str.replace(",","",regex=False).str.replace("+","",regex=False),errors="coerce")
        out["外資買進張數"]=num(buy)/1000
        out["外資賣出張數"]=num(sell)/1000
        out["外資買超張數"]=num(net)/1000
        out=out[out["代號"].str.fullmatch(r"\d{4}",na=False)].copy()
        return out.sort_values("外資買超張數",ascending=False),url,None
    except Exception as e:
        return pd.DataFrame(),url,"TWSE T86錯誤："+repr(e)

@st.cache_data(ttl=3600)
def taifex_stock_futures_map():
    """TAIFEX 官方股票期貨標的表：用標的證券代號確認是否有股票期貨。"""
    url="https://www.taifex.com.tw/cht/2/stockLists"
    try:
        tables=pd.read_html(url)
        rows=[]
        for t in tables:
            if t.empty: continue
            for _,r in t.iterrows():
                vals=[str(x).strip() for x in r.tolist()]
                code=next((x for x in vals if re.fullmatch(r"\d{4,6}",x)),None)
                prod=next((x for x in vals if re.fullmatch(r"[A-Z]{2}",x)),None)
                if code and prod:
                    name=""
                    try:
                        idx=vals.index(code)
                        if idx+1<len(vals): name=vals[idx+1]
                    except Exception: pass
                    unit=next((x.replace(",","") for x in vals if re.fullmatch(r"[\d,]+",x) and x!=code and int(x.replace(",","")) in [100,2000,4000,8000,10000]),"")
                    rows.append({"symbol":code,"product_code":prod,"name":name,"contract_unit":unit})
        d=pd.DataFrame(rows).drop_duplicates(["symbol","product_code"]) if rows else pd.DataFrame()
        return d,url,None
    except Exception as e:
        return pd.DataFrame(),url,str(e)

@st.cache_data(ttl=900)
def taifex_institutional():
    url="https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate"
    try:
        r=requests.get(url,headers=HEADERS,timeout=20); r.raise_for_status()
        d=pd.DataFrame(r.json())
        return d,url,None
    except Exception as e: return pd.DataFrame(),url,str(e)

@st.cache_data(ttl=900)
def twse_material(symbol):
    urls=[
      "https://openapi.twse.com.tw/v1/opendata/t187ap04_L",
      "https://openapi.twse.com.tw/v1/opendata/t187ap04_O"
    ]
    frames=[]; errs=[]
    for url in urls:
        try:
            r=requests.get(url,headers=HEADERS,timeout=20); r.raise_for_status()
            x=pd.DataFrame(r.json())
            if not x.empty:
                codecol=next((c for c in x.columns if "公司代號" in str(c)),None)
                if codecol: x=x[x[codecol].astype(str).str.strip()==str(symbol)]
                if not x.empty: frames.append(x)
        except Exception as e: errs.append(str(e))
    return (pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()),urls[0],"; ".join(errs) if errs and not frames else None

def numeric_col(df, words):
    for c in df.columns:
        if all(w in str(c) for w in words):
            return c
    return None

@st.cache_data(ttl=1800)
def pelosi_public():
    url="https://nancypelosistocktracker.org/zh-TW"
    try:
        r=requests.get(url,headers=HEADERS,timeout=20); r.raise_for_status()
        soup=BeautifulSoup(r.text,"html.parser")
        rows=[]
        for tr in soup.select("table tr"):
            cells=[x.get_text(" ",strip=True) for x in tr.select("th,td")]
            if len(cells)>=3: rows.append(cells)
        if len(rows)>1:
            width=max(map(len,rows)); rows=[x+[""]*(width-len(x)) for x in rows]
            return pd.DataFrame(rows[1:],columns=rows[0]),url,None
        return pd.DataFrame(),url,"公開追蹤頁為動態載入，伺服器 HTML 沒有交易表格。"
    except Exception as e: return pd.DataFrame(),url,str(e)

with st.sidebar:
    symbol=st.text_input("台股代號","3189").strip()
    run=st.button("🔎 查詢 / 更新",type="primary",use_container_width=True)
    st.caption("行情快取 5 分鐘；分點快取 15 分鐘。")

ticker,h,current,price_err=stock_data(symbol)
branch=pd.DataFrame()
branch_url=f"https://www.wantgoo.com/stock/etf/{symbol}/major-investors/branch-buysell"
branch_err="WantGoo 僅提供瀏覽器登入後查閱；Streamlit 不直接爬取登入資料。"

costs=market_costs(h) if not h.empty else {}

# 股票名稱供個股期貨等分頁共用；避免只在其他分頁的區域變數中取得。
stock_name=""
try:
    rr=requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",headers=HEADERS,timeout=15)
    if rr.ok:
        dd=pd.DataFrame(rr.json())
        cc=next((c for c in dd.columns if "Code" in str(c) or "證券代號" in str(c)),None)
        nn=next((c for c in dd.columns if "Name" in str(c) or "證券名稱" in str(c)),None)
        if cc and nn:
            hit=dd[dd[cc].astype(str).str.strip()==symbol]
            if not hit.empty: stock_name=str(hit.iloc[0][nn]).strip()
except Exception:
    pass

tabs=st.tabs(["🏠 總覽","🏦 分點成本","🌍 外資追蹤","🏆 外資分點排行","📈 個股期貨","📊 大盤期貨","📢 重大訊息"])
with tabs[0]:
    st.subheader(f"{symbol} 自動更新總覽")
    if not h.empty:
        prev=float(h["Close"].iloc[-2]) if len(h)>1 else current
        c1,c2,c3=st.columns(3)
        c1.metric("最新收盤",f"{current:,.2f}",f"{current-prev:,.2f}")
        c2.metric("資料日期",str(h.index[-1].date()))
        c3.metric("Yahoo 代號",ticker)
        # 日 K 線：使用 Altair（Streamlit 內建支援），避免 Plotly 前端動態模組載入失敗。
        k=h.reset_index().copy()
        date_col=k.columns[0]
        k[date_col]=pd.to_datetime(k[date_col],errors="coerce")
        for col in ["Open","High","Low","Close","Volume"]:
            if col in k.columns: k[col]=pd.to_numeric(k[col],errors="coerce")
        required=[date_col,"Open","High","Low","Close"]
        if all(c in k.columns for c in required):
            k=k.dropna(subset=required).sort_values(date_col)
        else:
            k=pd.DataFrame()
        if not k.empty:
            import altair as alt
            base=alt.Chart(k).encode(x=alt.X(f"{date_col}:T",title=None,axis=alt.Axis(format="%m/%d")))
            rule=base.mark_rule().encode(y=alt.Y("Low:Q",title="價格"),y2="High:Q")
            body=base.mark_bar(size=6).encode(
                y=alt.Y("Open:Q",title="價格"),y2="Close:Q",
                color=alt.condition("datum.Close >= datum.Open",alt.value("#ef5350"),alt.value("#26a69a")),
                tooltip=[alt.Tooltip(f"{date_col}:T",title="日期"),alt.Tooltip("Open:Q",title="開"),alt.Tooltip("High:Q",title="高"),alt.Tooltip("Low:Q",title="低"),alt.Tooltip("Close:Q",title="收"),alt.Tooltip("Volume:Q",title="量",format=",.0f")]
            )
            chart=(rule+body).properties(height=520,title="日 K 線").interactive()
            st.altair_chart(chart,use_container_width=True)
        else:
            st.warning("目前沒有足夠的 OHLC 行情資料可以繪製 K 線。")
        st.markdown("### 六大外資分點推估剩餘持倉成本")
        hist,hist_url,hist_err=foreign_history(symbol)

        rows=[]
        detail_by_period={}
        for n in [1,5,20,30,60,120,240]:
            if not hist.empty:
                hd=hist.sort_values("date").groupby("date",as_index=False)[["buy_lots","sell_lots","net_lots"]].sum().tail(n)
                buy=float(hd["buy_lots"].sum()) if not hd.empty else np.nan
                sell=float(hd["sell_lots"].sum()) if not hd.empty else np.nan
                net=float(hd["net_lots"].sum()) if not hd.empty else np.nan
                cost,inventory,unknown,used,detail=remaining_inventory_cost(hist,h,n)
                detail_by_period[n]=detail
            else:
                buy=sell=net=cost=np.nan
                inventory=unknown=used=0
                detail_by_period[n]=pd.DataFrame()
            rows.append({"期間":f"{n}日","六大外資買進張數":buy,"六大外資賣出張數":sell,
                         "六大外資淨買賣":net,"推估剩餘庫存張數":inventory,
                         "推估剩餘持倉成本":cost,
                         "現價距推估成本%":((current/cost-1)*100 if pd.notna(cost) and cost else np.nan),
                         "期初庫存不明賣出張數":unknown,
                         "可配對交易日":used})

        fc=pd.DataFrame(rows)
        st.dataframe(fc,use_container_width=True,hide_index=True,
            column_config={
                "推估剩餘持倉成本":st.column_config.NumberColumn(format="%.2f"),
                "現價距推估成本%":st.column_config.NumberColumn(format="%.2f%%"),
                "六大外資買進張數":st.column_config.NumberColumn(format="%.0f"),
                "六大外資賣出張數":st.column_config.NumberColumn(format="%.0f"),
                "六大外資淨買賣":st.column_config.NumberColumn(format="%.0f"),
                "推估剩餘庫存張數":st.column_config.NumberColumn(format="%.0f"),
                "期初庫存不明賣出張數":st.column_config.NumberColumn(format="%.0f"),
            })

        available_days=int(hist["date"].dt.date.nunique()) if not hist.empty else 0
        detail_period=min(20,available_days) if available_days else 0
        if detail_period and not detail_by_period.get(detail_period,pd.DataFrame()).empty:
            st.markdown(f"#### 各分點明細（最近 {detail_period} 個可用交易日）")
            st.dataframe(detail_by_period[detail_period],use_container_width=True,hide_index=True,
                column_config={
                    "推估剩餘庫存張數":st.column_config.NumberColumn(format="%.0f"),
                    "推估持倉成本":st.column_config.NumberColumn(format="%.2f"),
                    "現價損益%":st.column_config.NumberColumn(format="%.2f%%"),
                    "超出可追蹤庫存賣出":st.column_config.NumberColumn(format="%.0f"),
                })

        if not hist.empty:
            st.caption(f"每日歷史目前可用 {available_days} 個交易日。成本採逐日、逐分點移動平均法：買進增加庫存，賣出按當時平均成本扣除庫存。")
        else:
            st.caption("尚無逐日分點歷史，因此不顯示持倉成本。")
        st.caption("「推估剩餘持倉成本」不是券商實際帳簿成本；若賣出超過本系統可追蹤庫存，會列入「期初庫存不明賣出張數」，不以假資料補成本。")

        # 依成本、量價與外資流向產生「條件式」交易觀察，不把推估成本視為精確實際持倉成本。
        st.markdown("### 🧭 外資推估持倉成本交易策略觀察")
        valid_fc=fc.dropna(subset=["推估剩餘持倉成本"]).copy()
        if not valid_fc.empty:
            pref=valid_fc[valid_fc["期間"].isin(["20日","60日","120日","240日"])]
            base=pref.iloc[-1] if not pref.empty else valid_fc.iloc[-1]
            cost=float(base["推估剩餘持倉成本"])
            period=str(base["期間"])
            net=float(base["六大外資淨買賣"]) if pd.notna(base["六大外資淨買賣"]) else 0.0
            dist=(current/cost-1)*100 if cost else np.nan
            vol=float(h["Volume"].iloc[-1]) if len(h) else np.nan
            avg20=float(h["Volume"].tail(20).mean()) if len(h) else np.nan
            vol_ratio=(vol/avg20) if pd.notna(vol) and pd.notna(avg20) and avg20>0 else np.nan
            ma20=float(h["Close"].tail(20).mean()) if len(h)>=5 else np.nan
            high20=float(h["High"].tail(20).iloc[:-1].max()) if len(h)>1 else np.nan

            c1,c2,c3=st.columns(3)
            c1.metric(f"{period}外資推估持倉成本",f"{cost:,.2f}")
            c2.metric("現價距成本",f"{dist:+.2f}%")
            c3.metric("今日量 / 20日均量",f"{vol_ratio:.2f}x" if pd.notna(vol_ratio) else "—")

            signals=[]
            if -3 <= dist <= 3 and net>0 and (pd.isna(vol_ratio) or vol_ratio<1.0):
                signals.append(("🟢 成本防守觀察","股價位於外資推估持倉成本 ±3% 內、區間外資仍偏買方，且量能未明顯放大；可觀察成本帶是否形成支撐。"))
            if pd.notna(vol_ratio) and vol_ratio>=1.5 and current>cost and (pd.isna(high20) or current>=high20):
                signals.append(("🟢 突破／動能觀察","現價高於外資推估持倉成本且量能達20日均量1.5倍以上；若同時突破近期高點，可視為量價與成本方向共振。"))
            if net>0 and abs(dist)<=8 and (pd.isna(ma20) or current>=ma20):
                signals.append(("🟡 籌碼累積觀察","區間六大外資為淨買方，股價仍接近成本帶；可持續觀察是否出現橫盤吸收賣壓。"))
            if dist<=-3 and net<0:
                signals.append(("🔴 成本失守風險","現價已低於外資推估持倉成本3%以上，且區間外資為淨賣方；成本帶目前不宜直接視為有效支撐。"))
            if not signals:
                signals.append(("⚪ 等待確認","目前成本、量價與外資流向沒有形成明確共振，先觀察成本帶、20日均線與量能變化。"))
            for title,msg in signals:
                st.markdown(f"**{title}**  \\n{msg}")

            stop3=cost*0.97; stop5=cost*0.95
            st.caption(f"風險觀察帶：成本下方3% 約 {stop3:,.2f}；下方5% 約 {stop5:,.2f}。這是風險參考區，不是自動停損指令。")
            st.caption("判讀限制：分點不等於單一外資最終持倉；轉倉、對敲、隔日沖與跨分點交易都可能讓估算成本失真，需搭配量價、均線與後續分點流向。")
        else:
            st.info("目前尚無足夠的六大外資成本資料可產生策略觀察。")

    else: st.error("行情取得失敗："+str(price_err))

with tabs[1]:
    st.subheader("主要券商成本／籌碼")
    period=st.segmented_control("期間",[1,5],default=5,format_func=lambda x:f"{x}日",key="broker_period")
    text_data,fubon_url,fubon_err=fubon_stock_brokers(symbol,period)
    if text_data:
        broker_df=parse_fubon_brokers(text_data)
        st.dataframe(broker_df,use_container_width=True,hide_index=True)
        chart_df=broker_df.dropna(subset=["淨買超"]).set_index("主要券商")
        if not chart_df.empty:
            st.markdown("#### 六大外資券商淨買賣超")
            st.bar_chart(chart_df["淨買超"],horizontal=True)
            st.markdown("#### 買進 vs 賣出")
            st.bar_chart(chart_df[["買進張數","賣出張數"]],horizontal=True)
        st.caption("此公開頁提供各券商買進、賣出、淨買賣超與成交占比；公開頁的排行平均成本不是六大外資成本，因此不顯示，避免把排行序號誤認成股價。")
        st.info("六大外資成本請以上方「六大外資分點進出成本」的流量加權估算為準。")
    else:
        st.warning("富邦個股分點資料讀取失敗："+str(fubon_err))
    st.link_button("富邦 eBrokerDJ 個股分點原始頁",fubon_url)
    st.link_button("WantGoo 此股分點頁（登入後交叉查看）",branch_url)

with tabs[2]:
    st.subheader("🔥 最近外資買超股票")
    st.caption("先看全市場最近一個交易日外資買進哪些上市股票，再往下看目前輸入股票的六大外資分點。買超代表資金流向，不等同外資一定會拉抬股價。")
    fr,fr_url,fr_err=twse_foreign_buy_rank()
    ferr=fr_err  # 與外資分點排行共用診斷訊息
    if not fr.empty:
        topn=st.slider("顯示外資買超前幾名",5,30,15,5,key="foreign_rank_n")
        show=fr.head(topn).copy()
        show["外資買超張數"]=show["外資買超張數"].round(0)
        st.dataframe(show[["代號","名稱","外資買超張數"]],use_container_width=True,hide_index=True)
        st.markdown("#### 外資買超排行")
        st.bar_chart(show.set_index("名稱")["外資買超張數"],horizontal=True)
        st.caption("資料來源：臺灣證券交易所最新三大法人日報；目前先顯示上市股票單日排行。下一階段可累積每日資料後增加 3／5／10／20 日連續買超與價格轉強篩選。")
    else:
        st.warning("TWSE 外資買超排行暫時無法取得："+str(fr_err))

    st.divider()
    st.subheader("目前股票：六大外資券商追蹤")
    st.caption("自動追蹤摩根士丹利、摩根大通、美林、高盛、瑞銀、花旗環球。")
    foreign_period=st.segmented_control("外資期間",[1,5],default=5,format_func=lambda x:f"{x}日",key="foreign_period")
    ft,fu,fe=fubon_stock_brokers(symbol,foreign_period)
    if ft:
        fd=parse_fubon_brokers(ft)
        st.dataframe(fd,use_container_width=True,hide_index=True)
        st.caption("此頁只保留六大外資各分點的買進／賣出／淨買賣與隔日沖觀察；不再顯示個別外資成本。")
        fchart=fd.dropna(subset=["淨買超"]).set_index("主要券商")
        if not fchart.empty:
            st.bar_chart(fchart["淨買超"],horizontal=True)
        valid=fd["淨買超"].dropna()
        if len(valid):
            st.metric(f"六大外資分點合計淨買賣（近{foreign_period}日累計）",f"{valid.sum():,.0f} 張")

        # 最近5個交易日六大外資分點淨買賣變化
        ph,_,_=foreign_history(symbol)
        if not ph.empty:
            ph["date"]=pd.to_datetime(ph["date"],errors="coerce")
            market_dates=set(pd.to_datetime(h.index,errors="coerce").normalize()) if not h.empty else set()
            ph["_d"]=ph["date"].dt.normalize()
            if market_dates:
                ph=ph[ph["_d"].isin(market_dates)].copy()
            daily5=(ph.groupby("_d",as_index=False)["net_lots"].sum()
                      .sort_values("_d").tail(5))
            if not daily5.empty:
                daily5["日期"]=pd.to_datetime(daily5["_d"]).dt.strftime("%m/%d")
                daily5["六大外資分點淨買賣"]=pd.to_numeric(daily5["net_lots"],errors="coerce")
                st.markdown("#### 📊 外資買賣超近5日變化")
                st.caption("顯示六大外資券商分點每日合計淨買賣；正值為淨買超、負值為淨賣超。")
                st.bar_chart(daily5.set_index("日期")["六大外資分點淨買賣"],use_container_width=True)
                st.dataframe(daily5[["日期","六大外資分點淨買賣"]],use_container_width=True,hide_index=True)
    else:
        st.warning("外資分點資料讀取失敗："+str(fe))
    st.link_button("查看資料原頁",fu)

with tabs[3]:
    st.subheader("🏦 六大外資分點買賣排行榜")
    st.caption("用已累積的券商分點明細，把六大外資分點在各股票的買進／賣出／淨買賣加總排行；分點流向不等同 TWSE 投資人身分分類。")
    try:
        rr=requests.get("https://raw.githubusercontent.com/eva5389-pixel/taiwan-stock-dashboard/main/data/foreign_broker_history.csv",headers=HEADERS,timeout=15)
        rr.raise_for_status()
        from io import StringIO
        ah=pd.read_csv(StringIO(rr.text))
        ah["date"]=pd.to_datetime(ah["date"],errors="coerce")
        for c in ["buy_lots","sell_lots","net_lots"]: ah[c]=pd.to_numeric(ah[c],errors="coerce")
        ah=ah.dropna(subset=["date","symbol"])
        branch_err=None
    except Exception as e:
        ah=pd.DataFrame(); branch_err=str(e)
    
    if not ah.empty:
        rank_days=st.segmented_control("分點排行期間",[1,5,20,60],default=5,format_func=lambda x:f"{x}日",key="branch_rank_days")
        dates=sorted(ah["date"].dropna().dt.normalize().unique())
        chosen=dates[-int(rank_days):] if dates else []
        rh=ah[ah["date"].dt.normalize().isin(chosen)].copy()
        rg=rh.groupby("symbol",as_index=False)[["buy_lots","sell_lots","net_lots"]].sum()
        rg["symbol"]=rg["symbol"].astype(str).str.replace(".0","",regex=False).str.zfill(4)
        cost_rows=[]
        for sym in rg["symbol"].tolist():
            sh=ah[ah["symbol"].astype(str).str.replace(".0","",regex=False).str.zfill(4)==sym].copy()
            try:
                _ticker,yh,_last,_err=stock_data(sym)
            except Exception:
                yh=pd.DataFrame()
            cost30,used30=flow_weighted_cost(sh,yh,30) if not sh.empty and not yh.empty else (np.nan,0)
            cost_rows.append({"symbol":sym,"分點30日估算成本":cost30,"分點成本實際日數":used30})
        rg=rg.merge(pd.DataFrame(cost_rows),on="symbol",how="left")
        rg=rg.sort_values("net_lots",ascending=False)
        theme_map={"2330":"AI／先進製程／半導體","2317":"AI伺服器／電子代工","2454":"IC設計／AI邊緣運算","2382":"AI伺服器／電子代工","3231":"AI伺服器／電子代工","2308":"電源／AI伺服器","3017":"散熱／AI伺服器","2368":"PCB／AI伺服器","3189":"PCB／AI伺服器","2327":"被動元件／AI伺服器","2344":"記憶體","2408":"記憶體","6770":"記憶體／IC設計","3711":"封測／半導體","3037":"PCB／載板","6669":"散熱／伺服器","2376":"AI伺服器／板卡","2377":"AI伺服器／主機板","2357":"AI PC／伺服器","3661":"高速傳輸IC／半導體"}
        rg["題材"]=rg["symbol"].map(theme_map).fillna("—")
        official=fr[["代號","名稱","外資買進張數","外資賣出張數","外資買超張數"]].copy() if not fr.empty else pd.DataFrame()
        if fr.empty and ferr:
            st.warning("TWSE官方外資資料未載入："+str(ferr))
        if not official.empty:
            official["代號"]=official["代號"].astype(str).str.zfill(4)
            rg["代號"]=rg["symbol"]
            rg=rg.merge(official,on="代號",how="left")
        else:
            rg["代號"]=rg["symbol"]; rg["名稱"]=""; rg["外資買進張數"]=np.nan; rg["外資賣出張數"]=np.nan; rg["外資買超張數"]=np.nan
        # 排行未進 TWSE 當日榜時，仍以固定追蹤清單補齊股票名稱。
        name_map={"2330":"台積電","2317":"鴻海","2454":"聯發科","2382":"廣達","3231":"緯創","2308":"台達電","3017":"奇鋐","2368":"金像電","3189":"景碩","2327":"國巨","2344":"華邦電","2408":"南亞科","6770":"力積電","3711":"日月光投控","3037":"欣興","6669":"緯穎","2376":"技嘉","2377":"微星","2357":"華碩","3661":"世芯-KY"}
        rg["名稱"]=rg["名稱"].replace("",np.nan).fillna(rg["symbol"].map(name_map)).fillna("—")
        def sync_label(r):
            a=r.get("外資買超張數",np.nan); b=r.get("net_lots",np.nan)
            if pd.isna(a) or pd.isna(b): return "⚪ 資料不足"
            if a>0 and b>0: return "🟢 法人／分點同步買超"
            if a<0 and b<0: return "🔴 法人／分點同步賣超"
            return "🟡 法人／分點分歧"
        rg["籌碼訊號"]=rg.apply(sync_label,axis=1)
        rg=rg.rename(columns={"symbol":"代號","buy_lots":"六大分點買進張數","sell_lots":"六大分點賣出張數","net_lots":"六大分點淨買賣"})
        top_branch=st.slider("顯示分點排行前幾名",5,30,15,5,key="branch_rank_n")
        cols=["代號","名稱","題材","六大分點買進張數","六大分點賣出張數","六大分點淨買賣","分點30日估算成本","外資買進張數","外資賣出張數","外資買超張數","籌碼訊號"]
        display_rg=rg.head(top_branch)[cols].copy()
        # 全部轉成顯示字串，徹底避開 Streamlit Cloud / PyArrow 對 nullable dtype 的轉換差異。
        for c in ["代號","名稱","題材","籌碼訊號"]:
            display_rg[c]=display_rg[c].fillna("—").astype(str)
        for c in ["六大分點買進張數","六大分點賣出張數","六大分點淨買賣","外資買進張數","外資賣出張數","外資買超張數"]:
            nums=pd.to_numeric(display_rg[c],errors="coerce")
            display_rg[c]=nums.map(lambda x: f"{x:,.0f}" if pd.notna(x) else "—").astype(str)
        costnums=pd.to_numeric(display_rg["分點30日估算成本"],errors="coerce")
        display_rg["分點30日估算成本"]=costnums.map(lambda x: f"{x:,.2f}" if pd.notna(x) else "—").astype(str)
        # 用 HTML table 顯示，避開 Streamlit dataframe -> PyArrow 的序列化路徑。
        st.markdown(display_rg.to_html(index=False,escape=True),unsafe_allow_html=True)
        branch_start=pd.to_datetime(min(chosen)).strftime("%Y-%m-%d") if len(chosen) else "—"
        branch_end=pd.to_datetime(max(chosen)).strftime("%Y-%m-%d") if len(chosen) else "—"
        st.info(f"期間口徑不同：六大分點買進／賣出／淨買賣＝{branch_start} ～ {branch_end} 累計（實際 {len(chosen)} 個資料日；目前選擇 {rank_days} 日）；TWSE 外資買進／賣出／買超＝最新交易日單日。因此兩邊張數不能直接比大小。")
        st.caption("分點30日估算成本＝六大外資分點每日買進張數 × 當日典型價 [(高+低+收)/3] 的加權平均；「分點成本實際日數」顯示真正成功配對的交易日，未滿30日會明確保留實際日數。")
    else:
        st.warning("六大外資分點排行資料暫時無法取得："+str(branch_err))
    st.divider()
    

with tabs[4]:
    st.subheader("📈 個股期貨")
    st.caption("先依 TAIFEX 官方「股票期貨/股票選擇權交易標的」確認標的資格，不再用三大法人資料反推是否有個股期貨。")
    fmap,fmap_url,fmap_err=taifex_stock_futures_map()
    fm=fmap[fmap["symbol"].astype(str).str.zfill(4)==str(symbol).zfill(4)].copy() if not fmap.empty else pd.DataFrame()
    if not fm.empty:
        st.success(f"{stock_name or symbol} 是 TAIFEX 股票期貨標的")
        show=fm.rename(columns={"product_code":"期貨代碼","name":"標的名稱","contract_unit":"契約單位"})[["期貨代碼","標的名稱","契約單位"]]
        st.dataframe(show,use_container_width=True,hide_index=True)
        # 所有股票期貨標的都使用同一套分析，不針對單一股票硬編碼。
        contracts=[]
        for _,r in fm.iterrows():
            unit=str(r.get("contract_unit","")).strip()
            contracts.append(f"{r.get('product_code','—')}（{unit+'股/口' if unit else '契約單位依 TAIFEX'}）")
        if contracts:
            st.caption("本股可用期貨："+"、".join(contracts))
        sd,su,se=taifex_institutional()
        if not sd.empty:
            sprod=next((c for c in sd.columns if "商品" in str(c)),None)
            sdate=next((c for c in sd.columns if "日期" in str(c)),None)
            if not sprod and len(sd.columns)>=2: sprod=sd.columns[1]
            codes=fm["product_code"].astype(str).tolist()
            mask=pd.Series(False,index=sd.index)
            if sprod:
                for code in codes:
                    mask=mask | sd[sprod].astype(str).str.contains(rf"(^|\\s){re.escape(code)}(F|\\s|$)|{re.escape(stock_name or '')}",case=False,na=False,regex=True)
            sf=sd[mask].copy() if sprod else pd.DataFrame()
            if not sf.empty:
                if sdate and sdate in sf.columns:
                    latest=sf[sdate].astype(str).max(); sf=sf[sf[sdate].astype(str)==latest]
                st.markdown("#### 法人期貨資料")
                st.dataframe(sf,use_container_width=True,hide_index=True)
            else:
                st.info("已確認有股票期貨；目前三大法人公開資料未找到可配對的個股列。")
    else:
        if fmap_err:
            st.warning("TAIFEX 股票期貨標的表暫時無法取得："+str(fmap_err))
        else:
            st.info(f"{stock_name or symbol}：目前不在 TAIFEX 股票期貨標的表中。")
    st.link_button("TAIFEX 股票期貨官方標的表",fmap_url)


with tabs[5]:
    st.subheader("📊 大盤期貨")
    st.caption("TAIFEX 三大法人資料只能觀察法人合計部位，無法直接辨識每一口是避險或方向交易；下方採『現貨－期貨對照』做研究性推估。")
    td,tu,te=taifex_institutional()
    if not td.empty:
        spot,spot_url,spot_err=taiex_spot()
        if pd.notna(spot):
            st.metric("避險標的：臺灣加權股價指數",f"{spot:,.2f} 點")
        else:
            st.caption("加權指數現貨價暫時無法取得："+str(spot_err))
        # Flexible column discovery across TAIFEX OpenAPI naming variants
        cols=list(td.columns)
        product=next((c for c in cols if "商品" in str(c)),None)
        ident=next((c for c in cols if "身份" in str(c) or "身分" in str(c)),None)
        datec=next((c for c in cols if "日期" in str(c)),None)
        oi_net=next((c for c in cols if "未平倉" in str(c) and ("淨" in str(c) or "多空" in str(c)) and ("口" in str(c) or "數" in str(c))),None)
        long_oi=next((c for c in cols if "未平倉" in str(c) and "多方" in str(c) and ("口" in str(c) or "數" in str(c))),None)
        short_oi=next((c for c in cols if "未平倉" in str(c) and "空方" in str(c) and ("口" in str(c) or "數" in str(c))),None)
        tx=td.copy()
        # TAIFEX OpenAPI 欄名可能是英文代碼；依目前回傳順序補標準欄位。
        if not product and len(tx.columns)>=2: product=tx.columns[1]
        if not ident and len(tx.columns)>=3: ident=tx.columns[2]
        if not datec and len(tx.columns)>=1: datec=tx.columns[0]
        # 目前 API 常見順序：日期/商品/身份/多方口數/多方金額/空方口數/空方金額/多空淨額口數/...
        if not long_oi and len(tx.columns)>=4: long_oi=tx.columns[3]
        if not short_oi and len(tx.columns)>=6: short_oi=tx.columns[5]
        if not oi_net and len(tx.columns)>=8: oi_net=tx.columns[7]
        if product:
            mask=tx[product].astype(str).str.contains("臺股期貨|台股期貨",regex=True,na=False)
            if mask.any(): tx=tx[mask]
        for c in [oi_net,long_oi,short_oi]:
            if c: tx[c]=pd.to_numeric(tx[c].astype(str).str.replace(",","",regex=False).str.replace(" ","",regex=False),errors="coerce")
        if oi_net is None and long_oi and short_oi:
            tx["_淨未平倉"]=tx[long_oi]-tx[short_oi]; oi_net="_淨未平倉"

        show=[c for c in [datec,product,ident,long_oi,short_oi,oi_net] if c]
        st.dataframe(tx[show] if show else tx,use_container_width=True,hide_index=True)

        if oi_net and tx[oi_net].notna().any():
            if ident:
                cc=tx.groupby(ident,as_index=False)[oi_net].sum()
            else:
                ident="法人"
                cc=pd.DataFrame({ident:["三大法人合計"],oi_net:[tx[oi_net].sum()]})
            st.markdown("#### 三大法人臺股期貨淨未平倉")
            plot_df=cc[[ident,oi_net]].dropna().copy()
            plot_df[oi_net]=pd.to_numeric(plot_df[oi_net],errors="coerce").fillna(0)
            st.bar_chart(plot_df,x=ident,y=oi_net,horizontal=True,use_container_width=True)

            # Heuristic classification: do not assert true intent.
            rows=[]
            for _,r in cc.iterrows():
                who=str(r[ident]); net=float(r[oi_net])
                if who=="投信" and net>0:
                    label="🟠 較可能含避險／配置調整"
                    reason="投信期貨需和基金現貨曝險一起看；單靠期貨多空不能確認意圖"
                elif who=="外資" and net<0:
                    label="🟠 可能混合避險＋方向部位"
                    reason="外資是多家機構合計，空單可能對沖現貨，也可能是方向交易"
                elif who=="自營商":
                    label="🟡 可能含造市／套利／避險"
                    reason="自營商包含期貨及證券自營商，常同時存在造市、套利與避險需求"
                else:
                    label="⚪ 無法僅由三大法人資料判定"
                    reason="需要搭配現貨買賣超、選擇權、跨月價差與部位變化"
                rows.append({"法人":who,"淨未平倉口數":net,"部位性質推估":label,"判讀依據":reason})
            judge=pd.DataFrame(rows)
            st.markdown("#### 避險／方向部位推估")
            # 將「可能避險」部位的口數與契約名目金額一起顯示。
            # TAIFEX API 金額欄通常以千元呈現；優先使用官方多/空方未平倉契約金額。
            long_amt=next((c for c in cols if "未平倉" in str(c) and "多方" in str(c) and "金額" in str(c)),None)
            short_amt=next((c for c in cols if "未平倉" in str(c) and "空方" in str(c) and "金額" in str(c)),None)
            if not long_amt and len(tx.columns)>=5: long_amt=tx.columns[4]
            if not short_amt and len(tx.columns)>=7: short_amt=tx.columns[6]
            for c in [long_amt,short_amt]:
                if c: tx[c]=pd.to_numeric(tx[c].astype(str).str.replace(",","",regex=False).str.replace(" ","",regex=False),errors="coerce")
            hedge_rows=[]
            if ident:
                for who,g in tx.groupby(ident):
                    lo=float(g[long_oi].sum()) if long_oi else np.nan
                    so=float(g[short_oi].sum()) if short_oi else np.nan
                    la=float(g[long_amt].sum()) if long_amt else np.nan
                    sa=float(g[short_amt].sum()) if short_amt else np.nan
                    # 只把「可能作為對沖的一側」列為估計，不宣稱全部都是避險。
                    if str(who)=="投信" and lo>=so:
                        hp,ha,direction=lo,la,"多方可能避險/配置部位"
                    elif str(who)=="外資及陸資" and so>lo:
                        hp,ha,direction=so,sa,"空方可能避險部位"
                    elif str(who)=="自營商":
                        hp,ha,direction=min(lo,so),min(la,sa),"雙邊造市/套利可能避險部位"
                    else:
                        hp,ha,direction=np.nan,np.nan,"無法判定"
                    hedge_rows.append({"法人":who,"可能避險方向":direction,"可能避險口數（上限）":hp,
                                       "對應未平倉契約金額（千元）":ha,
                                       "約當億元":ha/100000 if pd.notna(ha) else np.nan})
            if hedge_rows:
                hedge=pd.DataFrame(hedge_rows)
                st.markdown("#### 可能避險部位：口數與名目金額")
                st.dataframe(hedge,use_container_width=True,hide_index=True)
                hv=hedge.dropna(subset=["約當億元"]).set_index("法人")
                if not hv.empty:
                    st.bar_chart(hv["約當億元"],horizontal=True)
                st.caption("『可能避險口數』是用途判讀的上限估計，不代表這些部位全部都是避險；金額採 TAIFEX 未平倉契約金額欄位，屬契約名目金額，不是實際投入保證金。")
            st.dataframe(judge,use_container_width=True,hide_index=True)
            st.caption("⚠️ 這是推估，不是 TAIFEX 對部位用途的官方分類。期交所也明確提醒：三大法人數字是眾多機構合計互抵結果，不能代表單一法人或整類法人的交易策略。")

            st.markdown("#### 如何判斷")
            st.markdown("**偏避險：** 現貨大量淨買，同期台指期空單增加；或自營商期貨與選擇權呈現明顯對沖結構。\n\n**偏方向：** 現貨與期貨方向一致，且淨未平倉連續增加；例如現貨賣超同時期貨空單持續增加。\n\n**混合／無法判定：** 現貨與期貨訊號不一致、或只有單日資料。")
    else:
        st.warning("TAIFEX 官方資料暫時讀取失敗："+str(te))
        # 第二張圖：多方與空方未平倉，讓圖一定出現在表格下方。
        if ident and long_oi and short_oi and tx[long_oi].notna().any():
            ls=tx.groupby(ident,as_index=False)[[long_oi,short_oi]].sum()
            st.markdown("#### 多方 vs 空方未平倉")
            st.bar_chart(ls,x=ident,y=[long_oi,short_oi],horizontal=True,use_container_width=True)
    st.markdown("### 選擇權：內涵價值 vs 時間價值")
    st.caption("期貨本身沒有選擇權式的『時間價值／內涵價值』拆分；這裡針對臺指選擇權 TXO 計算。")
    od,ou,oe=taifex_options()
    if not od.empty and pd.notna(spot):
        oc=list(od.columns)
        prod=next((c for c in oc if "商品" in str(c)),None)
        strike_c=next((c for c in oc if "履約價" in str(c) or "Strike" in str(c)),None)
        cp_c=next((c for c in oc if "買賣權" in str(c) or "買權賣權" in str(c) or "Call/Put" in str(c)),None)
        close_c=next((c for c in oc if "收盤價" in str(c) or "Last Traded Price" in str(c) or str(c)=="Close"),None)
        expiry_c=next((c for c in oc if "到期" in str(c) or "契約月份" in str(c) or "Contract Month" in str(c)),None)
        opt=od.copy()
        if prod:
            m=opt[prod].astype(str).str.contains("臺指選擇權|台指選擇權|TXO",regex=True,na=False)
            if m.any(): opt=opt[m]
        if strike_c and cp_c and close_c:
            opt[strike_c]=pd.to_numeric(opt[strike_c].astype(str).str.replace(",","",regex=False),errors="coerce")
            opt[close_c]=pd.to_numeric(opt[close_c].astype(str).str.replace(",","",regex=False),errors="coerce")
            opt=opt.dropna(subset=[strike_c,close_c])
            # Focus on strikes nearest spot for a useful hedge dashboard.
            opt["_距現貨"]=abs(opt[strike_c]-spot)
            opt=opt.sort_values("_距現貨").head(20).copy()
            vals=opt.apply(lambda r: option_value_split(spot,float(r[strike_c]),float(r[close_c]),str(r[cp_c])),axis=1)
            opt["內涵價值"]=vals.map(lambda x:x[0])
            opt["時間價值"]=vals.map(lambda x:x[1])
            opt["時間價值占權利金%"]=np.where(opt[close_c]>0,opt["時間價值"]/opt[close_c]*100,np.nan)
            showo=[c for c in [expiry_c,strike_c,cp_c,close_c] if c]+["內涵價值","時間價值","時間價值占權利金%"]
            # 標記目前成交量/未平倉口數最大的契約，並以外資偏空部位作為「疑似避險」提示。
            vol_c=next((c for c in oc if str(c)=="Volume" or "成交量" in str(c)),None)
            oi_c=next((c for c in oc if str(c)=="Open Interest" or "未平倉" in str(c)),None)
            if vol_c:
                opt[vol_c]=pd.to_numeric(opt[vol_c].astype(str).str.replace(",","",regex=False),errors="coerce")
            if oi_c:
                opt[oi_c]=pd.to_numeric(opt[oi_c].astype(str).str.replace(",","",regex=False),errors="coerce")
            opt["標記"]=""
            rank_c=oi_c if oi_c and opt[oi_c].notna().any() else vol_c
            if rank_c and opt[rank_c].notna().any():
                imax=opt[rank_c].idxmax()
                opt.loc[imax,"標記"]="🔥 目前口數最多"
            # 外資若在臺股期貨呈淨空，Put 僅列為可能避險觀察，不宣稱該選擇權就是外資持倉。
            foreign_net=np.nan
            try:
                if ident and oi_net:
                    fg=tx[tx[ident].astype(str).str.contains("外資",na=False)]
                    if not fg.empty: foreign_net=float(fg[oi_net].sum())
            except Exception:
                pass
            if pd.notna(foreign_net) and foreign_net<0:
                puts=opt[opt[cp_c].astype(str).str.upper().str.startswith(("P","賣權"))].copy()
                if not puts.empty:
                    if rank_c and puts[rank_c].notna().any(): hedge_idx=puts[rank_c].idxmax()
                    else: hedge_idx=puts["_距現貨"].idxmin()
                    prior=str(opt.loc[hedge_idx,"標記"]).strip()
                    opt.loc[hedge_idx,"標記"]=(prior+"｜" if prior else "")+"🛡️ 疑似外資避險觀察"
            showo=["標記"]+[c for c in [expiry_c,strike_c,cp_c,close_c,vol_c,oi_c] if c]+["內涵價值","時間價值","時間價值占權利金%"]
            st.dataframe(opt[showo],use_container_width=True,hide_index=True)
            if rank_c and opt[rank_c].notna().any():
                rr=opt.loc[opt[rank_c].idxmax()]
                st.success(f"🔥 目前口數最多：{rr.get(expiry_c,'')}｜履約價 {rr[strike_c]:,.0f}｜{rr[cp_c]}｜{rank_c} {rr[rank_c]:,.0f}")
            st.caption("🛡️『疑似外資避險觀察』是把外資期貨淨空方向與 Put 契約活躍度交叉標示；TAIFEX 公開選擇權行情無法證明該契約實際由外資持有，因此只作觀察提示。")
            charto=opt[[strike_c,cp_c,"內涵價值","時間價值"]].copy()
            charto["契約"]=charto[strike_c].astype(str)+" "+charto[cp_c].astype(str)
            st.bar_chart(charto.set_index("契約")[["內涵價值","時間價值"]],horizontal=True)
            st.caption("內涵價值：Call=max(現貨−履約價,0)，Put=max(履約價−現貨,0)；時間價值=max(權利金−內涵價值,0)。使用公開日行情時，這是收盤時點估算。")
        else:
            st.info("TAIFEX 選擇權資料已取得，但欄位格式暫時無法自動辨識。")
    else:
        st.info("臺指選擇權公開行情暫時無法取得："+str(oe))
    st.link_button("TAIFEX OpenAPI",tu)

with tabs[5]:
    st.subheader("台股重大訊息")
    md,mu,me=twse_material(symbol)
    if not md.empty:
        st.caption(f"直接顯示 {symbol} 的 TWSE OpenAPI 每日重大訊息")
        preferred=[c for c in md.columns if any(k in str(c) for k in ["日期","時間","公司代號","公司名稱","主旨","說明"])]
        st.dataframe(md[preferred] if preferred else md,use_container_width=True,hide_index=True)
        dc=next((c for c in md.columns if "日期" in str(c)),None)
        if dc:
            counts=md[dc].astype(str).value_counts().sort_index()
            st.markdown("#### 重大訊息發布筆數")
            st.bar_chart(counts)
        st.metric("目前取得重大訊息",f"{len(md)} 筆")
    else:
        st.info(f"目前官方 OpenAPI 沒有取得 {symbol} 的重大訊息。"+((" "+str(me)) if me else ""))
    st.link_button("TWSE OpenAPI 重大訊息",mu)

st.caption("最後重新執行："+datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
