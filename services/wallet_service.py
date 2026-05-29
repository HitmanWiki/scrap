"""
Wallet Service - Multi-wallet management with derived keys
"""
import hashlib
import os
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from database.db import Database

db = Database()

def derive_wallet(user_id: int, wallet_number: int) -> Keypair:
    """Derive deterministic wallet from user_id + wallet_number"""
    secret = os.getenv('WALLET_DERIVATION_SECRET', 'default-secret')
    seed_material = f"user_{user_id}_wallet_{wallet_number}_{secret}"
    seed_hash = hashlib.sha256(seed_material.encode()).digest()
    return Keypair.from_seed(seed_hash[:32])

def get_or_create_wallets(user_id: int) -> list:
    """Get existing wallets or create W1 if none exist"""
    wallets = db.get_user_wallets(user_id)
    
    if not wallets:
        # Create W1
        wallet_id = db.create_wallet(user_id, 'W1', 1)
        if wallet_id > 0:
            wallet = derive_wallet(user_id, 1)
            public_key = str(wallet.pubkey())
            db.update_wallet_settings(wallet_id, public_key=public_key)
            wallets = db.get_user_wallets(user_id)
    
    return wallets

def get_wallet_keypair(user_id: int, wallet_id: int) -> Keypair:
    """Get keypair for a specific wallet"""
    wallet = db.get_wallet(wallet_id)
    if wallet:
        return derive_wallet(user_id, wallet['wallet_number'])
    return None

def create_new_wallet(user_id: int, wallet_name: str = None) -> dict:
    """Create a new wallet for user"""
    wallets = db.get_user_wallets(user_id)
    if len(wallets) >= 5:
        return {'success': False, 'error': 'Max 5 wallets'}
    
    next_num = len(wallets) + 1
    wallet_id = db.create_wallet(user_id, wallet_name or f'W{next_num}', next_num)
    
    if wallet_id > 0:
        wallet = derive_wallet(user_id, next_num)
        public_key = str(wallet.pubkey())
        db.update_wallet_settings(wallet_id, public_key=public_key)
        return {'success': True, 'wallet_id': wallet_id, 'public_key': public_key}
    
    return {'success': False, 'error': 'Failed to create wallet'}