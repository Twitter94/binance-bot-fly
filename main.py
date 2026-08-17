import os, asyncio, time, math
from datetime import datetime
import pytz
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, JobQueue
from binance.client import Client
from binance.exceptions import BinanceAPIException
from supabase import create_client, Client as SupaClient

# ===== ENV =====
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")
BINANCE_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")
PAIR = os.getenv("PAIR", "BTCUSDT")
LOT = float(os.getenv("LOT", "5"))
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")

# ===== SETTING ATR & GRID =====
ATR_PERIOD = 14
ATR_MULTIPLIER = 0.5
MIN_GRID = 250
MAX_GRID = 1000
QTY_FIXED = 0.00001
wib = pytz.timezone('Asia/Jakarta')

binance = Client(BINANCE_KEY, BINANCE_SECRET)
supa: SupaClient = create_client(SUPA_URL, SUPA_KEY)

# ===== SUPABASE HELPERS =====
def save_position(area, buy_price, qty, lot, fee):
    supa.table("positions").upsert({"pair": PAIR, "area": area, "buy_price": buy_price, "qty": qty, "lot": lot, "fee": fee}).execute()

def delete_position(area):
    supa.table("positions").delete().eq("pair", PAIR).eq("area", area).execute()

def get_positions():
    res = supa.table("positions").select("*").eq("pair", PAIR).execute()
    return {p['area']: p for p in res.data}

def log_trade(type, price, profit, grid, area):
    supa.table("logs").insert({"pair": PAIR, "type": type, "price": price, "profit": profit, "grid": grid, "area": area, "time": datetime.now(wib).isoformat()}).execute()

# ===== BINANCE HELPERS =====
def get_price(): return float(binance.futures_ticker_price(symbol=PAIR)['price'])
def get_atr(): 
    klines = binance.futures_klines(symbol=PAIR, interval=Client.KLINE_INTERVAL_1HOUR, limit=ATR_PERIOD+1)
    highs = [float(k[2]) for k in klines]; lows = [float(k[3]) for k in klines]; closes = [float(k[4]) for k in klines]
    trs = [max(h-l, abs(h-closes[i-1]), abs(l-closes[i-1])) for i,(h,l) in enumerate(zip(highs[1:], lows[1:]))]
    return sum(trs)/ATR_PERIOD

def get_grid_atr():
    atr = get_atr()
    grid = round(atr * ATR_MULTIPLIER)
    return max(MIN_GRID, min(MAX_GRID, grid))

def get_lot_and_fee(price):
    try:
        info = binance.futures_exchange_info()
        symbol_info = next(s for s in info['symbols'] if s['symbol'] == PAIR)
        lot_size = next(f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE')
        min_notional = next(f for f in symbol_info['filters'] if f['filterType'] == 'MIN_NOTIONAL')['notional']
        fee = float(binance.futures_trade_fee(symbol=PAIR)[0]['makerCommission']) / 100
        
        qty = QTY_FIXED
        lot_needed = (float(min_notional) / price) / qty * qty # Satpam 1
        lot_needed = math.ceil(lot_needed / lot_size['stepSize']) * lot_size['stepSize']
        lot_needed = max(lot_needed, LOT)
        
        modal_potong = lot_needed + (lot_needed * fee * 2) + (lot_needed * 0.001) # Buffer 0.1%
        return qty, lot_needed, fee
    except: return QTY_FIXED, LOT, 0.001

def get_area(price, grid): return round(price / grid) * grid

async def send_telegram(msg):
    await app.bot.send_message(chat_id=TELE_CHAT_ID, text=msg)

# ===== CORE LOGIC =====
async def trading_loop(context): # TAMBAH context
    last_grid_update = 0
    while True:
        try:
            now = datetime.now(wib)
            price = get_price()
            positions = get_positions()
            
            # UPDATE GRID JAM 00:00
            if now.hour == 0 and now.minute == 0 and last_grid_update!= now.day:
                grid = get_grid_atr()
                last_grid_update = now.day
                await send_telegram(f"ATR SHIFT 00:00 | GRID BARU: {grid}")
            else:
                grid = get_grid_atr()

            area = get_area(price, grid)
            last_buy_price = max(positions.values(), key=lambda x: x['buy_price'])['buy_price'] if positions else price

            # [2.1] BUY CICIL: TURUN 1 GRID DARI BUY TERAKHIR
            if not positions or price <= last_buy_price - grid:
                if area not in positions: # [2.2B] 1 AREA 1 POSISI
                    qty, lot, fee = get_lot_and_fee(price)
                    # Cek saldo
                    balance = float(binance.futures_account_balance(asset="USDT")[0]['balance'])
                    if balance > lot * 1.1:
                        # BUY LIMIT
                        order = binance.futures_create_order(symbol=PAIR, side='BUY', type='LIMIT', timeInForce='GTC', quantity=qty, price=price)
                        save_position(area, price, qty, lot, fee)
                        log_trade("BUY", price, 0, grid, area)
                        await send_telegram(f"BUY @{price} | AREA: {area}")
                    else:
                        await send_telegram(f"PAUSE: SALDO KURANG. Nunggu TP atau isi saldo")

            # [4] CEK TP: HARGA NAIK 1 GRID
            for area_pos, pos in list(positions.items()):
                tp_price = pos['buy_price'] + grid
                if price >= tp_price:
                    # SELL
                    order = binance.futures_create_order(symbol=PAIR, side='SELL', type='LIMIT', timeInForce='GTC', quantity=pos['qty'], price=tp_price)
                    profit = (tp_price - pos['buy_price']) * pos['qty']
                    delete_position(area_pos)
                    log_trade("SELL", tp_price, profit, grid, area_pos)
                    
                    # [4.3] RE-ENTRY BERSYARAT
                    if area_pos not in get_positions(): # Area kosong
                        await send_telegram(f"SELL @{tp_price} +{profit:.2f} -> RE-ENTRY BUY @{tp_price}")
                        qty, lot, fee = get_lot_and_fee(tp_price)
                        binance.futures_create_order(symbol=PAIR, side='BUY', type='LIMIT', timeInForce='GTC', quantity=qty, price=tp_price)
                        save_position(area_pos, tp_price, qty, lot, fee)
                    else: # Area masih aktif
                        await send_telegram(f"SELL @{tp_price} +{profit:.2f} | AREA MASIH AKTIF. SKIP RE-ENTRY")
                        
        except Exception as e: await send_telegram(f"ERROR: {e}")
        await asyncio.sleep(10)

# ===== TELEGRAM HANDLER =====
async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    keyboard = [[KeyboardButton("STATUS")]]
    await u.message.reply_text("BOT INFINITE GRID v9.0.2 HIDUP 🚀", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

async def status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    price = get_price(); grid = get_grid_atr(); positions = get_positions()
    balance = float(binance.futures_account_balance(asset="USDT")[0]['balance'])
    qty, lot, fee = get_lot_and_fee(price)
    msg = f"*STATUS JALAN*\nHarga: {price}\nSaldo: {balance:.2f} USDT\nGrid: {grid}(ATR)\nLOT: {lot:.2f}\nFee: {fee*100:.3f}%\nPosisi: {len(positions)}"
    await u.message.reply_text(msg, parse_mode='Markdown')

app = ApplicationBuilder().token(TELE_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("status", status))

def main():
    app.job_queue.run_repeating(trading_loop, interval=10, first=5) # INI KUNCINYA
    app.run_webhook(listen="0.0.0.0", port=8080, url_path=TELE_TOKEN, webhook_url=f"https://bahaya.fly.dev/{TELE_TOKEN}")

if __name__ == "__main__": 
    main()
