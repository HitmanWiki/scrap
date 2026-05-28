"""
Sniper Service - Uses Jupiter API with fallbacks
"""

import asyncio
import base58
import base64
import json
import requests
import time
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.token.associated import get_associated_token_address
from solders import message
from typing import Optional


class SimpleRpcClient:
    """Simple RPC client using requests"""
    
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
    
    def _rpc_call(self, method: str, params: list) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        }
        try:
            response = requests.post(self.rpc_url, json=payload, timeout=30)
            return response.json()
        except Exception as e:
            return {"error": {"message": str(e)}}
    
    def get_token_balance(self, token_account: str) -> Optional[float]:
        result = self._rpc_call("getTokenAccountBalance", [token_account])
        if 'error' in result:
            return None
        if 'result' in result and result['result']:
            val = result['result']
            if isinstance(val, dict):
                return float(val.get('value', {}).get('uiAmount', 0))
        return None
    
    def send_raw_transaction(self, tx_bytes: bytes) -> str:
        tx_base58 = base58.b58encode(tx_bytes).decode()
        result = self._rpc_call("sendTransaction", [tx_base58, {"encoding": "base58"}])
        if 'error' in result:
            raise Exception(result['error'].get('message', 'Send failed'))
        return result['result']


class SniperService:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = SimpleRpcClient(rpc_url)
    
    async def get_token_decimals(self, token_mint: str) -> int:
        try:
            result = self.client._rpc_call("getMint", [token_mint])
            if 'result' in result and result['result']:
                return result['result'].get('decimals', 9)
        except:
            pass
        return 9
    
    async def get_token_price(self, token_mint: str) -> Optional[float]:
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('pairs') and len(data['pairs']) > 0:
                    return float(data['pairs'][0].get('priceUsd', 0))
        except:
            pass
        return None
    
    async def execute_buy(self, wallet: Keypair, token_mint: str, amount_sol: float, slippage_bps: int) -> dict:
        """Execute buy using Jupiter API"""
        try:
            print(f"   💰 Amount: {amount_sol} SOL | Slippage: {slippage_bps/100}%")
            
            # Use the main Jupiter API with timeout
            quote_url = "https://quote-api.jup.ag/v6/quote"
            params = {
                "inputMint": "So11111111111111111111111111111111111111112",
                "outputMint": token_mint,
                "amount": int(amount_sol * 10**9),
                "slippageBps": slippage_bps,
            }
            
            print(f"   Getting quote...")
            try:
                resp = requests.get(quote_url, params=params, timeout=15)
                if resp.status_code != 200:
                    return {"success": False, "error": f"Quote failed: HTTP {resp.status_code}"}
                quote = resp.json()
            except requests.exceptions.Timeout:
                return {"success": False, "error": "Quote timeout - try again"}
            except Exception as e:
                return {"success": False, "error": f"Quote error: {str(e)}"}
            
            # Build swap
            swap_url = "https://quote-api.jup.ag/v6/swap"
            payload = {
                "quoteResponse": quote,
                "userPublicKey": str(wallet.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto"
            }
            
            print(f"   Building transaction...")
            try:
                resp = requests.post(swap_url, json=payload, timeout=15)
                if resp.status_code != 200:
                    return {"success": False, "error": f"Swap failed: HTTP {resp.status_code}"}
                swap_data = resp.json()
            except requests.exceptions.Timeout:
                return {"success": False, "error": "Swap timeout - try again"}
            except Exception as e:
                return {"success": False, "error": f"Swap error: {str(e)}"}
            
            if "swapTransaction" not in swap_data:
                return {"success": False, "error": "No swapTransaction in response"}
            
            # Sign and send
            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            raw_tx = VersionedTransaction.from_bytes(tx_bytes)
            message_bytes = message.to_bytes_versioned(raw_tx.message)
            signature = wallet.sign_message(message_bytes)
            signed_tx = VersionedTransaction.populate(raw_tx.message, [signature])
            
            print(f"   Sending transaction...")
            try:
                txid = self.client.send_raw_transaction(bytes(signed_tx))
                print(f"   ✅ TXID: {txid}")
            except Exception as e:
                return {"success": False, "error": f"Send failed: {str(e)}"}
            
            # Get token amount
            tokens_bought = 0
            await asyncio.sleep(5)
            
            try:
                ata = get_associated_token_address(wallet.pubkey(), Pubkey.from_string(token_mint))
                balance = self.client.get_token_balance(str(ata))
                if balance:
                    tokens_bought = balance
            except:
                pass
            
            return {
                "success": True,
                "txid": txid,
                "tokens_bought": tokens_bought,
                "price": amount_sol / tokens_bought if tokens_bought > 0 else 0,
                "explorer": f"https://solscan.io/tx/{txid}"
            }
            
        except Exception as e:
            print(f"   ❌ Buy failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def execute_sell(self, wallet: Keypair, token_mint: str, amount_tokens: float, slippage_bps: int) -> dict:
        """Execute sell"""
        try:
            decimals = await self.get_token_decimals(token_mint)
            amount_raw = int(amount_tokens * 10**decimals)
            
            quote_url = "https://quote-api.jup.ag/v6/quote"
            params = {
                "inputMint": token_mint,
                "outputMint": "So11111111111111111111111111111111111111112",
                "amount": amount_raw,
                "slippageBps": slippage_bps,
            }
            
            resp = requests.get(quote_url, params=params, timeout=15)
            if resp.status_code != 200:
                return {"success": False, "error": f"Quote failed: HTTP {resp.status_code}"}
            quote = resp.json()
            
            swap_url = "https://quote-api.jup.ag/v6/swap"
            payload = {
                "quoteResponse": quote,
                "userPublicKey": str(wallet.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto"
            }
            
            resp = requests.post(swap_url, json=payload, timeout=15)
            if resp.status_code != 200:
                return {"success": False, "error": f"Swap failed: HTTP {resp.status_code}"}
            swap_data = resp.json()
            
            if "swapTransaction" not in swap_data:
                return {"success": False, "error": "No swapTransaction"}
            
            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            raw_tx = VersionedTransaction.from_bytes(tx_bytes)
            message_bytes = message.to_bytes_versioned(raw_tx.message)
            signature = wallet.sign_message(message_bytes)
            signed_tx = VersionedTransaction.populate(raw_tx.message, [signature])
            
            txid = self.client.send_raw_transaction(bytes(signed_tx))
            
            sol_received = 0
            try:
                out_amount = quote.get('outputAmount', '0')
                if out_amount != '0':
                    sol_received = int(out_amount) / 1e9
            except:
                pass
            
            return {
                "success": True,
                "txid": txid,
                "sol_received": sol_received,
                "explorer": f"https://solscan.io/tx/{txid}"
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}