import sqlite3
import json

class Database:
    def __init__(self, db_name='bot.db'):
        self.db_name = db_name
        self.conn = sqlite3.connect(db_name)
        # Включаем реальное соблюдение FOREIGN KEY (по умолчанию SQLite их игнорирует),
        # чтобы ON DELETE CASCADE в app_security действительно работал.
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.cursor = self.conn.cursor()
        self.create_tables()

    def create_tables(self):
        # Existing tables
        self.cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        self.cursor.execute("CREATE TABLE IF NOT EXISTS balances (user_id INTEGER, currency TEXT, amount REAL DEFAULT 0, PRIMARY KEY (user_id, currency))")
        self.cursor.execute("CREATE TABLE IF NOT EXISTS invoices (invoice_id TEXT PRIMARY KEY, creator_id INTEGER, amount_usd REAL, currencies TEXT, invoice_type TEXT, allow_comments INTEGER DEFAULT 1, allow_anonymous INTEGER DEFAULT 1, is_paid INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1, min_amount_usd REAL DEFAULT 0.01, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        # Миграция для БД, созданных до появления min_amount_usd
        self.cursor.execute("PRAGMA table_info(invoices)")
        existing_cols = [row[1] for row in self.cursor.fetchall()]
        if 'min_amount_usd' not in existing_cols:
            self.cursor.execute("ALTER TABLE invoices ADD COLUMN min_amount_usd REAL DEFAULT 0.01")
        self.cursor.execute("CREATE TABLE IF NOT EXISTS payments (payment_id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id TEXT, payer_id INTEGER, currency TEXT, amount_sent REAL, amount_usd REAL, comment TEXT, is_anonymous INTEGER DEFAULT 0, paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS invoice_messages (
                invoice_id TEXT,
                inline_message_id TEXT,
                PRIMARY KEY (invoice_id, inline_message_id)
            )
        """)
        
        # New tables for Crypto Pay Apps
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS apps (
                app_id TEXT PRIMARY KEY,
                creator_id INTEGER,
                name TEXT,
                description TEXT DEFAULT '',
                token TEXT UNIQUE,
                webhook_url TEXT DEFAULT NULL,
                balance REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (creator_id) REFERENCES users(user_id)
            )
        """)
        
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_security (
                app_id TEXT PRIMARY KEY,
                createcheck INTEGER DEFAULT 0,
                transfer INTEGER DEFAULT 0,
                whitelist_ip INTEGER DEFAULT 0,
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            )
        """)

        self.conn.commit()

    # --- User & Balance Methods (Existing) ---
    def add_user(self, user_id):
        self.cursor.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))
        self.conn.commit()

    def get_balance(self, user_id, currency):
        self.cursor.execute('SELECT amount FROM balances WHERE user_id=? AND currency=?', (user_id, currency))
        result = self.cursor.fetchone()
        return result[0] if result else 0.0

    def update_balance(self, user_id, currency, amount):
        self.cursor.execute('INSERT OR REPLACE INTO balances (user_id, currency, amount) VALUES (?, ?, ?)', 
                          (user_id, currency, amount))
        self.conn.commit()

    def add_to_balance(self, user_id, currency, amount):
        current = self.get_balance(user_id, currency)
        new_amount = current + amount
        if new_amount < 0:
            # Такого в норме быть не должно — списание всегда проверяется заранее.
            # Раньше это молча обнулялось, из-за чего расхождение было не видно нигде,
            # кроме как в самом факте пропажи денег. Логируем, чтобы баг было видно.
            print(f"[WARN] add_to_balance: попытка увести баланс в минус "
                  f"(user_id={user_id}, currency={currency}, current={current}, "
                  f"delta={amount}) — баланс обнулён вместо ухода в минус")
            new_amount = 0
        self.update_balance(user_id, currency, new_amount)

    def get_all_balances(self, user_id):
        self.cursor.execute('SELECT currency, amount FROM balances WHERE user_id=?', (user_id,))
        results = self.cursor.fetchall()
        return {row[0]: row[1] for row in results}

    # --- Invoice Methods (Existing) ---
    def create_invoice(self, invoice_id, creator_id, amount_usd, currencies, invoice_type, 
                      allow_comments=1, allow_anonymous=1):
        self.cursor.execute("INSERT INTO invoices (invoice_id, creator_id, amount_usd, currencies, invoice_type, allow_comments, allow_anonymous) VALUES (?, ?, ?, ?, ?, ?, ?)", 
            (invoice_id, creator_id, amount_usd, ','.join(currencies), invoice_type, 
             allow_comments, allow_anonymous))
        self.conn.commit()

    def get_invoice(self, invoice_id):
        self.cursor.execute(
            'SELECT invoice_id, creator_id, amount_usd, currencies, invoice_type, '
            'allow_comments, allow_anonymous, is_paid, is_active, min_amount_usd, created_at '
            'FROM invoices WHERE invoice_id=?', (invoice_id,))
        result = self.cursor.fetchone()
        if result:
            return {
                'invoice_id': result[0],
                'creator_id': result[1],
                'amount_usd': result[2],
                'currencies': result[3].split(','),
                'invoice_type': result[4],
                'allow_comments': result[5],
                'allow_anonymous': result[6],
                'is_paid': result[7],
                'is_active': result[8],
                'min_amount_usd': result[9] if result[9] is not None else 0.01,
                'created_at': result[10]
            }
        return None

    def update_invoice_min_amount(self, invoice_id, min_amount_usd):
        self.cursor.execute('UPDATE invoices SET min_amount_usd=? WHERE invoice_id=?',
                          (min_amount_usd, invoice_id))
        self.conn.commit()

    def update_invoice_settings(self, invoice_id, allow_comments=None, allow_anonymous=None):
        if allow_comments is not None:
            self.cursor.execute('UPDATE invoices SET allow_comments=? WHERE invoice_id=?', 
                              (allow_comments, invoice_id))
        if allow_anonymous is not None:
            self.cursor.execute('UPDATE invoices SET allow_anonymous=? WHERE invoice_id=?', 
                              (allow_anonymous, invoice_id))
        self.conn.commit()

    def mark_invoice_paid(self, invoice_id):
        self.cursor.execute('UPDATE invoices SET is_paid=1 WHERE invoice_id=?', (invoice_id,))
        self.conn.commit()

    def _adjust_balance_nocommit(self, user_id, currency, delta):
        """Изменяет баланс на delta БЕЗ commit — используется только внутри уже
        открытой транзакции (см. process_payment). Баланс не даём увести в минус:
        если бы это произошло, вся транзакция откатывается в process_payment."""
        self.cursor.execute(
            'INSERT INTO balances (user_id, currency, amount) VALUES (?, ?, ?) '
            'ON CONFLICT(user_id, currency) DO UPDATE SET amount = amount + excluded.amount',
            (user_id, currency, delta)
        )

    def process_payment(self, invoice_id, payer_id, creator_id, currency, amount,
                         amount_usd, comment='', is_anonymous=0, mark_single_paid=False):
        """
        Атомарно обрабатывает оплату счета одной транзакцией:
        списывает у плательщика, зачисляет создателю, при необходимости помечает
        одноразовый счет оплаченным и сохраняет запись о платеже.

        Раньше эти 4 шага коммитились по отдельности — при сбое посреди операции
        деньги могли списаться у плательщика, но не зачислиться получателю
        (при этом пользователю показывалось сообщение "средства не списаны",
        что было неправдой). Теперь либо применяются все изменения, либо
        не применяется ни одно — транзакция откатывается через conn.rollback().
        """
        try:
            self._adjust_balance_nocommit(payer_id, currency, -amount)

            # Защита от гонок/двойной оплаты: если после списания баланс плательщика
            # ушел в минус, откатываем всё и сообщаем об ошибке, а не тихо обнуляем.
            self.cursor.execute(
                'SELECT amount FROM balances WHERE user_id=? AND currency=?',
                (payer_id, currency)
            )
            row = self.cursor.fetchone()
            if row is None or row[0] < 0:
                raise ValueError(
                    f"Недостаточно средств для оплаты (user_id={payer_id}, "
                    f"currency={currency})"
                )

            self._adjust_balance_nocommit(creator_id, currency, amount)

            if mark_single_paid:
                self.cursor.execute(
                    'UPDATE invoices SET is_paid=1 WHERE invoice_id=?', (invoice_id,)
                )

            self.cursor.execute(
                "INSERT INTO payments (invoice_id, payer_id, currency, amount_sent, "
                "amount_usd, comment, is_anonymous) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (invoice_id, payer_id, currency, amount, amount_usd, comment, is_anonymous)
            )

            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def delete_invoice(self, invoice_id):
        self.cursor.execute('UPDATE invoices SET is_active=0 WHERE invoice_id=?', (invoice_id,))
        self.conn.commit()

    def get_user_invoices(self, user_id):
        self.cursor.execute('SELECT invoice_id FROM invoices WHERE creator_id=? AND is_active=1', (user_id,))
        return [row[0] for row in self.cursor.fetchall()]

    def get_active_invoices_for_list(self, user_id):
        self.cursor.execute("SELECT invoice_id FROM invoices WHERE creator_id=? AND is_active=1 AND NOT (invoice_type='single' AND is_paid=1)", (user_id,))
        return [row[0] for row in self.cursor.fetchall()]

    def add_payment(self, invoice_id, payer_id, currency, amount_sent, amount_usd, comment='', is_anonymous=0):
        self.cursor.execute("INSERT INTO payments (invoice_id, payer_id, currency, amount_sent, amount_usd, comment, is_anonymous) VALUES (?, ?, ?, ?, ?, ?, ?)", 
            (invoice_id, payer_id, currency, amount_sent, amount_usd, comment, is_anonymous))
        self.conn.commit()

    # --- Inline-сообщения счета (для редактирования после оплаты) ---
    def add_invoice_message(self, invoice_id, inline_message_id):
        self.cursor.execute("INSERT OR IGNORE INTO invoice_messages (invoice_id, inline_message_id) VALUES (?, ?)",
            (invoice_id, inline_message_id))
        self.conn.commit()

    def get_invoice_messages(self, invoice_id):
        self.cursor.execute("SELECT inline_message_id FROM invoice_messages WHERE invoice_id=?", (invoice_id,))
        return [row[0] for row in self.cursor.fetchall()]

    # --- Crypto Pay App Methods (New) ---

    def create_app(self, user_id, app_id, name, token):
        """Creates a new Crypto Pay application.

        app_id и token генерируются случайно, поэтому теоретически возможна коллизия
        с уже существующей записью (PRIMARY KEY на app_id, UNIQUE на token). Раньше
        это привело бы к необработанному sqlite3.IntegrityError и падению хэндлера.
        Теперь ошибка транзакции откатывается и пробрасывается вызывающему коду,
        чтобы он мог сгенерировать новые значения и повторить попытку.
        """
        try:
            self.cursor.execute(
                "INSERT INTO apps (app_id, creator_id, name, token) VALUES (?, ?, ?, ?)",
                (app_id, user_id, name, token)
            )
            # Initialize security settings with defaults
            self.cursor.execute(
                "INSERT INTO app_security (app_id) VALUES (?)",
                (app_id,)
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            self.conn.rollback()
            raise

    def get_app_by_id(self, app_id):
        """Retrieves app details including security settings"""
        self.cursor.execute("SELECT * FROM apps WHERE app_id=?", (app_id,))
        app_row = self.cursor.fetchone()
        
        if not app_row:
            return None
            
        app = {
            'app_id': app_row[0],
            'creator_id': app_row[1],
            'name': app_row[2],
            'description': app_row[3],
            'token': app_row[4],
            'webhook_url': app_row[5],
            'balance': app_row[6],
            'created_at': app_row[7],
            'security': {}
        }
        
        # Fetch security settings
        self.cursor.execute("SELECT * FROM app_security WHERE app_id=?", (app_id,))
        sec_row = self.cursor.fetchone()
        if sec_row:
            app['security'] = {
                'createcheck': bool(sec_row[1]),
                'transfer': bool(sec_row[2]),
                'whitelist_ip': bool(sec_row[3])
            }
            
        return app

    def get_user_apps(self, user_id):
        """Returns list of apps created by user"""
        self.cursor.execute("SELECT app_id, name, balance FROM apps WHERE creator_id=?", (user_id,))
        rows = self.cursor.fetchall()
        return [{'app_id': r[0], 'name': r[1], 'balance': r[2]} for r in rows]

    def update_app_token(self, app_id, new_token):
        self.cursor.execute("UPDATE apps SET token=? WHERE app_id=?", (new_token, app_id))
        self.conn.commit()

    def update_app_webhook(self, app_id, url):
        self.cursor.execute("UPDATE apps SET webhook_url=? WHERE app_id=?", (url, app_id))
        self.conn.commit()

    def update_app_security(self, app_id, setting_key, value):
        """Updates a specific security setting (createcheck, transfer, whitelist_ip)"""
        # Map python keys to column names
        col_map = {
            'createcheck': 'createcheck',
            'transfer': 'transfer',
            'whitelist_ip': 'whitelist_ip'
        }
        if setting_key in col_map:
            col = col_map[setting_key]
            val = 1 if value else 0
            self.cursor.execute(f"UPDATE app_security SET {col}=? WHERE app_id=?", (val, app_id))
            self.conn.commit()

    def update_app_name(self, app_id, name):
        self.cursor.execute("UPDATE apps SET name=? WHERE app_id=?", (name, app_id))
        self.conn.commit()

    def update_app_description(self, app_id, description):
        self.cursor.execute("UPDATE apps SET description=? WHERE app_id=?", (description, app_id))
        self.conn.commit()

    def delete_app(self, app_id):
        """Deletes app and its security settings"""
        self.cursor.execute("DELETE FROM app_security WHERE app_id=?", (app_id,))
        self.cursor.execute("DELETE FROM apps WHERE app_id=?", (app_id,))
        self.conn.commit()

    def close(self):
        self.conn.close()

