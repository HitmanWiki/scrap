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
    
    # ============================================
    # PUMP.FUN DIRECT SELL
    # ============================================
    
    async def execute_pump_sell_direct(self, wallet: Keypair, token_mint: str, amount_tokens: float, slippage_bps: int) -> dict:
        """Sell pump.fun tokens directly using Pump.fun program"""
        try:
            from solders.instruction import Instruction, AccountMeta
            from solders.system_program import ID as SYSTEM_PROGRAM_ID
            from solders.token.associated import get_associated_token_address
            from solders.pubkey import Pubkey
            from solders.transaction import VersionedTransaction
            from solders.message import MessageV0
            from solders.hash import Hash
            from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
            import base64
            
            decimals = await self.get_token_decimals(token_mint)
            amount_raw = int(amount_tokens * 10**decimals)
            
            # Pump.fun program IDs
            PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
            PUMP_GLOBAL = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
            PUMP_FEE = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VVAYAbxc9YetpAMWd")
            PUMP_EVENT = Pubkey.from_string("Ce6TQqeHC1p8KedsN6JpyhTLxLkZRaL2gPZQuJwH6mSA")
            TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
            ASSOC_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
            
            mint = Pubkey.from_string(token_mint)
            user_ata = get_associated_token_address(wallet.pubkey(), mint)
            
            # Derive bonding curve PDA
            bonding_curve, bump = Pubkey.find_program_address(
                [b"bonding-curve", bytes(mint)], PUMP_PROGRAM
            )
            
            # Derive associated bonding curve
            assoc_bonding_curve, _ = Pubkey.find_program_address(
                [bytes(bonding_curve), bytes(TOKEN_PROGRAM), bytes(mint)],
                ASSOC_TOKEN_PROGRAM
            )
            
            # Check if assoc_bonding_curve exists, if not create it
            create_ata_ix = None
            account_info = self._rpc_call("getAccountInfo", [str(assoc_bonding_curve)])
            if 'result' in account_info and account_info['result'] is None:
                # Need to create the associated token account
                from spl.token.instructions import create_associated_token_account, CreateAssociatedTokenAccountParams
                create_ata_ix = create_associated_token_account(
                    CreateAssociatedTokenAccountParams(
                        payer=wallet.pubkey(),
                        owner=bonding_curve,
                        mint=mint,
                        token_program_id=TOKEN_PROGRAM,
                        associated_token_program_id=ASSOC_TOKEN_PROGRAM,
                    )
                )
                print("   Creating associated bonding curve account...")
            
            # Sell discriminator
            SELL_DISCRIMINATOR = 12502976635542562355
            
            # Build sell instruction data
            data = bytearray()
            data.extend(SELL_DISCRIMINATOR.to_bytes(8, 'little'))
            data.extend(amount_raw.to_bytes(8, 'little'))
            data.extend(int(0).to_bytes(8, 'little'))  # min_sol_output
            
            sell_ix = Instruction(
                program_id=PUMP_PROGRAM,
                accounts=[
                    AccountMeta(PUMP_GLOBAL, False, False),
                    AccountMeta(PUMP_FEE, False, True),
                    AccountMeta(mint, False, False),
                    AccountMeta(bonding_curve, False, True),
                    AccountMeta(assoc_bonding_curve, False, True),
                    AccountMeta(user_ata, False, True),
                    AccountMeta(wallet.pubkey(), True, True),
                    AccountMeta(SYSTEM_PROGRAM_ID, False, False),
                    AccountMeta(ASSOC_TOKEN_PROGRAM, False, False),
                    AccountMeta(TOKEN_PROGRAM, False, False),
                    AccountMeta(PUMP_EVENT, False, False),
                    AccountMeta(PUMP_PROGRAM, False, False),
                ],
                data=bytes(data)
            )
            
            # Build instructions
            instructions = [
                set_compute_unit_limit(300000),
                set_compute_unit_price(100000),
            ]
            
            if create_ata_ix:
                instructions.append(create_ata_ix)
            
            instructions.append(sell_ix)
            
            # Get blockhash
            blockhash_str = self.client.get_latest_blockhash()
            blockhash = Hash.from_string(blockhash_str)
            
            # Build message
            msg = MessageV0.try_compile(
                payer=wallet.pubkey(),
                instructions=instructions,
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            
            tx = VersionedTransaction(msg, [wallet])
            
            # Send with base64
            tx_base64 = base64.b64encode(bytes(tx)).decode()
            result = self._rpc_call("sendTransaction", [
                tx_base64,
                {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "preflightCommitment": "processed",
                    "maxRetries": 3
                }
            ])
            
            if 'error' in result:
                err_msg = result['error'].get('message', str(result['error']))
                print(f"   ⚠️ RPC Error: {err_msg}")
                return {"success": False, "error": err_msg}
            
            txid = result.get('result', '')
            print(f"   ✅ PumpSell TXID: {txid}")
            
            return {
                "success": True,
                "txid": txid,
                "sol_received": 0,
                "explorer": f"https://solscan.io/tx/{txid}"
            }
            
        except Exception as e:
            print(f"   ❌ PumpSell error: {e}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e)}
        
    # ============================================
    # JUPITER SELL
    # ============================================
    
    async def execute_jupiter_sell(self, wallet: Keypair, token_mint: str, amount_tokens: float, slippage_bps: int) -> dict:
        """Sell tokens using Jupiter with multiple fallback routes"""
        try:
            decimals = await self.get_token_decimals(token_mint)
            amount_raw = int(amount_tokens * 10**decimals)
            
            output_mints = [
                ("So11111111111111111111111111111111111111112", 9),
                ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 6),
                ("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", 6),
            ]
            
            for output_mint, output_decimals in output_mints:
                for percentage in [1.0, 0.5, 0.25, 0.1]:
                    test_raw = int(amount_raw * percentage)
                    if test_raw < 100:
                        continue
                    
                    quote_url = "https://lite-api.jup.ag/swap/v1/quote"
                    params = {
                        "inputMint": token_mint,
                        "outputMint": output_mint,
                        "amount": test_raw,
                        "slippageBps": max(slippage_bps, 9000),
                    }
                    
                    try:
                        resp = requests.get(quote_url, params=params, timeout=10)
                        if resp.status_code != 200:
                            continue
                        quote = resp.json()
                        if quote.get("outputAmount", "0") == "0":
                            continue
                        
                        expected_output = int(quote["outputAmount"]) / (10 ** output_decimals)
                        print(f"   ✅ Route: {expected_output:.6f}")
                        
                        swap_url = "https://lite-api.jup.ag/swap/v1/swap"
                        payload = {
                            "quoteResponse": quote,
                            "userPublicKey": str(wallet.pubkey()),
                            "wrapAndUnwrapSol": True,
                            "dynamicComputeUnitLimit": True,
                        }
                        resp = requests.post(swap_url, json=payload, timeout=10)
                        if resp.status_code != 200:
                            continue
                        swap_data = resp.json()
                        if "swapTransaction" not in swap_data:
                            continue
                        
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
                    except:
                        continue
            
            return {"success": False, "error": "No routes found"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    # ============================================
    # MAIN SELL METHOD
    # ============================================
    
    async def execute_sell(self, wallet: Keypair, token_mint: str, amount_tokens: float, slippage_bps: int) -> dict:
        """Execute sell - PumpSwap for pump tokens, Jupiter for others"""
        is_pump = token_mint.endswith('pump')
        
        if is_pump:
            print(f"   🎯 Pump token - using PumpSwap")
            result = await self.execute_pump_sell_direct(wallet, token_mint, amount_tokens, slippage_bps)
            if result['success']:
                return result
            print(f"   ⚠️ PumpSwap failed, trying Jupiter...")
        
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