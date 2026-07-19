import os
from fastapi import FastAPI
from binance.client import Client

app = FastAPI()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")

client = Client(API_KEY, API_SECRET)

@app.get("/")
def root():
    return {"status": "ok", "message": "Bot Binance REAL Jalan"}

@app.get("/balance")
def get_balance():
    balance = client.get_asset_balance(asset='USDT')
    return {"USDT": balance}

def market_sell(symbol: str):
    try:
        order = client.order_market_sell(symbol=symbol)
        return order
    except Exception as e:
        return {"error": str(e)}
