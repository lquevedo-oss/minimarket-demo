from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from zoneinfo import ZoneInfo

db = SQLAlchemy()

def now_cl():
    return datetime.now(ZoneInfo("America/Santiago")).replace(tzinfo=None)


class Usuario(db.Model):
    __tablename__ = "usuarios"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    nombre = db.Column(db.String(100), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    rol = db.Column(db.String(20), nullable=False, default="vendedor")  # admin | vendedor
    activo = db.Column(db.Boolean, default=True)
    creado = db.Column(db.DateTime, default=now_cl)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


class Categoria(db.Model):
    __tablename__ = "categorias"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(80), unique=True, nullable=False)


class Proveedor(db.Model):
    __tablename__ = "proveedores"
    id = db.Column(db.Integer, primary_key=True)
    rut = db.Column(db.String(20))
    nombre = db.Column(db.String(150), nullable=False)
    giro = db.Column(db.String(150))
    contacto = db.Column(db.String(150))


class Producto(db.Model):
    __tablename__ = "productos"
    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(60), unique=True)  # código de barras / SKU
    nombre = db.Column(db.String(200), nullable=False)
    categoria_id = db.Column(db.Integer, db.ForeignKey("categorias.id"))
    categoria = db.relationship("Categoria")
    costo = db.Column(db.Float, default=0)     # costo neto unitario
    precio = db.Column(db.Float, default=0)    # precio venta
    stock = db.Column(db.Float, default=0)
    stock_min = db.Column(db.Float, default=5)
    unidad = db.Column(db.String(20), default="un")
    activo = db.Column(db.Boolean, default=True)
    creado = db.Column(db.DateTime, default=now_cl)

    @property
    def margen(self):
        if self.precio and self.costo:
            return round((self.precio - self.costo) / self.precio * 100, 1)
        return 0


class Venta(db.Model):
    __tablename__ = "ventas"
    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.DateTime, default=now_cl)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"))
    usuario = db.relationship("Usuario")
    total = db.Column(db.Float, default=0)
    costo_total = db.Column(db.Float, default=0)
    metodo_pago = db.Column(db.String(20), default="efectivo")  # efectivo|debito|credito|transferencia|fiado
    pagado = db.Column(db.Boolean, default=True)  # False si es fiado pendiente
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id"))
    cliente = db.relationship("Cliente")
    sesion_caja_id = db.Column(db.Integer, db.ForeignKey("sesiones_caja.id"))
    items = db.relationship("VentaItem", backref="venta", cascade="all, delete-orphan")

    @property
    def utilidad(self):
        return round(self.total - self.costo_total, 0)


class VentaItem(db.Model):
    __tablename__ = "venta_items"
    id = db.Column(db.Integer, primary_key=True)
    venta_id = db.Column(db.Integer, db.ForeignKey("ventas.id"))
    producto_id = db.Column(db.Integer, db.ForeignKey("productos.id"))
    producto = db.relationship("Producto")
    nombre = db.Column(db.String(200))  # snapshot
    cantidad = db.Column(db.Float)
    precio_unit = db.Column(db.Float)
    costo_unit = db.Column(db.Float)

    @property
    def subtotal(self):
        return round(self.cantidad * self.precio_unit, 0)


class Cliente(db.Model):
    __tablename__ = "clientes"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(150), nullable=False)
    telefono = db.Column(db.String(40))
    nota = db.Column(db.String(255))
    creado = db.Column(db.DateTime, default=now_cl)

    @property
    def deuda(self):
        from sqlalchemy import func
        pendiente = db.session.query(func.coalesce(func.sum(Venta.total), 0)).filter(
            Venta.cliente_id == self.id, Venta.metodo_pago == "fiado", Venta.pagado == False
        ).scalar() or 0
        abonos = db.session.query(func.coalesce(func.sum(Abono.monto), 0)).filter(
            Abono.cliente_id == self.id
        ).scalar() or 0
        return round(pendiente - abonos, 0)


class Abono(db.Model):
    __tablename__ = "abonos"
    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id"))
    cliente = db.relationship("Cliente")
    monto = db.Column(db.Float, nullable=False)
    fecha = db.Column(db.DateTime, default=now_cl)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"))
    nota = db.Column(db.String(255))


class SesionCaja(db.Model):
    __tablename__ = "sesiones_caja"
    id = db.Column(db.Integer, primary_key=True)
    abierta = db.Column(db.DateTime, default=now_cl)
    cerrada = db.Column(db.DateTime)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"))
    usuario = db.relationship("Usuario")
    monto_inicial = db.Column(db.Float, default=0)
    monto_final_real = db.Column(db.Float)  # conteo físico al cierre
    estado = db.Column(db.String(20), default="abierta")  # abierta | cerrada
    movimientos = db.relationship("MovimientoCaja", backref="sesion", cascade="all, delete-orphan")


class MovimientoCaja(db.Model):
    __tablename__ = "movimientos_caja"
    id = db.Column(db.Integer, primary_key=True)
    sesion_caja_id = db.Column(db.Integer, db.ForeignKey("sesiones_caja.id"))
    fecha = db.Column(db.DateTime, default=now_cl)
    tipo = db.Column(db.String(20))  # ingreso | egreso | venta | abono
    monto = db.Column(db.Float)
    descripcion = db.Column(db.String(255))
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"))


class Factura(db.Model):
    __tablename__ = "facturas"
    id = db.Column(db.Integer, primary_key=True)
    folio = db.Column(db.String(40))
    proveedor_id = db.Column(db.Integer, db.ForeignKey("proveedores.id"))
    proveedor = db.relationship("Proveedor")
    fecha_emision = db.Column(db.String(40))
    neto = db.Column(db.Float, default=0)
    iva = db.Column(db.Float, default=0)
    total = db.Column(db.Float, default=0)
    archivo = db.Column(db.String(255))
    procesada = db.Column(db.Boolean, default=False)  # True si ya se aplicó a inventario
    cargada = db.Column(db.DateTime, default=now_cl)
    detalle_json = db.Column(db.Text)  # items extraídos en JSON


class Config(db.Model):
    __tablename__ = "config"
    clave = db.Column(db.String(50), primary_key=True)
    valor = db.Column(db.String(255))
