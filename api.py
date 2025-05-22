from typing import List, Optional, Dict, Any, Union
import asyncio
import json
import base64
import re
from fastapi import FastAPI, HTTPException
from fastapi import Request
import aiohttp
from aiohttp import web
import tonutils.client
import tonutils.wallet
from pydantic import BaseModel, validator, field_validator
from tonsdk.boc import Cell

MNEMONIC: List[str] = []

TONAPI_KEY = ""
FRAGMENT_HASH = ""
FRAGMENT_COOKIES = {
    "stel_token": "",
    "stel_ssid": "",
    "stel_ton_token": ""
}
FRAGMENT_HEADERS = {

    "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
}


def strip_html_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;?", " ", text)
    return text.strip()


def clean_and_filter(obj: Union[Dict, List, str, int, float, None]) -> Union[Dict, List, str, int, float, None]:
    if isinstance(obj, dict):
        new = {}
        for k, v in obj.items():
            if k.endswith("_html"):
                continue
            clean_v = clean_and_filter(v)
            new[k] = clean_v
        return new
    if isinstance(obj, list):
        return [clean_and_filter(v) for v in obj]
    if isinstance(obj, str):
        return strip_html_tags(obj)
    return obj


class WalletManager:
    def __init__(self, api_key: str, mnemonic: List[str]):
        self.api_key = api_key
        self.mnemonic = mnemonic
        self.ton_client: Optional[tonutils.client.TonapiClient] = None
        self.wallet = None

    async def init_wallet(self):
        self.ton_client = tonutils.client.TonapiClient(api_key=self.api_key)
        self.wallet, _, _, _ = tonutils.wallet.WalletV4R2.from_mnemonic(
            self.ton_client, mnemonic=self.mnemonic
        )

    async def transfer(self, address: str, amount: float, comment: str) -> Dict[str, Any]:
        result = {
            "address": address,
            "amount": amount,
            "comment": comment,
            "success": False,
            "tx_hash": None,
            "error": None
        }
        try:
            tx_hash = await self.wallet.transfer(
                destination=address,
                amount=amount,
                body=comment
            )
            result["success"] = True
            result["tx_hash"] = tx_hash
        except Exception as e:
            result["error"] = str(e)
        return result

    async def close(self):
        if self.ton_client and hasattr(self.ton_client, "_session"):
            await self.ton_client._session.close()


def decode_payload_b64(payload: str) -> str:
    try:
        payload += "=" * (-len(payload) % 4)
        cell = Cell.one_from_boc(base64.b64decode(payload))
        sl = cell.begin_parse()
        return sl.read_string().strip()
    except Exception as e:
        return f"decode_error: {e}"


async def buy_stars_logic(login: str, quantity: int, hide_sender: int) -> Dict[str, Any]:
    wm = WalletManager(TONAPI_KEY, MNEMONIC)
    await wm.init_wallet()
    results: Dict[str, Any] = {}
    async with aiohttp.ClientSession(cookies=FRAGMENT_COOKIES, headers=FRAGMENT_HEADERS) as session:
        # Шаги 1–4
        steps = [
            ("updateStarsBuyState", {"mode": "new", "lv": "false", "dh": "1", "method": "updateStarsBuyState"}),
            ("searchStarsRecipient", {"query": login, "quantity": str(quantity), "method": "searchStarsRecipient"}),
            ("updateStarsPrices", {"stars": "", "quantity": str(quantity), "method": "updateStarsPrices"}),
            ("initBuyStarsRequest", {"recipient": None, "quantity": str(quantity), "method": "initBuyStarsRequest"}),
        ]
        for name, data in steps:
            if name == "initBuyStarsRequest":
                recipient = results["searchStarsRecipient"].get("found", {}).get("recipient")
                if not recipient:
                    break
                data["recipient"] = recipient
            async with session.post(f"https://fragment.com/api?hash={FRAGMENT_HASH}", data=data) as resp:
                raw = await resp.json()
            results[name] = clean_and_filter(raw)
            if name == "searchStarsRecipient" and "found" not in raw:
                await wm.close()
                return clean_and_filter(results)
            if name == "initBuyStarsRequest" and not raw.get("req_id"):
                await wm.close()
                return clean_and_filter(results)

        # Шаг 5 getBuyStarsLink
        req_id = results["initBuyStarsRequest"]["req_id"]
        account = ""
        device = {
            "platform": "browser",
            "appName": "telegram-wallet",
            "appVersion": "1",
            "maxProtocolVersion": 2,
            "features": ["SendTransaction", {"name": "SendTransaction", "maxMessages": 4, "extraCurrencySupported": True}]
        }
        data5 = {
            "account": json.dumps(account),
            "device": json.dumps(device),
            "transaction": "1",
            "id": req_id,
            "show_sender": str(hide_sender),
            "method": "getBuyStarsLink"
        }
        async with session.post(f"https://fragment.com/api?hash={FRAGMENT_HASH}", data=data5) as resp5:
            raw5 = await resp5.json()
        results["getBuyStarsLink"] = clean_and_filter(raw5)
        if not raw5.get("ok") or "transaction" not in raw5:
            await wm.close()
            return clean_and_filter(results)

        # Шаг 6 – переводы
        transfers = []
        for msg in raw5["transaction"].get("messages", []):
            addr = msg["address"]
            amount_ton = msg["amount"] / 1e9
            decoded = decode_payload_b64(msg.get("payload", ""))
            transfers.append(await wm.transfer(addr, amount_ton, decoded))
        results["transfers"] = transfers

    await wm.close()
    return clean_and_filter(results)


class BuyStarsRequest(BaseModel):
    username: str
    quantity: int
    hide_sender: int


    @field_validator("quantity")
    @classmethod
    def validate_quantity(cls, v):
        if v < 50:
            raise ValueError("Quantity must be at least 50")
        return v


    @field_validator("hide_sender")
    @classmethod
    def validate_hide_sender(cls, v):
        if v not in (0, 1):
            raise ValueError("hide_sender must be 0 or 1")
        return v


app = FastAPI()


@app.post("/api/buyStars")
async def handle_buy_stars(data: BuyStarsRequest):
    result = await buy_stars_logic(data.username, data.quantity, data.hide_sender)
    return result


