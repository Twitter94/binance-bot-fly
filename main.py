import asyncio, ccxt.async_support as ccxt, os, gc
from datetime import datetime, timezone
from supabase import create_client
from telegram import Bot

# ===== ENV =====
API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
SYMBOL = 'BTC/USDT'
MIN_USDT = 5.0
MIN_QTY = 0.00001
BUFFER = 1.005

SUPABASE_URL = os.getenv('SUPA_URL')
SUPABASE_KEY = os.getenv('SUPA_KEY')
TELEGRAM_TOKEN = os.getenv('TELE_TOKEN')
CHAT_ID = os.getenv('TELE_CHAT_ID')

# ===== ATR SETTING =====
ATR_PERIOD = 14
ATR_TF = '1h'
ATR_MULT = 0.5
ATR_UPDATE_HOUR = 0
ATR_SPIKE = 0.20

binance = ccxt.binance({'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
tg_bot = Bot(TELEGRAM_TOKEN)

grid_size = 250
fee_rate = 0.001
notified_saldo = False
last_atr = 250
last_atr_update = None

async def send(msg):
    try: await tg_bot.send_message(chat_id=CHAT_ID, text=msg)
    except: pass

async def get_atr():
    global grid_size, last_atr, last_atr_update
    ohlcv = await binance.fetch_ohlcv(SYMBOL, ATR_TF, limit=ATR_PERIOD+1)
    trs = [max(c[2]-c[3], abs(c[2]-c[4]), abs(c[3]-c[4])) for c in ohlcv[1:]]
    atr = (sum(trs)/len(trs)) * ATR_MULT
    atr = max(250, min(atr, 1000))

    now = datetime.now(timezone.utc)
    if last_atr_update and abs(atr - last_atr)/last_atr > ATR_SPIKE: # ATURAN 1.4
        await send(f"📊 ATR SPIKE 20%\n{last_atr:.2f} -> {atr:.2f}")
    if now.hour == ATR_UPDATE_HOUR and (not last_atr_update or now.date()!= last_atr_update.date()): # ATURAN 1.3
        await send(f"📊 ATR UPDATE JAM 00\nATR: {atr:.2f}")
    grid_size, last_atr, last_atr_update = atr, atr, now
    gc.collect()

async def supa_load(): # ATURAN 4
    res = supabase.table('grid_orders').select('*').eq('symbol', SYMBOL).execute()
    return {r['price']: r for r in res.data}

async def supa_save(price, side):
    supabase.table('grid_orders').upsert({'symbol': SYMBOL, 'price': price, 'side': side}).execute()

async def supa_delete(price):
    supabase.table('grid_orders').delete().eq('symbol', SYMBOL).eq('price', price).execute()

async def sync_data(binance_orders): # ATURAN 5
    supa = await supa_load()
    bin_prices = {o['price'] for o in binance_orders}
    sup_prices = set(supa.keys())
    for p in sup_prices - bin_prices: await supa_delete(p)
    for p in bin_prices - sup_prices:
        side = 'buy' if any(o['price']==p and o['side']=='buy' for o in binance_orders) else 'sell'
        await supa_save(p, side)

async def hitung_modal(price): # ATURAN MODAL 1 & 2
    qty = max(MIN_QTY, MIN_USDT / price)
    modal = price * qty
    fee = modal * fee_rate * 2
    return (modal + fee) * BUFFER, qty

async def place_buy(price): # ATURAN MODAL
    global notified_saldo
    cost, qty = await hitung_modal(price)
    balance = (await binance.fetch_balance())['USDT']['free']
    if balance < cost: # ATURAN MODAL 1
        if not notified_saldo:
            await send(f"⚠️ SALDO KURANG\nButuh: ${cost:.2f}\nAda: ${balance:.2f}")
            notified_saldo = True
        return None
    if notified_saldo: # ATURAN NOTIF 5
        await send(f"✅ LANJUT BERDAGANG\nSaldo: ${balance:.2f}")
        notified_saldo = False

    order = await binance.create_limit_buy_order(SYMBOL, qty, price)
    await supa_save(price, 'buy')
    await send(f"🟢 BUY @{price:.2f}\nQty: {qty:.5f}") # ATURAN NOTIF 3
    return order

async def main():
    await binance.load_markets()
    global fee_rate; fee_rate = binance.markets[SYMBOL]['taker']
    await get_atr()
    await send("🤖 BOT INFINITE GRID ATR START")

    orders = await binance.fetch_open_orders(SYMBOL) # ATURAN 4: SINGKRON AWAL
    await sync_data(orders)

    while True:
        try:
            await get_atr()
            price = (await binance.fetch_ticker(SYMBOL))['last']
            orders = await binance.fetch_open_orders(SYMBOL)
            await sync_data(orders)

            buys = [o for o in orders if o['side']=='buy']
            # Cek order yg baru keisi
            closed = await binance.fetch_closed_orders(SYMBOL, limit=10)
            closed_buys = [o for o in closed if o['side']=='buy' and o['status']=='closed']

            # ATURAN BUY 3: START AWAL FLEKSIBEL
            if not buys and not closed_buys:
                buy_price = round(price / grid_size) * grid_size
                await place_buy(buy_price)

            # ATURAN BUY 6: CUMA MENURUN
            if buys:
                lowest = min(buys, key=lambda x: x['price'])
                if price <= lowest['price'] - grid_size:
                    await place_buy(lowest['price'] - grid_size)

            # ATURAN SELL + REENTRY 1 2 3
            for b in closed_buys:
                buy_price = b['price']
                sell_price = round(buy_price + grid_size, 2)
                ada_buy_di_sell = any(abs(o['price'] - sell_price) < 1 for o in buys)
                if not ada_buy_di_sell:
                    await binance.create_limit_sell_order(SYMBOL, b['amount'], sell_price)
                    await supa_delete(buy_price); await supa_save(sell_price, 'sell')
                    await send(f"🔴 SELL @{sell_price:.2f}\n+1 GRID") # ATURAN NOTIF 3
                    await place_buy(sell_price)

            # ATURAN SELL 4: SELL INSTAN >1 GRID
            if buys:
                highest_buy = max(buys, key=lambda x: x['price'])
                if price > highest_buy['price'] + grid_size:
                    for b in buys:
                        if b['price'] < price - grid_size:
                            sell_p = round(b['price'] + grid_size, 2)
                            await binance.create_limit_sell_order(SYMBOL, b['amount'], sell_p)
                            await supa_delete(b['price'])
                            await send(f"⚡ SELL INSTAN @{sell_p:.2f}\nLonjakan >1 Grid")

            gc.collect()
            await asyncio.sleep(1) # TUGAS FLY: PANTAU PER DETIK, DIEM KALAU GAK ADA APA2
        except Exception as e:
            print("Error:", e); await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
