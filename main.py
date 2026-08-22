import os, asyncio, math
import ccxt.async_support as ccxt
from supabase import create_client
import aiohttp
from datetime import datetime, timezone

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")

SYMBOL = "BTC/USDT"
BASE_QTY = 0.00001
MIN_MODAL = 5.0
BUFFER = 1.005
ATR_PERIOD = 14
ATR_TF = "1h"
ATR_MULT = 0.5
ATR_MIN = 250
ATR_MAX = 1000

binance = ccxt.binance({'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True})
supa = create_client(SUPA_URL, SUPA_KEY)
notif_sent = set()
last_atr = 0

async def notif(msg):
    if msg in notif_sent: return
    url = f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage"
    async with aiohttp.ClientSession() as s:
        await s.post(url, json={"chat_id": TELE_CHAT_ID, "text": msg})
    notif_sent.add(msg)

async def get_atr():
    ohlcv = await binance.fetch_ohlcv(SYMBOL, ATR_TF, limit=ATR_PERIOD+1)
    trs = [max(c[2]-c[3], abs(c[2]-c[4]), abs(c[3]-c[4])) for c in ohlcv[1:]]
    atr = (sum(trs)/len(trs)) * ATR_MULT
    return max(ATR_MIN, min(atr, ATR_MAX))

async def get_state():
    res = supa.table("grid_state").select("*").eq("id", 1).single().execute()
    return res.data if res.data else {"grid": []}

async def save_state(grid, price, atr):
    supa.table("grid_state").upsert({"id": 1, "grid": grid, "last_price": price, "atr": atr}).execute()

async def sync_binance(grid):
    open_orders = await binance.fetch_open_orders(SYMBOL)
    if not open_orders and grid:
        await save_state([], 0, 0)
        await notif("Reset: Binance kosong")
        return []
    if open_orders and not grid:
        new_grid = [{"price": o['price'], "order_id": o['id'], "side": o['side'], "qty": float(o['amount'])} for o in open_orders]
        return new_grid
    return grid

async def hitung_qty(price):
    qty_modal = MIN_MODAL / price
    qty = max(BASE_QTY, qty_modal)
    return math.ceil(qty * 100000) / 100000

async def place_buy(price, fee):
    usdt = (await binance.fetch_balance())['USDT']['free']
    qty = await hitung_qty(price)
    modal = price * qty * (1 + fee*2) * BUFFER
    if usdt < modal: return None
    order = await binance.create_limit_buy_order(SYMBOL, qty, price)
    return {"price": price, "order_id": order['id'], "side": "buy", "qty": qty}

async def place_sell_and_reentry(buy, sell_price):
    qty = buy['qty']
    await binance.create_market_sell_order(SYMBOL, qty)
    order = await binance.create_limit_buy_order(SYMBOL, qty, sell_price)
    return {"price": sell_price, "order_id": order['id'], "side": "buy", "qty": qty}

async def main():
    global last_atr
    await notif("BOT START")
    while True:
        try:
            price = (await binance.fetch_ticker(SYMBOL))['last']
            atr = await get_atr()
            fee = (await binance.load_markets())[SYMBOL]['taker']
            grid_size = atr * 0.5
            state = await get_state()
            grid = await sync_binance(state['grid'])

            if datetime.now(timezone.utc).hour == 0: await notif(f"ATR: {atr:.2f}")
            if last_atr > 0 and abs(atr-last_atr)/last_atr > 0.2: await notif(f"ATR SPIKE")
            last_atr = atr

            if not grid:
                buy_price = round(price / grid_size) * grid_size
                new_order = await place_buy(buy_price, fee)
                if new_order: grid.append(new_order)
            else:
                buys = sorted([g for g in grid if g['side']=='buy'], key=lambda x: x['price'])
                last_buy_price = buys[0]['price']
                next_buy_price = last_buy_price - grid_size
                if price <= next_buy_price:
                    new_order = await place_buy(next_buy_price, fee)
                    if new_order: grid.append(new_order)

            buys = sorted([g for g in grid if g['side']=='buy'], key=lambda x: x['price'], reverse=True)
            for buy in buys:
                sell_price = buy['price'] * BUFFER
                if price > buy['price'] + grid_size:
                    new_order = await place_sell_and_reentry(buy, price)
                    grid = [g for g in grid if g['order_id']!=buy['order_id']]
                    grid.append(new_order)
                    break
                if price >= sell_price:
                    ada_buy_di_sell = any(abs(g['price']-sell_price)<1 and g['side']=='buy' for g in grid)
                    if not ada_buy_di_sell:
                        new_order = await place_sell_and_reentry(buy, sell_price)
                        grid.append(new_order)
                    grid = [g for g in grid if g['order_id']!=buy['order_id']]
                    break

            await save_state(grid, price, atr)
            await asyncio.sleep(1)

        except Exception as e:
            await asyncio.sleep(5)

asyncio.run(main())
