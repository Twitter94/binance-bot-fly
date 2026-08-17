import os, asyncio, time, math
from datetime import datetime
import pytz
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, JobQueue
from binance.client import Client
from binance.exceptions import BinanceAPIException
from supabase import create_client, Client as SupaClient

TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")
BINANCE_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")
PAIR = os.getenv("PAIR", "BTCUSDT")
LOT = float(os.getenv("LOT", "5"))
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")

ATR_PERIOD = 14
ATR_MULTIPLIER = 0.5
MIN_GRID = 250
MAX_GRID = 1000
wib = pytz.timezone('Asia/Jakarta')

binance = Client(BINANCE_KEY, BINANCE_SECRET)
supa: SupaClient = create_client(SUPA_URL, SUPA_KEY)

# TAMBAH CACHE BIAR GAK OOM
last_atr = 0
last_atr_time = 0
last_symbol_info = None

def save_position(area, buy_price, qty, lot, fee):
    supa.table("positions").upsert({"pair": PAIR, "area": area, "buy_price": buy_price, "qty": qty, "lot": lot, "fee": fee}).execute()

def delete_position(area):
    supa.table("positions").delete().eq("pair", PAIR).eq("area", area).execute()

def get_positions():
    res = supa.table("positions").select("*").eq("pair", PAIR).execute()
    return {p['area']: p for p in res.data}

def log_trade(type, price, profit, grid, area):
    supa.table("logs").insert({"pair": PAIR, "type": type, "price": price, "profit": profit, "grid": grid, "area": area, "time": datetime.now(wib).isoformat()}).execute()

# ===== BINANCE HELPERS SPOT VERSI HEMAT RAM =====
def get_price(): 
    return float(binance.get_symbol_ticker(symbol=PAIR)['price'])

def get_atr(): 
    global last_atr, last_atr_time
    now = time.time()
    # Cache ATR 5 menit sekali aja. Jangan tiap 10 detik
    if now - last_atr_time > 300:
        klines = binance.get_klines(symbol=PAIR, interval=Client.KLINE_INTERVAL_1HOUR, limit=ATR_PERIOD+1)
        highs = [float(k[2]) for k in klines]; lows = [float(k[3]) for k in klines]; closes = [float(k[4]) for k in klines]
        trs = [max(h-l, abs(h-closes[i-1]), abs(l-closes[i-1])) for i,(h,l) in enumerate(zip(highs[1:], lows[1:]))]
        last_atr = sum(trs)/ATR_PERIOD
        last_atr_time = now
    return last_atr

def get_grid_atr():
    atr = get_atr()
    grid = round(atr * ATR_MULTIPLIER)
    return max(MIN_GRID, min(MAX_GRID, grid))

def get_lot_and_fee(price):
    global last_symbol_info
    try:
        # Cache symbol_info 1 jam sekali
        if last_symbol_info is None:
            last_symbol_info = binance.get_symbol_info(PAIR)
        
        lot_size = next(f for f in last_symbol_info['filters'] if f['filterType'] == 'LOT_SIZE')
        min_notional = float(next(f for f in last_symbol_info['filters'] if f['filterType'] == 'MIN_NOTIONAL')['minNotional'])
        fee = 0.001
        
        usdt_to_spend = LOT
        qty = usdt_to_spend / price
        step = float(lot_size['stepSize'])
        qty = math.floor(qty / step) * step
        qty = max(qty, float(lot_size['minQty']))
        return qty, usdt_to_spend, fee
    except Exception as e: 
        print(f"ERROR get_lot: {e}")
        return 0.00001, LOT, 0.001

def get_area(price, grid): return round(price / grid) * grid

async def send_telegram(msg):
    await app.bot.send_message(chat_id=TELE_CHAT_ID, text=msg)

async def trading_loop(context):
    last_grid_update = 0
    while True:
        try:
            now = datetime.now(wib)
            price = get_price()
            positions = get_positions()
            
            if now.hour == 0 and now.minute == 0 and last_grid_update!= now.day:
                grid = get_grid_atr()
                last_grid_update = now.day
                await send_telegram(f"ATR SHIFT 00:00 | GRID BARU: {grid}")
            else:
                grid = get_grid_atr()

            area = get_area(price, grid)
            last_buy_price = max(positions.values(), key=lambda x: x['buy_price'])['buy_price'] if positions else price

            if not positions or price <= last_buy_price - grid:
                if area not in positions:
                    qty, lot, fee = get_lot_and_fee(price)
                    balance = float(binance.get_asset_balance(asset="USDT")['free'])
                    cost = qty * price
                    if balance > cost * 1.001:
                        order = binance.create_order(symbol=PAIR, side='BUY', type='LIMIT', timeInForce='GTC', quantity=qty, price=str(price))
                        save_position(area, price, qty, lot, fee)
                        log_trade("BUY", price, 0, grid, area)
                        await send_telegram(f"BUY @{price} | QTY: {qty} | AREA: {area}")
                    else:
                        await send_telegram(f"PAUSE: SALDO USDT KURANG. Butuh: {cost:.2f} | Ada: {balance:.2f}")

            for area_pos, pos in list(positions.items()):
                tp_price = pos['buy_price'] + grid
                if price >= tp_price:
                    order = binance.create_order(symbol=PAIR, side='SELL', type='LIMIT', timeInForce='GTC', quantity=pos['qty'], price=str(tp_price))
                    profit = (tp_price - pos['buy_price']) * pos['qty']
                    delete_position(area_pos)
                    log_trade("SELL", tp_price, profit, grid, area_pos)
                    
                    if area_pos not in get_positions():
                        await send_telegram(f"SELL @{tp_price} +{profit:.2f} USDT -> RE-ENTRY BUY @{tp_price}")
                        qty, lot, fee = get_lot_and_fee(tp_price)
                        binance.create_order(symbol=PAIR, side='BUY', type='LIMIT', timeInForce='GTC', quantity=qty, price=str(tp_price))
                        save_position(area_pos, tp_price, qty, lot, fee)
                    else:
                        await send_telegram(f"SELL @{tp_price} +{profit:.2f} USDT | AREA MASIH AKTIF. SKIP RE-ENTRY")
                        
        except Exception as e: 
            await send_telegram(f"ERROR: {e}")
        await asyncio.sleep(10)

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    keyboard = [[KeyboardButton("STATUS")]]
    await u.message.reply_text("BOT INFINITE GRID SPOT v9.1.2 HIDUP 🚀", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

async def status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    price = get_price(); grid = get_grid_atr(); positions = get_positions()
    balance = float(binance.get_asset_balance(asset="USDT")['free'])
    qty, lot, fee = get_lot_and_fee(price)
    msg = f"*STATUS SPOT JALAN*\nHarga: {price}\nSaldo USDT: {balance:.2f}\nGrid: {grid}(ATR)\nLOT/Buy: {lot:.2f} USDT\nFee: {fee*100:.3f}%\nPosisi: {len(positions)}"
    await u.message.reply_text(msg, parse_mode='Markdown')

app = ApplicationBuilder().token(TELE_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("status", status))

def main():
    app.job_queue.run_repeating(trading_loop, interval=10, first=5)
    app.run_webhook(listen="0.0.0.0", port=8080, url_path=TELE_TOKEN, webhook_url=f"https://bahaya.fly.dev/{TELE_TOKEN}")

if __name__ == "__main__": 
    main()
