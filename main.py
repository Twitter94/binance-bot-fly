from fastapi import FastAPI, HTTPException
from binance.client import Client
from binance.exceptions import BinanceAPIException
import os

app = FastAPI(title="Binance Bot")

# Ambil dari Fly Secrets. Jangan taruh key disini
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")

# testnet=True = pake duit palsu. Kalau mau real ganti False
client = Client(API_KEY, API_SECRET, testnet=True)

@app.get("/")
def home():
    return {"status": "ok", "message": "Bot Binance Jalan"}

@app.get("/balance")
def get_balance():
    try:
        return client.get_account()
    except BinanceAPIException as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/buy")
def market_buy(symbol: str, quantity: float):
    try:
        order = client.order_market_buy(symbol=symbol, quantity=quantity)
        return {"status": "success", "order": order}
    except BinanceAPIException as e:
        raise HTTPException(status_code=400, detail
