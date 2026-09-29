import os
import secrets
import json
from functools import wraps
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, jsonify, send_from_directory, abort)
from werkzeug.utils import secure_filename
from sqlalchemy import func

from models import (db, now_cl, Usuario, Categoria, Proveedor, Producto, Venta,
                    VentaItem, Cliente, Abono, SesionCaja, MovimientoCaja,
                    Factura, Config)
from factura_parser import parse_factura

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD = os.path.join(BASE, "uploads")
os.makedirs(UPLOAD, exist_ok=True)
os.makedirs(os.path.join(BASE, "instance"), exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
_db_url = os.environ.get("DATABASE_URL", "sqlite:///" + os.path.join(BASE, "instance", "minimarket.db"))
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
db.init_app(app)


# ---------- helpers ----------
def login_required(f):
    @wraps(f)
    def wrap(*a, **k):
        if "uid" not in session:
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrap


def admin_required(f):
    @wraps(f)
    def wrap(*a, **k):
        if session.get("rol") != "admin":
            flash("Acceso solo para administradores.", "error")
            return redirect(url_for("dashboard"))
        return f(*a, **k)
    return wrap


def current_user():
    if "uid" in session:
        return db.session.get(Usuario, session["uid"])
    return None


def sesion_caja_activa():
    return SesionCaja.query.filter_by(estado="abierta").first()


@app.context_processor
def inject():
    return {"user": current_user(), "caja": sesion_caja_activa(), "clp": clp}


def clp(v):
    try:
        return "$" + f"{int(round(v or 0)):,}".replace(",", ".")
    except Exception:
        return "$0"


app.jinja_env.filters["clp"] = clp


# ---------- auth ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = Usuario.query.filter_by(username=request.form["username"].strip()).first()
        if u and u.activo and u.check_password(request.form["password"]):
            session["uid"], session["rol"], session["nombre"] = u.id, u.rol, u.nombre
            return redirect(url_for("dashboard"))
        flash("Credenciales inválidas.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- dashboard ----------
@app.route("/")
@login_required
def dashboard():
    hoy = now_cl().date()
    inicio = datetime.combine(hoy, datetime.min.time())
    ventas_hoy = Venta.query.filter(Venta.fecha >= inicio).all()
    total_hoy = sum(v.total for v in ventas_hoy)
    util_hoy = sum(v.utilidad for v in ventas_hoy)
    n_ventas = len(ventas_hoy)

    productos_vendidos = int(db.session.query(
        func.coalesce(func.sum(VentaItem.cantidad), 0)
    ).join(Venta).filter(Venta.fecha >= inicio).scalar() or 0)

    bajo_stock = Producto.query.filter(Producto.stock <= Producto.stock_min,
                                       Producto.activo == True).count()
    bajo_stock_lista = (Producto.query
                        .filter(Producto.stock <= Producto.stock_min, Producto.activo == True)
                        .order_by(Producto.stock).limit(5).all())
    deuda_total = sum(c.deuda for c in Cliente.query.all())

    top_raw = (db.session.query(VentaItem.nombre, func.sum(VentaItem.cantidad).label("q"))
               .join(Venta).filter(Venta.fecha >= inicio - timedelta(days=7))
               .group_by(VentaItem.nombre).order_by(func.sum(VentaItem.cantidad).desc())
               .limit(5).all())
    top = [(r[0], float(r[1])) for r in top_raw]
    top_max = max((t[1] for t in top), default=1)

    cat_raw = (db.session.query(Categoria.nombre, func.sum(VentaItem.cantidad * VentaItem.precio_unit).label("m"))
               .join(Producto, VentaItem.producto_id == Producto.id)
               .join(Categoria, Producto.categoria_id == Categoria.id)
               .join(Venta, VentaItem.venta_id == Venta.id)
               .filter(Venta.fecha >= inicio - timedelta(days=7))
               .group_by(Categoria.nombre)
               .order_by(func.sum(VentaItem.cantidad * VentaItem.precio_unit).desc())
               .limit(5).all())
    cat_ventas = [(r[0], float(r[1])) for r in cat_raw]
    cat_total = sum(c[1] for c in cat_ventas) or 1

    facturas_recientes = Factura.query.order_by(Factura.cargada.desc()).limit(4).all()

    serie = []
    for i in range(6, -1, -1):
        d = hoy - timedelta(days=i)
        a = datetime.combine(d, datetime.min.time())
        b = a + timedelta(days=1)
        t = db.session.query(func.coalesce(func.sum(Venta.total), 0)).filter(
            Venta.fecha >= a, Venta.fecha < b).scalar()
        serie.append({"dia": d.strftime("%d/%m"), "total": float(t or 0)})

    return render_template("dashboard.html",
                           total_hoy=total_hoy, util_hoy=util_hoy,
                           n_ventas=n_ventas, bajo_stock=bajo_stock,
                           bajo_stock_lista=bajo_stock_lista,
                           deuda_total=deuda_total,
                           top=top, top_max=top_max,
                           cat_ventas=cat_ventas, cat_total=cat_total,
                           facturas_recientes=facturas_recientes,
                           productos_vendidos=productos_vendidos,
                           serie=serie)


# ---------- POS / ventas ----------
@app.route("/pos")
@login_required
def pos():
    if not sesion_caja_activa():
        flash("Debes abrir la caja antes de vender.", "error")
        return redirect(url_for("caja"))
    clientes = Cliente.query.order_by(Cliente.nombre).all()
    return render_template("pos.html", clientes=clientes)


@app.route("/api/buscar_producto")
@login_required
def api_buscar():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    res = Producto.query.filter(
        Producto.activo == True,
        db.or_(Producto.nombre.ilike(f"%{q}%"), Producto.codigo.ilike(f"%{q}%"))
    ).limit(15).all()
    return jsonify([{"id": p.id, "codigo": p.codigo, "nombre": p.nombre,
                     "precio": p.precio, "stock": p.stock, "costo": p.costo} for p in res])


@app.route("/api/venta", methods=["POST"])
@login_required
def api_venta():
    sc = sesion_caja_activa()
    if not sc:
        return jsonify({"ok": False, "error": "Caja cerrada"}), 400
    data = request.get_json()
    items = data.get("items", [])
    if not items:
        return jsonify({"ok": False, "error": "Carrito vacío"}), 400
    metodo = data.get("metodo", "efectivo")
    cliente_id = data.get("cliente_id") or None
    if metodo == "fiado" and not cliente_id:
        return jsonify({"ok": False, "error": "Selecciona cliente para fiar"}), 400

    v = Venta(usuario_id=session["uid"], metodo_pago=metodo,
              pagado=(metodo != "fiado"), cliente_id=cliente_id,
              sesion_caja_id=sc.id)
    total = costo = 0
    for it in items:
        p = db.session.get(Producto, it["id"])
        if not p:
            continue
        cant = float(it["cantidad"])
        if p.stock < cant:
            return jsonify({"ok": False, "error": f"Stock insuficiente: {p.nombre}"}), 400
        p.stock -= cant
        vi = VentaItem(producto_id=p.id, nombre=p.nombre, cantidad=cant,
                       precio_unit=p.precio, costo_unit=p.costo)
        v.items.append(vi)
        total += cant * p.precio
        costo += cant * p.costo
    v.total, v.costo_total = round(total), round(costo)
    db.session.add(v)
    if metodo != "fiado":
        db.session.add(MovimientoCaja(sesion_caja_id=sc.id, tipo="venta",
                       monto=v.total, descripcion=f"Venta #{v.id}",
                       usuario_id=session["uid"]))
    db.session.commit()
    return jsonify({"ok": True, "venta_id": v.id, "total": v.total})


@app.route("/ventas")
@login_required
def ventas():
    f = request.args.get("fecha", now_cl().date().isoformat())
    d = datetime.fromisoformat(f)
    a, b = datetime.combine(d, datetime.min.time()), datetime.combine(d, datetime.min.time()) + timedelta(days=1)
    lista = Venta.query.filter(Venta.fecha >= a, Venta.fecha < b).order_by(Venta.fecha.desc()).all()
    return render_template("ventas.html", ventas=lista, fecha=f,
                           total=sum(v.total for v in lista))


@app.route("/venta/<int:vid>")
@login_required
def venta_detalle(vid):
    v = db.session.get(Venta, vid) or abort(404)
    return render_template("venta_detalle.html", v=v)


# ---------- inventario ----------
@app.route("/inventario")
@login_required
def inventario():
    q = request.args.get("q", "").strip()
    cat = request.args.get("cat", "")
    query = Producto.query.filter_by(activo=True)
    if q:
        query = query.filter(db.or_(Producto.nombre.ilike(f"%{q}%"),
                                    Producto.codigo.ilike(f"%{q}%")))
    if cat:
        query = query.filter_by(categoria_id=int(cat))
    productos = query.order_by(Producto.nombre).all()
    cats = Categoria.query.order_by(Categoria.nombre).all()
    return render_template("inventario.html", productos=productos, cats=cats, q=q, cat=cat)


@app.route("/producto/nuevo", methods=["GET", "POST"])
@app.route("/producto/<int:pid>", methods=["GET", "POST"])
@login_required
@admin_required
def producto_form(pid=None):
    p = db.session.get(Producto, pid) if pid else None
    if request.method == "POST":
        if not p:
            p = Producto()
            db.session.add(p)
        f = request.form
        p.codigo = f.get("codigo") or None
        p.nombre = f["nombre"]
        p.categoria_id = int(f["categoria_id"]) if f.get("categoria_id") else None
        p.costo = float(f.get("costo") or 0)
        p.precio = float(f.get("precio") or 0)
        p.stock = float(f.get("stock") or 0)
        p.stock_min = float(f.get("stock_min") or 5)
        p.unidad = f.get("unidad", "un")
        db.session.commit()
        flash("Producto guardado.", "ok")
        return redirect(url_for("inventario"))
    cats = Categoria.query.order_by(Categoria.nombre).all()
    return render_template("producto_form.html", p=p, cats=cats)


@app.route("/producto/<int:pid>/eliminar", methods=["POST"])
@login_required
@admin_required
def producto_eliminar(pid):
    p = db.session.get(Producto, pid) or abort(404)
    p.activo = False
    db.session.commit()
    flash("Producto desactivado.", "ok")
    return redirect(url_for("inventario"))


@app.route("/categorias", methods=["GET", "POST"])
@login_required
@admin_required
def categorias():
    if request.method == "POST":
        n = request.form["nombre"].strip()
        if n and not Categoria.query.filter_by(nombre=n).first():
            db.session.add(Categoria(nombre=n))
            db.session.commit()
        return redirect(url_for("categorias"))
    return render_template("categorias.html", cats=Categoria.query.order_by(Categoria.nombre).all())


# ---------- facturas / carga masiva ----------
@app.route("/facturas")
@login_required
@admin_required
def facturas():
    lista = Factura.query.order_by(Factura.cargada.desc()).all()
    return render_template("facturas.html", facturas=lista)


@app.route("/facturas/subir", methods=["POST"])
@login_required
@admin_required
def factura_subir():
    archivos = request.files.getlist("pdf")
    n = 0
    for file in archivos:
        if not file or not file.filename.lower().endswith(".pdf"):
            continue
        fname = secure_filename(f"{int(now_cl().timestamp())}_{file.filename}")
        path = os.path.join(UPLOAD, fname)
        file.save(path)
        try:
            data = parse_factura(path)
        except Exception as e:
            flash(f"Error leyendo {file.filename}: {e}", "error")
            continue
        prov = None
        if data.get("rut_emisor"):
            prov = Proveedor.query.filter_by(rut=data["rut_emisor"]).first()
            if not prov:
                prov = Proveedor(rut=data["rut_emisor"], nombre=data.get("razon_social") or "Proveedor")
                db.session.add(prov)
                db.session.flush()
        fac = Factura(folio=data.get("folio"), proveedor_id=prov.id if prov else None,
                      fecha_emision=data.get("fecha"), neto=data.get("neto") or 0,
                      iva=data.get("iva") or 0, total=data.get("total") or 0,
                      archivo=fname, detalle_json=json.dumps(data.get("items", []), ensure_ascii=False))
        db.session.add(fac)
        n += 1
    db.session.commit()
    flash(f"{n} factura(s) cargada(s). Revisa y confirma para actualizar inventario.", "ok")
    return redirect(url_for("facturas"))


@app.route("/facturas/<int:fid>/revisar")
@login_required
@admin_required
def factura_revisar(fid):
    fac = db.session.get(Factura, fid) or abort(404)
    items = json.loads(fac.detalle_json or "[]")
    # match con productos existentes por código o nombre
    for it in items:
        match = None
        if it.get("codigo"):
            match = Producto.query.filter_by(codigo=it["codigo"]).first()
        if not match and it.get("nombre"):
            match = Producto.query.filter(Producto.nombre.ilike(it["nombre"][:40] + "%")).first()
        it["match_id"] = match.id if match else None
        it["match_nombre"] = match.nombre if match else None
    return render_template("factura_revisar.html", fac=fac, items=items,
                           cats=Categoria.query.order_by(Categoria.nombre).all())


@app.route("/facturas/<int:fid>/aplicar", methods=["POST"])
@login_required
@admin_required
def factura_aplicar(fid):
    fac = db.session.get(Factura, fid) or abort(404)
    if fac.procesada:
        flash("Esta factura ya fue aplicada.", "error")
        return redirect(url_for("facturas"))
    n_idx = request.form.getlist("idx")
    aplicados = 0
    for i in n_idx:
        accion = request.form.get(f"accion_{i}")  # nuevo | actualizar | ignorar
        if accion == "ignorar":
            continue
        nombre = request.form.get(f"nombre_{i}")
        cantidad = float(request.form.get(f"cantidad_{i}") or 0)
        costo = float(request.form.get(f"costo_{i}") or 0)
        precio = float(request.form.get(f"precio_{i}") or 0)
        codigo = request.form.get(f"codigo_{i}") or None
        if accion == "actualizar":
            pid = request.form.get(f"match_{i}")
            p = db.session.get(Producto, int(pid)) if pid else None
            if p:
                p.stock += cantidad
                if costo:
                    p.costo = costo
                if precio:
                    p.precio = precio
                aplicados += 1
        elif accion == "nuevo":
            cat = request.form.get(f"cat_{i}") or None
            p = Producto(codigo=codigo, nombre=nombre, stock=cantidad, costo=costo,
                         precio=precio or round(costo * 1.3),
                         categoria_id=int(cat) if cat else None)
            db.session.add(p)
            aplicados += 1
    fac.procesada = True
    db.session.commit()
    flash(f"Inventario actualizado: {aplicados} producto(s).", "ok")
    return redirect(url_for("facturas"))


@app.route("/uploads/<path:fname>")
@login_required
@admin_required
def ver_upload(fname):
    return send_from_directory(UPLOAD, fname)


# ---------- fiados ----------
@app.route("/fiados")
@login_required
def fiados():
    clientes = Cliente.query.order_by(Cliente.nombre).all()
    clientes = [c for c in clientes]
    total = sum(c.deuda for c in clientes)
    return render_template("fiados.html", clientes=clientes, total=total)


@app.route("/cliente/nuevo", methods=["POST"])
@login_required
def cliente_nuevo():
    db.session.add(Cliente(nombre=request.form["nombre"],
                           telefono=request.form.get("telefono"),
                           nota=request.form.get("nota")))
    db.session.commit()
    flash("Cliente creado.", "ok")
    return redirect(url_for("fiados"))


@app.route("/cliente/<int:cid>")
@login_required
def cliente_detalle(cid):
    c = db.session.get(Cliente, cid) or abort(404)
    ventas = Venta.query.filter_by(cliente_id=cid, metodo_pago="fiado").order_by(Venta.fecha.desc()).all()
    abonos = Abono.query.filter_by(cliente_id=cid).order_by(Abono.fecha.desc()).all()
    return render_template("cliente_detalle.html", c=c, ventas=ventas, abonos=abonos)


@app.route("/cliente/<int:cid>/abonar", methods=["POST"])
@login_required
def cliente_abonar(cid):
    c = db.session.get(Cliente, cid) or abort(404)
    monto = float(request.form["monto"])
    db.session.add(Abono(cliente_id=cid, monto=monto, usuario_id=session["uid"],
                         nota=request.form.get("nota")))
    sc = sesion_caja_activa()
    if sc:
        db.session.add(MovimientoCaja(sesion_caja_id=sc.id, tipo="abono", monto=monto,
                       descripcion=f"Abono {c.nombre}", usuario_id=session["uid"]))
    # marcar ventas como pagadas si la deuda llega a 0
    db.session.commit()
    if c.deuda <= 0:
        for v in Venta.query.filter_by(cliente_id=cid, metodo_pago="fiado", pagado=False):
            v.pagado = True
        db.session.commit()
    flash("Abono registrado.", "ok")
    return redirect(url_for("cliente_detalle", cid=cid))


# ---------- caja ----------
@app.route("/caja")
@login_required
def caja():
    sc = sesion_caja_activa()
    resumen = None
    if sc:
        movs = MovimientoCaja.query.filter_by(sesion_caja_id=sc.id).all()
        ingresos = sum(m.monto for m in movs if m.tipo in ("venta", "abono", "ingreso"))
        egresos = sum(m.monto for m in movs if m.tipo == "egreso")
        resumen = {"ingresos": ingresos, "egresos": egresos,
                   "esperado": sc.monto_inicial + ingresos - egresos, "movs": movs}
    historial = SesionCaja.query.filter_by(estado="cerrada").order_by(SesionCaja.cerrada.desc()).limit(15).all()
    return render_template("caja.html", sc=sc, resumen=resumen, historial=historial)


@app.route("/caja/abrir", methods=["POST"])
@login_required
def caja_abrir():
    if sesion_caja_activa():
        flash("Ya hay una caja abierta.", "error")
        return redirect(url_for("caja"))
    db.session.add(SesionCaja(usuario_id=session["uid"],
                              monto_inicial=float(request.form.get("monto_inicial") or 0)))
    db.session.commit()
    flash("Caja abierta.", "ok")
    return redirect(url_for("caja"))


@app.route("/caja/movimiento", methods=["POST"])
@login_required
def caja_movimiento():
    sc = sesion_caja_activa()
    if not sc:
        return redirect(url_for("caja"))
    db.session.add(MovimientoCaja(sesion_caja_id=sc.id, tipo=request.form["tipo"],
                   monto=float(request.form["monto"]), descripcion=request.form.get("descripcion"),
                   usuario_id=session["uid"]))
    db.session.commit()
    flash("Movimiento registrado.", "ok")
    return redirect(url_for("caja"))


@app.route("/caja/cerrar", methods=["POST"])
@login_required
def caja_cerrar():
    sc = sesion_caja_activa()
    if not sc:
        return redirect(url_for("caja"))
    sc.monto_final_real = float(request.form.get("monto_final_real") or 0)
    sc.cerrada = now_cl()
    sc.estado = "cerrada"
    db.session.commit()
    flash("Caja cerrada.", "ok")
    return redirect(url_for("caja"))


# ---------- reportes ----------
@app.route("/reportes")
@login_required
@admin_required
def reportes():
    desde = request.args.get("desde", (now_cl().date() - timedelta(days=30)).isoformat())
    hasta = request.args.get("hasta", now_cl().date().isoformat())
    a = datetime.fromisoformat(desde)
    b = datetime.fromisoformat(hasta) + timedelta(days=1)
    ventas = Venta.query.filter(Venta.fecha >= a, Venta.fecha < b).all()
    total = sum(v.total for v in ventas)
    costo = sum(v.costo_total for v in ventas)
    por_metodo = {}
    for v in ventas:
        por_metodo[v.metodo_pago] = por_metodo.get(v.metodo_pago, 0) + v.total
    top = (db.session.query(VentaItem.nombre,
           func.sum(VentaItem.cantidad).label("q"),
           func.sum(VentaItem.cantidad * VentaItem.precio_unit).label("monto"))
           .join(Venta).filter(Venta.fecha >= a, Venta.fecha < b)
           .group_by(VentaItem.nombre).order_by(func.sum(VentaItem.cantidad * VentaItem.precio_unit).desc())
           .limit(15).all())
    return render_template("reportes.html", desde=desde, hasta=hasta, total=total,
                           costo=costo, utilidad=total - costo, n=len(ventas),
                           por_metodo=por_metodo, top=top)


# ---------- usuarios (admin) ----------
@app.route("/usuarios", methods=["GET", "POST"])
@login_required
@admin_required
def usuarios():
    if request.method == "POST":
        un = request.form["username"].strip()
        if Usuario.query.filter_by(username=un).first():
            flash("Usuario ya existe.", "error")
        else:
            u = Usuario(username=un, nombre=request.form["nombre"], rol=request.form["rol"])
            u.set_password(request.form["password"])
            db.session.add(u)
            db.session.commit()
            flash("Usuario creado.", "ok")
        return redirect(url_for("usuarios"))
    return render_template("usuarios.html", usuarios=Usuario.query.all())


@app.route("/usuario/<int:uid>/toggle", methods=["POST"])
@login_required
@admin_required
def usuario_toggle(uid):
    u = db.session.get(Usuario, uid) or abort(404)
    if u.id != session["uid"]:
        u.activo = not u.activo
        db.session.commit()
    return redirect(url_for("usuarios"))


CATS_EXTRA = ["Ejemplos"]
PRODUCTOS_TEST = [
    ("Producto Demo 1", "Ejemplos", "un", 110, 220, 11, 5),
    ("Producto Demo 2", "Ejemplos", "un", 120, 240, 12, 5),
    ("Producto Demo 3", "Ejemplos", "un", 130, 260, 13, 5),
    ("Producto Demo 4", "Ejemplos", "un", 140, 280, 14, 5),
    ("Producto Demo 5", "Ejemplos", "un", 150, 300, 15, 5),
    ("Producto Demo 6", "Ejemplos", "un", 160, 320, 16, 5),
    ("Producto Demo 7", "Ejemplos", "un", 170, 340, 17, 5),
    ("Producto Demo 8", "Ejemplos", "un", 180, 360, 18, 5),
    ("Producto Demo 9", "Ejemplos", "un", 190, 380, 19, 5),
    ("Producto Demo 10", "Ejemplos", "un", 200, 400, 20, 5),
    ("Producto Demo 11", "Ejemplos", "un", 210, 420, 21, 5),
    ("Producto Demo 12", "Ejemplos", "un", 220, 440, 22, 5),
]

@app.route("/admin/seed-test", methods=["GET", "POST"])
@login_required
@admin_required
def seed_test():
    if request.method == "POST":
        for nombre in CATS_EXTRA:
            if not Categoria.query.filter_by(nombre=nombre).first():
                db.session.add(Categoria(nombre=nombre))
        db.session.commit()
        n = 0
        for nombre, cat_nombre, unidad, costo, precio, stock, stock_min in PRODUCTOS_TEST:
            if Producto.query.filter_by(nombre=nombre).first():
                continue
            cat = Categoria.query.filter_by(nombre=cat_nombre).first()
            db.session.add(Producto(nombre=nombre, categoria_id=cat.id if cat else None,
                                    unidad=unidad, costo=costo, precio=precio,
                                    stock=stock, stock_min=stock_min))
            n += 1
        db.session.commit()
        flash(f"{n} productos de prueba cargados correctamente.", "ok")
        return redirect(url_for("inventario"))
    total = Producto.query.filter_by(activo=True).count()
    return render_template("seed_test.html", total=total, cantidad=len(PRODUCTOS_TEST))


# ---------- init ----------
def seed():
    db.create_all()
    if not Usuario.query.first():
        admin = Usuario(username="admin", nombre="Administrador", rol="admin")
        admin.set_password(os.environ.get("DEMO_ADMIN_PASSWORD", "demo-local-only"))
        db.session.add(admin)
        vend = Usuario(username="vendedor", nombre="Vendedor", rol="vendedor")
        vend.set_password(os.environ.get("DEMO_SELLER_PASSWORD", "demo-local-only"))
        db.session.add(vend)
        for c in ["Bebidas", "Abarrotes", "Lácteos", "Snacks", "Cigarros", "Aseo", "Otros"]:
            db.session.add(Categoria(nombre=c))
        db.session.commit()
        print("Demo local inicializada. Consulta README.md para el acceso de muestra.")


if __name__ == "__main__":
    with app.app_context():
        seed()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
