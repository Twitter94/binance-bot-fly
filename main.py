import os, asyncio, math, requests
from datetime import datetime
import pytz
from binance.client import Client
from binance.exceptions import BinanceAPIException
from telegram import Bot, ReplyKeyboardMarkup, Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

load_dotenv()
wib = pytz.timezone('Asia/Jakarta')

# ===== [8] CONFIG =====
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
PAIR = os.getenv("PAIR")
MIN_LOT = float(os.getenv("LOT") or 5)
BUFFER = 0.003

for k in ["BINANCE_API_KEY","BINANCE_API_SECRET","PAIR","LOT","TELE_TOKEN","TELE_CHAT_ID","SUPA_URL","SUPA_KEY"]:
    if not os.getenv(k): raise Exception(f"ENV {k} KOSONG!")

# ===== [1] SETTING ATR & GRID =====
ATR_PERIOD, ATR_TIMEFRAME, ATR_MULTIPLIER = 14, Client.KLINE_INTERVAL_1HOUR, 0.5
ATR_UPDATE_HOUR = 0
MIN_GRID, MAX_GRID = 250, 1000

binance = Client(API_KEY, API_SECRET, tld='com')
tele_bot = Bot(os.getenv("TELE_TOKEN"))
CHAT_ID = os.getenv("TELE_CHAT_ID")

SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")
HEADERS = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}

grid_aktif = MIN_GRID
atr_awal = 0
atr_last_check = ""
sent_notif = set()
is_paused = False
FEE_BINANCE = 0.001

def get_fee_binance():
    global FEE_BINANCE
    try: FEE_BINANCE = float(binance.get_trade_fee(symbol=PAIR)[0]['maker'])
    except: FEE_BINANCE = 0.001

def supa_select(t, k=None, v=None): 
    try: return requests.get(f"{SUPA_URL}/rest/v1/{t}?select=*{'&'+k+'=eq.'+str(v) if k else ''}", headers=HEADERS, timeout=15).json()
    except: return []
def supa_insert(t, d): 
    try: requests.post(f"{SUPA_URL}/rest/v1/{t}", json=d, headers=HEADERS, timeout=15)
    except: pass
def supa_update(t, d, k, v): 
    try: requests.patch(f"{SUPA_URL}/rest/v1/{t}?{k}=eq.{v}", json=d, headers=HEADERS, timeout=15)
    except: pass
def supa_delete(t, k, v): 
    try: requests.delete(f"{SUPA_URL}/rest/v1/{t}?{k}=eq.{v}", headers=HEADERS, timeout=15)
    except: pass

async def log_db(level, msg, data={}):
    supa_insert("bot_logs", {"time": datetime.now(wib).isoformat(), "level": level, "message": msg, "data": data})

async def send_tele(msg, key="umum"):
    global sent_notif
    if key in sent_notif: return
    try: await tele_bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown", reply_markup=keyboard()); sent_notif.add(key); await asyncio.sleep(2)
    except: pass
def keyboard(): return ReplyKeyboardMarkup([["STATUS"]], resize_keyboard=True)

def rapikan_ke_grid(h, g): return round(h / g) * g
def get_atr():
    try:
        c = [float(k[4]) for k in binance.get_klines(symbol=PAIR, interval=ATR_TIMEFRAME, limit=ATR_PERIOD+1)]
        atr = sum([abs(c[i]-c[i-1]) for i in range(1,len(c))])/ATR_PERIOD
        return max(MIN_GRID, min(MAX_GRID, round((atr * ATR_MULTIPLIER)/10)*10))
    except: return MIN_GRID
def calc_modal(lot): return lot + (lot * FEE_BINANCE * 2) + (lot * BUFFER)
def hitung_lot(harga): return max(MIN_LOT, harga * 0.00001) # [RUMUS LOT v7.0]

async def get_positions_db():
    out = {}
    for r in supa_select("positions", "pair", PAIR):
        try: out[float(r['buy_price'])] = {"qty": float(r['qty']), "tp": float(r['tp_price']), "lot": float(r['lot'])}
        except: continue
    return out
async def save_position(bp, q, tp, lot): supa_insert("positions", {"pair": PAIR, "buy_price": bp, "qty": q, "tp_price": tp, "lot": lot})
async def delete_position(bp): supa_delete("positions", f"pair=eq.{PAIR}&buy_price=eq.{bp}", "")
async def update_tp(bp, tp): supa_update("positions", {"tp_price": tp}, f"pair=eq.{PAIR}&buy_price=eq.{bp}", "")
async def update_stats(p): 
    s = supa_select("stats", "id", 1)
    if not s: supa_insert("stats", {"id": 1, "total_sell": 1, "total_profit": p})
    else: supa_update("stats", {"total_profit": float(s[0]['total_profit'])+p, "total_sell": int(s[0]['total_sell'])+1}, "id", 1)

async def check_existing_order(p):
    try: return any(abs(float(o['price']) - p) < 1 for o in binance.get_open_orders(symbol=PAIR))
    except: return False

async def place_buy(price):
    global is_paused
    price = rapikan_ke_grid(price, grid_aktif)
    if price in await get_positions_db(): return
    if await check_existing_order(price): return

    LOT = hitung_lot(price)
    qty = LOT / price # [RUMUS QTY v7.0]
    modal = calc_modal(LOT)
    balance = float(binance.get_asset_balance('USDT')['free'])

    if balance < modal:
        if not is_paused: await send_tele(f"🔴 *PAUSE* | Harga: ${price:.2f}\nSALDO: ${balance:.4f}\nButuh: `${modal:.2f}`", key="SALDO"); is_paused = True
        return
    if is_paused: is_paused = False; await send_tele("✅ *SALDO CUKUP - BOT LANJUT BUY*", key="SALDO_OK")

    for i in range(3):
        try:
            await asyncio.sleep(1.5)
            o = binance.order_market_buy(symbol=PAIR, quantity=qty)
            rp, rq = float(o['fills'][0]['price']), float(o['executedQty'])
            tp = rp + grid_aktif
            await save_position(rp, rq, tp, LOT)
            await log_db("BUY", f"Buy {rq:.8f} @ {rp}", {"lot": LOT, "modal": modal})
            await send_tele(f"🟢 *BUY DI BINANCE*\n`{PAIR}` @ `{rp:.2f}`\nLOT: `${LOT:.2f}`\nModal: `${modal:.2f}`\nTP: `{tp:.2f}`", key=f"BUY_{rp}")
            return
        except: await asyncio.sleep(3)

async def place_sell(bp, reason="TP"):
    d = (await get_positions_db()).get(bp)
    if not d: return
    for i in range(3):
        try:
            await asyncio.sleep(1.5)
            binance.order_market_sell(symbol=PAIR, quantity=d['qty'])
            profit = BUFFER + (d['qty'] * grid_aktif) # [RUMUS PROFIT v7.0]
            await delete_position(bp); await update_stats(profit)
            await log_db("SELL", f"Sell {d['qty']:.8f}", {"profit": profit, "reason": reason})
            await send_tele(f"🔴 *SELL DI BINANCE*\n`{PAIR}` @ Market\nAlasan: `{reason}`\nLOT: `${d['lot']:.2f}`\nProfit: `+{profit:.2f}` USDT", key=f"SELL_{bp}")
            await place_buy(bp) # [RE-ENTRY]
            return
        except: await asyncio.sleep(3)

async def handle_atr_shift(ng):
    global grid_aktif
    pos = await get_positions_db()
    if ng > grid_aktif:
        await send_tele(f"⚡ *ATR NAIK 20%*\nGrid: {grid_aktif} -> {ng}\n*SELL INSTAN {len(pos)} POSISI*", "ATR_UP")
        for bp in list(pos.keys()): await place_sell(bp, "ATR SHIFT UP")
    else:
        await send_tele(f"⚡ *ATR TURUN 20%*\nGrid: {grid_aktif} -> {ng}\n*RESET TP SEMUA POSISI*", "ATR_DOWN")
        for bp in pos.keys(): await update_tp(bp, bp + ng)
    grid_aktif = ng

async def check_and_sell_passed_tp(p):
    for bp, d in (await get_positions_db()).items():
        if p >= d['tp']: await place_sell(bp, "TP LEWAT SAAT START")

async def main_loop():
    global grid_aktif, atr_awal, atr_last_check
    get_fee_binance(); grid_aktif = get_atr(); atr_awal = grid_aktif
    await check_and_sell_passed_tp(float(binance.get_symbol_ticker(symbol=PAIR)['price']))
    await send_tele(f"✅ *BOT v7.5.0 JALAN*\nGrid: `{grid_aktif}` | LOT Min: `{MIN_LOT}`", "START")
    while True:
        try:
            get_fee_binance(); p = float(binance.get_symbol_ticker(symbol=PAIR)['price']); pos = await get_positions_db()
            for bp, d in list(pos.items()): 
                if p >= d['tp']: await place_sell(bp, "TP HIT")
            now = datetime.now(wib)
            if now.hour == ATR_UPDATE_HOUR and now.strftime("%H:%M")!= atr_last_check:
                na = get_atr()
                if atr_awal > 0 and abs((na-atr_awal)/atr_awal) >= 0.2: await handle_atr_shift(na)
                atr_awal, atr_last_check = na, now.strftime("%H:%M")
            lb = min(pos.keys()) if pos else rapikan_ke_grid(p, grid_aktif)
            if p <= lb - grid_aktif: await place_buy(p)
            await asyncio.sleep(2)
        except Exception as e: await log_db("ERROR", str(e)); await asyncio.sleep(60)

async def status(u, c):
    get_fee_binance(); bal = float(binance.get_asset_balance('USDT')['free']); p = float(binance.get_symbol_ticker(symbol=PAIR)['price']); pos = await get_positions_db(); s = supa_select("stats", "id", 1)
    lot = hitung_lot(p); modal = calc_modal(lot) # [LOT STATUS = LOT BUY]
    st = "PAUSE" if is_paused else "JALAN"; em = "🔴" if is_paused else "🟢"
    pt = "\n".join([f"BUY ${bp:.2f} -> TP ${d['tp']:.2f} | LOT ${d['lot']:.2f}" for bp,d in sorted(pos.items())]) or "Kosong"
    msg = f"""{em} *{st}* | *Harga:* ${p:.2f}
*SALDO:* ${bal:.4f}
*GRID:* ${grid_aktif:.2f} | *LOT:* ${lot:.2f} | *Butuh:* ${modal:.2f}
*Fee:* {FEE_BINANCE*100:.3f}% | *Posisi:* {len(pos)} | *Sell:* {s[0]['total_sell'] if s else 0} | *Profit:* ${float(s[0]['total_profit']) if s else 0:.2f}

📌 *POSISI*
{pt}"""
    await u.message.reply_text(msg, parse_mode="Markdown")
async def handle_message(u, c): 
    if u.message and u.message.text.strip().upper() == "STATUS": await status(u, c)
async def on_startup(app): asyncio.create_task(main_loop())
def main():
    app = Application.builder().token(os.getenv("TELE_TOKEN")).post_init(on_startup).build()
    app.add_handler(MessageHandler(filters.TEXT, handle_message)); app.run_polling()
if __name__ == "__main__": main()
