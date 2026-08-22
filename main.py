import asyncio, ccxt.async_support as ccxt, os, gc
from datetime import datetime, timezone
import httpx

# ====== SETTING KAMU ======
API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
SYMBOL = 'BTC/USDT'
MIN_USDT = 5.0
MIN_QTY = 0.00001
BUFFER = 1.005

SUPA_URL = os.getenv('SUPA_URL')
SUPA_KEY = os.getenv('SUPA_KEY')
TELE_TOKEN = os.getenv('TELE_TOKEN')
CHAT_ID = os.getenv('TELE_CHAT_ID')

ATR_PERIOD, ATR_TF, ATR_MULT = 14, '1h', 0.5
ATR_UPDATE_HOUR, ATR_SPIKE = 0, 0.20
# ==========================

binance = ccxt.binance({'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
headers_supa = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json"}
tele_url = f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage"

grid_size, fee_rate = 250, 0.001
notified_saldo, last_atr, last_atr_update = False, 250, None

async def send(msg):
    async with httpx.AsyncClient(timeout=10) as client:
        try: await client.post(tele_url, json={"chat_id": CHAT_ID, "text": msg})
        except: pass

async def get_atr():
    global grid_size, last_atr, last_atr_update
    ohlcv = await binance.fetch_ohlcv(SYMBOL, ATR_TF, limit=ATR_PERIOD+1)
    trs = [max(c[2]-c[3], abs(c[2]-c[4]), abs(c[3]-c[4])) for c in ohlcv[1:]]
    atr = (sum(trs)/len(trs)) * ATR_MULT
    atr = max(250, min(atr, 1000))
    now = datetime.now(timezone.utc)
    if last_atr_update and abs(atr - last_atr)/last_atr > ATR_SPIKE: await send(f"📊 ATR SPIKE 20%\n{last_atr:.2f} -> {atr:.2f}")
    if now.hour == ATR_UPDATE_HOUR and (not last_atr_update or now.date()!= last_atr_update.date()): await send(f"📊 ATR UPDATE JAM 00\nATR: {atr:.2f}")
    grid_size, last_atr, last_atr_update = atr, atr, now
    gc.collect()

async def supa_req(method, url, json=None):
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.request(method, url, headers=headers_supa, json=json)
        return res.json() if res.text else []

async def supa_load():
    data = await supa_req("GET", f"{SUPA_URL}/rest/v1/grid_orders?symbol=eq.{SYMBOL}&select=*")
    return {r['price']: r for r in data}

async def supa_save(price, side):
    await supa_req("POST", f"{SUPA_URL}/rest/v1/grid_orders", {"symbol": SYMBOL, "price": price, "side": side})

async def supa_delete(price):
    await supa_req("DELETE", f"{SUPA_URL}/rest/v1/grid_orders?symbol=eq.{SYMBOL}&price=eq.{price}")

async def sync_data(binance_orders):
    supa = await supa_load()
    bin_prices = {o['price'] for o in binance_orders}
    sup_prices = set(supa.keys())
    for p in sup_prices - bin_prices: await supa_delete(p)
    for p in bin_prices - sup_prices:
        side = 'buy' if any(o['price']==p and o['side']=='buy' for o in binance_orders) else 'sell'
        await supa_save(p, side)

async def hitung_modal(price):
    qty = max(MIN_QTY, MIN_USDT / price)
    modal = price * qty
    fee = modal * fee_rate * 2
    return (modal + fee) * BUFFER, qty

async def place_buy(price):
    global notified_saldo
    cost, qty = await hitung_modal(price)
    balance = (await binance.fetch_balance())['USDT']['free']
    if balance < cost:
        if not notified_saldo: await send(f"⚠️ SALDO KURANG\nButuh: ${cost:.2f}\nAda: ${balance:.2f}"); notified_saldo = True
        return None
    if notified_saldo: await send(f"✅ LANJUT BERDAGANG\nSaldo: ${balance:.2f}"); notified_saldo = False
    await binance.create_limit_buy_order(SYMBOL, qty, price)
    await supa_save(price, 'buy')
    await send(f"🟢 BUY @{price:.2f}\nQty: {qty:.5f}")

async def main():
    await binance.load_markets()
    global fee_rate; fee_rate = binance.markets[SYMBOL]['taker']
    await send(f"🤖 BOT START\nFee: {fee_rate*100:.3f}%")
    await get_atr()
    orders = await binance.fetch_open_orders(SYMBOL)
    await sync_data(orders)

    while True:
        try:
            await get_atr()
            price = (await binance.fetch_ticker(SYMBOL))['last']
            orders = await binance.fetch_open_orders(SYMBOL)
            await sync_data(orders)
            buys = [o for o in orders if o['side']=='buy']
            closed = await binance.fetch_closed_orders(SYMBOL, limit=10)
            closed_buys = [o for o in closed if o['side']=='buy' and o['status']=='closed']

            if not buys and not closed_buys: await place_buy(round(price / grid_size) * grid_size)
            if buys:
                lowest = min(buys, key=lambda x: x['price'])
                if price <= lowest['price'] - grid_size: await place_buy(lowest['price'] - grid_size)

            for b in closed_buys:
                sell_price = round(b['price'] + grid_size, 2)
                ada_buy_di_sell = any(abs(o['price'] - sell_price) < 1 for o in buys)
                if not ada_buy_di_sell:
                    await binance.create_limit_sell_order(SYMBOL, b['amount'], sell_price)
                    await supa_delete(b['price']); await supa_save(sell_price, 'sell')
                    await send(f"🔴 SELL @{sell_price:.2f}\n+1 GRID")
                    await place_buy(sell_price)

            if buys:
                highest_buy = max(buys, key=lambda x: x['price'])
                if price > highest_buy['price'] + grid_size:
                    for b in buys:
                        if b['price'] < price - grid_size:
                            sell_p = round(b['price'] + grid_size, 2)
                            await binance.create_limit_sell_order(SYMBOL, b['amount'], sell_p)
                            await supa_delete(b['price'])
                            await send(f"⚡ SELL INSTAN @{sell_p:.2f}")

            gc.collect()
            await asyncio.sleep(3)
        except Exception as e:
            print("Error:", e); await asyncio.sleep(5)

if __name__ == "__main__": asyncio.run(main())
