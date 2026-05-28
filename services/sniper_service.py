"""
Sniper Service - NO solana package, uses only solders + requests
With PumpSwap direct sell for pump.fun tokens
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
from solders.instruction import Instruction, AccountMeta
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.message import MessageV0
from solders.hash import Hash
from typing import Optional


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
        result = self._rpc_call("sendTransaction", [tx_base58, {"skipPreflight": True, "preflightCommitment": "processed"}])
        if 'error' in result:
            raise Exception(result['error'].get('message', 'Send failed'))
        return result['result']
    
    def get_balance(self, pubkey: str) -> float:
        result = self._rpc_call("getBalance", [pubkey])
        if 'result' in result:
            val = result['result']
            if isinstance(val, dict):
                return val.get('value', 0) / 1e9
            return val / 1e9
        return 0
    
    def get_token_balance(self, token_account: str) -> float:
        result = self._rpc_call("getTokenAccountBalance", [token_account])
        if 'result' in result and result['result']:
            val = result['result']
            if isinstance(val, dict):
                return float(val.get('value', {}).get('uiAmount', val.get('uiAmount', 0)))
        return 0
    
    def get_latest_blockhash(self) -> str:
        result = self._rpc_call("getLatestBlockhash", [{"commitment": "processed"}])
        if 'result' in result:
            return result['result']['value']['blockhash']
        raise Exception("Failed to get blockhash")


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
                print(f"   ✅ DexScreener: {token[:8]}...")
                return token
        pattern = r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b'
        all_matches = re.findall(pattern, text)
        valid = [addr for addr in all_matches if self.is_valid_solana_address(addr)]
        return valid[-1] if valid else None
    
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
    
    async def execute_sell(self, wallet: Keypair, token_mint: str, amount_tokens: float, slippage_bps: int) -> dict:
        """Sell pump tokens via PumpSwap - direct program interaction"""
        try:
            from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
            import base64
            
            decimals = await self.get_token_decimals(token_mint)
            amount_raw = int(amount_tokens * 10**decimals)
            
            print(f"   💱 PumpSwap sell: {amount_tokens:,.2f} tokens ({amount_raw} raw)...")
            
            # PumpSwap Program ID
            PUMP_SWAP_PROGRAM = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwRh52yfVVHy")
            SOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
            TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
            
            mint = Pubkey.from_string(token_mint)
            user_ata = get_associated_token_address(wallet.pubkey(), mint)
            
            # Sort mints for pool derivation (PumpSwap sorts alphabetically)
            mint_a = bytes(mint) if bytes(mint) < bytes(SOL_MINT) else bytes(SOL_MINT)
            mint_b = bytes(SOL_MINT) if bytes(mint) < bytes(SOL_MINT) else bytes(mint)
            
            # Derive pool address
            pool, pool_bump = Pubkey.find_program_address([b"pool", mint_a, mint_b], PUMP_SWAP_PROGRAM)
            
            # Derive vaults
            token_vault, _ = Pubkey.find_program_address([b"vault", bytes(pool), bytes(mint)], PUMP_SWAP_PROGRAM)
            sol_vault, _ = Pubkey.find_program_address([b"vault", bytes(pool), bytes(SOL_MINT)], PUMP_SWAP_PROGRAM)
            
            # Derive pool authority
            authority, _ = Pubkey.find_program_address([b"authority", bytes(pool)], PUMP_SWAP_PROGRAM)
            
            # Also need user's SOL account (writable to receive SOL)
            user_sol_account = wallet.pubkey()
            
            print(f"   Pool: {str(pool)[:12]}...")
            print(f"   TokenVault: {str(token_vault)[:12]}...")
            print(f"   SolVault: {str(sol_vault)[:12]}...")
            print(f"   Authority: {str(authority)[:12]}...")
            
            # PumpSwap swap discriminator (8 bytes)
            SWAP_DISCRIMINATOR = 0x2a7b5c2e1f3d4a6b.to_bytes(8, 'little')
            
            # Instruction data
            data = SWAP_DISCRIMINATOR
            data += amount_raw.to_bytes(8, 'little')  # exact_input_amount
            data += int(0).to_bytes(8, 'little')       # min_output_amount (0 = no minimum)
            
            swap_ix = Instruction(
                program_id=PUMP_SWAP_PROGRAM,
                accounts=[
                    AccountMeta(pool, False, True),
                    AccountMeta(token_vault, False, True),
                    AccountMeta(sol_vault, False, True),
                    AccountMeta(user_ata, False, True),
                    AccountMeta(user_sol_account, True, True),
                    AccountMeta(authority, False, False),
                    AccountMeta(TOKEN_PROGRAM, False, False),
                ],
                data=data
            )
            
            instructions = [
                set_compute_unit_limit(300000),
                set_compute_unit_price(500000),
                swap_ix,
            ]
            
            blockhash_str = self.client.get_latest_blockhash()
            blockhash = Hash.from_string(blockhash_str)
            
            msg = MessageV0.try_compile(
                payer=wallet.pubkey(),
                instructions=instructions,
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            tx = VersionedTransaction(msg, [wallet])
            
            tx_base64 = base64.b64encode(bytes(tx)).decode()
            result = self._rpc_call("sendTransaction", [
                tx_base64,
                {
                    "encoding": "base64",
                    "skipPreflight": True,
                    "preflightCommitment": "processed",
                    "maxRetries": 3
                }
            ])
            
            if 'error' in result:
                err_msg = str(result['error'])[:300]
                print(f"   ⚠️ PumpSwap error: {err_msg}")
                
                # If account not found, the pool might not exist (use Jupiter)
                if "account" in err_msg.lower():
                    print(f"   Trying Jupiter instead...")
                    return await self.execute_jupiter_sell(wallet, token_mint, amount_tokens, slippage_bps)
                
                return {"success": False, "error": err_msg}
            
            txid = result.get('result', '')
            print(f"   ✅ PumpSwap SELL TXID: {txid}")
            
            return {
                "success": True,
                "txid": txid,
                "sol_received": 0,
                "explorer": f"https://solscan.io/tx/{txid}"
            }
            
        except Exception as e:
            print(f"   ❌ PumpSwap error: {e}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e)}
    async def execute_sell(self, wallet: Keypair, token_mint: str, amount_tokens: float, slippage_bps: int) -> dict:
        """Try PumpSwap first, fall back to Jupiter"""
        is_pump = token_mint.endswith('pump')
        
        if is_pump:
            print(f"   🎯 Pump token - trying PumpSwap...")
            result = await self.execute_sell_pumpswap(wallet, token_mint, amount_tokens, slippage_bps)
            if result['success']:
                return result
            print(f"   ⚠️ PumpSwap failed: {result.get('error', 'Unknown')[:100]}")
        
        return await self.execute_jupiter_sell(wallet, token_mint, amount_tokens, slippage_bps)
    # ============================================
    # BUY METHOD
    # ============================================
    
    async def execute_buy(self, wallet: Keypair, token_mint: str, amount_sol: float, slippage_bps: int) -> dict:
        """Execute buy - Read amount from transaction response"""
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
            
            # 3. Sign transaction
            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            raw_tx = VersionedTransaction.from_bytes(tx_bytes)
            message_bytes = message.to_bytes_versioned(raw_tx.message)
            signature = wallet.sign_message(message_bytes)
            signed_tx = VersionedTransaction.populate(raw_tx.message, [signature])
            signed_tx_bytes = bytes(signed_tx)
            
            # 4. Send transaction (FIXED - no TxOpts)
            print("   Sending transaction...")
            txid = self.client.send_raw_transaction(signed_tx_bytes)
            print(f"   ✅ TXID: {txid}")
            
            # 5. Get token amount from the transaction
            print("   ⏳ Fetching transaction details...")
            
            tokens_bought = 0
            decimals = await self.get_token_decimals(token_mint)
            
            # Wait for transaction to be available and parse it
            for attempt in range(8):
                await asyncio.sleep(2)
                try:
                    tx_detail = self._rpc_call("getTransaction", [
                        txid,
                        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                    ])
                    
                    if 'result' in tx_detail and tx_detail['result']:
                        meta = tx_detail['result'].get('meta', {})
                        post_balances = meta.get('postTokenBalances', [])
                        
                        for token in post_balances:
                            if token.get('mint') == token_mint:
                                amount_raw = token.get('uiTokenAmount', {}).get('uiAmount', 0)
                                tokens_bought = float(amount_raw) if amount_raw else 0
                                print(f"   📊 Attempt {attempt+1}: {tokens_bought:.6f} tokens")
                                break
                        
                        if tokens_bought > 0:
                            break
                    else:
                        print(f"   ⏳ Attempt {attempt+1}: Not available yet...")
                        
                except Exception as e:
                    print(f"   ⚠️ Attempt {attempt+1}: {e}")
            
            # Fallback: use quote's outputAmount
            if tokens_bought <= 0:
                raw_output = quote.get("outputAmount", "0")
                if raw_output != "0":
                    tokens_bought = int(raw_output) / 10**decimals
                    print(f"   📊 Using quote: {tokens_bought:.6f} tokens")
            
            print(f"   📊 Final: {tokens_bought:.6f} tokens")
            
            return {
                "success": True,
                "txid": txid,
                "tokens_bought": tokens_bought,
                "explorer": f"https://solscan.io/tx/{txid}"
            }
        except Exception as e:
            print(f"   ❌ Buy failed: {e}")
            return {"success": False, "error": str(e)}