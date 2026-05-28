"""
Sniper Service - NO solana package, uses only solders + requests
"""

import asyncio
import base58
import base64
import requests
import aiohttp
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.token.associated import get_associated_token_address
from solders import message
from typing import Optional
import json


class SimpleRpcClient:
    """Lightweight RPC client - no solana package needed"""
    
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
    
    def _rpc_call(self, method: str, params: list) -> dict:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            response = requests.post(self.rpc_url, json=payload, timeout=30)
            return response.json()
        except Exception as e:
            return {"error": {"message": str(e)}}
    
    def send_raw_transaction(self, tx_bytes: bytes) -> str:
        tx_base58 = base58.b58encode(tx_bytes).decode()
        result = self._rpc_call("sendTransaction", [tx_base58])
        if 'error' in result:
            raise Exception(result['error'].get('message', 'Send failed'))
        return result['result']
    
    def get_balance(self, pubkey: str) -> float:
        result = self._rpc_call("getBalance", [pubkey])
        if 'result' in result:
            return result['result']['value'] / 1e9
        return 0
    
    def get_token_balance(self, token_account: str) -> float:
        result = self._rpc_call("getTokenAccountBalance", [token_account])
        if 'result' in result and result['result']:
            val = result['result']
            if isinstance(val, dict):
                return float(val.get('value', {}).get('uiAmount', 0))
        return 0


class SniperService:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = SimpleRpcClient(rpc_url)
    
    def _rpc_call(self, method: str, params: list) -> dict:
        return self.client._rpc_call(method, params)
    
    async def get_token_decimals(self, token_mint: str) -> int:
        if token_mint.endswith('pump'):
            return 6
        
        try:
            url = f"https://tokens.jup.ag/token/{token_mint}"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if 'decimals' in data:
                    return int(data['decimals'])
        except:
            pass
        
        return 9
    
    def is_pump_fun_token(self, token_mint: str) -> bool:
        return token_mint.endswith('pump')
    
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
    
    def is_valid_solana_address(self, address: str) -> bool:
        try:
            base58.b58decode(address)
            return True
        except:
            return False
    
    async def resolve_dexscreener_pair(self, pair_url: str) -> Optional[str]:
        import re
        try:
            match = re.search(r'dexscreener\.com/([a-zA-Z0-9]+)/([a-zA-Z0-9]+)', pair_url)
            if not match:
                return None
            chain, pair_id = match.groups()
            api_url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_id}"
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, timeout=5) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get('pair') and data['pair'].get('baseToken'):
                            addr = data['pair']['baseToken']['address']
                            if self.is_valid_solana_address(addr):
                                return addr
        except:
            pass
        return None
    
    async def extract_contract_address(self, text: str) -> Optional[str]:
        import re
        urls = re.findall(r'https?://(?:www\.)?dexscreener\.com/[^\s]+', text)
        for url in urls:
            token = await self.resolve_dexscreener_pair(url)
            if token:
                print(f"   ✅ Resolved from DexScreener: {token[:8]}...")
                return token
        
        pattern = r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b'
        all_matches = re.findall(pattern, text)
        valid = [addr for addr in all_matches if self.is_valid_solana_address(addr)]
        if valid:
            return valid[-1]
        return None
    
    async def is_holding_token(self, wallet_pubkey: Pubkey, token_mint: str) -> bool:
        try:
            mint_pubkey = Pubkey.from_string(token_mint)
            ata = get_associated_token_address(wallet_pubkey, mint_pubkey)
            balance = self.client.get_token_balance(str(ata))
            return balance > 0
        except:
            return False
    
    async def get_wallet_balance(self, wallet_pubkey: Pubkey) -> float:
        return self.client.get_balance(str(wallet_pubkey))
    
    async def get_token_balance(self, wallet_pubkey: Pubkey, token_mint: str) -> float:
        try:
            mint_pubkey = Pubkey.from_string(token_mint)
            ata = get_associated_token_address(wallet_pubkey, mint_pubkey)
            return self.client.get_token_balance(str(ata))
        except:
            return 0
    
    # ============================================
    # SELL METHODS
    # ============================================
    
    async def execute_pump_sell(self, wallet: Keypair, token_mint: str, amount_tokens: float, slippage_bps: int) -> dict:
        """Sell pump.fun tokens using pump_swap"""
        try:
            from pump_swap import sell
            
            print(f"   🎯 Using PumpSwap for pump.fun token")
            
            decimals = await self.get_token_decimals(token_mint)
            amount_raw = int(amount_tokens * 10**decimals)
            private_key_base58 = base58.b58encode(bytes(wallet)).decode()
            slippage_percent = slippage_bps / 100
            
            result = await sell(
                mint=token_mint,
                amount=amount_raw,
                slippage=slippage_percent,
                private_key=private_key_base58,
                rpc_url=self.rpc_url
            )
            
            txid = result.get('txid', '')
            print(f"   ✅ PumpSwap Sell TXID: {txid}")
            
            return {
                "success": True,
                "txid": txid,
                "sol_received": 0,
                "explorer": f"https://solscan.io/tx/{txid}"
            }
        except ImportError:
            return {"success": False, "error": "pump_swap not installed"}
        except Exception as e:
            print(f"   ❌ PumpSwap sell failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def execute_jupiter_sell(self, wallet: Keypair, token_mint: str, amount_tokens: float, slippage_bps: int) -> dict:
        """Sell tokens using Jupiter with multiple fallback routes"""
        try:
            decimals = await self.get_token_decimals(token_mint)
            amount_raw = int(amount_tokens * 10**decimals)
            
            # Try multiple output tokens (SOL, USDC, USDT)
            output_mints = [
                ("So11111111111111111111111111111111111111112", 9),   # SOL
                ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 6),  # USDC
                ("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", 6),  # USDT
            ]
            
            for output_mint, output_decimals in output_mints:
                print(f"   Trying route to {output_mint[:8]}...")
                
                # Try different percentages of the amount
                for percentage in [1.0, 0.75, 0.5, 0.25, 0.1, 0.05]:
                    test_raw = int(amount_raw * percentage)
                    if test_raw < 100:  # Too small
                        continue
                    
                    quote_url = "https://lite-api.jup.ag/swap/v1/quote"
                    params = {
                        "inputMint": token_mint,
                        "outputMint": output_mint,
                        "amount": test_raw,
                        "slippageBps": max(slippage_bps, 5000),  # Min 50% slippage
                    }
                    
                    try:
                        resp = requests.get(quote_url, params=params, timeout=10)
                        if resp.status_code != 200:
                            continue
                        
                        quote = resp.json()
                        
                        if "outputAmount" not in quote or quote.get("outputAmount", "0") == "0":
                            continue
                        
                        expected_output = int(quote["outputAmount"]) / (10 ** output_decimals)
                        print(f"   ✅ Route found! Expected: {expected_output:.6f}")
                        
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
                            continue
                        
                        swap_data = resp.json()
                        if "swapTransaction" not in swap_data:
                            continue
                        
                        # Sign and send
                        tx_bytes = base64.b64decode(swap_data["swapTransaction"])
                        raw_tx = VersionedTransaction.from_bytes(tx_bytes)
                        msg_bytes = message.to_bytes_versioned(raw_tx.message)
                        signature = wallet.sign_message(msg_bytes)
                        signed_tx = VersionedTransaction.populate(raw_tx.message, [signature])
                        
                        txid = self.client.send_raw_transaction(bytes(signed_tx))
                        print(f"   ✅ SELL TXID: {txid}")
                        
                        return {
                            "success": True,
                            "txid": txid,
                            "sol_received": expected_output,
                            "explorer": f"https://solscan.io/tx/{txid}"
                        }
                        
                    except Exception as e:
                        print(f"   ⚠️ Attempt failed: {e}")
                        continue
            
            # If all routes fail, try Jupiter API v6
            print(f"   Trying Jupiter v6 API...")
            try:
                v6_url = "https://quote-api.jup.ag/v6/quote"
                params = {
                    "inputMint": token_mint,
                    "outputMint": "So11111111111111111111111111111111111111112",
                    "amount": amount_raw,
                    "slippageBps": max(slippage_bps, 5000),
                }
                resp = requests.get(v6_url, params=params, timeout=10)
                if resp.status_code == 200:
                    quote = resp.json()
                    if quote.get("outAmount") and quote["outAmount"] != "0":
                        # Build swap using v6
                        swap_url = "https://quote-api.jup.ag/v6/swap"
                        payload = {
                            "quoteResponse": quote,
                            "userPublicKey": str(wallet.pubkey()),
                            "wrapAndUnwrapSol": True,
                            "dynamicComputeUnitLimit": True,
                        }
                        resp = requests.post(swap_url, json=payload, timeout=10)
                        if resp.status_code == 200:
                            swap_data = resp.json()
                            if "swapTransaction" in swap_data:
                                tx_bytes = base64.b64decode(swap_data["swapTransaction"])
                                raw_tx = VersionedTransaction.from_bytes(tx_bytes)
                                msg_bytes = message.to_bytes_versioned(raw_tx.message)
                                signature = wallet.sign_message(msg_bytes)
                                signed_tx = VersionedTransaction.populate(raw_tx.message, [signature])
                                
                                txid = self.client.send_raw_transaction(bytes(signed_tx))
                                print(f"   ✅ V6 SELL TXID: {txid}")
                                return {
                                    "success": True,
                                    "txid": txid,
                                    "sol_received": float(quote.get("outAmount", 0)) / 1e9,
                                    "explorer": f"https://solscan.io/tx/{txid}"
                                }
            except Exception as e:
                print(f"   ⚠️ V6 failed: {e}")
            
            return {"success": False, "error": "No routes found - try smaller amount or check liquidity"}
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def execute_sell(self, wallet: Keypair, token_mint: str, amount_tokens: float, slippage_bps: int) -> dict:
        """Execute sell using Jupiter (works for all tokens)"""
        return await self.execute_jupiter_sell(wallet, token_mint, amount_tokens, slippage_bps)
    
    # ============================================
    # BUY METHOD
    # ============================================
    
    async def execute_buy(self, wallet: Keypair, token_mint: str, amount_sol: float, slippage_bps: int) -> dict:
        try:
            print(f"   Amount: {amount_sol} SOL | Slippage: {slippage_bps/100}%")
            
            quote_url = "https://lite-api.jup.ag/swap/v1/quote"
            params = {
                "inputMint": "So11111111111111111111111111111111111111112",
                "outputMint": token_mint,
                "amount": int(amount_sol * 10**9),
                "slippageBps": slippage_bps,
            }
            print("   Getting quote...")
            resp = requests.get(quote_url, params=params, timeout=10)
            if resp.status_code != 200:
                return {"success": False, "error": f"Quote HTTP {resp.status_code}"}
            quote = resp.json()
            
            swap_url = "https://lite-api.jup.ag/swap/v1/swap"
            payload = {
                "quoteResponse": quote,
                "userPublicKey": str(wallet.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto"
            }
            print("   Building transaction...")
            resp = requests.post(swap_url, json=payload, timeout=10)
            if resp.status_code != 200:
                return {"success": False, "error": f"Swap HTTP {resp.status_code}"}
            swap_data = resp.json()
            if "swapTransaction" not in swap_data:
                return {"success": False, "error": "No swapTransaction"}
            
            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            raw_tx = VersionedTransaction.from_bytes(tx_bytes)
            message_bytes = message.to_bytes_versioned(raw_tx.message)
            signature = wallet.sign_message(message_bytes)
            signed_tx = VersionedTransaction.populate(raw_tx.message, [signature])
            signed_tx_bytes = bytes(signed_tx)
            
            print("   Sending transaction...")
            txid = self.client.send_raw_transaction(signed_tx_bytes)
            print(f"   ✅ TXID: {txid}")
            
            await asyncio.sleep(8)
            
            tokens_bought = 0
            decimals = await self.get_token_decimals(token_mint)
            
            try:
                mint_pubkey = Pubkey.from_string(token_mint)
                ata = get_associated_token_address(wallet.pubkey(), mint_pubkey)
                balance = self.client.get_token_balance(str(ata))
                if balance > 0:
                    tokens_bought = balance
                    print(f"   📊 Balance: {tokens_bought:.6f} tokens")
            except:
                pass
            
            if tokens_bought <= 0:
                raw_output = quote.get("outputAmount", "0")
                if raw_output != "0":
                    tokens_bought = int(raw_output) / 10**decimals
            
            return {
                "success": True,
                "txid": txid,
                "tokens_bought": tokens_bought,
                "explorer": f"https://solscan.io/tx/{txid}"
            }
        except Exception as e:
            print(f"   ❌ Buy failed: {e}")
            return {"success": False, "error": str(e)}