"""
Solana blockchain interaction service
Uses raw HTTP RPC calls - NO solana package needed
"""

import requests
from typing import Optional, Dict, Any
from solders.pubkey import Pubkey
from solders.token.associated import get_associated_token_address

class SolanaService:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
    
    def _rpc_call(self, method: str, params: list) -> dict:
        """Make a JSON-RPC call"""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        }
        try:
            resp = requests.post(self.rpc_url, json=payload, timeout=10)
            return resp.json()
        except:
            return {"error": "Connection failed"}
    
    async def get_balance(self, public_key: str) -> float:
        """Get SOL balance"""
        try:
            result = self._rpc_call("getBalance", [public_key])
            if 'result' in result:
                val = result['result']
                if isinstance(val, dict):
                    return val.get('value', 0) / 10**9
                return val / 10**9
            return 0
        except Exception as e:
            print(f"Balance error: {e}")
            return 0
    
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
    
    async def get_token_decimals(self, token_mint: str) -> int:
        """Get token decimals from Jupiter"""
        try:
            url = f"https://tokens.jup.ag/token/{token_mint}"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return data.get('decimals', 9)
        except:
            pass
        return 9