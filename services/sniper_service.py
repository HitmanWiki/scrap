"""
Sniper service for executing token purchases
Uses raw HTTP RPC calls - matching single-user bot's working method
"""

import base64
import base58 as b58
import requests
from typing import Dict, Any, Optional
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders import message
from solders.pubkey import Pubkey
from solders.token.associated import get_associated_token_address

class SniperService:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
    
    def _rpc_call(self, method: str, params: list) -> dict:
        """Make a JSON-RPC call to Solana"""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        }
        try:
            resp = requests.post(self.rpc_url, json=payload, timeout=15)
            return resp.json()
        except Exception as e:
            print(f"   ⚠️ RPC error: {e}")
            return {"error": str(e)}
    
    def _send_raw_transaction(self, signed_tx_bytes: bytes) -> str:
        """Send raw transaction via RPC - matching single-user bot"""
        tx_base58 = b58.b58encode(signed_tx_bytes).decode()
        
        result = self._rpc_call("sendTransaction", [
            tx_base58,
            {
                "skipPreflight": True,
                "preflightCommitment": "processed",
                "encoding": "base58",
                "maxRetries": 3
            }
        ])
        
        if 'result' in result:
            return result['result']
        elif 'error' in result:
            raise Exception(result['error'].get('message', 'Unknown RPC error'))
        else:
            raise Exception(f"Unexpected RPC response: {result}")
    
    async def get_token_decimals(self, token_mint: str) -> int:
        """Fetch token decimals - try on-chain first, then Jupiter"""
        # Try on-chain first (most reliable)
        try:
            result = self._rpc_call("getAccountInfo", [token_mint, {"encoding": "jsonParsed"}])
            if 'result' in result and result['result']:
                value = result['result'].get('value', {}) if isinstance(result['result'], dict) else {}
                if value and 'data' in value:
                    parsed = value['data'].get('parsed', {})
                    if parsed and 'info' in parsed:
                        decimals = parsed['info'].get('decimals', 0)
                        if decimals > 0:
                            print(f"   ℹ️ On-chain decimals: {decimals}")
                            return decimals
        except:
            pass
        
        # Try Jupiter token list
        try:
            url = f"https://tokens.jup.ag/token/{token_mint}"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if 'decimals' in data:
                    decimals = int(data['decimals'])
                    print(f"   ℹ️ Jupiter decimals: {decimals}")
                    return decimals
        except:
            pass
        
        # Default for most memecoins
        print("   ℹ️ Default decimals: 6")
        return 6
    
    def sign_transaction(self, tx_bytes: bytes, wallet: Keypair) -> bytes:
        """Sign a transaction - matching single-user bot"""
        raw_tx = VersionedTransaction.from_bytes(tx_bytes)
        message_bytes = message.to_bytes_versioned(raw_tx.message)
        signature = wallet.sign_message(message_bytes)
        signed_tx = VersionedTransaction.populate(raw_tx.message, [signature])
        return bytes(signed_tx)
    
    async def execute_buy(
        self, 
        wallet: Keypair, 
        token_mint: str, 
        amount_sol: float, 
        slippage_bps: int
    ) -> Dict[str, Any]:
        """Execute a buy order via Jupiter API"""
        try:
            print(f"   💰 Amount: {amount_sol} SOL | Slippage: {slippage_bps/100}%")
            
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
            print(f"   📊 Output: {quote.get('outputAmount', '0')} | Impact: {quote.get('priceImpactPct', '?')}%")
            
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
            
            print("   Signing transaction...")
            signed_tx_bytes = self.sign_transaction(tx_bytes, wallet)
            
            print("   Sending transaction...")
            txid = self._send_raw_transaction(signed_tx_bytes)
            print(f"   ✅ TXID: {txid}")
            
            token_decimals = await self.get_token_decimals(token_mint)
            raw_output = quote.get("outputAmount", "0")
            
            if raw_output and raw_output != "0":
                tokens_bought = float(raw_output) / (10 ** token_decimals)
            else:
                tokens_bought = 0
            
            print(f"   📊 Decimals: {token_decimals}, Raw: {raw_output}, Tokens: {tokens_bought:.9f}")
            
            return {
                "success": True,
                "txid": txid,
                "tokens_bought": tokens_bought,
                "price": 0,
                "explorer": f"https://solscan.io/tx/{txid}"
            }
            
        except Exception as e:
            print(f"   ❌ Buy execution error: {e}")
            return {"success": False, "error": str(e)}
    
    async def execute_sell(
        self, 
        wallet: Keypair, 
        token_mint: str, 
        amount_tokens: float, 
        slippage_bps: int
    ) -> Dict[str, Any]:
        """Execute a sell order via Jupiter API"""
        try:
            decimals = await self.get_token_decimals(token_mint)
            amount_raw = int(amount_tokens * 10**decimals)
            
            print(f"   Selling {amount_tokens} tokens ({amount_raw} raw, {decimals} dec)")
            
            quote_url = "https://lite-api.jup.ag/swap/v1/quote"
            params = {
                "inputMint": token_mint,
                "outputMint": "So11111111111111111111111111111111111111112",
                "amount": amount_raw,
                "slippageBps": slippage_bps,
            }
            
            print(f"   Getting sell quote...")
            resp = requests.get(quote_url, params=params, timeout=10)
            
            if resp.status_code != 200:
                # Try USDC route
                print(f"   ⚠️ Trying USDC route...")
                params["outputMint"] = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                resp = requests.get(quote_url, params=params, timeout=10)
            
            if resp.status_code != 200:
                return {"success": False, "error": f"Quote HTTP {resp.status_code}: {resp.text[:200]}"}
            
            quote = resp.json()
            
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
                return {"success": False, "error": f"Swap HTTP {resp.status_code}: {resp.text[:200]}"}
            
            swap_data = resp.json()
            
            if "swapTransaction" not in swap_data:
                return {"success": False, "error": "No swapTransaction"}
            
            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            signed_tx_bytes = self.sign_transaction(tx_bytes, wallet)
            txid = self._send_raw_transaction(signed_tx_bytes)
            
            sol_received = float(quote.get("outputAmount", "0")) / 10**9
            
            return {
                "success": True,
                "txid": txid,
                "sol_received": sol_received,
                "explorer": f"https://solscan.io/tx/{txid}"
            }
            
        except Exception as e:
            print(f"   ❌ Sell execution error: {e}")
            return {"success": False, "error": str(e)}
    
    async def is_holding_token(self, wallet_pubkey: Pubkey, token_mint: str) -> bool:
        """Check if wallet holds a token"""
        try:
            mint_pubkey = Pubkey.from_string(token_mint) if isinstance(token_mint, str) else token_mint
            ata = get_associated_token_address(wallet_pubkey, mint_pubkey)
            
            result = self._rpc_call("getTokenAccountBalance", [str(ata)])
            if 'result' in result and result['result'] is not None:
                return result['result'].get('uiAmount', 0) > 0
            return False
        except:
            return False
    
    async def get_token_price(self, token_mint: str) -> Optional[float]:
        """Get token price from Jupiter"""
        try:
            url = f"https://price.jup.ag/v4/price?ids={token_mint}"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('data') and token_mint in data['data']:
                    return data['data'][token_mint]['price']
        except:
            pass
        return None