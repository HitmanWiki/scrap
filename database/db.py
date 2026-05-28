"""
Database module - Supports SQLite (local) and Neon PostgreSQL (production)
NO PRIVATE KEYS STORED!
"""

import os
import sqlite3
import json
from datetime import datetime
from typing import Dict, List, Optional
import threading

# Try to import PostgreSQL driver
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
        """Get appropriate database connection"""
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
        """Get cursor based on db type"""
        if self.db_type == 'postgres':
            return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            return conn.cursor()
    
    def placeholder(self):
        """Return appropriate placeholder for parameterized queries"""
        return '%s' if self.db_type == 'postgres' else '?'
    
    def initialize(self):
        """Create all tables"""
        with self.lock:
            conn = self.get_connection()
            cursor = self.get_cursor(conn)
            
            if self.db_type == 'postgres':
                self._init_postgres(cursor)
            else:
                self._init_sqlite(cursor)
            
            conn.commit()
            conn.close()
            print(f"✅ Database initialized ({self.db_type})")
    
    def _init_postgres(self, cursor):
        """PostgreSQL table creation"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                public_key TEXT,
                default_buy_amount REAL DEFAULT 0.01,
                default_slippage INTEGER DEFAULT 1000,
                max_daily_trades INTEGER DEFAULT 10,
                daily_trades INTEGER DEFAULT 0,
                telegram_api_id INTEGER,
                telegram_api_hash TEXT,
                telegram_phone TEXT,
                telegram_session TEXT,
                is_active BOOLEAN DEFAULT true,
                last_trade_reset TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                channel_name TEXT,
                channel_id TEXT,
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
                token_address TEXT,
                trade_type TEXT,
                amount REAL,
                price REAL,
                total_value REAL,
                txid TEXT,
                explorer_url TEXT,
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
                buy_gas_fee REAL DEFAULT 0.001,
                sell_gas_fee REAL DEFAULT 0.001,
                notifications_enabled BOOLEAN DEFAULT true,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS wallet_history (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                old_public_key TEXT,
                new_public_key TEXT,
                action TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_positions_user ON positions(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_positions_active ON positions(user_id, is_active)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_channels_user ON channels(user_id, is_active)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trade_history_user ON trade_history(user_id, created_at DESC)')
    
    def _init_sqlite(self, cursor):
        """SQLite table creation"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                public_key TEXT,
                default_buy_amount REAL DEFAULT 0.01,
                default_slippage INTEGER DEFAULT 1000,
                max_daily_trades INTEGER DEFAULT 10,
                daily_trades INTEGER DEFAULT 0,
                telegram_api_id INTEGER,
                telegram_api_hash TEXT,
                telegram_phone TEXT,
                telegram_session TEXT,
                is_active BOOLEAN DEFAULT 1,
                last_trade_reset TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                channel_name TEXT,
                channel_id TEXT,
                is_private BOOLEAN DEFAULT 0,
                is_active BOOLEAN DEFAULT 1,
                last_signal_at TIMESTAMP,
                signal_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
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
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                token_address TEXT,
                trade_type TEXT,
                amount REAL,
                price REAL,
                total_value REAL,
                txid TEXT,
                explorer_url TEXT,
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
                buy_gas_fee REAL DEFAULT 0.001,
                sell_gas_fee REAL DEFAULT 0.001,
                notifications_enabled BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS wallet_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                old_public_key TEXT,
                new_public_key TEXT,
                action TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        
        # Create indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_positions_user ON positions(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_positions_active ON positions(user_id, is_active)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_channels_user ON channels(user_id, is_active)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trade_history_user ON trade_history(user_id, created_at DESC)')
    
    # ============================================
    # USER OPERATIONS
    # ============================================
    def create_user(self, user_id: int, username: str, public_key: str = None) -> bool:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                
                cursor.execute(f'''
                    INSERT INTO users (user_id, username, public_key)
                    VALUES ({ph}, {ph}, {ph})
                    ON CONFLICT (user_id) DO NOTHING
                ''', (user_id, username, public_key))
                
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
                ph = self.placeholder()
                
                cursor.execute(f'SELECT * FROM users WHERE user_id = {ph}', (user_id,))
                row = cursor.fetchone()
                conn.close()
                return dict(row) if row else None
        except Exception as e:
            print(f"❌ Error getting user: {e}")
            return None
    
    def update_user_settings(self, user_id: int, **kwargs) -> bool:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                
                user_fields = ['default_buy_amount', 'default_slippage', 'max_daily_trades',
                              'telegram_api_id', 'telegram_api_hash', 'telegram_phone', 
                              'telegram_session', 'public_key']
                
                settings_fields = ['auto_snipe', 'auto_sell_enabled', 'take_profit_percent',
                                  'target_mc', 'stop_loss_percent', 'max_slippage',
                                  'buy_gas_fee', 'sell_gas_fee', 'notifications_enabled']
                
                user_updates = []
                user_values = []
                settings_updates = []
                settings_values = []
                
                for key, value in kwargs.items():
                    if key in user_fields:
                        user_updates.append(f"{key} = {ph}")
                        user_values.append(value)
                    elif key in settings_fields:
                        settings_updates.append(f"{key} = {ph}")
                        settings_values.append(value)
                
                if user_updates:
                    user_values.append(user_id)
                    query = f"UPDATE users SET {', '.join(user_updates)}, updated_at = CURRENT_TIMESTAMP WHERE user_id = {ph}"
                    cursor.execute(query, user_values)
                
                if settings_updates:
                    cursor.execute(f'INSERT INTO user_settings (user_id) VALUES ({ph}) ON CONFLICT (user_id) DO NOTHING', (user_id,))
                    settings_values.append(user_id)
                    query = f"UPDATE user_settings SET {', '.join(settings_updates)}, updated_at = CURRENT_TIMESTAMP WHERE user_id = {ph}"
                    cursor.execute(query, settings_values)
                
                conn.commit()
                conn.close()
                return True
        except Exception as e:
            print(f"❌ Error updating settings: {e}")
            return False
    
    def get_user_settings(self, user_id: int) -> Optional[Dict]:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                cursor.execute(f'SELECT * FROM user_settings WHERE user_id = {ph}', (user_id,))
                row = cursor.fetchone()
                conn.close()
                return dict(row) if row else None
        except Exception as e:
            print(f"❌ Error getting settings: {e}")
            return None
    
    def increment_daily_trades(self, user_id: int) -> bool:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                
                if self.db_type == 'postgres':
                    cursor.execute(f'''
                        UPDATE users 
                        SET daily_trades = CASE 
                            WHEN DATE(last_trade_reset) < CURRENT_DATE OR last_trade_reset IS NULL THEN 1
                            ELSE daily_trades + 1 
                        END,
                        last_trade_reset = CASE 
                            WHEN DATE(last_trade_reset) < CURRENT_DATE OR last_trade_reset IS NULL THEN CURRENT_TIMESTAMP
                            ELSE last_trade_reset 
                        END,
                        updated_at = CURRENT_TIMESTAMP
                        WHERE user_id = {ph}
                    ''', (user_id,))
                else:
                    cursor.execute(f'''
                        UPDATE users 
                        SET daily_trades = CASE 
                            WHEN DATE(last_trade_reset) < DATE('now') OR last_trade_reset IS NULL THEN 1
                            ELSE daily_trades + 1 
                        END,
                        last_trade_reset = CASE 
                            WHEN DATE(last_trade_reset) < DATE('now') OR last_trade_reset IS NULL THEN CURRENT_TIMESTAMP
                            ELSE last_trade_reset 
                        END,
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
    # CHANNEL OPERATIONS
    # ============================================
    def add_channel(self, user_id: int, channel_name: str, channel_id: str = None,
                   is_private: bool = False) -> int:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                
                cursor.execute(f'SELECT id FROM channels WHERE user_id = {ph} AND channel_name = {ph}', 
                             (user_id, channel_name))
                existing = cursor.fetchone()
                
                if existing:
                    cursor.execute(f'UPDATE channels SET is_active = true WHERE id = {ph}', 
                                 (existing['id'],))
                    conn.commit()
                    conn.close()
                    return existing['id']
                
                cursor.execute(f'''
                    INSERT INTO channels (user_id, channel_name, channel_id, is_private)
                    VALUES ({ph}, {ph}, {ph}, {ph})
                ''', (user_id, channel_name, channel_id, is_private))
                
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
                ph = self.placeholder()
                cursor.execute(f'''
                    SELECT * FROM channels 
                    WHERE user_id = {ph} AND is_active = true
                    ORDER BY created_at DESC
                ''', (user_id,))
                rows = cursor.fetchall()
                conn.close()
                return [dict(row) for row in rows]
        except Exception as e:
            print(f"❌ Error getting channels: {e}")
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
        except Exception as e:
            print(f"❌ Error getting channels: {e}")
            return []
    
    def deactivate_channel(self, channel_id: int) -> bool:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                cursor.execute(f'UPDATE channels SET is_active = false WHERE id = {ph}', (channel_id,))
                conn.commit()
                conn.close()
                return True
        except Exception as e:
            print(f"❌ Error deactivating channel: {e}")
            return False
    
    # ============================================
    # POSITION OPERATIONS
    # ============================================
    def add_position(self, user_id: int, token_address: str, amount: float,
                    entry_price: float, txid: str, token_symbol: str = None) -> int:
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
                    new_entry = ((existing['entry_price'] * existing['amount']) + (entry_price * amount)) / new_amount
                    cursor.execute(f'''
                        UPDATE positions SET amount = {ph}, entry_price = {ph}, updated_at = CURRENT_TIMESTAMP
                        WHERE id = {ph}
                    ''', (new_amount, new_entry, existing['id']))
                    conn.commit()
                    conn.close()
                    return existing['id']
                
                cursor.execute(f'''
                    INSERT INTO positions (user_id, token_address, token_symbol, amount, entry_price, buy_txid)
                    VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                ''', (user_id, token_address, token_symbol, amount, entry_price, txid))
                
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
    
    def get_user_positions(self, user_id: int, active_only: bool = True) -> List[Dict]:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                
                if active_only:
                    cursor.execute(f'''
                        SELECT * FROM positions 
                        WHERE user_id = {ph} AND is_active = true
                        ORDER BY created_at DESC
                    ''', (user_id,))
                else:
                    cursor.execute(f'''
                        SELECT * FROM positions WHERE user_id = {ph}
                        ORDER BY created_at DESC
                    ''', (user_id,))
                
                rows = cursor.fetchall()
                conn.close()
                return [dict(row) for row in rows]
        except Exception as e:
            print(f"❌ Error getting positions: {e}")
            return []
    
    def get_user_positions_count(self, user_id: int) -> int:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                cursor.execute(f'SELECT COUNT(*) as count FROM positions WHERE user_id = {ph} AND is_active = true', (user_id,))
                result = cursor.fetchone()
                conn.close()
                return result['count'] if result else 0
        except Exception as e:
            print(f"❌ Error counting positions: {e}")
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
        except Exception as e:
            print(f"❌ Error getting positions: {e}")
            return []
    
    def close_position(self, position_id: int, sell_txid: str = None) -> bool:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                cursor.execute(f'''
                    UPDATE positions SET is_active = false, sell_txid = {ph}, updated_at = CURRENT_TIMESTAMP
                    WHERE id = {ph}
                ''', (sell_txid, position_id))
                conn.commit()
                conn.close()
                return True
        except Exception as e:
            print(f"❌ Error closing position: {e}")
            return False
    
    # ============================================
    # TRADE HISTORY
    # ============================================
    def add_trade_history(self, user_id: int, token_address: str, trade_type: str,
                         amount: float, price: float, txid: str, 
                         explorer_url: str = None, status: str = 'completed') -> int:
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                
                total_value = amount * price if price > 0 else 0
                if not explorer_url and txid:
                    explorer_url = f"https://solscan.io/tx/{txid}"
                
                cursor.execute(f'''
                    INSERT INTO trade_history 
                    (user_id, token_address, trade_type, amount, price, total_value, txid, explorer_url, status)
                    VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                ''', (user_id, token_address, trade_type, amount, price, total_value, 
                     txid, explorer_url, status))
                
                if self.db_type == 'postgres':
                    cursor.execute('SELECT LASTVAL() as id')
                    trade_id = cursor.fetchone()['id']
                else:
                    trade_id = cursor.lastrowid
                
                conn.commit()
                conn.close()
                return trade_id
        except Exception as e:
            print(f"❌ Error adding trade history: {e}")
            return -1
    
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
                    INSERT INTO snipe_logs 
                    (user_id, channel_name, token_address, message_text, status, txid, error_message)
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
        except Exception as e:
            print(f"❌ Error adding snipe log: {e}")
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
                tables = ['positions', 'channels', 'trade_history', 'snipe_logs', 
                         'wallet_history', 'user_settings']
                for table in tables:
                    cursor.execute(f'DELETE FROM {table} WHERE user_id = {ph}', (user_id,))
                cursor.execute(f'DELETE FROM users WHERE user_id = {ph}', (user_id,))
                conn.commit()
                conn.close()
                return True
        except Exception as e:
            print(f"❌ Error deleting user: {e}")
            return False
    def update_position_amount(self, position_id: int, new_amount: float) -> bool:
        """Update position amount with actual balance"""
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = self.get_cursor(conn)
                ph = self.placeholder()
                cursor.execute(
                    f'UPDATE positions SET amount = {ph}, updated_at = CURRENT_TIMESTAMP WHERE id = {ph}',
                    (new_amount, position_id)
                )
                conn.commit()
                conn.close()
                return True
        except Exception as e:
            print(f"❌ Error updating position: {e}")
            return False