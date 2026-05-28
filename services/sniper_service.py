"""
Sniper Service - Compatible with solders 0.18.1 and your working script
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
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from typing import Optional


class SniperService:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)
    
    def _rpc_call(self, method: str, params: list) -> dict:
        """Make raw RPC call - used by show_positions"""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            response = requests.post(self.rpc_url, json=payload, timeout=30)
            return response.json()
        except Exception as e:
            return {"error": {"message": str(e)}}
    
    async def get_token_decimals(self, token_mint: str) -> int:
        """Get token decimals from Jupiter token list"""
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
    
    async def get_token_price(self, token_mint: str) -> Optional[float]:
        """Get token price from DexScreener"""
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
        """Resolve DexScreener URL to token address"""
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
        """Extract contract address - matches your working script"""
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
        """Check if wallet holds token"""
        try:
            mint_pubkey = Pubkey.from_string(token_mint)
            ata = get_associated_token_address(wallet_pubkey, mint_pubkey)
            response = self.client.get_token_account_balance(ata, "confirmed")
            if response.value:
                return response.value.ui_amount > 0
            return False
        except:
            return False
    
    async def get_wallet_balance(self, wallet_pubkey: Pubkey) -> float:
        """Get SOL balance"""
        try:
            response = self.client.get_balance(wallet_pubkey, "processed")
            return response.value / 1e9
        except:
            return 0
    
    async def get_token_balance(self, wallet_pubkey: Pubkey, token_mint: str) -> float:
        """Get token balance - added for show_positions"""
        try:
            mint_pubkey = Pubkey.from_string(token_mint)
            ata = get_associated_token_address(wallet_pubkey, mint_pubkey)
            response = self.client.get_token_account_balance(ata, "confirmed")
            if response.value:
                return response.value.ui_amount
            return 0
        except:
            return 0
    
    async def execute_buy(self, wallet: Keypair, token_mint: str, amount_sol: float, slippage_bps: int) -> dict:
        """Execute buy - MATCHES YOUR WORKING SCRIPT EXACTLY"""
        try:
            print(f"   Amount: {amount_sol} SOL | Slippage: {slippage_bps/100}%")
            
            # 1. Quote from Lite API
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
            
            # 2. Build swap
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
            
            # 3. Sign transaction (using solders 0.18.1 compatible method)
            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            raw_tx = VersionedTransaction.from_bytes(tx_bytes)
            message_bytes = message.to_bytes_versioned(raw_tx.message)
            signature = wallet.sign_message(message_bytes)
            signed_tx = VersionedTransaction.populate(raw_tx.message, [signature])
            signed_tx_bytes = bytes(signed_tx)
            
            # 4. Send transaction
            print("   Sending transaction...")
            result = self.client.send_raw_transaction(
                signed_tx_bytes,
                opts=TxOpts(skip_preflight=True)
            )
            txid = str(result.value)
            print(f"   ✅ TXID: {txid}")
            
            # 5. Calculate tokens bought
            token_decimals = await self.get_token_decimals(token_mint)
            raw_output = quote.get("outputAmount", "0")
            tokens_bought = int(raw_output) / 10**token_decimals if raw_output != "0" else 0
            
            return {
                "success": True,
                "txid": txid,
                "tokens_bought": tokens_bought,
                "explorer": f"https://solscan.io/tx/{txid}"
            }
        except Exception as e:
            print(f"   ❌ Buy failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def execute_sell(self, wallet: Keypair, token_mint: str, amount_tokens: float, slippage_bps: int) -> dict:
        """Execute sell"""
        try:
            print(f"   Selling {amount_tokens:.6f} tokens...")
            
            decimals = await self.get_token_decimals(token_mint)
            amount_raw = int(amount_tokens * 10**decimals)
            
            quote_url = "https://lite-api.jup.ag/swap/v1/quote"
            params = {
                "inputMint": token_mint,
                "outputMint": "So11111111111111111111111111111111111111112",
                "amount": amount_raw,
                "slippageBps": slippage_bps,
            }
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
            
            result = self.client.send_raw_transaction(signed_tx_bytes, opts=TxOpts(skip_preflight=True))
            txid = str(result.value)
            
            sol_received = 0
            raw_output = quote.get("outputAmount", "0")
            if raw_output != "0":
                sol_received = int(raw_output) / 1e9
            
            return {
                "success": True,
                "txid": txid,
                "sol_received": sol_received,
                "explorer": f"https://solscan.io/tx/{txid}"
            }
        except Exception as e:
            return {"success": False, "error": str(e)}