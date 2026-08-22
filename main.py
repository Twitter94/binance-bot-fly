import asyncio, ccxt, os
from datetime import datetime
from supabase import create_client
from telegram import Bot

API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
MIN_USDT = float(os.getenv('MIN_NOTIONAL_ENV', 5))
SYMBOL = os.getenv('PAIR', 'BTC/USDT')
SUPABASE_URL = os.getenv('SUPA_URL')
SUPABASE_KEY = os.getenv('SUPA_KEY')
TELEGRAM_TOKEN = os.getenv('TELE_TOKEN')
CHAT_ID = os.getenv('TELE_CHAT_ID')

ATR_PERIOD, ATR_TF, ATR_MULT = 14, '1h', 0.5
ATR_MIN, ATR_MAX = 250, 1000
ATR_UPDATE_HOUR = 0
ATR_SPIKE = 0.20
MIN_QTY = 0.00001
BUFFER = 1.005 # buat nutup fee doang

binance = ccxt.binance({'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True})
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
tg_bot = Bot(TELEGRAM_TOKEN)

grid_size = ATR_MIN * ATR_MULT
last_atr = ATR_MIN
last_atr_update = None
notified_saldo = False
fee_rate = binance.load_markets()[SYMBOL]['taker']

async def send(msg):
    try: await tg_bot.send_message(chat_id=CHAT_ID, text=msg)
    except: pass

async def get_atr():
    global last_atr, last_atr_update, grid_size
    ohlcv = binance.fetch_ohlcv(SYMBOL, ATR_TF, limit=ATR_PERIOD+1)
    trs = [max(c[2]-c[3], abs(c[2]-c[4]), abs(c[3]-c[4])) for c in ohlcv[1:]]
    atr = (sum(trs)/len(trs)) * ATR_MULT
    atr = max(ATR_MIN, min(atr, ATR_MAX))
    grid_size = atr # GRID SEKARANG = ATR LANGSUNG

    now = datetime.utcnow()
    if last_atr_update and abs(atr - last_atr)/last_atr > ATR_SPIKE:
        await send(f"📊 ATR SPIKE\n{last_atr:.2f} -> {atr:.2f}")
    if now.hour == ATR_UPDATE_HOUR and (not last_atr_update or now.date()!= last_atr_update.date()):
        await send(f"📊 ATR UPDATE\nATR: {atr:.2f}")
    last_atr, last_atr_update = atr, now
    return atr

async def place_buy(price):
    global notified_saldo
    qty = max(MIN_QTY, MIN_USDT / price)
    cost_modal = price * qty
    fee_buy = cost_modal * fee_rate
    fee_sell = (price + grid_size) * qty * fee_rate # estimasi fee sell di +1 grid
    cost_total = (cost_modal + fee_buy + fee_sell) * BUFFER

    balance = binance.fetch_balance()['USDT']['free']
    if balance < cost_total:
        if not notified_saldo:
            await send(f"⚠️ SALDO KURANG\nButuh: ${cost_total:.2f}\nAda: ${balance:.2f}")
            notified_saldo = True
        return None
    if notified_saldo:
        await send(f"✅ LANJUT BERDAGANG\nSaldo: ${balance:.2f}")

    order = binance.create_limit_buy_order(SYMBOL, qty, price)
    await send(f"🟢 BUY @{price:.2f}\nQty: {qty:.5f}")
    notified_saldo = False
    return order

async def main():
    global grid_size
    await send("🤖 BOT GRID ATR MURNI START")
    while True:
        try:
            await get_atr()
            price = binance.fetch_ticker(SYMBOL)['last']
            orders = binance.fetch_open_orders(SYMBOL)
            buys = [o for o in orders if o['side']=='buy']

            # ATURAN BUY: turun 1 grid
            if not buys:
                buy_price = round(price / grid_size) * grid_size
                await place_buy(buy_price)
            else:
                lowest = min(buys, key=lambda x: x['price'])
                if price <= lowest['price'] - grid_size:
                    await place_buy(lowest['price'] - grid_size)

            # ATURAN SELL: naik 1 grid dari harga buy
            for b in buys:
                if b['status'] == 'closed':
                    sell_price = b['price'] + grid_size # NAIK 1 GRID
                    sell_price = round(sell_price, 2)
                    binance.create_limit_sell_order(SYMBOL, b['amount'], sell_price)
                    await send(f"🔴 SELL @{sell_price:.2f}\n+1 GRID dari buy")
                    # REENTRY di harga sell
                    await place_buy(sell_price)

            await asyncio.sleep(1)
        except Exception as e:
            print("Error:", e); await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
