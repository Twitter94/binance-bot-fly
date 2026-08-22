import asyncio, ccxt, os
from datetime import datetime
from supabase import create_client
from telegram import Bot

# AMBIL DARI FLY
API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
MIN_USDT = float(os.getenv('MIN_NOTIONAL_ENV', 5))
SYMBOL = os.getenv('PAIR', 'BTC/USDT')
SUPABASE_URL = os.getenv('SUPA_URL')
SUPABASE_KEY = os.getenv('SUPA_KEY')
TELEGRAM_TOKEN = os.getenv('TELE_TOKEN')
CHAT_ID = os.getenv('TELE_CHAT_ID')

# CONFIG GRID
ATR_PERIOD, ATR_TF, ATR_MULT = 14, '1h', 0.5
ATR_MIN, ATR_MAX = 250, 1000
ATR_UPDATE_HOUR = 0
ATR_SPIKE = 0.20
MIN_QTY = 0.00001
BUFFER = 1.005
TP_PCT = 0.002

binance = ccxt.binance({'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True})
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
tg_bot = Bot(TELEGRAM_TOKEN)

grid_size = ATR_MIN * ATR_MULT
last_atr = ATR_MIN
last_atr_update = None
notified_saldo = False
last_price_saved = 0

async def send(msg):
    try: await tg_bot.send_message(chat_id=CHAT_ID, text=msg)
    except: pass

def get_state():
    res = supabase.table('grid_state').select('*').eq('symbol', SYMBOL).execute()
    return res.data[0] if res.data else None

def save_state(data):
    data['symbol'] = SYMBOL
    supabase.table('grid_state').upsert(data).execute()

async def sync():
    binance_orders = binance.fetch_open_orders(SYMBOL)
    state = get_state()
    if not binance_orders and state:
        supabase.table('grid_state').delete().eq('symbol', SYMBOL).execute()
        await send("♻️ RESET: Data Binance kosong, hapus Supabase")
        return None
    if binance_orders and not state:
        save_state({"last_price": binance.fetch_ticker(SYMBOL)['last']})
        await send("🔄 SYNC: Isi Supabase dari Binance")
        return get_state()
    return state

async def get_atr():
    global last_atr, last_atr_update
    ohlcv = binance.fetch_ohlcv(SYMBOL, ATR_TF, limit=ATR_PERIOD+1)
    trs = [max(c[2]-c[3], abs(c[2]-c[4]), abs(c[3]-c[4])) for c in ohlcv[1:]]
    atr = (sum(trs)/len(trs)) * ATR_MULT
    atr = max(ATR_MIN, min(atr, ATR_MAX))
    now = datetime.utcnow()
    if last_atr_update and abs(atr - last_atr)/last_atr > ATR_SPIKE:
        await send(f"📊 ATR SPIKE\n{last_atr:.2f} -> {atr:.2f}")
    if now.hour == ATR_UPDATE_HOUR and (not last_atr_update or now.date()!= last_atr_update.date()):
        await send(f"📊 ATR UPDATE\nATR: {atr:.2f}")
    last_atr, last_atr_update = atr, now
    return atr

async def place_buy(price):
    global notified_saldo
    fee = binance.load_markets()[SYMBOL]['taker']
    qty = max(MIN_QTY, MIN_USDT / price)
    cost = price * qty * BUFFER * (1 + fee*2)
    balance = binance.fetch_balance()['USDT']['free']

    if balance < cost:
        if not notified_saldo:
            await send(f"⚠️ SALDO KURANG\nButuh: ${cost:.2f} | Ada: ${balance:.2f}")
            notified_saldo = True
        return None

    if notified_saldo and balance >= cost: # INI NOTIF LANJUT BERDAGANG
        await send(f"✅ LANJUT BERDAGANG\nSaldo: ${balance:.2f}")

    binance.create_limit_buy_order(SYMBOL, qty, price)
    await send(f"🟢 BUY @{price:.2f} | Qty: {qty:.5f}")
    notified_saldo = False
    return True

async def check_sell_reentry(buys):
    for b in buys:
        if b['status'] == 'closed':
            sell_price = b['price'] * (1 + TP_PCT) * BUFFER
            conflict = any(o['price'] >= sell_price for o in buys)
            if not conflict:
                binance.create_limit_sell_order(SYMBOL, b['amount'], sell_price)
                await send(f"🔴 SELL + REENTRY @{sell_price:.2f}")
                await place_buy(sell_price)

async def check_instant_sell(price):
    global last_price_saved
    if price > last_price_saved + grid_size:
        orders = binance.fetch_open_orders(SYMBOL)
        for o in orders:
            if o['side'] == 'buy' and o['price'] < price - grid_size:
                binance.cancel_order(o['id'], SYMBOL)
                binance.create_market_sell_order(SYMBOL, o['amount'])
                await send(f"⚡ INSTANT SELL @{price:.2f}")

async def main():
    global grid_size, last_price_saved
    await send("🤖 BOT GRID START")
    await sync()

    while True:
        try:
            grid_size = await get_atr() * ATR_MULT
            price = binance.fetch_ticker(SYMBOL)['last']

            await check_instant_sell(price) # Cek lonjakan

            orders = binance.fetch_open_orders(SYMBOL)
            buys = [o for o in orders if o['side']=='buy']

            if not buys: # Buy pertama
                buy_price = round(price / grid_size) * grid_size
                await place_buy(buy_price)
            else: # Buy turun
                lowest = min(buys, key=lambda x: x['price'])
                if price <= lowest['price'] - grid_size:
                    await place_buy(lowest['price'] - grid_size)

            await check_sell_reentry(orders) # Cek sell

            last_price_saved = price
            save_state({"last_price": price})
            await asyncio.sleep(1)

        except Exception as e:
            print("Error:", e); await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
