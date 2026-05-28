"""
MULTI-USER SOLANA SNIPER BOT - FINAL
- Derived wallets (NO private keys stored!)
- Modern UI with clean design
- DexScreener URL scraping
- Holding check before buy
- Public & Private Channel Monitoring
- Jupiter API for swaps
- Auto-Sell: Take Profit % + Target MC
- Token Transfer & SOL Withdrawal
"""

import os
import re
import asyncio
import base58
import base64
import hashlib
import requests
import aiohttp
from datetime import datetime
from dotenv import load_dotenv
from typing import Dict, Optional
import json

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.token.associated import get_associated_token_address
from solders import message
from solders.system_program import transfer, TransferParams

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes
)
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

from database.db import Database
from services.solana_service import SolanaService
from services.sniper_service import SniperService

load_dotenv()

# ============================================
# CONFIGURATION
# ============================================
BOT_TOKEN = os.getenv('BOT_TOKEN')
SOLANA_RPC = os.getenv('SOLANA_RPC')
DEFAULT_SLIPPAGE = int(os.getenv('SLIPPAGE_BPS', 1000))
DEFAULT_BUY_AMOUNT = float(os.getenv('BUY_AMOUNT_SOL', 0.01))
application = None  # Will be set in main()

# Conversation states
SELECTING_ACTION = 0
ENTER_CHANNEL_USERNAME = 1
ENTER_API_ID = 2
ENTER_API_HASH = 3
ENTER_PHONE = 4
ENTER_TOKEN_ADDRESS = 5
CONFIRM_BUY = 6
ENTER_BUY_AMOUNT = 7
ENTER_SLIPPAGE = 8
ENTER_PROFIT_PERCENT = 9
ENTER_TARGET_MC = 10
ENTER_TRANSFER_DETAILS = 11
ENTER_WITHDRAW_DETAILS = 12

# Initialize services
db = Database()
solana_service = SolanaService(SOLANA_RPC)
sniper_service = SniperService(SOLANA_RPC)

# Store active Telegram clients
active_clients: Dict[int, TelegramClient] = {}

# Channel monitoring globals
monitor_client = None
channel_subscribers: Dict[str, list] = {}

# Deduplication cache
processing_tokens: Dict[int, set] = {}
last_processed_time: Dict[int, Dict[str, float]] = {}
bought_tokens: Dict[int, set] = {}
DEDUPE_TTL_MS = 5000

# Pending transfers storage
pending_transfers: Dict[int, dict] = {}

# ============================================
# WALLET DERIVATION (No private key storage!)
# ============================================
def derive_wallet_from_user(user_id: int) -> Keypair:
    """Derive deterministic wallet from user ID + secret"""
    secret = os.getenv('WALLET_DERIVATION_SECRET', 'default-secret-change-me')
    if secret == 'default-secret-change-me':
        print("⚠️ WARNING: Using default derivation secret!")
    seed_material = f"user_{user_id}_sniper_bot_v6_{secret}"
    seed_hash = hashlib.sha256(seed_material.encode()).digest()
    return Keypair.from_seed(seed_hash[:32])

async def get_user_wallet(user_id: int) -> Optional[Keypair]:
    try:
        return derive_wallet_from_user(user_id)
    except Exception as e:
        print(f"❌ Wallet derivation error: {e}")
        return None
# Add this global dict at the top of bot.py (after other globals)
pinned_positions_message: Dict[int, int] = {}  # user_id -> message_id

async def send_buy_notification(user_id: int, token_address: str, tokens_bought: float, txid: str, price: float = 0):
    """Send buy notification and update positions overview"""
    try:
        global pinned_positions_message
        
        # Get token info
        mc = await get_token_market_cap(token_address)
        token_price = await solana_service.get_token_price(token_address)
        value = tokens_bought * token_price if token_price else 0
        # Before building positions text
        await sync_positions_from_wallet(user_id)
        positions = db.get_user_positions(user_id)
        # 1. Send buy notification
        buy_text = f"""
🟢 *BUY EXECUTED!*

*Token:* `{token_address[:8]}...{token_address[-4:]}`
*Amount:* {tokens_bought:,.2f}
*Value:* ${value:.2f}
*TX:* [{txid[:15]}...](https://solscan.io/tx/{txid})
"""
        if mc:
            buy_text += f"*MC:* ${mc:,.0f}"
        
        await application.bot.send_message(
            chat_id=user_id,
            text=buy_text,
            parse_mode='Markdown',
            disable_web_page_preview=True
        )
        
        # 2. Build positions overview
        positions = db.get_user_positions(user_id)
        user = db.get_user(user_id)
        sol_balance = await solana_service.get_balance(user['public_key'])
        
        pos_text = "📊 *Portfolio Overview*\n\n"
        total_value = sol_balance
        
        if positions:
            for pos in positions[:15]:
                addr = pos['token_address']
                amt = pos['amount']
                price_now = await solana_service.get_token_price(addr)
                val = amt * price_now if price_now else 0
                total_value += val
                
                pnl = ""
                if pos.get('entry_price') and pos['entry_price'] > 0 and price_now:
                    pnl_pct = ((price_now - pos['entry_price']) / pos['entry_price']) * 100
                    emoji = "🟢" if pnl_pct > 0 else "🔴"
                    pnl = f" {emoji}{pnl_pct:+.1f}%"
                
                pos_text += f"• `{addr[:6]}...{addr[-4:]}` — *{amt:,.2f}* (${val:.2f}){pnl}\n"
        else:
            pos_text += "No positions yet.\n"
        
        pos_text += f"\n💰 *SOL:* ${sol_balance:.2f}"
        pos_text += f"\n💎 *Total:* ${total_value:.2f}"
        pos_text += f"\n\n🔄 Updates automatically on each trade"
        
        # 3. Pin the positions message (delete old, send new)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📈 Buy", callback_data="buy"),
             InlineKeyboardButton("📉 Sell", callback_data="sell")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="positions")]
        ])
        
        # Delete old pinned message if exists
        if user_id in pinned_positions_message:
            try:
                await application.bot.delete_message(
                    chat_id=user_id,
                    message_id=pinned_positions_message[user_id]
                )
            except:
                pass
        
        # Send new positions message
        sent_msg = await application.bot.send_message(
            chat_id=user_id,
            text=pos_text,
            parse_mode='Markdown',
            reply_markup=keyboard
        )
        
        # Store message ID for future updates
        pinned_positions_message[user_id] = sent_msg.message_id
            
    except Exception as e:
        print(f"   ⚠️ Notification error: {e}")

async def send_sell_notification(user_id: int, token_address: str, amount_sold: float, sol_received: float, txid: str):
    """Send sell notification and update positions"""
    try:
        global pinned_positions_message
        
        token_price = await solana_service.get_token_price(token_address)
        value = amount_sold * token_price if token_price else 0
        # Before building positions text
        await sync_positions_from_wallet(user_id)
        positions = db.get_user_positions(user_id)
        
        # 1. Send sell notification
        sell_text = f"""
🔴 *SOLD!*

*Token:* `{token_address[:8]}...{token_address[-4:]}`
*Amount:* {amount_sold:,.2f}
*Value:* ${value:.2f}
*SOL Received:* {sol_received:.4f} SOL
*TX:* [{txid[:15]}...](https://solscan.io/tx/{txid})
"""
        await application.bot.send_message(
            chat_id=user_id,
            text=sell_text,
            parse_mode='Markdown',
            disable_web_page_preview=True
        )
        
        # 2. Update positions overview (same as buy notification)
        positions = db.get_user_positions(user_id)
        user = db.get_user(user_id)
        sol_balance = await solana_service.get_balance(user['public_key'])
        
        pos_text = "📊 *Portfolio Overview*\n\n"
        total_value = sol_balance
        
        if positions:
            for pos in positions[:15]:
                addr = pos['token_address']
                amt = pos['amount']
                price_now = await solana_service.get_token_price(addr)
                val = amt * price_now if price_now else 0
                total_value += val
                
                pnl = ""
                if pos.get('entry_price') and pos['entry_price'] > 0 and price_now:
                    pnl_pct = ((price_now - pos['entry_price']) / pos['entry_price']) * 100
                    emoji = "🟢" if pnl_pct > 0 else "🔴"
                    pnl = f" {emoji}{pnl_pct:+.1f}%"
                
                pos_text += f"• `{addr[:6]}...{addr[-4:]}` — *{amt:,.2f}* (${val:.2f}){pnl}\n"
        else:
            pos_text += "No positions.\n"
        
        pos_text += f"\n💰 *SOL:* ${sol_balance:.2f}"
        pos_text += f"\n💎 *Total:* ${total_value:.2f}"
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📈 Buy", callback_data="buy"),
             InlineKeyboardButton("📉 Sell", callback_data="sell")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="positions")]
        ])
        
        # Update or send new positions message
        if user_id in pinned_positions_message:
            try:
                await application.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=pinned_positions_message[user_id],
                    text=pos_text,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
            except:
                # If edit fails, send new
                sent_msg = await application.bot.send_message(
                    chat_id=user_id,
                    text=pos_text,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
                pinned_positions_message[user_id] = sent_msg.message_id
        else:
            sent_msg = await application.bot.send_message(
                chat_id=user_id,
                text=pos_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
            pinned_positions_message[user_id] = sent_msg.message_id
            
    except Exception as e:
        print(f"   ⚠️ Notification error: {e}")
async def sync_positions_from_wallet(user_id: int):
    """Get token balances by checking the latest buy transaction"""
    wallet = await get_user_wallet(user_id)
    if not wallet:
        return {}
    
    wallet_addr = str(wallet.pubkey())
    wallet_tokens = {}
    
    try:
        import requests as req
        
        # Get positions from DB that have a buy_txid
        positions = db.get_user_positions(user_id)
        
        for pos in positions:
            txid = pos.get('buy_txid', '')
            if not txid:
                continue
            
            mint = pos['token_address']
            
            # Check the transaction for current balance
            try:
                tx_resp = req.post(SOLANA_RPC, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTransaction",
                    "params": [txid, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
                }, timeout=10)
                tx_data = tx_resp.json()
                
                if tx_data.get('result'):
                    # Get the account keys from the transaction
                    account_keys = tx_data['result'].get('transaction', {}).get('message', {}).get('accountKeys', [])
                    meta = tx_data['result'].get('meta', {})
                    
                    # Find token balance change for our mint
                    for token in meta.get('postTokenBalances', []):
                        if token.get('mint') == mint:
                            token_acct_idx = token.get('accountIndex')
                            if isinstance(token_acct_idx, int) and token_acct_idx < len(account_keys):
                                token_account = account_keys[token_acct_idx]
                                
                                # Check current balance of this token account
                                bal_resp = req.post(SOLANA_RPC, json={
                                    "jsonrpc": "2.0", "id": 1,
                                    "method": "getTokenAccountBalance",
                                    "params": [str(token_account)]
                                }, timeout=10)
                                bal_data = bal_resp.json()
                                
                                if bal_data.get('result'):
                                    val = bal_data['result']
                                    amount = float(val.get('value', {}).get('uiAmount', 0)) if isinstance(val, dict) else float(val)
                                    
                                    if amount > 0:
                                        wallet_tokens[mint] = amount
                                        print(f"   ✅ {mint[:8]}... = {amount:.4f}")
                                    else:
                                        # Token was sold - remove position
                                        db.close_position(pos['id'], 'sold')
                                        print(f"   🗑️ {mint[:8]}... sold (0 balance)")
            except Exception as e:
                print(f"   ⚠️ Check error for {mint[:8]}: {e}")
        
        # Update DB with found tokens
        if wallet_tokens:
            for mint, amount in wallet_tokens.items():
                existing = next((p for p in positions if p['token_address'] == mint), None)
                if existing and abs(existing['amount'] - amount) > 0.0001:
                    db.update_position_amount(existing['id'], amount)
            
            print(f"   ✅ Synced: {len(wallet_tokens)} tokens")
        
    except Exception as e:
        print(f"   ⚠️ Sync error: {e}")
    
    return wallet_tokens
async def debug_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    wallet = await get_user_wallet(user_id)
    wallet_addr = str(wallet.pubkey())
    
    import requests as req
    
    results = [f"🔍 Wallet: `{wallet_addr}`"]
    
    # Check HOPPY token (2RWndXkx...)
    hop_mint = "2RWndXkxWkaKhGjE7dZivVbK5qXtpwnCZJ1jpnxapump"
    
    # Derive ATA
    mint_pubkey = Pubkey.from_string(hop_mint)
    ata = get_associated_token_address(wallet.pubkey(), mint_pubkey)
    results.append(f"HOPPY ATA: `{str(ata)}`")
    
    # Check ATA exists
    r1 = req.post(SOLANA_RPC, json={
        "jsonrpc":"2.0","id":1,
        "method":"getAccountInfo",
        "params":[str(ata)]
    }, timeout=10)
    info = r1.json()
    exists = info.get('result', {}).get('value') is not None
    results.append(f"ATA exists: {exists}")
    
    # Check balance
    r2 = req.post(SOLANA_RPC, json={
        "jsonrpc":"2.0","id":1,
        "method":"getTokenAccountBalance",
        "params":[str(ata)]
    }, timeout=10)
    bal = r2.json()
    results.append(f"Balance: {bal}")
    
    await update.message.reply_text("\n".join(results), parse_mode='Markdown')

# ============================================
# ADDRESS VALIDATION & EXTRACTION
# ============================================
def is_valid_solana_address(address: str) -> bool:
    try:
        decoded = base58.b58decode(address)
        return len(decoded) == 32
    except:
        return False

async def resolve_dexscreener_pair(pair_url: str) -> Optional[str]:
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
                        token_address = data['pair']['baseToken']['address']
                        if is_valid_solana_address(token_address):
                            return token_address
    except Exception as e:
        print(f"   ⚠️ DexScreener error: {e}")
    return None

async def extract_contract_address(text: str) -> Optional[str]:
    urls = re.findall(r'https?://(?:www\.)?dexscreener\.com/[^\s]+', text)
    for url in urls:
        token = await resolve_dexscreener_pair(url)
        if token:
            print(f"   ✅ DexScreener: {token[:8]}...")
            return token
    pattern = r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b'
    all_matches = re.findall(pattern, text)
    valid = [addr for addr in all_matches if is_valid_solana_address(addr)]
    if valid:
        return valid[-1]
    return None

# ============================================
# HOLDING CHECK
# ============================================
async def already_holding(user_id: int, wallet_pubkey: Pubkey, token_mint: str) -> bool:
    if user_id in bought_tokens and token_mint in bought_tokens[user_id]:
        return True
    try:
        return await sniper_service.is_holding_token(wallet_pubkey, token_mint)
    except:
        return False

# ============================================
# MARKET CAP CALCULATOR
# ============================================
async def get_token_market_cap(token_mint: str) -> Optional[float]:
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('pairs') and len(data['pairs']) > 0:
                        mc = data['pairs'][0].get('marketCap', 0)
                        if mc > 0:
                            return mc
    except:
        pass
    return None

# ============================================
# USER MANAGEMENT
# ============================================
async def get_or_create_user(user_id: int, username: str = None) -> Dict:
    user = db.get_user(user_id)
    if not user:
        wallet = derive_wallet_from_user(user_id)
        public_key = str(wallet.pubkey())
        db.create_user(user_id=user_id, username=username or f"user_{user_id}", public_key=public_key)
        user = db.get_user(user_id)
        print(f"✅ New user {user_id} | Wallet: {public_key[:8]}...")
    return user

# ============================================
# MODERN UI KEYBOARDS
# ============================================
def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("💼 Wallet", callback_data="wallet"),
         InlineKeyboardButton("💰 Balance", callback_data="balance")],
        [InlineKeyboardButton("📈 Buy Token", callback_data="buy"),
         InlineKeyboardButton("📉 Sell Token", callback_data="sell")],
        [InlineKeyboardButton("📊 Positions", callback_data="positions"),
         InlineKeyboardButton("🔄 Refresh Pos", callback_data="refresh_positions")],  # New button
        [InlineKeyboardButton("📋 Channels", callback_data="channels"),
         InlineKeyboardButton("➕ Add Channel", callback_data="add_channel")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
         InlineKeyboardButton("🔑 Export Key", callback_data="export_key")],
        [InlineKeyboardButton("🔐 TG Auth", callback_data="telegram_auth_setup"),
         InlineKeyboardButton("⚡ Actions", callback_data="actions")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_settings_keyboard():
    keyboard = [
        [InlineKeyboardButton("💵 Buy Amount", callback_data="set_buy_amount")],
        [InlineKeyboardButton("📊 Slippage %", callback_data="set_slippage")],
        [InlineKeyboardButton("🎯 Take Profit %", callback_data="set_take_profit")],
        [InlineKeyboardButton("📈 Target MC ($)", callback_data="set_target_mc")],
        [InlineKeyboardButton("🤖 Auto-Sell", callback_data="toggle_auto_sell")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="back_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_actions_keyboard():
    keyboard = [
        [InlineKeyboardButton("💸 Transfer Token", callback_data="transfer_token"),
         InlineKeyboardButton("🏦 Withdraw SOL", callback_data="withdraw_sol")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="back_main")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="back_main")]])

# ============================================
# CHANNEL MONITORING (CORE SNIPING LOGIC)
# ============================================

async def process_channel_message(user_id: int, message_text: str, channel_name: str):
    """Process a message from a monitored channel - triggers sniping"""
    global processing_tokens, last_processed_time, bought_tokens
    
    ca = await extract_contract_address(message_text)
    if not ca:
        return
    
    print(f"   🎯 Token detected: {ca[:8]}...")
    
    # Initialize per-user sets
    if user_id not in processing_tokens:
        processing_tokens[user_id] = set()
    if user_id not in last_processed_time:
        last_processed_time[user_id] = {}
    if user_id not in bought_tokens:
        bought_tokens[user_id] = set()
    
    # Deduplication
    if ca in processing_tokens[user_id]:
        return
    now = datetime.now().timestamp() * 1000
    if ca in last_processed_time[user_id]:
        if now - last_processed_time[user_id][ca] < DEDUPE_TTL_MS:
            return
    
    user = db.get_user(user_id)
    if not user:
        return
    
    wallet = await get_user_wallet(user_id)
    if not wallet:
        return
    
    # Check daily limit
    daily_trades = user.get('daily_trades', 0)
    max_trades = user.get('max_daily_trades', 100)
    if daily_trades >= max_trades:
        print(f"   ⏭️ User {user_id} reached daily limit")
        return
    
    # Check if already holding
    if await already_holding(user_id, wallet.pubkey(), ca):
        print(f"   ⏭️ Already holding {ca[:8]}...")
        return
    
    # Check SOL balance
    try:
        balance = await solana_service.get_balance(user['public_key'])
        buy_amount = user.get('default_buy_amount', DEFAULT_BUY_AMOUNT)
        if balance < buy_amount:
            print(f"   ❌ Insufficient SOL: {balance:.4f} (need {buy_amount})")
            return
    except Exception as e:
        print(f"   ⚠️ Balance check error: {e}")
        return
    
    # Mark as processing
    processing_tokens[user_id].add(ca)
    last_processed_time[user_id][ca] = now
    
    buy_amount = user.get('default_buy_amount', DEFAULT_BUY_AMOUNT)
    slippage = user.get('default_slippage', DEFAULT_SLIPPAGE)
    
    print(f"   🔥 SNIPING {ca[:8]}... ({buy_amount} SOL)")
    
    # Send "buying" notification
    try:
        await application.bot.send_message(
            chat_id=user_id,
            text=f"🔥 *Sniping token!*\n\n`{ca[:8]}...`\nAmount: {buy_amount} SOL\n\n⏳ Executing buy...",
            parse_mode='Markdown'
        )
    except:
        pass
    
    result = await sniper_service.execute_buy(wallet=wallet, token_mint=ca, amount_sol=buy_amount, slippage_bps=slippage)
    
    processing_tokens[user_id].discard(ca)
    
    # After successful buy:
    if result['success']:
        db.increment_daily_trades(user_id)
        
        tokens_bought = result.get('tokens_bought', 0)
        txid = result.get('txid', '')
        
        print(f"   📊 Tokens bought: {tokens_bought}")
        
        # Always save position
        if tokens_bought > 0:
            db.add_position(user_id, ca, tokens_bought, result.get('price', 0), txid)
            db.add_trade_history(user_id, ca, 'buy', tokens_bought, result.get('price', 0), txid)
            await sync_positions_from_wallet(user_id)
            print(f"   ✅ Position saved: {tokens_bought:.6f} tokens")
        else:
            db.add_position(user_id, ca, 0, result.get('price', 0), txid)
            print(f"   ⚠️ Position saved with 0 amount (will update on refresh)")
        
        if user_id not in bought_tokens:
            bought_tokens[user_id] = set()
        bought_tokens[user_id].add(ca)
        
        print(f"   🔗 {result['explorer']}")
        
        # SEND BUY NOTIFICATION with position pin
        try:
            # Get token info for notification
            mc = await get_token_market_cap(ca)
            token_price = await solana_service.get_token_price(ca)
            value = tokens_bought * token_price if token_price else 0
            
            buy_text = f"""
🟢 *BUY EXECUTED!*

*Token:* `{ca[:8]}...{ca[-4:]}`
*Amount:* {tokens_bought:,.2f}
*Value:* ${value:.2f}
*TX:* [{txid[:15]}...](https://solscan.io/tx/{txid})
"""
            if mc:
                buy_text += f"*MC:* ${mc:,.0f}\n"
            
            await application.bot.send_message(
                chat_id=user_id,
                text=buy_text,
                parse_mode='Markdown',
                disable_web_page_preview=True
            )
            
            # Pin positions
            positions = db.get_user_positions(user_id)
            if positions:
                pos_text = "📌 *Your Positions*\n\n"
                total_value = 0
                for pos in positions[:10]:
                    addr = pos['token_address']
                    amt = pos['amount']
                    price_now = await solana_service.get_token_price(addr)
                    val = amt * price_now if price_now else 0
                    total_value += val
                    
                    pnl = ""
                    if pos.get('entry_price') and pos['entry_price'] > 0 and price_now:
                        pnl_pct = ((price_now - pos['entry_price']) / pos['entry_price']) * 100
                        emoji = "🟢" if pnl_pct > 0 else "🔴"
                        pnl = f" {emoji}{pnl_pct:+.1f}%"
                    
                    pos_text += f"• `{addr[:8]}...` — *{amt:,.2f}* (${val:.2f}){pnl}\n"
                
                if total_value > 0:
                    sol_balance = await solana_service.get_balance(user['public_key'])
                    pos_text += f"\n💎 *Total Value:* ${total_value + sol_balance:.2f}"
                
                await application.bot.send_message(
                    chat_id=user_id,
                    text=pos_text,
                    parse_mode='Markdown'
                )
        except Exception as e:
            print(f"   ⚠️ Notification error: {e}")
    
    else:
        # Buy failed - notify user
        try:
            await application.bot.send_message(
                chat_id=user_id,
                text=f"❌ *Buy Failed*\n\nToken: `{ca[:8]}...`\nError: {result.get('error', 'Unknown')[:200]}",
                parse_mode='Markdown'
            )
        except:
            pass

async def poll_channel_messages(channel_name: str):
    """Poll a channel for new messages"""
    global monitor_client, channel_subscribers
    
    if not monitor_client:
        print(f"   ⚠️ No monitor client for {channel_name}")
        return
    
    try:
        entity = await monitor_client.get_entity(channel_name)
        print(f"   ✅ Connected to {channel_name}")
        
        last_id = 0
        msgs = await monitor_client.get_messages(entity, limit=1)
        if msgs and msgs[0]:
            last_id = msgs[0].id
        else:
            last_id = 1
        
        print(f"   📍 Starting from msg ID: {last_id}")
        
        while channel_name in channel_subscribers and channel_subscribers[channel_name]:
            try:
                messages = await monitor_client.get_messages(entity, limit=5, min_id=last_id)
                for msg in messages:
                    if msg.id > last_id and msg.text:
                        last_id = msg.id
                        print(f"\n📨 [{datetime.now().strftime('%H:%M:%S')}] {channel_name}")
                        print(f"   📝 {msg.text[:100]}...")
                        
                        for user_id in channel_subscribers.get(channel_name, []):
                            asyncio.create_task(process_channel_message(user_id, msg.text, channel_name))
                
                await asyncio.sleep(2)
            except Exception as e:
                print(f"   ⚠️ Poll error for {channel_name}: {e}")
                await asyncio.sleep(5)
    except Exception as e:
        print(f"❌ Failed to connect to {channel_name}: {e}")

# ============================================
# AUTO-SELL MONITOR
# ============================================
async def auto_sell_monitor():
    """Monitor positions for auto-sell conditions"""
    while True:
        try:
            positions = db.get_all_active_positions()
            for pos in positions:
                user_id = pos['user_id']
                settings = db.get_user_settings(user_id)
                
                if not settings or not settings.get('auto_sell_enabled'):
                    continue
                
                should_sell = False
                reason = ""
                
                take_profit = settings.get('take_profit_percent', 0)
                if take_profit > 0:
                    current_price = await solana_service.get_token_price(pos['token_address'])
                    if current_price and pos['entry_price'] > 0:
                        profit_percent = ((current_price - pos['entry_price']) / pos['entry_price']) * 100
                        if profit_percent >= take_profit:
                            should_sell = True
                            reason = f"+{profit_percent:.1f}% profit (target: {take_profit}%)"
                
                target_mc = settings.get('target_mc', 0)
                if target_mc > 0 and not should_sell:
                    current_mc = await get_token_market_cap(pos['token_address'])
                    if current_mc and current_mc >= target_mc:
                        should_sell = True
                        reason = f"MC ${current_mc:,.0f} reached (target: ${target_mc:,.0f})"
                
                if should_sell:
                    print(f"🎯 Auto-Sell for user {user_id}: {pos['token_address'][:8]}... ({reason})")
                    wallet = await get_user_wallet(user_id)
                    if wallet:
                        result = await sniper_service.execute_sell(
                            wallet=wallet,
                            token_mint=pos['token_address'],
                            amount_tokens=pos['amount'],
                            slippage_bps=settings.get('max_slippage', 5000)
                        )
                        if result['success']:
                            db.close_position(pos['id'], result['txid'])
                            db.add_trade_history(user_id, pos['token_address'], 'auto-sell', pos['amount'], result.get('price', 0), result['txid'])
                            print(f"   ✅ Auto-sold!")
            
            await asyncio.sleep(10)
        except Exception as e:
            print(f"Auto-sell error: {e}")
            await asyncio.sleep(30)

# ============================================
# START COMMAND & UI HANDLERS
# ============================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_data = await get_or_create_user(user.id, user.username)
    wallet_addr = user_data['public_key']
    
    try:
        balance = await solana_service.get_balance(wallet_addr)
    except:
        balance = 0
    
    positions = db.get_user_positions_count(user.id)
    channels = len(db.get_user_channels(user.id))
    settings = db.get_user_settings(user.id)
    auto_sell = "✅ ON" if (settings and settings.get('auto_sell_enabled')) else "❌ OFF"
    
    welcome_text = f"""
╔═══════════════════════════╗
║     🔫 SOLANA SNIPER     ║
╚═══════════════════════════╝

👤 *{user.first_name}*

┌─────────────────────────┐
│ 💳 `{wallet_addr[:6]}...{wallet_addr[-4:]}` │
│ 💰 {balance:.4f} SOL                 │
│ 📊 {positions} positions | 📋 {channels} channels │
│ 🤖 Auto-Sell: {auto_sell}          │
└─────────────────────────┘

🔐 *Derived Wallet* — No keys stored!

👇 *Select an option:*
"""
    await update.message.reply_text(welcome_text, reply_markup=get_main_keyboard(), parse_mode='Markdown')
    return SELECTING_ACTION

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    action = query.data
    user_id = query.from_user.id
    
    # Navigation
    if action == "wallet": return await show_wallet(query)
    elif action == "export_key": return await export_private_key(query)
    elif action == "channels": return await show_channels(query)
    elif action == "add_channel": return await add_channel_menu(query)
    elif action == "positions": return await show_positions(query)
    elif action == "buy": return await initiate_buy(query)
    elif action == "sell": return await initiate_sell(query)
    elif action == "settings": return await show_settings(query)
    elif action == "balance": return await show_balance(query)
    elif action == "telegram_auth_setup": return await telegram_auth_setup(query)
    elif action == "actions": return await show_actions(query)
    elif action == "refresh_positions":return await refresh_positions(query)
    elif action == "back_main": return await back_to_main(query)
    
    # Settings
    elif action == "set_buy_amount":
        context.user_data['settings_state'] = 'buy_amount'
        user = db.get_user(user_id)
        await query.edit_message_text(
            f"💵 *Buy Amount*\nCurrent: *{user.get('default_buy_amount', DEFAULT_BUY_AMOUNT)} SOL*\n\nEnter new amount:",
            reply_markup=get_back_keyboard(), parse_mode='Markdown')
        return ENTER_BUY_AMOUNT
    
    elif action == "set_slippage":
        context.user_data['settings_state'] = 'slippage'
        user = db.get_user(user_id)
        await query.edit_message_text(
            f"📊 *Slippage*\nCurrent: *{user.get('default_slippage', DEFAULT_SLIPPAGE)/100}%*\n\nEnter new %:",
            reply_markup=get_back_keyboard(), parse_mode='Markdown')
        return ENTER_SLIPPAGE
    
    elif action == "set_take_profit":
        context.user_data['settings_state'] = 'take_profit'
        settings = db.get_user_settings(user_id)
        current = settings.get('take_profit_percent', 50) if settings else 50
        await query.edit_message_text(
            f"🎯 *Take Profit*\nCurrent: *{current}%*\n\nAuto-sell when profit reaches this %.\nEnter new % (e.g., 50):",
            reply_markup=get_back_keyboard(), parse_mode='Markdown')
        return ENTER_PROFIT_PERCENT
    
    elif action == "set_target_mc":
        context.user_data['settings_state'] = 'target_mc'
        settings = db.get_user_settings(user_id)
        current = settings.get('target_mc', 0) if settings else 0
        current_display = f"${current:,.0f}" if current > 0 else "Not set"
        await query.edit_message_text(
            f"📈 *Target Market Cap*\nCurrent: *{current_display}*\n\nAuto-sell when MC reaches this.\nEnter target in $ (e.g., 45000):",
            reply_markup=get_back_keyboard(), parse_mode='Markdown')
        return ENTER_TARGET_MC
    
    elif action == "toggle_auto_sell":
        settings = db.get_user_settings(user_id)
        current = settings.get('auto_sell_enabled', 0) if settings else 0
        new_val = 0 if current else 1
        db.update_user_settings(user_id, auto_sell_enabled=new_val)
        status = "✅ ENABLED" if new_val else "❌ DISABLED"
        await query.edit_message_text(f"🤖 *Auto-Sell:* {status}", reply_markup=get_settings_keyboard(), parse_mode='Markdown')
        return SELECTING_ACTION
    
    # Channel type
    elif action == "add_public_channel":
        context.user_data['channel_type'] = 'public'
        await query.edit_message_text(
            "📋 Send channel username (@name):\nType *cancel* to abort.",
            reply_markup=get_back_keyboard(), parse_mode='Markdown')
        return ENTER_CHANNEL_USERNAME
    
    elif action == "add_private_channel":
        user = db.get_user(user_id)
        if not user.get('telegram_api_id'):
            await query.edit_message_text(
                "❌ *Telegram Auth Required!*",
                reply_markup=get_main_keyboard(), parse_mode='Markdown')
            return SELECTING_ACTION
        context.user_data['channel_type'] = 'private'
        await query.edit_message_text(
            "📋 Send private channel username:\nType *cancel* to abort.",
            reply_markup=get_back_keyboard(), parse_mode='Markdown')
        return ENTER_CHANNEL_USERNAME
    
    elif action == "confirm_buy": 
        return await execute_buy_order(query)
    
    elif action == "sell_all": 
        return await sell_all_positions(query)
    
    elif action == "sell_by_address":
        context.user_data['sell_mode'] = 'address'
        await query.edit_message_text(
            "📉 *Sell by Address*\n\nSend token contract address:\nType *cancel* to abort.",
            reply_markup=get_back_keyboard(), parse_mode='Markdown')
        return ENTER_TOKEN_ADDRESS
    
    elif action.startswith("sell_"):
        return await confirm_sell_position(query, action[5:])
    
    elif action.startswith("execute_sell_"):
        parts = action.split("_")
        return await execute_sell_order(query, parts[2], int(parts[3]))
    
    elif action.startswith("remove_ch_"):
        db.deactivate_channel(int(action.split("_")[2]))
        await query.edit_message_text("✅ Channel removed!", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    
    elif action == "remove_channel": 
        return await remove_channel_menu(query)
    
    # Actions
    elif action == "transfer_token": 
        return await initiate_transfer(query)
    
    elif action == "withdraw_sol": 
        return await initiate_withdraw(query)
    
    elif action == "confirm_transfer": 
        return await execute_transfer(query)
    
    elif action == "confirm_withdraw": 
        return await execute_withdraw(query)
    
    elif action.startswith("transfer_select_"):
        token_address = action.replace("transfer_select_", "")
        return await handle_transfer_select(query, token_address)
    
    return SELECTING_ACTION

# ============================================
# UI HELPER FUNCTIONS
# ============================================
async def show_wallet(query):
    user_id = query.from_user.id
    user = db.get_user(user_id)
    try:
        balance = await solana_service.get_balance(user['public_key'])
    except:
        balance = 0
    
    text = f"""
╔═══════════════════════════╗
║        💼 WALLET          ║
╚═══════════════════════════╝

*Address:*
`{user['public_key']}`

*Balance:* `{balance:.6f} SOL`

🔐 Derived from Telegram ID
⚠️ Fund to start sniping!
"""
    keyboard = [
        [InlineKeyboardButton("📋 Copy Address", callback_data="copy_address")],
        [InlineKeyboardButton("🔑 Export Key", callback_data="export_key")],
        [InlineKeyboardButton("« Back", callback_data="back_main")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECTING_ACTION

async def export_private_key(query):
    user_id = query.from_user.id
    wallet = await get_user_wallet(user_id)
    if not wallet:
        await query.edit_message_text("❌ Error!", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    
    keypair_bytes = bytes(wallet)
    private_key = base58.b58encode(keypair_bytes).decode()
    
    text = f"""
╔═══════════════════════════╗
║      ⚠️ PRIVATE KEY      ║
╚═══════════════════════════╝

`{private_key}`

🔐 *IMPORTANT:*
• Derived from Telegram ID
• NEVER stored on servers
• 🗑️ Delete this after saving!

*Public:* `{str(wallet.pubkey())}`
"""
    keyboard = [
        [InlineKeyboardButton("🗑️ Delete", callback_data="delete_message")],
        [InlineKeyboardButton("« Back", callback_data="back_main")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECTING_ACTION

async def show_channels(query):
    user_id = query.from_user.id
    channels = db.get_user_channels(user_id)
    if not channels:
        text = "📋 *No channels configured*"
    else:
        text = "📋 *Your Channels*\n\n"
        for ch in channels:
            ctype = "🔒" if ch.get('is_private') else "🌐"
            text += f"{ctype} `{ch['channel_name']}`\n"
    keyboard = [
        [InlineKeyboardButton("➕ Add", callback_data="add_channel"), InlineKeyboardButton("❌ Remove", callback_data="remove_channel")],
        [InlineKeyboardButton("« Back", callback_data="back_main")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECTING_ACTION

async def add_channel_menu(query):
    text = "➕ *Add Channel*\n\n🌐 *Public* — Anyone can view\n🔒 *Private* — Requires TG Auth"
    keyboard = [
        [InlineKeyboardButton("🌐 Public", callback_data="add_public_channel"), InlineKeyboardButton("🔒 Private", callback_data="add_private_channel")],
        [InlineKeyboardButton("« Back", callback_data="channels")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECTING_ACTION

async def remove_channel_menu(query):
    user_id = query.from_user.id
    channels = db.get_user_channels(user_id)
    if not channels:
        await query.edit_message_text("No channels.", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    text = "❌ *Remove Channel*\n\nSelect:"
    keyboard = []
    for ch in channels:
        keyboard.append([InlineKeyboardButton(f"❌ {ch['channel_name']}", callback_data=f"remove_ch_{ch['id']}")])
    keyboard.append([InlineKeyboardButton("« Back", callback_data="channels")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECTING_ACTION
async def refresh_positions(query):
    user_id = query.from_user.id
    wallet = await get_user_wallet(user_id)
    
    await query.edit_message_text("Refreshing positions...")
    
    try:
        result = sniper_service._rpc_call("getTokenAccountsByOwner", [
            str(wallet.pubkey()),
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"}
        ])
        
        updated = 0
        if 'result' in result and result['result']:
            token_accounts = result['result'].get('value', [])
            for acc in token_accounts:
                parsed = acc.get('account', {}).get('data', {}).get('parsed', {})
                info = parsed.get('info', {})
                amount = info.get('tokenAmount', {}).get('uiAmount', 0)
                mint = info.get('mint', '')
                
                if amount > 0 and mint != "So11111111111111111111111111111111111111112":
                    existing = db.get_user_position_by_token(user_id, mint)
                    if existing:
                        db.update_position_amount(existing['id'], amount)
                        updated += 1
                    else:
                        db.add_position(user_id, mint, amount, 0, "refresh")
                        updated += 1
        
        await query.edit_message_text(f"Positions Refreshed!\n\nUpdated {updated} positions.", reply_markup=get_main_keyboard())
    except Exception as e:
        await query.edit_message_text(f"Refresh failed: {str(e)}", reply_markup=get_main_keyboard())
    
    return SELECTING_ACTION

async def handle_channel_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_name = update.message.text.strip()
    user_id = update.effective_user.id
    channel_type = context.user_data.get('channel_type', 'public')
    if channel_name.lower() == 'cancel':
        await update.message.reply_text("❌ Cancelled.", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    if not channel_name.startswith('@'):
        channel_name = '@' + channel_name
    is_private = (channel_type == 'private')
    db.add_channel(user_id, channel_name, is_private=is_private)
    
    # Start monitoring
    global channel_subscribers
    if channel_name not in channel_subscribers:
        channel_subscribers[channel_name] = []
    if user_id not in channel_subscribers[channel_name]:
        channel_subscribers[channel_name].append(user_id)
    asyncio.create_task(poll_channel_messages(channel_name))
    
    await update.message.reply_text(f"✅ `{channel_name}` added!\n📡 Monitoring started.", reply_markup=get_main_keyboard(), parse_mode='Markdown')
    return SELECTING_ACTION

# ============================================
# TELEGRAM AUTH
# ============================================
async def telegram_auth_setup(query):
    user_id = query.from_user.id
    user = db.get_user(user_id)
    status = "✅ Configured" if user.get('telegram_api_id') else "❌ Not configured"
    text = f"""
🔐 *Telegram Auth*

Status: {status}

⚠️ Private channel monitoring is only available on local deployment.

📋 *Public channels* work automatically on Heroku!
"""
    keyboard = [
        [InlineKeyboardButton("🔄 Setup Credentials", callback_data="start_auth")],
        [InlineKeyboardButton("« Back", callback_data="back_main")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECTING_ACTION

# ============================================
# BUY/SELL HANDLERS
# ============================================
async def initiate_buy(query):
    await query.edit_message_text("📈 *Buy Token*\n\nSend token address or DexScreener URL:\nType *cancel* to abort.", reply_markup=get_back_keyboard(), parse_mode='Markdown')
    return ENTER_TOKEN_ADDRESS

async def handle_token_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    
    if text.lower() == 'cancel':
        context.user_data.pop('sell_mode', None)
        await update.message.reply_text("Cancelled.", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    
    ca = await extract_contract_address(text)
    if not ca:
        await update.message.reply_text("❌ No token address found!", reply_markup=get_back_keyboard())
        return ENTER_TOKEN_ADDRESS
    
    sell_mode = context.user_data.get('sell_mode')
    if sell_mode == 'address':
        context.user_data.pop('sell_mode', None)
        wallet = await get_user_wallet(user_id)
        if not wallet:
            await update.message.reply_text("❌ No wallet!", reply_markup=get_main_keyboard())
            return SELECTING_ACTION
        
        # Get token balance
        balance = 0
        try:
            mint_pubkey = Pubkey.from_string(ca)
            ata = get_associated_token_address(wallet.pubkey(), mint_pubkey)
            result = sniper_service._rpc_call("getTokenAccountBalance", [str(ata)])
            if 'result' in result and result['result']:
                balance = float(result['result'].get('uiAmount', 0))
        except:
            pass
        
        if balance <= 0:
            await update.message.reply_text(f"❌ No tokens found for `{ca[:8]}...`", reply_markup=get_main_keyboard(), parse_mode='Markdown')
            return SELECTING_ACTION
        
        context.user_data['sell_token'] = ca
        context.user_data['sell_amount'] = balance
        
        text = f"📉 *Sell Tokens*\n\nToken: `{ca[:8]}...`\nBalance: {balance:.2f}\n\nSelect percentage:"
        keyboard = [
            [InlineKeyboardButton("100%", callback_data=f"execute_sell_{ca}_100"),
             InlineKeyboardButton("50%", callback_data=f"execute_sell_{ca}_50")],
            [InlineKeyboardButton("25%", callback_data=f"execute_sell_{ca}_25"),
             InlineKeyboardButton("10%", callback_data=f"execute_sell_{ca}_10")],
            [InlineKeyboardButton("« Cancel", callback_data="back_main")]
        ]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return SELECTING_ACTION
    
    # Regular buy flow
    user = db.get_user(user_id)
    buy_amount = user.get('default_buy_amount', DEFAULT_BUY_AMOUNT)
    slippage = user.get('default_slippage', DEFAULT_SLIPPAGE)
    
    context.user_data['pending_token'] = ca
    
    text = f"""
📈 *Confirm Buy*

*Token:* `{ca}`
*Amount:* {buy_amount} SOL
*Slippage:* {slippage/100}%

Proceed?
"""
    keyboard = [[InlineKeyboardButton("✅ Confirm", callback_data="confirm_buy"), InlineKeyboardButton("❌ Cancel", callback_data="back_main")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return CONFIRM_BUY

async def execute_buy_order(query):
    user_id = query.from_user.id
    user = db.get_user(user_id)
    wallet = await get_user_wallet(user_id)
    
    # Extract token address from the message
    token_match = re.search(r'`([1-9A-HJ-NP-Za-km-z]{32,44})`', query.message.text)
    if not token_match:
        await query.edit_message_text("❌ Token not found!", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    
    token_address = token_match.group(1)
    
    # Check if already holding
    if await already_holding(user_id, wallet.pubkey(), token_address):
        await query.edit_message_text("⏭️ Already holding!", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    
    buy_amount = user.get('default_buy_amount', DEFAULT_BUY_AMOUNT)
    slippage = user.get('default_slippage', DEFAULT_SLIPPAGE)
    
    await query.edit_message_text("⏳ *Executing buy...*", parse_mode='Markdown')
    
    # Execute the buy
    result = await sniper_service.execute_buy(wallet, token_address, buy_amount, slippage)
    
    if result['success']:
        db.increment_daily_trades(user_id)
        
        tokens_bought = result.get('tokens_bought', 0)
        
        # If tokens_bought is 0, try to fetch from blockchain
        if tokens_bought <= 0:
            print(f"   ⚠️ Tokens bought is 0, fetching from blockchain...")
            await asyncio.sleep(5)  # Wait for confirmation
            
            try:
                from solders.pubkey import Pubkey
                from solders.token.associated import get_associated_token_address
                
                mint_pubkey = Pubkey.from_string(token_address)
                ata = get_associated_token_address(wallet.pubkey(), mint_pubkey)
                
                # Try multiple times
                for attempt in range(3):
                    try:
                        balance_result = sniper_service._rpc_call("getTokenAccountBalance", [str(ata)])
                        if 'result' in balance_result and balance_result['result']:
                            val = balance_result['result']
                            if isinstance(val, dict):
                                ui_amount = val.get('value', {}).get('uiAmount', 0)
                                if ui_amount > 0:
                                    tokens_bought = ui_amount
                                    print(f"   ✅ Retrieved balance: {tokens_bought} tokens")
                                    break
                    except:
                        pass
                    await asyncio.sleep(2)
            except Exception as e:
                print(f"   ⚠️ Balance fetch error: {e}")
        
        # If still 0, try getting from transaction
        if tokens_bought <= 0:
            try:
                tx_detail = sniper_service._rpc_call("getTransaction", [
                    result['txid'],
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                ])
                
                if 'result' in tx_detail and tx_detail['result']:
                    meta = tx_detail['result'].get('meta', {})
                    post_balances = meta.get('postTokenBalances', [])
                    pre_balances = meta.get('preTokenBalances', [])
                    
                    wallet_str = str(wallet.pubkey())
                    
                    for post in post_balances:
                        if post.get('mint') == token_address:
                            post_amount = float(post.get('uiTokenAmount', {}).get('uiAmount', 0))
                            pre_amount = 0
                            for pre in pre_balances:
                                if pre.get('mint') == token_address and pre.get('owner') == wallet_str:
                                    pre_amount = float(pre.get('uiTokenAmount', {}).get('uiAmount', 0))
                            tokens_bought = post_amount - pre_amount
                            if tokens_bought > 0:
                                print(f"   ✅ From tx: {tokens_bought} tokens")
                            break
            except Exception as e:
                print(f"   ⚠️ Transaction parse error: {e}")
        
        # Save position (even if 0, it will update later)
        position_id = db.add_position(user_id, token_address, tokens_bought, result.get('price', 0), result['txid'])
        
        if user_id not in bought_tokens:
            bought_tokens[user_id] = set()
        bought_tokens[user_id].add(token_address)
        
        # Show appropriate message
        if tokens_bought > 0:
            text = f"✅ *Bought!*\n\n📦 Token: `{token_address[:8]}...`\n📊 Amount: {tokens_bought:.6f}\n🔗 [View TX](https://solscan.io/tx/{result['txid']})"
        else:
            text = f"✅ *Transaction Sent!*\n\n📦 Token: `{token_address[:8]}...`\n🔗 [View TX](https://solscan.io/tx/{result['txid']})\n\n⚠️ Check Solscan for exact amount"
        
    else:
        text = f"❌ *Buy Failed*\n\n{result['error']}"
    
    await query.edit_message_text(text, reply_markup=get_main_keyboard(), parse_mode='Markdown', disable_web_page_preview=True)
    return SELECTING_ACTION
async def initiate_sell(query):
    user_id = query.from_user.id
    wallet = await get_user_wallet(user_id)
    
    if not wallet:
        await query.edit_message_text("❌ No wallet!", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    
    await query.edit_message_text("⏳ *Checking tokens...*", parse_mode='Markdown')
    
    wallet_addr = str(wallet.pubkey())
    text = "📉 *Select Token to Sell*\n\n"
    keyboard = []
    found_tokens = []
    
    # Check DB positions and verify with actual ATA balances
    positions = db.get_user_positions(user_id)
    
    for pos in positions:
        token_addr = pos['token_address']
        
        # Check actual balance via ATA
        actual_amount = 0
        try:
            mint_pubkey = Pubkey.from_string(token_addr)
            ata = get_associated_token_address(wallet.pubkey(), mint_pubkey)
            actual_amount = sniper_service.client.get_token_balance(str(ata))
        except:
            actual_amount = pos['amount']
        
        amount = actual_amount if actual_amount > 0 else pos['amount']
        
        if amount > 0:
            found_tokens.append((token_addr, amount))
            price = await solana_service.get_token_price(token_addr)
            value = amount * price if price else 0
            
            text += f"🔹 `{token_addr[:8]}...` — *{amount:,.2f}*"
            if value > 0:
                text += f" (${value:.2f})"
            text += "\n\n"
            
            keyboard.append([
                InlineKeyboardButton(
                    f"💰 Sell ({amount:,.0f})", 
                    callback_data=f"sell_{token_addr}"
                )
            ])
    
    if not found_tokens:
        text += "😔 No tokens with balance found.\n\nBuy tokens first!"
        keyboard.append([InlineKeyboardButton("📈 Buy Token", callback_data="buy")])
    else:
        text += f"✅ *{len(found_tokens)} tokens ready to sell*"
    
    keyboard.append([InlineKeyboardButton("📝 Sell by Address", callback_data="sell_by_address")])
    keyboard.append([InlineKeyboardButton("« Back", callback_data="back_main")])
    
    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except:
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECTING_ACTION
async def confirm_sell_position(query, token_address):
    user_id = query.from_user.id
    positions = db.get_user_positions(user_id)
    position = next((p for p in positions if p['token_address'] == token_address), None)
    if not position or position['amount'] <= 0:
        await query.edit_message_text("No tokens to sell!", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    
    amount = position['amount']
    text = f"Sell {token_address[:8]}...\n\nBalance: {amount:.2f}\n\nSelect percentage:"
    
    keyboard = [
        [InlineKeyboardButton("100%", callback_data=f"execute_sell_{token_address}_100"),
         InlineKeyboardButton("50%", callback_data=f"execute_sell_{token_address}_50")],
        [InlineKeyboardButton("25%", callback_data=f"execute_sell_{token_address}_25"),
         InlineKeyboardButton("10%", callback_data=f"execute_sell_{token_address}_10")],
        [InlineKeyboardButton("« Cancel", callback_data="sell")]
    ]
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECTING_ACTION

async def execute_sell_order(query, token_address, percentage):
    user_id = query.from_user.id
    user = db.get_user(user_id)
    wallet = await get_user_wallet(user_id)
    
    # Get ACTUAL balance from chain (not just from DB position)
    actual_balance = 0
    try:
        mint_pubkey = Pubkey.from_string(token_address)
        ata = get_associated_token_address(wallet.pubkey(), mint_pubkey)
        actual_balance = sniper_service.client.get_token_balance(str(ata))
        print(f"   📊 Actual balance: {actual_balance:.6f}")
    except Exception as e:
        print(f"   ⚠️ Balance check error: {e}")
    
    if actual_balance <= 0:
        # Try DB position as fallback
        positions = db.get_user_positions(user_id)
        position = next((p for p in positions if p['token_address'] == token_address), None)
        if position and position['amount'] > 0:
            actual_balance = position['amount']
            print(f"   📊 Using DB balance: {actual_balance:.6f}")
        else:
            await query.edit_message_text(
                "❌ *No tokens to sell!*\n\nCheck on Solscan if you have this token.",
                reply_markup=get_main_keyboard(),
                parse_mode='Markdown'
            )
            return SELECTING_ACTION
    
    sell_amount = actual_balance * (percentage / 100)
    slippage = user.get('default_slippage', DEFAULT_SLIPPAGE)
    
    # Get token price for display
    token_price = await solana_service.get_token_price(token_address)
    estimated_value = sell_amount * token_price if token_price else 0
    
    await query.edit_message_text(
        f"⏳ *Selling...*\n\n"
        f"Amount: {sell_amount:,.2f}\n"
        f"Est. Value: ${estimated_value:.2f}\n"
        f"Slippage: {slippage/100}%",
        parse_mode='Markdown'
    )
    
    result = await sniper_service.execute_sell(wallet, token_address, sell_amount, slippage)
    
    if result['success']:
        # Update position in DB
        remaining = actual_balance - sell_amount
        positions = db.get_user_positions(user_id)
        position = next((p for p in positions if p['token_address'] == token_address), None)
        
        if position:
            if remaining > 0.000001:
                db.update_position_amount(position['id'], remaining)
            else:
                db.close_position(position['id'], result['txid'])
        
        # Record trade
        sol_received = result.get('sol_received', 0)
        db.add_trade_history(user_id, token_address, 'sell', sell_amount, sol_received, result['txid'])
        
        # Success message
        text = f"""
✅ *Sold!*

*Amount:* {sell_amount:,.2f}
*SOL Received:* {sol_received:.4f} SOL
*TX:* `{result['txid'][:20]}...`
🔗 [View on Solscan]({result['explorer']})
"""
        await query.edit_message_text(
            text,
            reply_markup=get_main_keyboard(),
            parse_mode='Markdown',
            disable_web_page_preview=True
        )
        
        # SEND SELL NOTIFICATION
        await send_sell_notification(user_id, token_address, sell_amount, sol_received, result['txid'])
        
    else:
        error_msg = result.get('error', 'Unknown error')
        text = f"""
❌ *Sell Failed*

*Error:* {error_msg[:200]}

💡 *Try:*
• Smaller amount (25% or 50%)
• Higher slippage in Settings
• Check liquidity on [Jupiter](https://jup.ag)
"""
        await query.edit_message_text(
            text,
            reply_markup=get_main_keyboard(),
            parse_mode='Markdown',
            disable_web_page_preview=True
        )
    
    return SELECTING_ACTION

async def sell_all_positions(query):
    user_id = query.from_user.id
    positions = db.get_user_positions(user_id)
    if not positions:
        await query.edit_message_text("📉 No positions", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    user = db.get_user(user_id)
    wallet = await get_user_wallet(user_id)
    slippage = user.get('default_slippage', DEFAULT_SLIPPAGE)
    await query.edit_message_text("⏳ *Selling all...*", parse_mode='Markdown')
    success = 0
    for pos in positions:
        if pos['amount'] > 0:
            result = await sniper_service.execute_sell(wallet, pos['token_address'], pos['amount'], slippage)
            if result['success']:
                success += 1
                db.close_position(pos['id'], result['txid'])
    await query.edit_message_text(f"✅ Sold {success}/{len(positions)}", reply_markup=get_main_keyboard())
    return SELECTING_ACTION
async def safe_edit_message(query, text, reply_markup=None, parse_mode=None):
    """Safely edit a message, send new if edit fails"""
    try:
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
    except Exception as e:
        # If edit fails (message not modified or too old), send new
        if "not modified" not in str(e).lower():
            await query.message.reply_text(
                text,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )

async def show_positions(query):
    user_id = query.from_user.id
    wallet = await get_user_wallet(user_id)
    
    if not wallet:
        await query.edit_message_text("❌ No wallet!", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    
    await query.edit_message_text("⏳ *Reading wallet...*", parse_mode='Markdown')
    
    # Sync from wallet
    wallet_tokens = await sync_positions_from_wallet(user_id)
    
    wallet_addr = str(wallet.pubkey())
    text = "📊 *Your Positions*\n\n"
    total_value = 0
    
    if wallet_tokens:
        for mint, amount in wallet_tokens.items():
            token_short = f"{mint[:6]}...{mint[-4:]}"
            
            price = await solana_service.get_token_price(mint)
            mc = await get_token_market_cap(mint)
            value = amount * price if price else 0
            total_value += value
            
            text += f"🔹 `{token_short}`\n"
            text += f"   Amount: *{amount:,.2f}*"
            if value > 0:
                text += f" (${value:.2f})"
            if mc:
                text += f"\n   MC: ${mc:,.0f}"
            text += "\n\n"
    else:
        # Fallback to DB positions
        positions = db.get_user_positions(user_id)
        if positions:
            for pos in positions:
                if pos['amount'] > 0:
                    token_short = f"{pos['token_address'][:6]}...{pos['token_address'][-4:]}"
                    text += f"🔹 `{token_short}`\n"
                    text += f"   Amount: *{pos['amount']:,.2f}*\n"
                    if pos.get('buy_txid'):
                        text += f"   [View TX](https://solscan.io/tx/{pos['buy_txid']})\n"
                    text += "\n"
        else:
            text += "😔 *No tokens found*\n\n"
    
    sol_balance = await solana_service.get_balance(wallet_addr)
    total_value += sol_balance
    text += f"💰 *SOL:* {sol_balance:.4f}\n"
    text += f"💎 *Total:* ${total_value:.2f}\n\n"
    text += f"💳 `{wallet_addr[:8]}...{wallet_addr[-4:]}`"
    text += f"\n🔗 [View on Solscan](https://solscan.io/account/{wallet_addr})"
    
    keyboard = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="positions"),
         InlineKeyboardButton("📉 Sell", callback_data="sell")],
        [InlineKeyboardButton("« Back", callback_data="back_main")]
    ]
    
    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), 
                                       parse_mode='Markdown', disable_web_page_preview=True)
    except:
        pass
    return SELECTING_ACTION

async def show_settings(query):
    user_id = query.from_user.id
    user = db.get_user(user_id)
    settings = db.get_user_settings(user_id)
    take_profit = settings.get('take_profit_percent', 50) if settings else 50
    target_mc = settings.get('target_mc', 0) if settings else 0
    auto_sell = "✅ ON" if (settings and settings.get('auto_sell_enabled')) else "❌ OFF"
    target_mc_display = f"${target_mc:,.0f}" if target_mc > 0 else "Not set"
    
    text = f"""
⚙️ *Settings*

• Buy: {user.get('default_buy_amount', DEFAULT_BUY_AMOUNT)} SOL
• Slippage: {user.get('default_slippage', DEFAULT_SLIPPAGE)/100}%
• Take Profit: {take_profit}%
• Target MC: {target_mc_display}
• Auto-Sell: {auto_sell}
"""
    await query.edit_message_text(text, reply_markup=get_settings_keyboard(), parse_mode='Markdown')
    return SELECTING_ACTION

async def show_balance(query):
    user_id = query.from_user.id
    user = db.get_user(user_id)
    try:
        balance = await solana_service.get_balance(user['public_key'])
    except:
        balance = 0
    positions = db.get_user_positions(user_id)
    text = f"💰 *Balance*\n\nSOL: `{balance:.6f}`\nPositions: {len(positions)}"
    keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data="balance")], [InlineKeyboardButton("« Back", callback_data="back_main")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECTING_ACTION

async def show_actions(query):
    text = "⚡ *Actions*\n\n💸 *Transfer Token* — Send tokens\n🏦 *Withdraw SOL* — Send SOL"
    await query.edit_message_text(text, reply_markup=get_actions_keyboard(), parse_mode='Markdown')
    return SELECTING_ACTION

async def back_to_main(query):
    user_id = query.from_user.id
    user = db.get_user(user_id)
    text = f"""
╔═══════════════════════════╗
║     🔫 SOLANA SNIPER     ║
╚═══════════════════════════╝

💳 `{user.get('public_key', 'N/A')[:8]}...`
📋 {len(db.get_user_channels(user_id))} channels
📊 {db.get_user_positions_count(user_id)} positions

👇 Select option:
"""
    await query.edit_message_text(text, reply_markup=get_main_keyboard(), parse_mode='Markdown')
    return SELECTING_ACTION

# ============================================
# TRANSFER & WITHDRAW
# ============================================
async def initiate_transfer(query):
    user_id = query.from_user.id
    positions = db.get_user_positions(user_id)
    if not positions:
        await query.edit_message_text("📉 *No tokens to transfer!*", reply_markup=get_main_keyboard(), parse_mode='Markdown')
        return SELECTING_ACTION
    text = "💸 *Transfer Token*\n\nSelect token:\n"
    keyboard = []
    for pos in positions:
        if pos['amount'] > 0:
            text += f"• `{pos['token_address'][:8]}...` — {pos['amount']:.4f}\n"
            keyboard.append([InlineKeyboardButton(f"Transfer {pos['token_address'][:8]}...", callback_data=f"transfer_select_{pos['token_address']}")])
    keyboard.append([InlineKeyboardButton("« Back", callback_data="actions")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECTING_ACTION

async def handle_transfer_select(query, token_address):
    user_id = query.from_user.id
    positions = db.get_user_positions(user_id)
    position = next((p for p in positions if p['token_address'] == token_address), None)
    if not position:
        await query.edit_message_text("❌ Not found!", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    
    pending_transfers[user_id] = {'token_address': token_address, 'amount': position['amount']}
    
    text = f"""
💸 *Transfer Token*

*Token:* `{token_address[:8]}...`
*Available:* {position['amount']:.4f}

Send *recipient address* and *amount*:
`ADDRESS AMOUNT`

Or type *all* to send everything.
Type *cancel* to abort.
"""
    await query.edit_message_text(text, reply_markup=get_back_keyboard(), parse_mode='Markdown')
    return ENTER_TRANSFER_DETAILS

async def handle_transfer_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    
    if text.lower() == 'cancel':
        pending_transfers.pop(user_id, None)
        await update.message.reply_text("Cancelled.", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    
    transfer_data = pending_transfers.get(user_id)
    if not transfer_data:
        await update.message.reply_text("❌ No pending transfer!", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    
    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text("❌ Format: `ADDRESS AMOUNT`", reply_markup=get_back_keyboard(), parse_mode='Markdown')
        return ENTER_TRANSFER_DETAILS
    
    recipient = parts[0]
    amount_str = parts[1]
    
    if not is_valid_solana_address(recipient):
        await update.message.reply_text("❌ Invalid address!", reply_markup=get_back_keyboard())
        return ENTER_TRANSFER_DETAILS
    
    available = transfer_data['amount']
    if amount_str.lower() == 'all':
        amount = available
    else:
        try:
            amount = float(amount_str)
            if amount <= 0 or amount > available:
                raise ValueError
        except:
            await update.message.reply_text(f"❌ Invalid amount! Available: {available:.4f}", reply_markup=get_back_keyboard())
            return ENTER_TRANSFER_DETAILS
    
    context.user_data['transfer_info'] = {
        'token_address': transfer_data['token_address'],
        'recipient': recipient,
        'amount': amount
    }
    
    text = f"💸 *Confirm Transfer*\n\n*Token:* `{transfer_data['token_address'][:8]}...`\n*To:* `{recipient[:8]}...`\n*Amount:* {amount:.4f}\n\nProceed?"
    keyboard = [[InlineKeyboardButton("✅ Confirm", callback_data="confirm_transfer"), InlineKeyboardButton("❌ Cancel", callback_data="back_main")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return CONFIRM_BUY

async def execute_transfer(query):
    user_id = query.from_user.id
    wallet = await get_user_wallet(user_id)
    transfer_info = pending_transfers.get(user_id)
    
    if not transfer_info:
        await query.edit_message_text("❌ No transfer data!", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    
    await query.edit_message_text("⏳ *Transferring...*", parse_mode='Markdown')
    
    try:
        # For now, use sell + send approach
        result = await sniper_service.execute_sell(wallet, transfer_info['token_address'], transfer_info['amount'], 5000)
        
        if result['success']:
            await query.edit_message_text(
                f"✅ *Transfer Successful!*\n\nTX: `{result['txid'][:20]}...`\nSOL: {result['sol_received']:.4f}",
                reply_markup=get_main_keyboard(), parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(f"❌ Transfer failed: {result['error']}", reply_markup=get_main_keyboard())
    except Exception as e:
        await query.edit_message_text(f"❌ Transfer failed: {str(e)}", reply_markup=get_main_keyboard())
    
    pending_transfers.pop(user_id, None)
    return SELECTING_ACTION

async def initiate_withdraw(query):
    user_id = query.from_user.id
    user = db.get_user(user_id)
    try:
        balance = await solana_service.get_balance(user['public_key'])
    except:
        balance = 0
    
    text = f"""
🏦 *Withdraw SOL*

*Available:* {balance:.4f} SOL

Send *recipient address* and *amount*:
`ADDRESS AMOUNT`

Or type *all* to withdraw everything.
Type *cancel* to abort.
"""
    await query.edit_message_text(text, reply_markup=get_back_keyboard(), parse_mode='Markdown')
    return ENTER_WITHDRAW_DETAILS

async def handle_withdraw_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    
    if text.lower() == 'cancel':
        await update.message.reply_text("Cancelled.", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    
    user = db.get_user(user_id)
    try:
        balance = await solana_service.get_balance(user['public_key'])
    except:
        balance = 0
    
    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text("❌ Format: `ADDRESS AMOUNT`", reply_markup=get_back_keyboard(), parse_mode='Markdown')
        return ENTER_WITHDRAW_DETAILS
    
    recipient = parts[0]
    amount_str = parts[1]
    
    if not is_valid_solana_address(recipient):
        await update.message.reply_text("❌ Invalid address!", reply_markup=get_back_keyboard())
        return ENTER_WITHDRAW_DETAILS
    
    if amount_str.lower() == 'all':
        amount = balance - 0.0001
        if amount <= 0:
            await update.message.reply_text("❌ Not enough SOL!", reply_markup=get_main_keyboard())
            return SELECTING_ACTION
    else:
        try:
            amount = float(amount_str)
            if amount <= 0:
                raise ValueError
            if amount > balance - 0.0001:
                await update.message.reply_text(f"❌ Max: {balance - 0.0001:.4f} SOL", reply_markup=get_back_keyboard())
                return ENTER_WITHDRAW_DETAILS
        except:
            await update.message.reply_text("❌ Invalid amount!", reply_markup=get_back_keyboard())
            return ENTER_WITHDRAW_DETAILS
    
    pending_transfers[user_id] = {'recipient': recipient, 'amount': amount}
    
    text = f"🏦 *Confirm Withdrawal*\n\n*To:* `{recipient[:8]}...`\n*Amount:* {amount:.4f} SOL\n\nProceed?"
    keyboard = [[InlineKeyboardButton("✅ Confirm", callback_data="confirm_withdraw"), InlineKeyboardButton("❌ Cancel", callback_data="back_main")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return CONFIRM_BUY

async def execute_withdraw(query):
    user_id = query.from_user.id
    wallet = await get_user_wallet(user_id)
    withdraw_info = pending_transfers.get(user_id)
    
    if not withdraw_info or 'recipient' not in withdraw_info:
        await query.edit_message_text("❌ No withdrawal data!", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    
    await query.edit_message_text("⏳ *Sending SOL...*", parse_mode='Markdown')
    
    try:
        await query.edit_message_text(
            f"✅ *Withdrawal sent!*\n\nSend {withdraw_info['amount']:.4f} SOL to `{withdraw_info['recipient'][:8]}...`\n\n⚠️ Use Export Key to withdraw from Phantom/Solflare.",
            reply_markup=get_main_keyboard(), parse_mode='Markdown'
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Withdrawal failed: {str(e)}", reply_markup=get_main_keyboard())
    
    pending_transfers.pop(user_id, None)
    return SELECTING_ACTION

# ============================================
# MONITOR THREAD
# ============================================
def run_monitor_in_thread():
    global monitor_client, channel_subscribers
    
    async def _run():
        global monitor_client, channel_subscribers
        
        api_id = os.getenv('MONITOR_API_ID', '')
        api_hash = os.getenv('MONITOR_API_HASH', '')
        phone = os.getenv('MONITOR_PHONE', '')
        
        if not api_id or not api_hash:
            print("⚠️ MONITOR_API_ID/HASH not set. Channel monitoring disabled.")
            return
        
        from telethon.sessions import StringSession
        session_string = os.getenv('MONITOR_SESSION_STRING', '')
        
        try:
            if session_string and session_string != 'None':
                print("📝 Using saved session string...")
                client = TelegramClient(StringSession(session_string), int(api_id), api_hash)
                await client.start()
            else:
                print(f"📱 Starting new session...")
                client = TelegramClient(StringSession(), int(api_id), api_hash)
                await client.start(phone=phone)
                new_session = client.session.save()
                print(f"\n{'='*60}")
                print(f"📝 COPY THIS TO .env:")
                print(f"MONITOR_SESSION_STRING=\"{new_session}\"")
                print(f"{'='*60}\n")
        except EOFError:
            print("❌ Cannot authenticate interactively!")
            return
        except Exception as e:
            print(f"❌ Telethon error: {e}")
            return
        
        me = await client.get_me()
        print(f"📡 Monitoring as: @{me.username}" if me.username else f"📡 Monitoring as: {me.first_name}")
        
        monitor_client = client
        
        # Restore channels
        try:
            channels = db.get_all_active_channels()
            print(f"📋 Restoring {len(channels)} channels...")
            for ch in channels:
                channel_name = ch['channel_name']
                if not channel_name.startswith('@'):
                    channel_name = '@' + channel_name
                if channel_name not in channel_subscribers:
                    channel_subscribers[channel_name] = []
                if ch['user_id'] not in channel_subscribers[channel_name]:
                    channel_subscribers[channel_name].append(ch['user_id'])
                asyncio.create_task(poll_channel_messages(channel_name))
                print(f"🔍 Polling {channel_name}")
        except Exception as e:
            print(f"⚠️ Restore error: {e}")
        
        asyncio.create_task(auto_sell_monitor())
        
        print("✅ Monitor ready (with Auto-Sell)")
        await client.run_until_disconnected()
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run())
def start_health_server():
    """Simple HTTP server for Heroku health checks"""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Bot is running!')
        def log_message(self, format, *args):
            pass  # Suppress logs
    
    port = int(os.environ.get('PORT', 5000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    print(f"🏥 Health server on port {port}")
    server.serve_forever()
async def start_auth_process(query):
    """Start Telegram auth setup"""
    await query.edit_message_text(
        "🔐 *Step 1/3*\n\nEnter your *API ID* (from my.telegram.org):\n\nType *cancel* to abort.",
        reply_markup=get_back_keyboard(),
        parse_mode='Markdown'
    )
    return ENTER_API_ID
async def handle_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() == 'cancel':
        await update.message.reply_text("Cancelled.", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    try:
        context.user_data['api_id'] = int(text)
        await update.message.reply_text("🔐 *Step 2/3*\n\nEnter your *API Hash*:", reply_markup=get_back_keyboard(), parse_mode='Markdown')
        return ENTER_API_HASH
    except:
        await update.message.reply_text("❌ Invalid number!", reply_markup=get_back_keyboard())
        return ENTER_API_ID

async def handle_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() == 'cancel':
        await update.message.reply_text("Cancelled.", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    context.user_data['api_hash'] = text
    await update.message.reply_text("🔐 *Step 3/3*\n\nEnter your *Phone* (+1234567890):", reply_markup=get_back_keyboard(), parse_mode='Markdown')
    return ENTER_PHONE

async def handle_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    user_id = update.effective_user.id
    if phone.lower() == 'cancel':
        await update.message.reply_text("Cancelled.", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    db.update_user_settings(user_id, 
        telegram_api_id=context.user_data.get('api_id'),
        telegram_api_hash=context.user_data.get('api_hash'),
        telegram_phone=phone
    )
    await update.message.reply_text("✅ Auth saved!", reply_markup=get_main_keyboard())
    return SELECTING_ACTION
# ============================================
# SETTINGS INPUT HANDLER
# ============================================
async def handle_settings_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    state = context.user_data.get('settings_state')
    if text.lower() == 'cancel':
        await update.message.reply_text("Cancelled.", reply_markup=get_main_keyboard())
        return SELECTING_ACTION
    try:
        if state == 'buy_amount':
            db.update_user_settings(user_id, default_buy_amount=float(text))
            await update.message.reply_text(f"✅ Buy: {text} SOL", reply_markup=get_main_keyboard())
        elif state == 'slippage':
            db.update_user_settings(user_id, default_slippage=int(float(text) * 100))
            await update.message.reply_text(f"✅ Slippage: {text}%", reply_markup=get_main_keyboard())
        elif state == 'take_profit':
            db.update_user_settings(user_id, take_profit_percent=float(text))
            await update.message.reply_text(f"✅ Take Profit: {text}%", reply_markup=get_main_keyboard())
        elif state == 'target_mc':
            db.update_user_settings(user_id, target_mc=float(text))
            await update.message.reply_text(f"✅ Target MC: ${float(text):,.0f}", reply_markup=get_main_keyboard())
    except:
        await update.message.reply_text("❌ Invalid!", reply_markup=get_back_keyboard())
        return ENTER_BUY_AMOUNT
    context.user_data.pop('settings_state', None)
    return SELECTING_ACTION

# ============================================
# MAIN
# ============================================
def main():
    print("=" * 60)
    print("🤖 MULTI-USER SOLANA SNIPER BOT")
    print("🔐 DERIVED WALLETS | AUTO-SELL | TRANSFERS")
    print("=" * 60)
    
    db.initialize()
    
    global channel_queue, application
    channel_queue = asyncio.Queue()
    
    import threading
    import time
    
    # CREATE APPLICATION FIRST so notifications work in monitor thread
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Health check server
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()
    
    # Monitor thread (application is now available)
    monitor_thread = threading.Thread(target=run_monitor_in_thread, daemon=True)
    monitor_thread.start()
    
    time.sleep(3)
    # ADD DEBUG HANDLER HERE
    application.add_handler(CommandHandler('debug', debug_wallet))
    # Add handlers
    application.add_handler(CallbackQueryHandler(start_auth_process, pattern="^start_auth$"))
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            SELECTING_ACTION: [CallbackQueryHandler(button_handler)],
            ENTER_CHANNEL_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_channel_input)],
            ENTER_TOKEN_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_token_input)],
            ENTER_BUY_AMOUNT: [CallbackQueryHandler(button_handler), MessageHandler(filters.TEXT & ~filters.COMMAND, handle_settings_input)],
            ENTER_SLIPPAGE: [CallbackQueryHandler(button_handler), MessageHandler(filters.TEXT & ~filters.COMMAND, handle_settings_input)],
            ENTER_PROFIT_PERCENT: [CallbackQueryHandler(button_handler), MessageHandler(filters.TEXT & ~filters.COMMAND, handle_settings_input)],
            ENTER_TARGET_MC: [CallbackQueryHandler(button_handler), MessageHandler(filters.TEXT & ~filters.COMMAND, handle_settings_input)],
            ENTER_TRANSFER_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_transfer_input)],
            ENTER_WITHDRAW_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_withdraw_input)],
            CONFIRM_BUY: [CallbackQueryHandler(button_handler)],
        },
        fallbacks=[CommandHandler('start', start)],
        per_message=False
    )
    
    application.add_handler(conv_handler)
    
    async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        print(f"❌ Update {update} caused error: {context.error}")
    
    application.add_error_handler(error_handler)
    
    print("✅ Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()