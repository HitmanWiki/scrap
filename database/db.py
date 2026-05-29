"""
Database module - Neon PostgreSQL (Production) + SQLite (Local)
NO PRIVATE KEYS STORED!
Multi-Wallet Support
"""

import os
import sqlite3
import json
from datetime import datetime
from typing import Dict, List, Optional
import threading

try:
    import psycopg2
    import psycopg2.extras
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

class Database:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or os.environ.get('DATABASE_PATH', 'sniper_bot.db')
        self.db_type = 'postgres' if os.environ.get('DATABASE_URL', '').startswith('postgres') else 'sqlite'
        self.lock = threading.Lock()
        self.initialize()
    
    def get_connection(self):
        if self.db_type == 'postgres':
            db_url = os.environ.get('DATABASE_URL', '')
            conn = psycopg2.connect(db_url, sslmode='require')
            conn.autocommit = False
            return conn
        else:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn
    
    def get_cursor(self, conn):
        if self.db_type == 'postgres':
            return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            return conn.cursor()
    
    def placeholder(self):
        return '%s' if self.db_type == 'postgres' else '?'
    
    def initialize(self):
        with self.lock:
            conn = self.get_connection()
            cursor = self.get_cursor(conn)
            
            if self.db_type == 'postgres':
                self._init_postgres(cursor)
            else:
                self._init_sqlite(cursor)
            
            # Run migrations
            self._migrate(cursor, conn)
            
            conn.commit()
            conn.close()
            print(f"✅ Database initialized ({self.db_type})")
    
    def _migrate(self, cursor, conn):
        """Add new columns/tables if they don't exist"""
        if self.db_type == 'postgres':
            try:
                cursor.execute('ALTER TABLE channels ADD COLUMN IF NOT EXISTS wallet_id INT')
                cursor.execute('ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS wallet_id INT')
                cursor.execute('ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS channel_name TEXT')
                cursor.execute('ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS entry_price REAL')
                cursor.execute('ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS exit_price REAL')
                cursor.execute('ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS pnl_percent REAL')
                cursor.execute('ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS pnl_sol REAL')
                cursor.execute('ALTER TABLE positions ADD COLUMN IF NOT EXISTS wallet_id INT')
                conn.commit()
            except:
                pass
    
    def _init_postgres(self, cursor):
        """PostgreSQL tables"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                default_buy_amount REAL DEFAULT 0.01,
                default_slippage INTEGER DEFAULT 1000,
                max_daily_trades INTEGER DEFAULT 100,
                daily_trades INTEGER DEFAULT 0,
                telegram_api_id INTEGER,
                telegram_api_hash TEXT,
                telegram_phone TEXT,
                is_active BOOLEAN DEFAULT true,
                last_trade_reset TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS wallets (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                wallet_name VARCHAR(10) DEFAULT 'W1',
                wallet_number INT DEFAULT 1,
                public_key TEXT,
                default_buy_amount REAL DEFAULT 0.01,
                default_slippage INT DEFAULT 1000,
                take_profit_percent REAL DEFAULT 50,
                target_mc REAL DEFAULT 0,
                auto_sell_enabled BOOLEAN DEFAULT false,
                is_active BOOLEAN DEFAULT true,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                channel_name TEXT,
                wallet_id INT REFERENCES wallets(id),
                is_private BOOLEAN DEFAULT false,
                is_active BOOLEAN DEFAULT true,
                last_signal_at TIMESTAMP,
                signal_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS positions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                wallet_id INT REFERENCES wallets(id),
                token_address TEXT,
                token_symbol TEXT,
                amount REAL,
                entry_price REAL,
                current_price REAL,
                buy_txid TEXT,
                sell_txid TEXT,
                is_active BOOLEAN DEFAULT true,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trade_history (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                wallet_id INT,
                token_address TEXT,
                trade_type TEXT,
                amount REAL,
                price REAL,
                total_value REAL,
                txid TEXT,
                explorer_url TEXT,
                channel_name TEXT,
                entry_price REAL,
                exit_price REAL,
                pnl_percent REAL,
                pnl_sol REAL,
                status TEXT DEFAULT 'completed',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS snipe_logs (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                channel_name TEXT,
                token_address TEXT,
                message_text TEXT,
                status TEXT,
                txid TEXT,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
                auto_snipe BOOLEAN DEFAULT true,
                auto_sell_enabled BOOLEAN DEFAULT false,
                take_profit_percent REAL DEFAULT 50,
                target_mc REAL DEFAULT 0,
                stop_loss_percent REAL DEFAULT 20,
                max_slippage INTEGER DEFAULT 5000,
                notifications_enabled BOOLEAN DEFAULT true,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_positions_user ON positions(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_positions_active ON positions(user_id, is_active)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_channels_user ON channels(user_id, is_active)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_wallets_user ON wallets(user_id, is_active)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trade_history_user ON trade_history(user_id, created_at DESC)')
    
    def _init_sqlite(self, cursor):
        """SQLite tables (same structure, different syntax)"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                default_buy_amount REAL DEFAULT 0.01,
                default_slippage INTEGER DEFAULT 1000,
                max_daily_trades INTEGER DEFAULT 100,
                daily_trades INTEGER DEFAULT 0,
                telegram_api_id INTEGER,
                telegram_api_hash TEXT,
                telegram_phone TEXT,
                is_active BOOLEAN DEFAULT 1,
                last_trade_reset TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                wallet_name TEXT DEFAULT 'W1',
                wallet_number INTEGER DEFAULT 1,
                public_key TEXT,
                default_buy_amount REAL DEFAULT 0.01,
                default_slippage INTEGER DEFAULT 1000,
                take_profit_percent REAL DEFAULT 50,
                target_mc REAL DEFAULT 0,
                auto_sell_enabled BOOLEAN DEFAULT 0,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                channel_name TEXT,
                wallet_id INTEGER,
                is_private BOOLEAN DEFAULT 0,
                is_active BOOLEAN DEFAULT 1,
                last_signal_at TIMESTAMP,
                signal_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (wallet_id) REFERENCES wallets(id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                wallet_id INTEGER,
                token_address TEXT,
                token_symbol TEXT,
                amount REAL,
                entry_price REAL,
                current_price REAL,
                buy_txid TEXT,
                sell_txid TEXT,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (wallet_id) REFERENCES wallets(id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                wallet_id INTEGER,
                token_address TEXT,
                trade_type TEXT,
                amount REAL,
                price REAL,
                total_value REAL,
                txid TEXT,
                explorer_url TEXT,
                channel_name TEXT,
                entry_price REAL,
                exit_price REAL,
                pnl_percent REAL,
                pnl_sol REAL,
                status TEXT DEFAULT 'completed',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS snipe_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                channel_name TEXT,
                token_address TEXT,
                message_text TEXT,
                status TEXT,
                txid TEXT,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                auto_snipe BOOLEAN DEFAULT 1,
                auto_sell_enabled BOOLEAN DEFAULT 0,
                take_profit_percent REAL DEFAULT 50,
                target_mc REAL DEFAULT 0,
                stop_loss_percent REAL DEFAULT 20,
                max_slippage INTEGER DEFAULT 5000,
                notifications_enabled BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_positions_user ON positions(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_positions_active ON positions(user_id, is_active)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_channels_user ON channels(user_id, is_active)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_wallets_user ON wallets(user_id, is_active)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trade_history_user ON trade_history(user_id, created_at DESC)')
    
    # ============================================
    # USER OPERATIONS
    # ============================================
    def create_user(self, user_id: int, username: str) -> bool:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                
                cursor.execute(f'''
                    INSERT INTO users (user_id, username)
                    VALUES ({ph}, {ph})
                    ON CONFLICT (user_id) DO NOTHING
                ''', (user_id, username))
                
                cursor.execute(f'''
                    INSERT INTO user_settings (user_id)
                    VALUES ({ph})
                    ON CONFLICT (user_id) DO NOTHING
                ''', (user_id,))
                
                conn.commit()
                conn.close()
                return True
        except Exception as e:
            print(f"❌ Error creating user: {e}")
            return False
    
    def get_user(self, user_id: int) -> Optional[Dict]:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                cursor.execute(f'SELECT * FROM users WHERE user_id = {self.placeholder()}', (user_id,))
                row = cursor.fetchone()
                conn.close()
                return dict(row) if row else None
        except:
            return None
    
    def get_user_settings(self, user_id: int) -> Optional[Dict]:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                cursor.execute(f'SELECT * FROM user_settings WHERE user_id = {self.placeholder()}', (user_id,))
                row = cursor.fetchone()
                conn.close()
                return dict(row) if row else None
        except:
            return None
    
    def update_user_settings(self, user_id: int, **kwargs) -> bool:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                
                user_fields = ['default_buy_amount', 'default_slippage', 'max_daily_trades',
                              'telegram_api_id', 'telegram_api_hash', 'telegram_phone']
                settings_fields = ['auto_snipe', 'auto_sell_enabled', 'take_profit_percent',
                                  'target_mc', 'stop_loss_percent', 'max_slippage', 'notifications_enabled']
                boolean_fields = ['auto_snipe', 'auto_sell_enabled', 'notifications_enabled']
                
                user_updates = []
                user_values = []
                settings_updates = []
                settings_values = []
                
                for key, value in kwargs.items():
                    if key in user_fields:
                        user_updates.append(f"{key} = {ph}")
                        user_values.append(value)
                    elif key in settings_fields:
                        if key in boolean_fields and self.db_type == 'postgres':
                            settings_updates.append(f"{key} = {'TRUE' if value else 'FALSE'}")
                        else:
                            settings_updates.append(f"{key} = {ph}")
                            settings_values.append(1 if value else 0 if key in boolean_fields else value)
                
                if user_updates:
                    user_values.append(user_id)
                    cursor.execute(f"UPDATE users SET {', '.join(user_updates)}, updated_at = CURRENT_TIMESTAMP WHERE user_id = {ph}", user_values)
                
                if settings_updates:
                    cursor.execute(f'INSERT INTO user_settings (user_id) VALUES ({ph}) ON CONFLICT (user_id) DO NOTHING', (user_id,))
                    if self.db_type == 'postgres':
                        cursor.execute(f"UPDATE user_settings SET {', '.join(settings_updates)}, updated_at = CURRENT_TIMESTAMP WHERE user_id = {ph}", (user_id,))
                    else:
                        settings_values.append(user_id)
                        cursor.execute(f"UPDATE user_settings SET {', '.join(settings_updates)}, updated_at = CURRENT_TIMESTAMP WHERE user_id = {ph}", settings_values)
                
                conn.commit()
                conn.close()
                return True
        except Exception as e:
            print(f"❌ Error updating settings: {e}")
            return False
    
    def increment_daily_trades(self, user_id: int) -> bool:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                
                if self.db_type == 'postgres':
                    cursor.execute(f'''
                        UPDATE users SET daily_trades = CASE 
                            WHEN DATE(last_trade_reset) < CURRENT_DATE OR last_trade_reset IS NULL THEN 1
                            ELSE daily_trades + 1 END,
                            last_trade_reset = CASE 
                            WHEN DATE(last_trade_reset) < CURRENT_DATE OR last_trade_reset IS NULL THEN CURRENT_TIMESTAMP
                            ELSE last_trade_reset END,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE user_id = {ph}
                    ''', (user_id,))
                else:
                    cursor.execute(f'''
                        UPDATE users SET daily_trades = CASE 
                            WHEN DATE(last_trade_reset) < DATE('now') OR last_trade_reset IS NULL THEN 1
                            ELSE daily_trades + 1 END,
                            last_trade_reset = CASE 
                            WHEN DATE(last_trade_reset) < DATE('now') OR last_trade_reset IS NULL THEN CURRENT_TIMESTAMP
                            ELSE last_trade_reset END,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE user_id = {ph}
                    ''', (user_id,))
                
                conn.commit()
                conn.close()
                return True
        except Exception as e:
            print(f"❌ Error incrementing trades: {e}")
            return False
    
    # ============================================
    # WALLET OPERATIONS
    # ============================================
    def create_wallet(self, user_id: int, wallet_name: str = 'W1', wallet_number: int = 1) -> int:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                
                cursor.execute(f'SELECT COUNT(*) as cnt FROM wallets WHERE user_id = {ph} AND is_active = true', (user_id,))
                count = cursor.fetchone()['cnt']
                if count >= 5:
                    conn.close()
                    return -1
                
                cursor.execute(f'''
                    INSERT INTO wallets (user_id, wallet_name, wallet_number)
                    VALUES ({ph}, {ph}, {ph})
                ''', (user_id, wallet_name, wallet_number))
                
                if self.db_type == 'postgres':
                    cursor.execute('SELECT LASTVAL() as id')
                    wallet_id = cursor.fetchone()['id']
                else:
                    wallet_id = cursor.lastrowid
                
                conn.commit()
                conn.close()
                return wallet_id
        except Exception as e:
            print(f"❌ Error creating wallet: {e}")
            return -1
    
    def get_user_wallets(self, user_id: int) -> List[Dict]:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                cursor.execute(f'SELECT * FROM wallets WHERE user_id = {self.placeholder()} AND is_active = true ORDER BY wallet_number', (user_id,))
                rows = cursor.fetchall()
                conn.close()
                return [dict(row) for row in rows]
        except:
            return []
    
    def get_wallet(self, wallet_id: int) -> Optional[Dict]:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                cursor.execute(f'SELECT * FROM wallets WHERE id = {self.placeholder()}', (wallet_id,))
                row = cursor.fetchone()
                conn.close()
                return dict(row) if row else None
        except:
            return None
    
    def update_wallet_settings(self, wallet_id: int, **kwargs) -> bool:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                
                updates = []
                values = []
                for key, value in kwargs.items():
                    updates.append(f"{key} = {ph}")
                    values.append(value)
                
                if updates:
                    values.append(wallet_id)
                    cursor.execute(f"UPDATE wallets SET {', '.join(updates)} WHERE id = {ph}", values)
                
                conn.commit()
                conn.close()
                return True
        except:
            return False
    
    def update_channel_wallet(self, channel_id: int, wallet_id: int) -> bool:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                cursor.execute(f'UPDATE channels SET wallet_id = {self.placeholder()} WHERE id = {self.placeholder()}', (wallet_id, channel_id))
                conn.commit()
                conn.close()
                return True
        except:
            return False
    
    # ============================================
    # CHANNEL OPERATIONS
    # ============================================
    def add_channel(self, user_id: int, channel_name: str, wallet_id: int = None, is_private: bool = False) -> int:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                
                cursor.execute(f'SELECT id FROM channels WHERE user_id = {ph} AND channel_name = {ph}', (user_id, channel_name))
                existing = cursor.fetchone()
                
                if existing:
                    if wallet_id:
                        cursor.execute(f'UPDATE channels SET is_active = true, wallet_id = {ph} WHERE id = {ph}', (wallet_id, existing['id']))
                    else:
                        cursor.execute(f'UPDATE channels SET is_active = true WHERE id = {ph}', (existing['id'],))
                    conn.commit()
                    conn.close()
                    return existing['id']
                
                cursor.execute(f'''
                    INSERT INTO channels (user_id, channel_name, wallet_id, is_private)
                    VALUES ({ph}, {ph}, {ph}, {ph})
                ''', (user_id, channel_name, wallet_id, is_private))
                
                if self.db_type == 'postgres':
                    cursor.execute('SELECT LASTVAL() as id')
                    channel_id = cursor.fetchone()['id']
                else:
                    channel_id = cursor.lastrowid
                
                conn.commit()
                conn.close()
                return channel_id
        except Exception as e:
            print(f"❌ Error adding channel: {e}")
            return -1
    
    def get_user_channels(self, user_id: int) -> List[Dict]:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                cursor.execute(f'SELECT * FROM channels WHERE user_id = {self.placeholder()} AND is_active = true ORDER BY created_at DESC', (user_id,))
                rows = cursor.fetchall()
                conn.close()
                return [dict(row) for row in rows]
        except:
            return []
    
    def get_all_active_channels(self) -> List[Dict]:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                cursor.execute('''
                    SELECT c.*, u.user_id as uid
                    FROM channels c
                    JOIN users u ON c.user_id = u.user_id
                    WHERE c.is_active = true AND u.is_active = true
                ''')
                rows = cursor.fetchall()
                conn.close()
                return [dict(row) for row in rows]
        except:
            return []
    
    def deactivate_channel(self, channel_id: int) -> bool:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                cursor.execute(f'UPDATE channels SET is_active = false WHERE id = {self.placeholder()}', (channel_id,))
                conn.commit()
                conn.close()
                return True
        except:
            return False
    
    # ============================================
    # POSITION OPERATIONS
    # ============================================
    def add_position(self, user_id: int, token_address: str, amount: float,
                    entry_price: float, txid: str, wallet_id: int = None, token_symbol: str = None) -> int:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                
                cursor.execute(f'''
                    SELECT id, amount, entry_price FROM positions 
                    WHERE user_id = {ph} AND token_address = {ph} AND is_active = true
                ''', (user_id, token_address))
                existing = cursor.fetchone()
                
                if existing:
                    new_amount = existing['amount'] + amount
                    new_entry = ((existing['entry_price'] * existing['amount']) + (entry_price * amount)) / new_amount if new_amount > 0 else 0
                    cursor.execute(f'UPDATE positions SET amount = {ph}, entry_price = {ph}, updated_at = CURRENT_TIMESTAMP WHERE id = {ph}',
                                  (new_amount, new_entry, existing['id']))
                    conn.commit()
                    conn.close()
                    return existing['id']
                
                cursor.execute(f'''
                    INSERT INTO positions (user_id, wallet_id, token_address, token_symbol, amount, entry_price, buy_txid)
                    VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                ''', (user_id, wallet_id, token_address, token_symbol, amount, entry_price, txid))
                
                if self.db_type == 'postgres':
                    cursor.execute('SELECT LASTVAL() as id')
                    position_id = cursor.fetchone()['id']
                else:
                    position_id = cursor.lastrowid
                
                conn.commit()
                conn.close()
                return position_id
        except Exception as e:
            print(f"❌ Error adding position: {e}")
            return -1
    
    def update_position_amount(self, position_id: int, new_amount: float) -> bool:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                cursor.execute(f'UPDATE positions SET amount = {self.placeholder()}, updated_at = CURRENT_TIMESTAMP WHERE id = {self.placeholder()}', 
                              (new_amount, position_id))
                conn.commit()
                conn.close()
                return True
        except:
            return False
    
    def get_user_positions(self, user_id: int, active_only: bool = True) -> List[Dict]:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                if active_only:
                    cursor.execute(f'SELECT * FROM positions WHERE user_id = {self.placeholder()} AND is_active = true ORDER BY created_at DESC', (user_id,))
                else:
                    cursor.execute(f'SELECT * FROM positions WHERE user_id = {self.placeholder()} ORDER BY created_at DESC', (user_id,))
                rows = cursor.fetchall()
                conn.close()
                return [dict(row) for row in rows]
        except:
            return []
    
    def get_user_positions_count(self, user_id: int) -> int:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                cursor.execute(f'SELECT COUNT(*) as count FROM positions WHERE user_id = {self.placeholder()} AND is_active = true', (user_id,))
                result = cursor.fetchone()
                conn.close()
                return result['count'] if result else 0
        except:
            return 0
    
    def get_all_active_positions(self) -> List[Dict]:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                cursor.execute('''
                    SELECT p.*, u.username
                    FROM positions p
                    JOIN users u ON p.user_id = u.user_id
                    WHERE p.is_active = true
                ''')
                rows = cursor.fetchall()
                conn.close()
                return [dict(row) for row in rows]
        except:
            return []
    
    def close_position(self, position_id: int, sell_txid: str = None) -> bool:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                cursor.execute(f'UPDATE positions SET is_active = false, sell_txid = {self.placeholder()}, updated_at = CURRENT_TIMESTAMP WHERE id = {self.placeholder()}', 
                              (sell_txid, position_id))
                conn.commit()
                conn.close()
                return True
        except:
            return False
    
    def get_user_position_by_token(self, user_id: int, token_address: str) -> Optional[Dict]:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                cursor.execute(f'SELECT * FROM positions WHERE user_id = {self.placeholder()} AND token_address = {self.placeholder()} AND is_active = true', 
                              (user_id, token_address))
                row = cursor.fetchone()
                conn.close()
                return dict(row) if row else None
        except:
            return None
    
    # ============================================
    # TRADE HISTORY
    # ============================================
    def add_trade_history(self, user_id: int, token_address: str, trade_type: str,
                         amount: float, price: float, txid: str, 
                         wallet_id: int = None, channel_name: str = None,
                         entry_price: float = 0, exit_price: float = 0,
                         explorer_url: str = None, status: str = 'completed') -> int:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                
                total_value = amount * price if price > 0 else 0
                if not explorer_url and txid:
                    explorer_url = f"https://solscan.io/tx/{txid}"
                
                pnl_sol = (amount * exit_price) - (amount * entry_price) if exit_price and entry_price else 0
                pnl_percent = ((exit_price - entry_price) / entry_price * 100) if entry_price and entry_price > 0 else 0
                
                cursor.execute(f'''
                    INSERT INTO trade_history 
                    (user_id, wallet_id, token_address, trade_type, amount, price, total_value, txid, explorer_url, 
                     channel_name, entry_price, exit_price, pnl_percent, pnl_sol, status)
                    VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                ''', (user_id, wallet_id, token_address, trade_type, amount, price, total_value, 
                     txid, explorer_url, channel_name, entry_price, exit_price, pnl_percent, pnl_sol, status))
                
                if self.db_type == 'postgres':
                    cursor.execute('SELECT LASTVAL() as id')
                    trade_id = cursor.fetchone()['id']
                else:
                    trade_id = cursor.lastrowid
                
                conn.commit()
                conn.close()
                return trade_id
        except Exception as e:
            print(f"❌ Error adding trade: {e}")
            return -1
    
    def get_user_trade_history(self, user_id: int, limit: int = 50) -> List[Dict]:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                cursor.execute(f'SELECT * FROM trade_history WHERE user_id = {ph} ORDER BY created_at DESC LIMIT {limit}', (user_id,))
                rows = cursor.fetchall()
                conn.close()
                result = [dict(row) for row in rows] if rows else []
                return result
        except Exception as e:
            print(f"❌ Error getting trade history: {e}")
            return []  # Always return empty list on error
        
    # ============================================
    # SNIPE LOGS
    # ============================================
    def add_snipe_log(self, user_id: int, channel_name: str, token_address: str,
                     message_text: str = None, status: str = 'success', 
                     txid: str = None, error_message: str = None) -> int:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                cursor.execute(f'''
                    INSERT INTO snipe_logs (user_id, channel_name, token_address, message_text, status, txid, error_message)
                    VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                ''', (user_id, channel_name, token_address, message_text, status, txid, error_message))
                
                if self.db_type == 'postgres':
                    cursor.execute('SELECT LASTVAL() as id')
                    log_id = cursor.fetchone()['id']
                else:
                    log_id = cursor.lastrowid
                
                conn.commit()
                conn.close()
                return log_id
        except:
            return -1
    
    # ============================================
    # UTILITY
    # ============================================
    def delete_user_data(self, user_id: int) -> bool:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                tables = ['positions', 'channels', 'trade_history', 'snipe_logs', 'wallets', 'user_settings']
                for table in tables:
                    cursor.execute(f'DELETE FROM {table} WHERE user_id = {ph}', (user_id,))
                cursor.execute(f'DELETE FROM users WHERE user_id = {ph}', (user_id,))
                conn.commit()
                conn.close()
                return True
        except:
            return False