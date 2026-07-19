from fastapi import FastAPI, HTTPException
from binance.client import Client
from binance.exceptions import BinanceAPIException
import os

app = FastAPI(title="Binance Bot")

API_KEY = os.getenv("qJHn1V12hdrDqWtC0D1bquYenStwcM1L4P1tgOMJTRw9RkGNZe1RkxQSVCiB07Sb)
API_SECRET = os.getenv("K2lXPQxb3JQ1kd8m6GcrJI6fAjLjO5bkg0ti8Ahhl92khQS1jo8uvgsvS2suP0c5")
client = Client(API_KEY, API_SECRET, testnet=True)

@app.get("/")
def home():
    return {"message": "Bot Binance Jalan", "region": "DOSA"}

@app.post("/buy")
def market_buy(symbol: str, quantity: float):
    try:
        order = client.order_market_buy(symbol=symbol, quantity=quantity)
        return {"status": "success", "order": order}
    except BinanceAPIException as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/balance")
def get_balance():
    return client.get_account()
