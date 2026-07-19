from fastapi import FastAPI, HTTPException
from binance.client import Client
from binance.exceptions import BinanceAPIException
import os
import uvicorn

app = FastAPI(title="Binance Bot REAL")

# Ambil API dari Fly Secrets. JANGAN TARUH KEY DISINI
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")

# testnet=False = DUIT BENERAN
client = Client(API_KEY, API_SECRET, testnet=False)

@app.get("/")
def home():
    return {"status": "ok", "message": "Bot Binance REAL Jalan"}

@app.get("/balance")
def get_balance():
    try:
        account = client.get_account()
        return {"status": "success", "balance": account['balances']}
    except BinanceAPIException as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/buy")
def market_buy(symbol: str, quantity: float):
    try:
        order = client.order_market_buy(symbol=symbol, quantity=quantity)
        return {"status": "success", "order": order}
    except BinanceAPIException as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/sell")
def market_sell(symbol: str
