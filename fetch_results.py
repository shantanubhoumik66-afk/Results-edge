"""Results-edge data builder. Runs on GitHub Actions, writes results.json."""
import io, json, time, datetime as dt
import requests, pandas as pd, yfinance as yf

H = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}
MIN_MCAP_CR = 10000
MAX_AGE_DAYS = 110          # only quarters that ended recently (fresh results)

def nse():
    s = requests.Session(); s.headers.update(H)
    try: s.get("https://www.nseindia.com", timeout=20)
    except Exception: pass
    return s

def universe(s):
    try:
        r = s.get("https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv", timeout=30)
        df = pd.read_csv(io.StringIO(r.text))
        return list(zip(df["Symbol"], df["Company Name"]))
    except Exception as e:
        print("universe failed:", e); return []

def announcements(s, sym):
    try:
        r = s.get("https://www.nseindia.com/api/corporate-announcements",
                  params={"index": "equities", "symbol": sym},
                  headers={"Referer": "https://www.nseindia.com/"}, timeout=20)
        items = r.json()
        keys = ("result", "outcome", "presentation", "call", "press", "order", "outlook")
        pick = [i for i in items if any(k in (i.get("desc", "") + i.get("attchmntText", "")).lower() for k in keys)] or items
        return [{"date": (i.get("an_dt") or "")[:10], "title": i.get("desc", ""),
                 "text": i.get("attchmntText", ""), "link": i.get("attchmntFile", "")} for i in pick[:4]]
    except Exception:
        return []

def row(df, names):
    if df is None or df.empty: return None
    for n in names:
        if n in df.index: return df.loc[n]
    return None

def get(s, i):
    try:
        v = s.iloc[i]; return None if pd.isna(v) else float(v)
    except Exception: return None

def cr(v): return None if v is None else round(v / 1e7, 1)
def gr(a, b): return None if a is None or b in (None, 0) else round((a - b) / abs(b) * 100, 1)
def clamp(x, lo, hi): return max(lo, min(hi, x))

def metric(s):
    c, p, y = get(s, 0), get(s, 1), get(s, 4)
    return {"cur": cr(c), "prev": cr(p), "yago": cr(y), "qoq": gr(c, p), "yoy": gr(c, y)}

def quarter_label(d):
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(d.month)
    if not q: return d.strftime("%b %Y")
    fy = d.year + 1 if d.month != 3 else d.year
    return f"{d.strftime('%b %Y')} (Q{q} FY{str(fy)[2:]})"

def analyze(sym, name, s):
    t = yf.Ticker(sym + ".NS")
    try: mcap = t.fast_info["market_cap"] / 1e7
    except Exception: return None
    if mcap < MIN_MCAP_CR: return None
    q = t.quarterly_income_stmt
    if q is None or q.empty or q.shape[1] < 2: return None
    latest = pd.Timestamp(q.columns[0]).to_pydatetime()
    if (dt.datetime.now() - latest).days > MAX_AGE_DAYS: return None
    rev = row(q, ["Total Revenue", "Operating Revenue"])
    ebitda = row(q, ["EBITDA", "Normalized EBITDA"])
    pat = row(q, ["Net Income", "Net Income Common Stockholders"])
    if rev is None or pat is None: return None
    R, E, P = metric(rev), metric(ebitda) if ebitda is not None else {}, metric(pat)
    b, cf = t.quarterly_balance_sheet, t.quarterly_cashflow
    debt = get(row(b, ["Total Debt"]), 0); cash = get(row(b, ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]), 0)
    eq = get(row(b, ["Stockholders Equity", "Common Stock Equity"]), 0); ocf = get(row(cf, ["Operating Cash Flow"]), 0)
    de = round(debt / eq, 2) if debt is not None and eq else None
    m_cur = round(get(ebitda, 0) / get(rev, 0) * 100, 1) if ebitda is not None and get(ebitda, 0) and get(rev, 0) else None
    m_old = round(get(ebitda, 4) / get(rev, 4) * 100, 1) if ebitda is not None and get(ebitda, 4) and get(rev, 4) else None
    score, why = 0, []
    for lab, m in (("Revenue", R), ("EBITDA", E), ("PAT", P)):
        g = m.get("yoy")
        if g is not None:
            score += clamp(g, -40, 80) / 4
            why.append(f"{lab} {g:+.0f}% YoY")
    if m_cur is not None and m_old is not None:
        score += clamp(m_cur - m_old, -5, 5) * 2; why.append(f"EBITDA margin {m_old}% to {m_cur}%")
    if ocf is not None:
        score += 5 if ocf > 0 else -5; why.append("Operating cash flow " + ("positive" if ocf > 0 else "negative"))
    if de is not None:
        score += 5 if de < 0.5 else (-5 if de > 1.5 else 0); why.append(f"Debt/Equity {de}")
    return {"symbol": sym, "name": name, "mcap_cr": round(mcap), "score": round(score, 1),
            "quarter": quarter_label(latest), "quarter_end": latest.strftime("%Y-%m-%d"), "reasons": why,
            "result": {"revenue": R, "ebitda": E, "pat": P, "margin_cur": m_cur, "margin_yago": m_old},
            "balance": {"debt": cr(debt), "cash": cr(cash), "equity": cr(eq), "de": de, "ocf": cr(ocf)},
            "announcements": announcements(s, sym)}

def main():
    s = nse(); out = []
    for sym, name in universe(s):
        try:
            r = analyze(sym, name, s)
            if r: out.append(r)
        except Exception as e: print(sym, "skip:", e)
        time.sleep(0.3)
    quarters = {}
    for lab in sorted({o["quarter"] for o in out}, key=lambda l: max(o["quarter_end"] for o in out if o["quarter"] == l), reverse=True):
        rows = sorted([o for o in out if o["quarter"] == lab], key=lambda o: -o["score"])
        good = rows[:10]; bad = [o for o in reversed(rows) if o not in good][:10]
        quarters[lab] = {"good": good, "bad": bad, "count": len(rows)}
    if not quarters:
        print("No data fetched; keeping old results.json"); return
    json.dump({"updated": dt.datetime.utcnow().isoformat() + "Z", "quarters": quarters}, open("results.json", "w"), indent=1)

main()
