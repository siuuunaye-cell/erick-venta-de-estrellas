import os, sqlite3, secrets
from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, flash, session, abort
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
DB_PATH = os.environ.get('DATABASE_PATH', '/tmp/erick_venta.db')
PACKAGES = [(10, 5), (20, 10), (30, 17), (40, 27), (50, 35)]

def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL, player_name TEXT DEFAULT '', contact TEXT NOT NULL,
            stars INTEGER NOT NULL, price INTEGER NOT NULL, payment_method TEXT NOT NULL,
            note TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'Pendiente', created_at TEXT NOT NULL
        )''')
init_db()

@app.get('/')
def home():
    return render_template('index.html', packages=PACKAGES)

@app.route('/pedido', methods=['GET','POST'])
def order():
    if request.method == 'POST':
        player_id = request.form.get('player_id','').strip()
        player_name = request.form.get('player_name','').strip()[:80]
        contact = request.form.get('contact','').strip()[:120]
        note = request.form.get('note','').strip()[:500]
        payment_method = request.form.get('payment_method','').strip()
        try: stars = int(request.form.get('stars','0'))
        except ValueError: stars = 0
        price = dict(PACKAGES).get(stars)
        if not player_id or len(player_id) > 40 or not contact or not price or payment_method not in ('Mercado Pago','PayPal','Otro'):
            flash('Revisá los datos y elegí un paquete válido.', 'error')
            return render_template('order.html', packages=PACKAGES, form=request.form)
        with connect() as conn:
            cur = conn.execute('INSERT INTO orders(player_id,player_name,contact,stars,price,payment_method,note,created_at) VALUES(?,?,?,?,?,?,?,?)',
                (player_id, player_name, contact, stars, price, payment_method, note, datetime.now(timezone.utc).isoformat()))
            order_id = cur.lastrowid
        return render_template('confirmation.html', order_id=order_id, stars=stars, price=price, payment_method=payment_method)
    return render_template('order.html', packages=PACKAGES, form={})

@app.route('/admin', methods=['GET','POST'])
def admin():
    admin_password = os.environ.get('ADMIN_PASSWORD','')
    if not admin_password:
        return 'Panel deshabilitado: configurá ADMIN_PASSWORD en Environment de Render.', 503
    if request.method == 'POST':
        password = request.form.get('password','')
        if secrets.compare_digest(password, admin_password): session['admin_ok'] = True
        else: flash('Contraseña incorrecta.', 'error')
        return redirect(url_for('admin'))
    if not session.get('admin_ok'):
        return render_template('admin_login.html')
    with connect() as conn: orders = conn.execute('SELECT * FROM orders ORDER BY id DESC LIMIT 200').fetchall()
    return render_template('admin.html', orders=orders)

@app.post('/admin/logout')
def admin_logout():
    session.pop('admin_ok', None)
    return redirect(url_for('home'))

@app.post('/admin/order/<int:order_id>')
def update_order(order_id):
    if not session.get('admin_ok'): abort(403)
    status = request.form.get('status','')
    if status not in ('Pendiente','Pago verificado','Completado','Cancelado'): abort(400)
    with connect() as conn: conn.execute('UPDATE orders SET status=? WHERE id=?',(status,order_id))
    flash('Pedido actualizado.', 'success')
    return redirect(url_for('admin'))

@app.errorhandler(403)
def forbidden(_): return render_template('message.html', title='Acceso denegado', message='Iniciá sesión en el panel de administración.'),403

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
