import os, ssl, time, sqlite3, secrets
from urllib.parse import quote, unquote, urlparse
from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, flash, session, abort, g

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
DB_PATH = os.environ.get('DATABASE_PATH', '/tmp/erick_venta.db')

# ---------- Datos de cobro ----------
PAYMENT_ALIAS = os.environ.get('PAYMENT_ALIAS', 'ericoviedo.bru.2526').strip()
PAYMENT_HOLDER = os.environ.get('PAYMENT_HOLDER', 'Eric Daniel Oviedo').strip()
PAYMENT_BANK = os.environ.get('PAYMENT_BANK', 'Brubank').strip()
WHATSAPP_NUMBER = ''.join(c for c in os.environ.get('WHATSAPP_NUMBER', '+5491162445083') if c.isdigit())
PAYMENT_METHODS = ['Transferencia (Argentina)', 'AstroPay (otros países)', 'Otro']

# ---------- Valores iniciales del catálogo (se usan solo si la base está vacía) ----------
DEFAULT_PACKAGES = [(10, 5), (20, 10), (30, 17), (40, 27), (50, 35)]
DEFAULT_ASTROPAY_LINKS = {
    10: 'https://onetouch.astropay.com/payment?external_reference_id=v23EQ1WItg8hlA8GAcTzgKc1fuge6znT',
    20: 'https://onetouch.astropay.com/payment?external_reference_id=tKCSEVE45kDNDv2MTJdClSyx6PEefeSy',
    30: 'https://onetouch.astropay.com/payment?external_reference_id=NMbAfe1LUcrU26Nt7aohvZVOVpkghcOV',
    40: 'https://onetouch.astropay.com/payment?external_reference_id=KLkuNXE8ktiliC7gZIGFtu4ZCcvR1kt4',
    50: 'https://onetouch.astropay.com/payment?external_reference_id=58eV0YQHxFWF1fkmlkYEZtwcKVgR3aad',
}
try: ENV_RATE = float(os.environ.get('USD_ARS_RATE', '1560'))
except ValueError: ENV_RATE = 1560.0


# ---------- Base de datos (PostgreSQL permanente si hay DATABASE_URL, si no SQLite temporal) ----------
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
USE_PG = bool(DATABASE_URL)
DB_ERROR = ''

class Conn:
    def __init__(self):
        if USE_PG:
            import pg8000.dbapi
            u = urlparse(DATABASE_URL)
            self.raw = pg8000.dbapi.connect(
                user=unquote(u.username or ''), password=unquote(u.password or ''),
                host=u.hostname, port=u.port or 5432, database=(u.path or '/').lstrip('/') or None,
                ssl_context=ssl.create_default_context(), timeout=20)
        else:
            self.raw = sqlite3.connect(DB_PATH)

    def _run(self, sql, params):
        if USE_PG: sql = sql.replace('?', '%s')
        cur = self.raw.cursor()
        cur.execute(sql, tuple(params))
        return cur

    def rows(self, sql, params=()):
        cur = self._run(sql, params)
        if not cur.description: return []
        cols = [d[0].decode() if isinstance(d[0], bytes) else d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def one(self, sql, params=()):
        r = self.rows(sql, params)
        return r[0] if r else None

    def write(self, sql, params=()):
        self._run(sql, params)
        self.raw.commit()

    def insert(self, sql, params=()):
        if USE_PG:
            new_id = self._run(sql + ' RETURNING id', params).fetchone()[0]
        else:
            new_id = self._run(sql, params).lastrowid
        self.raw.commit()
        return new_id

    def close(self):
        try: self.raw.close()
        except Exception: pass

def db():
    if 'db' not in g: g.db = Conn()
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop('db', None)
    if d: d.close()

def init_db(c):
    pk = 'SERIAL PRIMARY KEY' if USE_PG else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    real = 'DOUBLE PRECISION' if USE_PG else 'REAL'
    c.write(f"""CREATE TABLE IF NOT EXISTS orders (
        id {pk}, player_id TEXT NOT NULL, player_name TEXT DEFAULT '', contact TEXT NOT NULL,
        stars INTEGER NOT NULL, price {real} NOT NULL, payment_method TEXT NOT NULL,
        note TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'Pendiente', created_at TEXT NOT NULL,
        item TEXT DEFAULT '', pay_link TEXT DEFAULT '')""")
    if not USE_PG:  # bases SQLite viejas: agregar columnas nuevas
        cols = {r['name'] for r in c.rows('PRAGMA table_info(orders)')}
        if 'item' not in cols: c.write("ALTER TABLE orders ADD COLUMN item TEXT DEFAULT ''")
        if 'pay_link' not in cols: c.write("ALTER TABLE orders ADD COLUMN pay_link TEXT DEFAULT ''")
    c.write(f"""CREATE TABLE IF NOT EXISTS packages (
        id {pk}, stars INTEGER NOT NULL, price {real} NOT NULL, old_price {real},
        label TEXT DEFAULT '', astropay_link TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1)""")
    c.write(f"""CREATE TABLE IF NOT EXISTS products (
        id {pk}, name TEXT NOT NULL, description TEXT DEFAULT '', price {real} NOT NULL, old_price {real},
        label TEXT DEFAULT '', astropay_link TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1)""")
    c.write('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    if not c.one("SELECT value FROM settings WHERE key='seeded'"):
        if not c.one('SELECT 1 AS x FROM packages LIMIT 1'):
            for stars, price in DEFAULT_PACKAGES:
                c.write('INSERT INTO packages(stars,price,label,astropay_link) VALUES(?,?,?,?)',
                        (stars, price, 'POPULAR' if stars == 30 else '', DEFAULT_ASTROPAY_LINKS.get(stars, '')))
        c.write("INSERT INTO settings(key,value) VALUES('seeded','1') ON CONFLICT(key) DO NOTHING")

def start_db():
    global USE_PG, DB_ERROR
    last = None
    for attempt in range(3 if USE_PG else 1):
        try:
            c = Conn()
            try: init_db(c)
            finally: c.close()
            return
        except Exception as e:
            last = e
            print('DB error:', repr(e), flush=True)
            if attempt < 2 and USE_PG: time.sleep(3)
    if USE_PG:  # no se pudo conectar a la base permanente: seguimos con SQLite temporal para que la web no se caiga
        DB_ERROR = f'{type(last).__name__}: {last}'[:200]
        USE_PG = False
        c = Conn()
        try: init_db(c)
        finally: c.close()
start_db()


# ---------- Precios y formato ----------
def get_rate():
    if 'rate' not in g:
        g.rate = ENV_RATE
        try:
            r = db().one("SELECT value FROM settings WHERE key='usd_ars_rate'")
            if r: g.rate = float(r['value'])
        except Exception:
            pass
    return g.rate

def fmt_num(x):
    return f'{float(x):.2f}'.rstrip('0').rstrip('.')

def fmt_pesos(n):
    return '$' + f'{round(n):,}'.replace(',', '.')

@app.template_filter('usd')
def usd_filter(x): return fmt_num(x)

@app.template_filter('ars')
def ars_filter(x): return fmt_pesos(float(x) * get_rate())

@app.context_processor
def inject_globals():
    return {'usd_ars_rate': fmt_pesos(get_rate()), 'db_pg': USE_PG, 'db_error': DB_ERROR}


# ---------- Consultas ----------
def active_packages():
    return db().rows('SELECT * FROM packages WHERE active=1 ORDER BY stars, id')

def active_products():
    return db().rows('SELECT * FROM products WHERE active=1 ORDER BY id')

def find_item(token):
    kind, _, ident = (token or '').partition(':')
    if not ident.isdigit(): return None
    if kind == 'pkg':
        r = db().one('SELECT * FROM packages WHERE id=? AND active=1', (int(ident),))
        if r: return dict(stars=r['stars'], name=f"{r['stars']} estrellas", price=r['price'], link=r['astropay_link'] or '', is_stars=True)
    elif kind == 'prod':
        r = db().one('SELECT * FROM products WHERE id=? AND active=1', (int(ident),))
        if r: return dict(stars=0, name=r['name'], price=r['price'], link=r['astropay_link'] or '', is_stars=False)
    return None

def render_order(form, selected):
    return render_template('order.html', packages=active_packages(), products=active_products(),
                           methods=PAYMENT_METHODS, form=form, selected=selected)


# ---------- Tienda ----------
@app.get('/')
def home():
    return render_template('index.html', packages=active_packages(), products=active_products())

@app.route('/pedido', methods=['GET', 'POST'])
def order():
    if request.method == 'POST':
        token = request.form.get('item', '')
        item = find_item(token)
        player_id = request.form.get('player_id', '').strip()
        player_name = request.form.get('player_name', '').strip()[:80]
        contact = request.form.get('contact', '').strip()[:120]
        note = request.form.get('note', '').strip()[:500]
        payment_method = request.form.get('payment_method', '').strip()
        if (not item or not contact or len(player_id) > 40 or payment_method not in PAYMENT_METHODS
                or (item['is_stars'] and not player_id)):
            flash('Revisá los datos y elegí un producto válido. Para estrellas necesitás tu ID de jugador.', 'error')
            return render_order(request.form, token)
        order_id = db().insert(
            'INSERT INTO orders(player_id,player_name,contact,stars,price,payment_method,note,created_at,item,pay_link) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (player_id, player_name, contact, item['stars'], item['price'], payment_method, note,
             datetime.now(timezone.utc).isoformat(), item['name'], item['link']))
        return redirect(url_for('order_done', order_id=order_id))
    selected = ''
    if request.args.get('pkg', '').isdigit(): selected = 'pkg:' + request.args['pkg']
    elif request.args.get('prod', '').isdigit(): selected = 'prod:' + request.args['prod']
    return render_order({}, selected)

@app.get('/pedido/<int:order_id>/pago')
def order_done(order_id):
    o = db().one('SELECT id,stars,price,payment_method,item,pay_link FROM orders WHERE id=?', (order_id,))
    if not o: abort(404)
    name = o['item'] or f"{o['stars']} estrellas"
    wa_link = ''
    if WHATSAPP_NUMBER:
        msg = f'Hola! Hice el pedido #{o["id"]} de {name} (USD {fmt_num(o["price"])} / {fmt_pesos(o["price"] * get_rate())} ARS). Te paso el comprobante de pago.'
        wa_link = f'https://wa.me/{WHATSAPP_NUMBER}?text={quote(msg)}'
    method = o['payment_method']
    return render_template('confirmation.html', order=o, name=name, alias=PAYMENT_ALIAS, holder=PAYMENT_HOLDER,
                           bank=PAYMENT_BANK, whatsapp=WHATSAPP_NUMBER, wa_link=wa_link,
                           astropay_link=o['pay_link'] or '',
                           show_alias=not method.startswith('AstroPay'),
                           show_astropay=not method.startswith('Transferencia'))


# ---------- Administración ----------
def require_admin():
    if not session.get('admin_ok'): abort(403)

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    admin_password = os.environ.get('ADMIN_PASSWORD', '')
    if not admin_password:
        return 'Panel deshabilitado: configurá ADMIN_PASSWORD en Environment de Render.', 503
    if request.method == 'POST':
        password = request.form.get('password', '')
        if secrets.compare_digest(password, admin_password): session['admin_ok'] = True
        else: flash('Contraseña incorrecta.', 'error')
        return redirect(url_for('admin'))
    if not session.get('admin_ok'):
        return render_template('admin_login.html')
    orders = db().rows('SELECT * FROM orders ORDER BY id DESC LIMIT 200')
    return render_template('admin.html', orders=orders)

@app.post('/admin/logout')
def admin_logout():
    session.pop('admin_ok', None)
    return redirect(url_for('home'))

@app.post('/admin/order/<int:order_id>')
def update_order(order_id):
    require_admin()
    status = request.form.get('status', '')
    if status not in ('Pendiente', 'Pago verificado', 'Completado', 'Cancelado'): abort(400)
    db().write('UPDATE orders SET status=? WHERE id=?', (status, order_id))
    flash('Pedido actualizado.', 'success')
    return redirect(url_for('admin'))

def parse_price(value, required=True):
    value = (value or '').strip().replace(',', '.')
    if not value:
        if required: raise ValueError('precio')
        return None
    x = float(value)
    if x <= 0 or x > 100000: raise ValueError('precio')
    return round(x, 2)

def parse_link(value):
    value = (value or '').strip()[:500]
    if value and not value.startswith('https://'): raise ValueError('link')
    return value

@app.get('/admin/catalogo')
def admin_catalog():
    require_admin()
    packages = db().rows('SELECT * FROM packages ORDER BY stars, id')
    products = db().rows('SELECT * FROM products ORDER BY id')
    return render_template('admin_catalog.html', packages=packages, products=products, rate=get_rate())

@app.post('/admin/catalogo/cotizacion')
def admin_rate():
    require_admin()
    try: rate = parse_price(request.form.get('rate'))
    except ValueError:
        flash('Cotización inválida.', 'error'); return redirect(url_for('admin_catalog'))
    db().write("INSERT INTO settings(key,value) VALUES('usd_ars_rate',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(rate),))
    flash('Cotización actualizada.', 'success')
    return redirect(url_for('admin_catalog'))

@app.post('/admin/catalogo/paquete')
def admin_package():
    require_admin()
    pid = request.form.get('id', '')
    if request.form.get('action') == 'delete' and pid.isdigit():
        db().write('DELETE FROM packages WHERE id=?', (int(pid),))
        flash('Paquete eliminado.', 'success'); return redirect(url_for('admin_catalog'))
    try:
        stars = int(request.form.get('stars', ''))
        if stars <= 0 or stars > 1000000: raise ValueError('estrellas')
        price = parse_price(request.form.get('price'))
        old_price = parse_price(request.form.get('old_price'), required=False)
        link = parse_link(request.form.get('astropay_link'))
    except ValueError:
        flash('Revisá los datos: estrellas y precio deben ser números y el link tiene que empezar con https://', 'error')
        return redirect(url_for('admin_catalog'))
    label = request.form.get('label', '').strip()[:30]
    active = 1 if request.form.get('active') else 0
    if pid.isdigit():
        db().write('UPDATE packages SET stars=?,price=?,old_price=?,label=?,astropay_link=?,active=? WHERE id=?',
                   (stars, price, old_price, label, link, active, int(pid)))
    else:
        db().write('INSERT INTO packages(stars,price,old_price,label,astropay_link,active) VALUES(?,?,?,?,?,?)',
                   (stars, price, old_price, label, link, active))
    flash('Paquete guardado.', 'success')
    return redirect(url_for('admin_catalog'))

@app.post('/admin/catalogo/producto')
def admin_product():
    require_admin()
    pid = request.form.get('id', '')
    if request.form.get('action') == 'delete' and pid.isdigit():
        db().write('DELETE FROM products WHERE id=?', (int(pid),))
        flash('Producto eliminado.', 'success'); return redirect(url_for('admin_catalog'))
    name = request.form.get('name', '').strip()[:80]
    try:
        if not name: raise ValueError('nombre')
        price = parse_price(request.form.get('price'))
        old_price = parse_price(request.form.get('old_price'), required=False)
        link = parse_link(request.form.get('astropay_link'))
    except ValueError:
        flash('Revisá los datos: el nombre es obligatorio, el precio debe ser un número y el link tiene que empezar con https://', 'error')
        return redirect(url_for('admin_catalog'))
    description = request.form.get('description', '').strip()[:400]
    label = request.form.get('label', '').strip()[:30]
    active = 1 if request.form.get('active') else 0
    if pid.isdigit():
        db().write('UPDATE products SET name=?,description=?,price=?,old_price=?,label=?,astropay_link=?,active=? WHERE id=?',
                   (name, description, price, old_price, label, link, active, int(pid)))
    else:
        db().write('INSERT INTO products(name,description,price,old_price,label,astropay_link,active) VALUES(?,?,?,?,?,?,?)',
                   (name, description, price, old_price, label, link, active))
    flash('Producto guardado.', 'success')
    return redirect(url_for('admin_catalog'))


@app.errorhandler(403)
def forbidden(_): return render_template('message.html', title='Acceso denegado', message='Iniciá sesión en el panel de administración.'), 403

@app.errorhandler(404)
def not_found(_): return render_template('message.html', title='No encontrado', message='Esa página no existe.'), 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
