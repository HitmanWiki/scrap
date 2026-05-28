"""
Sniper service for executing token purchases
Matches the working single-user bot implementation
"""

import base64
import requests
from typing import Dict, Any, Optional
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders import message
from solders.pubkey import Pubkey
from solders.token.associated import get_associated_token_address
from solana.rpc.types import TxOpts
from solana.rpc.api import Client

class SniperService:
    def __init__(self, rpc_url: str):
        self.sol_client = Client(rpc_url)
    
    async def get_token_decimals(self, token_mint: str) -> int:
        """Fetch token decimals from Jupiter token list; fallback to 9."""
        try:
            url = f"https://tokens.jup.ag/token/{token_mint}"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if 'decimals' in data:
                    return int(data['decimals'])
        except Exception as e:
            print(f"   ⚠️ Could not fetch decimals: {e}")
        return 9
    
    def sign_transaction(self, tx_bytes: bytes, wallet: Keypair) -> bytes:
        """
        Sign a transaction - EXACT MATCH to single-user bot's method
        """
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
        """
        Execute a buy order via Jupiter API
        EXACT MATCH to single-user bot's working implementation
        """
        try:
            print(f"   💰 Amount: {amount_sol} SOL | Slippage: {slippage_bps/100}%")
            
            # Step 1: Get quote from Jupiter
            quote_url = "https://lite-api.jup.ag/swap/v1/quote"
            params = {
                "inputMint": "So11111111111111111111111111111111111111112",  # SOL
                "outputMint": token_mint,
                "amount": int(amount_sol * 10**9),  # Convert SOL to lamports
                "slippageBps": slippage_bps,
            }
            
            print("   Getting quote...")
            resp = requests.get(quote_url, params=params, timeout=10)
            
            if resp.status_code != 200:
                return {
                    "success": False,
                    "error": f"Quote HTTP {resp.status_code}: {resp.text[:200]}"
                }
            
            quote = resp.json()
            
            # Step 2: Build swap transaction
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
                return {
                    "success": False,
                    "error": f"Swap HTTP {resp.status_code}: {resp.text[:200]}"
                }
            
            swap_data = resp.json()
            
            if "swapTransaction" not in swap_data:
                return {
                    "success": False,
                    "error": "No swapTransaction in response"
                }
            
            # Step 3: Decode, sign, and send
            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            
            print("   Signing transaction...")
            signed_tx_bytes = self.sign_transaction(tx_bytes, wallet)
            
            print("   Sending transaction...")
            # USE TxOpts exactly like the single-user bot
            result = self.sol_client.send_raw_transaction(
                signed_tx_bytes, 
                opts=TxOpts(skip_preflight=True)
            )
            
            txid = str(result.value)
            
            # Step 4: Calculate tokens bought
            token_decimals = await self.get_token_decimals(token_mint)
            raw_output = quote.get("outputAmount", "0")
            tokens_bought = int(raw_output) / 10**token_decimals if raw_output != "0" else 0
            
            # Try to get token price
            try:
                price_url = f"https://price.jup.ag/v4/price?ids={token_mint}"
                price_resp = requests.get(price_url, timeout=5)
                if price_resp.status_code == 200:
                    price_data = price_resp.json()
                    price = price_data.get('data', {}).get(token_mint, {}).get('price', 0)
                else:
                    price = 0
            except:
                price = 0
            
            return {
                "success": True,
                "txid": txid,
                "tokens_bought": tokens_bought,
                "price": price,
                "explorer": f"https://solscan.io/tx/{txid}",
                "quote": quote
            }
            
        except Exception as e:
            print(f"   ❌ Buy execution error: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def execute_sell(
        self, 
        wallet: Keypair, 
        token_mint: str, 
        amount_tokens: float, 
        slippage_bps: int
    ) -> Dict[str, Any]:
        """
        Execute a sell order via Jupiter API
        """
        try:
            # Get token decimals
            decimals = await self.get_token_decimals(token_mint)
            amount_raw = int(amount_tokens * 10**decimals)
            
            # Step 1: Get quote (token -> SOL)
            quote_url = "https://lite-api.jup.ag/swap/v1/quote"
            params = {
                "inputMint": token_mint,
                "outputMint": "So11111111111111111111111111111111111111112",
                "amount": amount_raw,
                "slippageBps": slippage_bps,
            }
            
            print(f"   Getting sell quote for {token_mint[:8]}...")
            resp = requests.get(quote_url, params=params, timeout=10)
            
            if resp.status_code != 200:
                return {
                    "success": False,
                    "error": f"Quote HTTP {resp.status_code}"
                }
            
            quote = resp.json()
            
            # Step 2: Build swap transaction
            swap_url = "https://lite-api.jup.ag/swap/v1/swap"
            payload = {
                "quoteResponse": quote,
                "userPublicKey": str(wallet.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto"
            }
            
            print("   Building sell transaction...")
            resp = requests.post(swap_url, json=payload, timeout=10)
            
            if resp.status_code != 200:
                return {
                    "success": False,
                    "error": f"Swap HTTP {resp.status_code}"
                }
            
            swap_data = resp.json()
            
            if "swapTransaction" not in swap_data:
                return {
                    "success": False,
                    "error": "No swapTransaction"
                }
            
            # Step 3: Sign and send
            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            
            print("   Signing sell transaction...")
            signed_tx_bytes = self.sign_transaction(tx_bytes, wallet)
            
            print("   Sending sell transaction...")
            result = self.sol_client.send_raw_transaction(
                signed_tx_bytes, 
                opts=TxOpts(skip_preflight=True)
            )
            
            txid = str(result.value)
            
            # Calculate SOL received
            raw_output = quote.get("outputAmount", "0")
            sol_received = int(raw_output) / 10**9 if raw_output != "0" else 0
            
            return {
                "success": True,
                "txid": txid,
                "sol_received": sol_received,
                "explorer": f"https://solscan.io/tx/{txid}"
            }
            
        except Exception as e:
            print(f"   ❌ Sell execution error: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def is_holding_token(self, wallet_pubkey: Pubkey, token_mint: str) -> bool:
        """Check if wallet holds a token"""
        try:
            mint_pubkey = Pubkey.from_string(token_mint)
            ata = get_associated_token_address(wallet_pubkey, mint_pubkey)
            
            response = self.sol_client.get_account_info(ata)
            if response.value is None:
                return False
            
            balance = self.sol_client.get_token_account_balance(ata)
            if balance.value is None:
                return False
            
            return balance.value.ui_amount > 0
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