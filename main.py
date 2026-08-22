import asyncio, ccxt, os
from datetime import datetime
from supabase import create_client
from telegram import Bot

# INI AMBIL DARI 8 NAMA DI FLY KAMU
API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
MIN_USDT = float(os.getenv('MIN_NOTIONAL_ENV', 5))
SYMBOL = os.getenv('PAIR', 'BTC/USDT')
SUPABASE_URL = os.getenv('SUPA_URL')
SUPABASE_KEY = os.getenv('SUPA_KEY')
TELEGRAM_TOKEN = os.getenv('TELE_TOKEN')
CHAT_ID = os.getenv('TELE_CHAT_ID')

# CONFIG
ATR_PERIOD, ATR_TF, ATR_MULT = 14, '1h', 0.5
ATR_MIN, ATR_MAX = 250, 1000
MIN_QTY = 0.00001

binance = ccxt.binance({'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True})
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
tg_bot = Bot(TELEGRAM_TOKEN)

async def send_telegram(msg):
    await tg_bot.send_message(chat_id=CHAT_ID, text=msg)

async def main():
    await send_telegram("BOT JALAN - 8 ENV SUDAH TERBACA")
    print("API_KEY:", "ADA" if API_KEY else "KOSONG")
    print("PAIR:", SYMBOL)
    while True:
        await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
