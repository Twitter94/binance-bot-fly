import os, asyncio
import ccxt.async_support as ccxt
from supabase import create_client
import aiohttp
from datetime import datetime, timezone

# === CONFIG DARI SECRETS FLY ===
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")

SYMBOL = "BTC/USDT"
QTY = 0.00001 # ATUR INI. Modal per grid
BUFFER = 1.005 # 0.2%
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
    if msg in notif_sent: return # Notif 1x aja
    url = f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage"
    async with aiohttp.ClientSession() as s:
        await s.post(url, json={"chat_id": TELE_CHAT_ID, "text": msg})
    notif_sent.add(msg)

async def get_atr():
    ohlcv = await binance.fetch_ohlcv(SYMBOL, ATR_TF, limit=ATR_PERIOD+1)
    trs = [max(c[2]-c[3], abs(c[2]-c[4]), abs(c[3]-c[4])) for c in ohlcv[1:]]
    atr = (sum(trs)/len(trs)) * ATR_MULT
    return max(ATR_MIN, min(atr, ATR_MAX)) # clamp 250-1000

async def get_state():
    res = supa.table("grid_state").select("*").eq("id", 1).single().execute()
    return res.data if res.data else {"grid": []}

async def save_state(grid, price, atr):
    supa.table("grid_state").upsert({
        "id": 1, "grid": grid, "last_price": price, "atr": atr, "updated_at": "now()"
    }).execute()

async def sync_binance(grid):
    open_orders = await binance.fetch_open_orders(SYMBOL)
    binance_ids = {o['id'] for o in open_orders}
    db_ids = {g['order_id'] for g in grid}

    # 5. Sync: Binance kosong -> hapus supa. Supa kosong -> isi dari binance
    if not open_orders and grid:
        await save_state([], 0, 0)
        await notif("🗑️ Reset: Binance kosong, Supabase dihapus")
        return []
    if open_orders and not grid:
        new_grid = [{"price": o['price'], "order_id": o['id'], "side": o['side']} for o in open_orders]
        await notif("🔄 Sync: Ambil data dari Binance")
        return new_grid
    return grid

async def place_buy(price, fee):
    usdt = (await binance.fetch_balance())['USDT']['free']
    modal = price * QTY * (1 + fee*2) * BUFFER
    if usdt < modal:
        if 'low' not in notif_sent:
            await notif(f"⚠️ SALDO KURANG. Butuh: ${modal:.2f} | Ada: ${usdt:.2f}")
            notif_sent.add('low')
        return None
    order = await binance.create_limit_buy_order(SYMBOL, QTY, price)
    await notif(f"🟢 BUY 1 @ ${price:.2f}")
    return {"price": price, "order_id": order['id'], "side": "buy"}

async def place_sell_and_reentry(buy_price, sell_price):
    await binance.create_market_sell_order(SYMBOL, QTY)
    order = await binance.create_limit_buy_order(SYMBOL, QTY, sell_price)
    await notif(f"🔴 SELL+REENTRY 1 | Sell@${sell_price:.2f} Buy@${sell_price:.2f}")
    return {"price": sell_price, "order_id": order['id'], "side": "buy"}

async def main():
    global last_atr
    await notif("🚀 BOT START: Infinite Grid 1 per 1")

    while True:
        try:
            price = (await binance.fetch_ticker(SYMBOL))['last']
            atr = await get_atr()
            fee = (await binance.load_markets())[SYMBOL]['taker']
            grid_size = atr * 0.5

            state = await get_state()
            grid = await sync_binance(state['grid'])

            # 1. ATR UPDATE JAM 00.00
            hour = datetime.now(timezone.utc).hour
            if hour == 0: await notif(f"📊 ATR UPDATE: ${atr:.2f}")

            # 2. ATR SPIKE 20%
            if last_atr > 0 and abs(atr-last_atr)/last_atr > 0.2:
                await notif(f"📈 ATR SPIKE 20%! ${last_atr:.2f} -> ${atr:.2f}")
            last_atr = atr

            # === LOGIC BUY 1 PER 1 ===
            if not grid: # START AWAL FLEKSIBEL
                buy_price = round(price / grid_size) * grid_size
                new_order = await place_buy(buy_price, fee)
                if new_order: grid.append(new_order)
            else:
                buys = sorted([g for g in grid if g['side']=='buy'], key=lambda x: x['price'])
                last_buy_price = buys[0]['price']
                next_buy_price = last_buy_price - grid_size
                if price <= next_buy_price: # Turun 1 grid baru buy
                    new_order = await place_buy(next_buy_price, fee)
                    if new_order: grid.append(new_order)

            # === LOGIC SELL + REENTRY 1 PER 1 ===
            buys = sorted([g for g in grid if g['side']=='buy'], key=lambda x: x['price'], reverse=True)
            for buy in buys: # Cek dari atas
                sell_price = buy['price'] * BUFFER

                # 4. SELL INSTAN KALO LONJAKAN > 1 GRID
                if price > buy['price'] + grid_size:
                    new_order = await place_sell_and_reentry(buy['price'], price)
                    grid = [g for g in grid if g['order_id']!=buy['order_id']]
                    grid.append(new_order)
                    break

                if price >= sell_price:
                    ada_buy_di_sell = any(abs(g['price']-sell_price)<1 and g['side']=='buy' for g in grid)
                    if ada_buy_di_sell: # Sell normal
                        await binance.create_limit_sell_order(SYMBOL, QTY, sell_price)
                    else: # Sell + Reentry
                        new_order = await place_sell_and_reentry(buy['price'], sell_price)
                        grid.append(new_order)
                    grid = [g for g in grid if g['order_id']!=buy['order_id']]
                    break # Cuma proses 1 per detik

            await save_state(grid, price, atr)
            await asyncio.sleep(1) # PANTAU PER DETIK

        except Exception as e:
            await notif(f"❌ ERROR: {e}")
            await asyncio.sleep(5)

asyncio.run(main())
