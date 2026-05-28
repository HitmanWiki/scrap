"""
Sniper Service - No solana package dependency
Uses only solders + requests for all Solana operations
"""

import asyncio
import base58
import base64
import json
import requests
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.token.associated import get_associated_token_address
from solders import message
from typing import Optional
from dataclasses import dataclass


@dataclass
class TxOpts:
    """Transaction options"""
    skip_preflight: bool = False
    preflight_commitment: str = "confirmed"
    max_retries: int = 3


class SimpleRpcClient:
    """Simple RPC client using requests - no solana package needed"""
    
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
    
    def _rpc_call(self, method: str, params: list) -> dict:
        """Make a raw RPC call to Solana"""
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
    
    def get_balance(self, pubkey: str, commitment: str = "confirmed") -> int:
        """Get SOL balance in lamports"""
        result = self._rpc_call("getBalance", [pubkey, {"commitment": commitment}])
        if 'error' in result:
            return 0
        return result.get('result', {}).get('value', 0)
    
    def get_token_balance(self, token_account: str, commitment: str = "confirmed") -> Optional[float]:
        """Get token balance in UI amount"""
        result = self._rpc_call("getTokenAccountBalance", [token_account, {"commitment": commitment}])
        if 'error' in result:
            return None
        if 'result' in result and result['result']:
            val = result['result']
            if isinstance(val, dict):
                return float(val.get('value', {}).get('uiAmount', 0))
        return None
    
    def send_raw_transaction(self, tx_bytes: bytes) -> str:
        """Send a raw signed transaction"""
        tx_base58 = base58.b58encode(tx_bytes).decode()
        result = self._rpc_call("sendTransaction", [tx_base58, {"encoding": "base58"}])
        if 'error' in result:
            raise Exception(result['error'].get('message', 'Send transaction failed'))
        return result['result']
    
    def get_transaction(self, txid: str) -> dict:
        """Get transaction details"""
        result = self._rpc_call("getTransaction", [
            txid,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
        ])
        return result.get('result', {})
    
    def get_account_info(self, pubkey: str) -> dict:
        """Get account info"""
        result = self._rpc_call("getAccountInfo", [pubkey, {"encoding": "base64"}])
        return result.get('result', {})


class SniperService:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = SimpleRpcClient(rpc_url)
    
    def _rpc_call(self, method: str, params: list) -> dict:
        """Direct RPC call"""
        return self.client._rpc_call(method, params)
    
    async def get_token_decimals(self, token_mint: str) -> int:
        """Get token decimals from RPC"""
        try:
            result = self._rpc_call("getMint", [token_mint])
            if 'result' in result and result['result']:
                decimals = result['result'].get('decimals', 9)
                print(f"   ℹ️ Token decimals: {decimals}")
                return decimals
        except Exception as e:
            print(f"   ⚠️ Could not fetch decimals: {e}")
        return 9
    
    async def get_token_price(self, token_mint: str) -> Optional[float]:
        """Get token price from Jupiter"""
        try:
            url = f"https://price.jup.ag/v4/price?ids={token_mint}"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if 'data' in data and token_mint in data['data']:
                    return float(data['data'][token_mint]['price'])
        except:
            pass
        return None
    
    async def is_holding_token(self, wallet_pubkey: Pubkey, token_mint: str) -> bool:
        """Check if wallet holds any amount of a token"""
        try:
            ata = get_associated_token_address(wallet_pubkey, Pubkey.from_string(token_mint))
            balance = self.client.get_token_balance(str(ata))
            return balance is not None and balance > 0
        except Exception as e:
            return False
    
    async def get_wallet_balance(self, wallet_pubkey: Pubkey) -> float:
        """Get SOL balance of a wallet in SOL"""
        try:
            balance_lamports = self.client.get_balance(str(wallet_pubkey))
            return balance_lamports / 1e9
        except:
            return 0
    
    async def get_token_balance(self, wallet_pubkey: Pubkey, token_mint: str) -> float:
        """Get token balance of a wallet"""
        try:
            ata = get_associated_token_address(wallet_pubkey, Pubkey.from_string(token_mint))
            balance = self.client.get_token_balance(str(ata))
            return balance if balance else 0
        except:
            return 0
    
    async def get_token_amount_from_tx(self, txid: str, wallet_pubkey: Pubkey, token_mint: str) -> float:
        """Extract token amount from transaction"""
        try:
            tx_detail = self.client.get_transaction(txid)
            if tx_detail:
                meta = tx_detail.get('meta', {})
                post_balances = meta.get('postTokenBalances', [])
                pre_balances = meta.get('preTokenBalances', [])
                wallet_str = str(wallet_pubkey)
                
                for post in post_balances:
                    if post.get('mint') == token_mint:
                        post_amount = float(post.get('uiTokenAmount', {}).get('uiAmount', 0))
                        pre_amount = 0
                        for pre in pre_balances:
                            if pre.get('mint') == token_mint and pre.get('owner') == wallet_str:
                                pre_amount = float(pre.get('uiTokenAmount', {}).get('uiAmount', 0))
                        return post_amount - pre_amount
            return 0
        except Exception as e:
            print(f"   ⚠️ Tx parse error: {e}")
            return 0
    
    async def execute_buy(self, wallet: Keypair, token_mint: str, amount_sol: float, slippage_bps: int) -> dict:
        """Execute a buy transaction using Jupiter Lite API"""
        try:
            print(f"   💰 Amount: {amount_sol} SOL | Slippage: {slippage_bps/100}%")
            
            # 1. Get quote from Jupiter LITE API (more reliable)
            quote_url = "https://lite-api.jup.ag/swap/v1/quote"
            params = {
                "inputMint": "So11111111111111111111111111111111111111112",
                "outputMint": token_mint,
                "amount": int(amount_sol * 10**9),
                "slippageBps": slippage_bps,
            }
            
            print(f"   Getting quote from Lite API...")
            resp = requests.get(quote_url, params=params, timeout=10)
            if resp.status_code != 200:
                return {"success": False, "error": f"Quote failed: HTTP {resp.status_code}"}
            quote = resp.json()
            
            # 2. Build swap transaction using Lite API
            swap_url = "https://lite-api.jup.ag/swap/v1/swap"
            payload = {
                "quoteResponse": quote,
                "userPublicKey": str(wallet.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto"
            }
            
            print(f"   Building transaction...")
            resp = requests.post(swap_url, json=payload, timeout=10)
            if resp.status_code != 200:
                return {"success": False, "error": f"Swap build failed: HTTP {resp.status_code}"}
            swap_data = resp.json()
            
            if "swapTransaction" not in swap_data:
                return {"success": False, "error": "No swapTransaction in response"}
            
            # 3. Sign the transaction
            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            raw_tx = VersionedTransaction.from_bytes(tx_bytes)
            message_bytes = message.to_bytes_versioned(raw_tx.message)
            signature = wallet.sign_message(message_bytes)
            signed_tx = VersionedTransaction.populate(raw_tx.message, [signature])
            
            # 4. Send the transaction
            print(f"   Sending transaction...")
            try:
                txid = self.client.send_raw_transaction(bytes(signed_tx))
                print(f"   ✅ TXID: {txid}")
            except Exception as e:
                return {"success": False, "error": f"Send failed: {str(e)}"}
            
            # 5. Wait and fetch actual token amount
            print(f"   ⏳ Waiting 8 seconds for confirmation...")
            await asyncio.sleep(8)
            
            tokens_bought = 0
            
            # Try multiple times to get balance
            for attempt in range(5):
                try:
                    ata = get_associated_token_address(wallet.pubkey(), Pubkey.from_string(token_mint))
                    balance = self.client.get_token_balance(str(ata))
                    if balance is not None and balance > 0:
                        tokens_bought = balance
                        print(f"   ✅ Attempt {attempt+1}: {tokens_bought:.8f} tokens")
                        break
                except Exception as e:
                    print(f"   ⚠️ Attempt {attempt+1} failed: {e}")
                
                if attempt < 4:
                    await asyncio.sleep(2)
            
            # If still 0, try from transaction
            if tokens_bought <= 0:
                tokens_bought = await self.get_token_amount_from_tx(txid, wallet.pubkey(), token_mint)
                if tokens_bought > 0:
                    print(f"   ✅ From transaction: {tokens_bought:.8f} tokens")
            
            # Calculate price
            price = 0
            if tokens_bought > 0:
                price = amount_sol / tokens_bought
            
            return {
                "success": True,
                "txid": txid,
                "tokens_bought": tokens_bought,
                "price": price,
                "explorer": f"https://solscan.io/tx/{txid}"
            }
            
        except Exception as e:
            print(f"   ❌ Buy failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def execute_sell(self, wallet: Keypair, token_mint: str, amount_tokens: float, slippage_bps: int) -> dict:
        """Execute a sell transaction using Jupiter Lite API"""
        try:
            print(f"   Selling {amount_tokens:.6f} tokens of {token_mint[:8]}...")
            
            # Get token decimals
            decimals = await self.get_token_decimals(token_mint)
            amount_raw = int(amount_tokens * 10**decimals)
            
            # 1. Get quote from Jupiter LITE API
            quote_url = "https://lite-api.jup.ag/swap/v1/quote"
            params = {
                "inputMint": token_mint,
                "outputMint": "So11111111111111111111111111111111111111112",
                "amount": amount_raw,
                "slippageBps": slippage_bps,
            }
            
            resp = requests.get(quote_url, params=params, timeout=10)
            if resp.status_code != 200:
                return {"success": False, "error": f"Quote failed: HTTP {resp.status_code}"}
            quote = resp.json()
            
            # 2. Build swap transaction
            swap_url = "https://lite-api.jup.ag/swap/v1/swap"
            payload = {
                "quoteResponse": quote,
                "userPublicKey": str(wallet.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto"
            }
            
            resp = requests.post(swap_url, json=payload, timeout=10)
            if resp.status_code != 200:
                return {"success": False, "error": f"Swap build failed: HTTP {resp.status_code}"}
            swap_data = resp.json()
            
            if "swapTransaction" not in swap_data:
                return {"success": False, "error": "No swapTransaction in response"}
            
            # 3. Sign the transaction
            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            raw_tx = VersionedTransaction.from_bytes(tx_bytes)
            message_bytes = message.to_bytes_versioned(raw_tx.message)
            signature = wallet.sign_message(message_bytes)
            signed_tx = VersionedTransaction.populate(raw_tx.message, [signature])
            
            # 4. Send the transaction
            try:
                txid = self.client.send_raw_transaction(bytes(signed_tx))
                print(f"   ✅ TXID: {txid}")
            except Exception as e:
                return {"success": False, "error": f"Send failed: {str(e)}"}
            
            # 5. Get SOL received
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
            print(f"   ❌ Sell failed: {e}")
            return {"success": False, "error": str(e)}