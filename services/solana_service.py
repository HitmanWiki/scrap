"""
Solana blockchain interaction service
"""

from typing import Optional, Dict, Any
from solders.pubkey import Pubkey
from solders.token.associated import get_associated_token_address
from solders.keypair import Keypair
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
import requests

class SolanaService:
    def __init__(self, rpc_url: str):
        self.client = Client(rpc_url)
    
    async def get_balance(self, public_key: str) -> float:
        """Get SOL balance"""
        try:
            pubkey = Pubkey.from_string(public_key)
            response = self.client.get_balance(pubkey)
            if response.value is not None:
                return response.value / 10**9
            return 0
        except Exception as e:
            print(f"Balance error: {e}")
            return 0
    
    async def is_holding_token(self, wallet_pubkey: Pubkey, token_mint: str) -> bool:
        """Check if wallet holds a token"""
        try:
            mint_pubkey = Pubkey.from_string(token_mint)
            ata = get_associated_token_address(wallet_pubkey, mint_pubkey)
            
            response = self.client.get_account_info(ata)
            if response.value is None:
                return False
            
            balance = self.client.get_token_account_balance(ata)
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
    
    async def get_token_decimals(self, token_mint: str) -> int:
        """Get token decimals"""
        try:
            url = f"https://tokens.jup.ag/token/{token_mint}"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return data.get('decimals', 9)
        except:
            pass
        return 9