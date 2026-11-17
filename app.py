"""
MJ Streaming · tienda + panel de administración (Flask)

  /                     → tienda pública (se actualiza sola cada 20 s cuando editas)
  /admin                → panel para editar productos, precios, stock, imágenes y ajustes
  /api/catalogo         → datos públicos (JSON)
  /img/<archivo>        → imágenes de productos

Variables de entorno:
  ADMIN_PASSWORD   (obligatoria) contraseña del panel /admin
  SECRET_KEY       (opcional)    clave para firmar la sesión; si falta se deriva de ADMIN_PASSWORD
  DATA_DIR         (opcional)    carpeta donde se guardan catalogo.json e imágenes (por defecto ./data)
  GITHUB_TOKEN, GITHUB_REPO, GITHUB_BRANCH   (opcionales) respaldo automático en GitHub para
                   hosting con disco temporal (Render gratis). Ver README.md

Pagos automáticos con Bold + entrega por correo (ver README.md):
  /api/checkout         → crea el pedido, reserva cuentas y devuelve el link de pago de Bold
  /api/bold/webhook     → recibe SALE_APPROVED de Bold, asigna cuentas y las envía al correo del cliente
  /api/pedido/<ref>     → estado público del pedido (para la página de retorno)
  /api/admin/inventario → credenciales de plataformas (cifradas en disco)
  /api/admin/pedidos    → pedidos, reenvío manual, entrega manual

  BOLD_API_KEY     llave de identidad de Bold (crea los links)
  BOLD_SECRET_KEY  llave secreta de Bold (verifica la firma del webhook)
  BOLD_TEST=1      modo pruebas de Bold (la firma del webhook usa llave vacía)
  PUBLIC_URL       https://tudominio.com  (para el callback_url; si falta se deduce de la petición)
  INVENTORY_KEY    frase larga y secreta que cifra las credenciales (¡guárdala! sin ella no se descifran)
  RESEND_API_KEY + MAIL_FROM            envío de correo vía Resend (recomendado en Render)
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM   alternativa SMTP
  ADMIN_EMAIL      (opcional) recibe avisos: sin stock, errores de correo, pagos con monto distinto
  RESERVA_MIN      (opcional, 30) minutos que se reservan las cuentas mientras el cliente paga
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from html import escape as h
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file, session
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover
    Fernet = None

BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE / "data"))
IMG_DIR = DATA_DIR / "img"
HIST_DIR = DATA_DIR / "hist"
CATALOG = DATA_DIR / "catalogo.json"
DEFAULT = BASE / "catalogo.default.json"
PUBLIC = BASE / "public"
STATIC_IMG = BASE / "static" / "img"
IMG_EXT = ("webp", "png", "jpg", "jpeg", "avif", "svg", "gif")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_REPO", "")
GH_BRANCH = os.environ.get("GITHUB_BRANCH", "data")
GH_DIR = "tienda-data"          # carpeta dentro del repo donde se guarda el respaldo
GH_ON = bool(GH_TOKEN and GH_REPO)

INV_FILE = DATA_DIR / "inventario.json"
ORD_FILE = DATA_DIR / "pedidos.json"
BOLD_API = "https://integrations.api.bold.co"
BOLD_API_KEY = os.environ.get("BOLD_API_KEY", "").strip()
BOLD_SECRET_KEY = os.environ.get("BOLD_SECRET_KEY", "").strip()
BOLD_TEST = os.environ.get("BOLD_TEST", "") == "1"
BOLD_METHODS = [x.strip() for x in os.environ.get("BOLD_METHODS", "").split(",") if x.strip()]
BOLD_EXPIRE_MIN = int(os.environ.get("BOLD_EXPIRE_MIN", "0") or 0)
BOLD_EXPIRE_UNIT = os.environ.get("BOLD_EXPIRE_UNIT", "ns")      # la doc de Bold dice nanosegundos; usa "ms" si tu cuenta lo exige
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
RESERVA_MIN = int(os.environ.get("RESERVA_MIN", "30") or 30)
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip()
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
MAIL_FROM = os.environ.get("MAIL_FROM", "").strip()
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465") or 465)
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "")
if not MAIL_FROM:
    MAIL_FROM = SMTP_USER

app = Flask(__name__, static_folder=None)
if os.environ.get("RENDER") or os.environ.get("TRUST_PROXY"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(("mj-secret:" + ADMIN_PASSWORD).encode()).hexdigest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER") or os.environ.get("FORCE_HTTPS")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
)
_lock = threading.RLock()


# ─────────────────────────── cifrado de credenciales ───────────────────────────
KEY_EXPLICIT = bool(os.environ.get("INVENTORY_KEY"))
_pass = os.environ.get("INVENTORY_KEY") or app.secret_key
FERNET = Fernet(base64.urlsafe_b64encode(hashlib.sha256(("mj-inv:" + _pass).encode()).digest())) if Fernet else None


# ─────────────────────────── almacenamiento ───────────────────────────
def load_data():
    with _lock:
        for p in (CATALOG, DEFAULT):
            if p.exists():
                try:
                    return json.loads(p.read_text("utf-8"))
                except Exception:
                    continue
    return {"config": {}, "products": []}


def save_data(d):
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        HIST_DIR.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(d, ensure_ascii=False, indent=1)
        tmp = CATALOG.with_suffix(".tmp")
        tmp.write_text(raw, "utf-8")
        os.replace(tmp, CATALOG)
        (HIST_DIR / ("cat-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")).write_text(raw, "utf-8")
        for old in sorted(HIST_DIR.glob("cat-*.json"))[:-40]:      # conserva las últimas 40 versiones
            old.unlink(missing_ok=True)
    gh_async("catalogo.json", raw.encode("utf-8"))


# ─────────────────────────── validación ───────────────────────────
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
SLUG = re.compile(r"[^a-z0-9]+")


def s(v, n=300):
    return str(v if v is not None else "").strip()[:n]


def to_int(v, default=0, lo=0, hi=10**9):
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return default


def lst(v):
    return v if isinstance(v, list) else []


SOCIAL = {"instagram": "https://www.instagram.com/", "tiktok": "https://www.tiktok.com/@", "facebook": "https://www.facebook.com/"}


def norm_social(k, v):
    """Acepta URL completa, 'instagram.com/usuario', '@usuario' o 'usuario' y devuelve una URL https válida (o vacío)."""
    v = s(v, 300).strip()
    if not v:
        return ""
    if not re.match(r"^https?://", v, re.I):
        if re.match(r"^(www\.)?[a-z0-9-]+\.[a-z]{2,}(/|$)", v, re.I):
            v = "https://" + v
        else:
            handle = v.lstrip("@/ ").split("?")[0].strip("/")
            if not re.match(r"^[A-Za-z0-9._-]{1,60}$", handle):
                return ""
            v = SOCIAL[k] + handle
    return v if re.match(r"^https?://[^\s\"'<>]+$", v) else ""


def clean_url(v):
    v = s(v, 500)
    return v if re.match(r"^(https?://|/img/)", v) else ""


def clean_product(p, seen):
    if not isinstance(p, dict):
        return None
    name = s(p.get("name"), 80)
    if not name:
        return None
    pid = SLUG.sub("-", s(p.get("id") or name, 60).lower()).strip("-") or "producto"
    base, k = pid, 2
    while pid in seen:
        pid, k = f"{base}-{k}", k + 1
    seen.add(pid)
    plans = []
    for x in lst(p.get("plans"))[:12]:
        if not isinstance(x, dict) or not s(x.get("l"), 40):
            continue
        o = to_int(x.get("o"), 0)
        plans.append({"l": s(x.get("l"), 40), "p": to_int(x.get("p")), **({"o": o} if o else {})})
    if not plans:
        plans = [{"l": "1 mes", "p": 0}]
    stock = p.get("stock")
    return {
        "id": pid,
        "name": name,
        "cat": s(p.get("cat"), 40) or "Video",
        "tipo": s(p.get("tipo"), 30) or "Perfil",
        "c1": p.get("c1") if HEX.match(str(p.get("c1", ""))) else "#2f7bff",
        "c2": p.get("c2") if HEX.match(str(p.get("c2", ""))) else "#0b1020",
        "badge": s(p.get("badge"), 24),
        "hot": bool(p.get("hot")),
        "oculto": bool(p.get("oculto")),
        "stock": None if stock in (None, "") else to_int(stock),
        "img": clean_url(p.get("img")),
        "fit": "contain" if p.get("fit") == "contain" else "",
        "desc": s(p.get("desc"), 300),
        "incluye": [s(i, 120) for i in lst(p.get("incluye"))[:12] if s(i, 120)],
        "nota": s(p.get("nota"), 400),
        "plans": plans,
    }


def clean(d):
    if not isinstance(d, dict):
        raise ValueError("Formato inválido")
    c = d.get("config") if isinstance(d.get("config"), dict) else {}
    redes = c.get("redes") if isinstance(c.get("redes"), dict) else {}
    cupones = {}
    for k, v in (c.get("cupones") if isinstance(c.get("cupones"), dict) else {}).items():
        k = s(k, 30).upper()
        if k:
            cupones[k] = to_int(v, 0, 0, 90)
    desc = []
    for x in lst(c.get("descuentos"))[:8]:
        if isinstance(x, dict):
            desc.append({"min": to_int(x.get("min"), 2, 1, 99), "pct": to_int(x.get("pct"), 0, 0, 90)})
    wa = re.sub(r"\D", "", s(c.get("whatsapp"), 20))
    if len(wa) == 10:
        wa = "57" + wa
    if not 8 <= len(wa) <= 15:
        raise ValueError("El número de WhatsApp debe tener entre 8 y 15 dígitos (con indicativo, ej. 573215296552)")
    config = {
        "tienda": s(c.get("tienda"), 60) or "MJ Streaming",
        "anuncio": s(c.get("anuncio"), 200),
        "whatsapp": wa,
        "moneda": s(c.get("moneda"), 5) or "COP",
        "locale": s(c.get("locale"), 10) or "es-CO",
        "ciudad": s(c.get("ciudad"), 80),
        "horario": s(c.get("horario"), 120),
        "redes": {k: norm_social(k, redes.get(k)) for k in ("instagram", "tiktok", "facebook")},
        "tema": re.sub(r"[^a-z0-9-]", "", s(c.get("tema"), 30).lower()) or "neon",
        "efectos": c.get("efectos") is not False,
        "tema_anuncio": s(c.get("tema_anuncio"), 200),
        "tema_desde": s(c.get("tema_desde"), 10) if re.match(r"^\d{4}-\d{2}-\d{2}$", s(c.get("tema_desde"), 10)) else "",
        "tema_hasta": s(c.get("tema_hasta"), 10) if re.match(r"^\d{4}-\d{2}-\d{2}$", s(c.get("tema_hasta"), 10)) else "",
        "pagos": [s(x, 40) for x in lst(c.get("pagos"))[:12] if s(x, 40)],
        "descuentos": sorted(desc, key=lambda x: x["min"]),
        "cupones": cupones,
        "distribuidores": bool(c.get("distribuidores")),
        "stockBajo": to_int(c.get("stockBajo"), 3, 0, 99),
    }
    seen = set()
    products = [x for x in (clean_product(p, seen) for p in lst(d.get("products"))[:200]) if x]
    testimonios = []
    for t in lst(d.get("testimonios"))[:30]:
        if isinstance(t, dict) and s(t.get("texto"), 400) and s(t.get("nombre"), 60):
            testimonios.append({k: s(t.get(k), 400 if k == "texto" else 60) for k in ("nombre", "ciudad", "plataforma", "texto")})
    faq = []
    for f in lst(d.get("faq"))[:30]:
        if isinstance(f, dict) and s(f.get("q"), 200) and s(f.get("a"), 1000):
            faq.append({"q": s(f.get("q"), 200), "a": s(f.get("a"), 1000)})
    return {
        "config": config,
        "products": products,
        "testimonios": testimonios,
        "faq": faq,
        "terminos": str(d.get("terminos") or "")[:8000],
    }


# ─────────────────────────── imágenes ───────────────────────────
def find_image(pid):
    for ext in IMG_EXT:
        for base in (IMG_DIR, STATIC_IMG):
            f = base / f"{pid}.{ext}"
            if f.is_file():
                return f"/img/{f.name}?v={int(f.stat().st_mtime)}"
    return ""


def with_images(d, public):
    out = json.loads(json.dumps(d))
    prods = []
    for p in out.get("products", []):
        if public and p.get("oculto"):
            continue
        auto = find_image(p["id"])
        if public:
            p["img"] = p.get("img") or auto
            p.pop("oculto", None)
        else:
            p["_auto"] = auto
        prods.append(p)
    out["products"] = prods
    cfg = out.setdefault("config", {})
    cfg["redesImg"] = {k: find_image("red-" + k) for k in ("instagram", "tiktok", "facebook")}
    cfg["temaImg"] = find_image("tema-fondo")
    return out


MAGIC = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg", b"RIFF": "webp", b"GIF8": "gif"}


def sniff(raw):
    for sig, ext in MAGIC.items():
        if raw.startswith(sig):
            if ext == "webp" and raw[8:12] != b"WEBP":
                continue
            return ext
    return None


# ─────────────────────────── respaldo en GitHub (opcional) ───────────────────────────
SYNC = {"ok": None, "msg": "Respaldo en GitHub desactivado" if not GH_ON else "Sin cambios aún", "t": 0}


def gh(method, path, body=None):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + GH_TOKEN,
            "Accept": "application/vnd.github+json",
            "User-Agent": "mj-streaming-tienda",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def gh_ensure_branch():
    try:
        gh("GET", f"/repos/{GH_REPO}/git/ref/heads/{GH_BRANCH}")
        return
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    repo = gh("GET", f"/repos/{GH_REPO}")
    ref = gh("GET", f"/repos/{GH_REPO}/git/ref/heads/{repo['default_branch']}")
    gh("POST", f"/repos/{GH_REPO}/git/refs", {"ref": f"refs/heads/{GH_BRANCH}", "sha": ref["object"]["sha"]})


def gh_put(rel, data):
    gh_ensure_branch()
    path = urllib.parse.quote(f"{GH_DIR}/{rel}")
    sha = None
    try:
        sha = gh("GET", f"/repos/{GH_REPO}/contents/{path}?ref={GH_BRANCH}")["sha"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    body = {"message": f"tienda: actualiza {rel}", "content": base64.b64encode(data).decode(), "branch": GH_BRANCH}
    if sha:
        body["sha"] = sha
    gh("PUT", f"/repos/{GH_REPO}/contents/{path}", body)


def gh_delete(rel):
    path = urllib.parse.quote(f"{GH_DIR}/{rel}")
    try:
        sha = gh("GET", f"/repos/{GH_REPO}/contents/{path}?ref={GH_BRANCH}")["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return
        raise
    gh("DELETE", f"/repos/{GH_REPO}/contents/{path}", {"message": f"tienda: borra {rel}", "sha": sha, "branch": GH_BRANCH})


def gh_async(rel, data=None, delete=False):
    if not GH_ON:
        return

    def work():
        try:
            gh_delete(rel) if delete else gh_put(rel, data)
            SYNC.update(ok=True, msg="Respaldo en GitHub al día", t=time.time())
        except Exception as e:  # noqa: BLE001
            SYNC.update(ok=False, msg=f"Falló el respaldo en GitHub: {e}", t=time.time())

    threading.Thread(target=work, daemon=True).start()


LATEST, LATEST_LOCK, WORKERS = {}, threading.Lock(), set()


def gh_push_latest(rel, data):
    """Sube siempre la última versión de un archivo que cambia seguido (pedidos, inventario), sin choques de commits."""
    if not GH_ON:
        return
    with LATEST_LOCK:
        LATEST[rel] = data
        if rel in WORKERS:
            return
        WORKERS.add(rel)

    def work():
        while True:
            with LATEST_LOCK:
                data = LATEST.pop(rel, None)
                if data is None:
                    WORKERS.discard(rel)
                    return
            try:
                gh_put(rel, data)
                SYNC.update(ok=True, msg="Respaldo en GitHub al día", t=time.time())
            except Exception as e:  # noqa: BLE001
                SYNC.update(ok=False, msg=f"Falló el respaldo en GitHub: {e}", t=time.time())
            time.sleep(2)

    threading.Thread(target=work, daemon=True).start()


def gh_restore():
    """Al arrancar sin datos locales (disco temporal), recupera catálogo, inventario, pedidos e imágenes desde la rama de respaldo."""
    try:
        tree = gh("GET", f"/repos/{GH_REPO}/git/trees/{GH_BRANCH}?recursive=1")["tree"]
        for node in tree:
            if node["type"] != "blob" or not node["path"].startswith(GH_DIR + "/"):
                continue
            rel = node["path"][len(GH_DIR) + 1:]
            if rel not in ("catalogo.json", "inventario.json", "pedidos.enc") and not re.match(r"^img/[A-Za-z0-9_.-]+$", rel):
                continue
            if rel in ("inventario.json", "pedidos.enc") and not KEY_EXPLICIT:
                continue            # sin INVENTORY_KEY no se restauran datos privados
            blob = gh("GET", f"/repos/{GH_REPO}/git/blobs/{node['sha']}")
            content = base64.b64decode(blob["content"])
            if rel == "pedidos.enc":
                try:
                    content = FERNET.decrypt(content)
                except Exception:  # noqa: BLE001
                    continue
                rel = "pedidos.json"
            dest = DATA_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        SYNC.update(ok=True, msg="Datos restaurados desde GitHub", t=time.time())
    except Exception as e:  # noqa: BLE001
        SYNC.update(ok=False, msg=f"No se pudo restaurar desde GitHub: {e}", t=time.time())




# ─────────────────────────── autenticación ───────────────────────────
FAILS = {}


def client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "") or "").split(",")[0].strip()


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not ADMIN_PASSWORD:
            return jsonify(error="Falta definir ADMIN_PASSWORD en el servidor"), 503
        if not session.get("admin"):
            return jsonify(error="No autorizado"), 401
        if request.method != "GET" and request.headers.get("X-Admin") != "1":
            return jsonify(error="Solicitud no válida"), 400
        return f(*a, **kw)

    return wrapper


@app.post("/api/admin/login")
def login():
    if not ADMIN_PASSWORD:
        return jsonify(error="Falta definir ADMIN_PASSWORD en el servidor"), 503
    ip, now = client_ip(), time.time()
    FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < 600]
    if len(FAILS[ip]) >= 8:
        return jsonify(error="Demasiados intentos. Espera unos minutos."), 429
    pw = str((request.get_json(silent=True) or {}).get("password", ""))
    if hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
        session.clear()
        session["admin"] = True
        session.permanent = True
        return jsonify(ok=True)
    FAILS[ip].append(now)
    time.sleep(1)
    return jsonify(error="Contraseña incorrecta"), 401


@app.post("/api/admin/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


# ─────────────────────────── API pública ───────────────────────────
@app.get("/api/catalogo")
def api_catalogo():
    raw, etag = public_payload()
    if request.headers.get("If-None-Match") == etag:
        r = Response(status=304)
    else:
        r = Response(raw, mimetype="application/json")
    r.headers["ETag"] = etag
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/")
def index():
    html = (PUBLIC / "index.html").read_text("utf-8")
    raw, etag = public_payload()
    raw = raw.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = html.replace("window.__DATA__=null;window.__ETAG__=null;", f"window.__DATA__={raw};window.__ETAG__={json.dumps(etag)};", 1)
    r = Response(html, mimetype="text/html")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/img/<path:name>")
def img(name):
    if not re.match(r"^[A-Za-z0-9_.-]+$", name):
        abort(404)
    for base in (IMG_DIR, STATIC_IMG):
        f = base / name
        if f.is_file():
            r = send_file(f, max_age=86400)
            return r
    abort(404)


@app.get("/healthz")
def healthz():
    return "ok"


# ─────────────────────────── panel ───────────────────────────
@app.get("/admin")
def admin_page():
    r = Response((PUBLIC / "admin.html").read_text("utf-8"), mimetype="text/html")
    r.headers["Cache-Control"] = "no-store"
    r.headers["X-Robots-Tag"] = "noindex"
    return r


@app.get("/api/admin/catalogo")
@admin_required
def admin_get():
    return jsonify(with_images(load_data(), public=False))


@app.put("/api/admin/catalogo")
@admin_required
def admin_put():
    try:
        d = clean(request.get_json(force=True, silent=True))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    save_data(d)
    return jsonify(with_images(d, public=False))


@app.get("/api/admin/estado")
@admin_required
def admin_estado():
    return jsonify(
        sync=SYNC, github=GH_ON,
        bold={"api": bool(BOLD_API_KEY), "secret": bool(BOLD_SECRET_KEY), "test": BOLD_TEST, "webhook": f"{PUBLIC_URL or request.host_url.rstrip('/')}/api/bold/webhook"},
        correo=mail_ready(), key_explicit=KEY_EXPLICIT, public_url=PUBLIC_URL, reserva_min=RESERVA_MIN,
        eventos=list(EVENT_LOG),
    )


@app.post("/api/admin/imagen")
@admin_required
def admin_img_up():
    pid = SLUG.sub("-", request.form.get("id", "").lower()).strip("-")
    f = request.files.get("file")
    if not pid or not f:
        return jsonify(error="Falta el producto o el archivo"), 400
    raw = f.read()
    ext = sniff(raw)
    if not ext:
        return jsonify(error="Formato no soportado (usa PNG, JPG o WebP)"), 400
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    for e in IMG_EXT:
        (IMG_DIR / f"{pid}.{e}").unlink(missing_ok=True)
    (IMG_DIR / f"{pid}.{ext}").write_bytes(raw)
    gh_async(f"img/{pid}.{ext}", raw)
    return jsonify(url=f"/img/{pid}.{ext}?v={int(time.time())}")


@app.delete("/api/admin/imagen/<pid>")
@admin_required
def admin_img_del(pid):
    pid = SLUG.sub("-", pid.lower()).strip("-")
    for e in IMG_EXT:
        if (IMG_DIR / f"{pid}.{e}").exists():
            (IMG_DIR / f"{pid}.{e}").unlink()
            gh_async(f"img/{pid}.{e}", delete=True)
    return jsonify(ok=True, auto=find_image(pid))


@app.get("/api/admin/historial")
@admin_required
def admin_hist():
    names = sorted((p.name for p in HIST_DIR.glob("cat-*.json")), reverse=True) if HIST_DIR.exists() else []
    return jsonify(items=names[:40])


@app.post("/api/admin/restaurar")
@admin_required
def admin_restore():
    name = str((request.get_json(silent=True) or {}).get("name", ""))
    if not re.match(r"^cat-\d{8}-\d{6}\.json$", name) or not (HIST_DIR / name).exists():
        return jsonify(error="Versión no encontrada"), 404
    d = clean(json.loads((HIST_DIR / name).read_text("utf-8")))
    save_data(d)
    return jsonify(with_images(d, public=False))


# ═══════════════════════════ PAGOS BOLD · INVENTARIO · PEDIDOS ═══════════════════════════
if GH_ON and not CATALOG.exists():
    gh_restore()

EVENT_LOG = deque(maxlen=60)          # últimos avisos de Bold (para depurar desde /admin)
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}$")


def log_event(kind, msg, **extra):
    EVENT_LOG.appendleft({"t": int(time.time()), "kind": kind, "msg": msg, **extra})


def _read(path, default):
    with _lock:
        for p in (path, path.with_suffix(".bak")):
            if p.exists():
                try:
                    return json.loads(p.read_text("utf-8"))
                except Exception:  # noqa: BLE001
                    continue
    return default


def _write(path, obj):
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(obj, ensure_ascii=False, indent=1)
        if path.exists():
            try:
                path.with_suffix(".bak").write_bytes(path.read_bytes())
            except OSError:
                pass
        tmp = path.with_suffix(".tmp")
        tmp.write_text(raw, "utf-8")
        os.replace(tmp, path)
    if KEY_EXPLICIT and GH_ON:
        if path == ORD_FILE:
            gh_push_latest("pedidos.enc", FERNET.encrypt(raw.encode()))
        else:
            gh_push_latest("inventario.json", raw.encode())


def load_inv():
    return _read(INV_FILE, {"items": []})


def load_ord():
    return _read(ORD_FILE, {"orders": []})


def enc_acc(email, password, perfil="", pin="", extra=""):
    return FERNET.encrypt(json.dumps({"e": email, "p": password, "f": perfil, "n": pin, "x": extra}, ensure_ascii=False).encode()).decode()


def dec_acc(a):
    try:
        d = json.loads(FERNET.decrypt(a["enc"].encode()))
        return {"email": d.get("e", ""), "password": d.get("p", ""), "perfil": d.get("f", ""), "pin": d.get("n", ""), "extra": d.get("x", "")}
    except Exception:  # noqa: BLE001
        return {"email": "⚠ no se pudo descifrar (¿cambió INVENTORY_KEY?)", "password": "", "perfil": "", "pin": "", "extra": ""}


def acc_free(a, now, ref=None):
    """Una cuenta está libre si está disponible, o reservada para este pedido, o con reserva vencida."""
    if a["status"] == "disponible":
        return True
    return a["status"] == "reservada" and (a.get("order") == ref or a.get("hasta", 0) < now)


def acc_fits(a, plan):
    return not a.get("plan") or a["plan"].strip().lower() == plan.strip().lower()


def managed_pids(inv):
    return {a["pid"] for a in inv["items"]}


def apply_stock(d, inv):
    """Pone en el catálogo público el stock real (cuentas libres) de los productos con inventario."""
    now = time.time()
    managed = managed_pids(inv)
    for p in d.get("products", []):
        if p["id"] not in managed:
            continue
        free = [a for a in inv["items"] if a["pid"] == p["id"] and acc_free(a, now)]
        p["inv"] = True
        p["stock"] = len(free)
        for pl in p["plans"]:
            pl["n"] = sum(1 for a in free if acc_fits(a, pl["l"]))
    return d


def public_payload():
    d = apply_stock(with_images(load_data(), public=True), load_inv())
    raw = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
    return raw, '"' + hashlib.sha1(raw.encode()).hexdigest()[:20] + '"'


def r100(x):
    return int(x / 100 + 0.5) * 100


def price_cart(cfg, products, items, coupon):
    """Calcula el pedido en el servidor con el mismo esquema de descuentos de la tienda (nunca confía en el navegador)."""
    byid = {p["id"]: p for p in products if not p.get("oculto")}
    lines, n, sub = [], 0, 0
    for it in items[:30]:
        if not isinstance(it, dict):
            continue
        p = byid.get(s(it.get("id"), 60))
        q = to_int(it.get("qty"), 0, 0, 20)
        i = to_int(it.get("plan"), 0, 0, 99)
        if not p or not q or i >= len(p["plans"]):
            raise ValueError("Hay un producto del carrito que ya no está disponible. Actualiza la página.")
        pl = p["plans"][i]
        if pl["p"] < 1:
            raise ValueError(f"{p['name']} no tiene precio configurado.")
        lines.append({"pid": p["id"], "name": p["name"], "tipo": p.get("tipo", ""), "plan": pl["l"], "qty": q, "unit": pl["p"], "stock": p.get("stock")})
        n += q
        sub += pl["p"] * q
    if not lines:
        raise ValueError("El carrito está vacío.")
    tiers = sorted(cfg.get("descuentos") or [], key=lambda t: t["min"])
    tier = next((t for t in reversed(tiers) if n >= t["min"]), None)
    d_tier = r100(sub * tier["pct"] / 100) if tier else 0
    code = s(coupon, 30).upper()
    pct = (cfg.get("cupones") or {}).get(code, 0) if code else 0
    d_cup = r100((sub - d_tier) * pct / 100) if pct else 0
    return {"lines": lines, "n": n, "subtotal": sub, "d_tier": d_tier, "d_cup": d_cup, "coupon": code if pct else "", "total": sub - d_tier - d_cup}


def reserve(inv, lines, ref):
    """Reserva (sin guardar) las cuentas de las líneas con inventario. Devuelve None si todo OK o el mensaje de falta de stock."""
    now = time.time()
    used = set()
    managed = managed_pids(inv)
    plan = []
    for ln in lines:
        if ln["pid"] not in managed:
            if ln.get("stock") is not None and ln["stock"] < ln["qty"]:
                return f"{ln['name']} está agotado." if ln["stock"] == 0 else f"Solo quedan {ln['stock']} de {ln['name']}."
            continue
        cand = [a for a in inv["items"] if a["pid"] == ln["pid"] and a["id"] not in used and acc_free(a, now, ref) and acc_fits(a, ln["plan"])]
        cand.sort(key=lambda a: (not a.get("plan"), a.get("created", 0)))     # primero las específicas del plan, luego las genéricas
        if len(cand) < ln["qty"]:
            return f"No hay suficiente stock de {ln['name']} ({ln['plan']}). Quedan {len(cand)}."
        for a in cand[: ln["qty"]]:
            used.add(a["id"])
            plan.append(a)
    until = now + RESERVA_MIN * 60
    for a in plan:
        a.update(status="reservada", order=ref, hasta=until)
    return None


def assign(inv, o):
    """Pasa de 'reservada' a 'vendida' las cuentas del pedido pagado; si faltan, toma otras libres. Devuelve líneas pendientes."""
    now = time.time()
    taken = {c for ln in o["items"] for c in ln.get("cuentas", [])}
    managed = managed_pids(inv)
    missing = 0
    for ln in o["items"]:
        if ln["pid"] not in managed:
            ln["manual"] = True
            continue
        have = ln.setdefault("cuentas", [])
        need = ln["qty"] - len(have)
        if need <= 0:
            continue
        cand = [a for a in inv["items"] if a["pid"] == ln["pid"] and a["id"] not in taken and acc_free(a, now, o["ref"]) and acc_fits(a, ln["plan"])]
        cand.sort(key=lambda a: (a.get("order") != o["ref"], not a.get("plan"), a.get("created", 0)))
        for a in cand[:need]:
            a.update(status="vendida", order=o["ref"], sold=int(now))
            a.pop("hasta", None)
            have.append(a["id"])
            taken.add(a["id"])
        missing += ln["qty"] - len(have)
    return missing


def release(inv, ref):
    for a in inv["items"]:
        if a.get("order") == ref and a["status"] == "reservada":
            a.update(status="disponible")
            a.pop("order", None)
            a.pop("hasta", None)


# ─────────────────────────── correo ───────────────────────────
def mail_ready():
    return bool((RESEND_API_KEY and MAIL_FROM) or (SMTP_HOST and SMTP_USER and MAIL_FROM))


def send_mail(to, subject, html, text):
    if RESEND_API_KEY and MAIL_FROM:
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps({"from": MAIL_FROM, "to": [to], "subject": subject, "html": html, "text": text}).encode(),
            headers={"Authorization": "Bearer " + RESEND_API_KEY, "Content-Type": "application/json", "User-Agent": "mj-streaming-tienda"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Resend {e.code}: {e.read()[:200].decode('utf-8', 'ignore')}") from None
        return
    if SMTP_HOST and SMTP_USER and MAIL_FROM:
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = MAIL_FROM, to, subject
        m.set_content(text)
        m.add_alternative(html, subtype="html")
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=25, context=ssl.create_default_context()) as sm:
                sm.login(SMTP_USER, SMTP_PASS)
                sm.send_message(m)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=25) as sm:
                sm.starttls(context=ssl.create_default_context())
                sm.login(SMTP_USER, SMTP_PASS)
                sm.send_message(m)
        return
    raise RuntimeError("Correo no configurado (define RESEND_API_KEY+MAIL_FROM o SMTP_*)")


def build_mail(o, delivered, manual, cfg, prods):
    store, wa = cfg.get("tienda", "MJ Streaming"), cfg.get("whatsapp", "")
    notas = {p["id"]: p.get("nota", "") for p in prods}

    def row(label, value, color="#fff"):
        return (f"<tr><td style='padding:5px 12px 5px 0;color:#98a3c7;font-size:13px;white-space:nowrap;vertical-align:top'>{label}</td>"
                f"<td style='padding:5px 0;font-family:Consolas,monospace;font-size:15px;color:{color};word-break:break-all'>{h(value)}</td></tr>")

    cards, txt = [], [f"¡Gracias por tu compra en {store}! Pedido {o['ref']}", ""]
    for ln, acc in delivered:
        rows = row("Cuenta", acc["email"]) + row("Contraseña", acc["password"], "#8dff2e")
        if acc["perfil"]:
            rows += row("Perfil", acc["perfil"], "#ffe93a")
        if acc["pin"]:
            rows += row("PIN", acc["pin"], "#ffe93a")
        extra = f"<div style='color:#b8c2e6;font-size:13px;margin-top:6px'>{h(acc['extra'])}</div>" if acc["extra"] else ""
        nota = f"<div style='color:#8fa0d6;font-size:12px;margin-top:8px'>{h(notas.get(ln['pid'], ''))}</div>" if notas.get(ln["pid"]) else ""
        cards.append(
            f"<div style='background:#0d1530;border:1px solid #24305f;border-radius:14px;padding:16px;margin:12px 0'>"
            f"<div style='font-weight:800;color:#ffe93a;font-size:16px'>{h(ln['name'])} <span style='color:#98a3c7;font-weight:500;font-size:13px'>· {h(ln['plan'])}</span></div>"
            f"<table style='margin-top:8px;border-collapse:collapse'>{rows}</table>{extra}{nota}</div>"
        )
        txt += [f"{ln['name']} · {ln['plan']}", f"  Cuenta: {acc['email']}", f"  Contraseña: {acc['password']}"]
        if acc["perfil"]:
            txt.append(f"  Perfil: {acc['perfil']}")
        if acc["pin"]:
            txt.append(f"  PIN: {acc['pin']}")
        if acc["extra"]:
            txt.append(f"  {acc['extra']}")
        txt.append("")
    manual_html = ""
    if manual:
        names = ", ".join(f"{m['qty']} x {m['name']} ({m['plan']})" for m in manual)
        manual_html = f"<p style='background:#1a2350;border-radius:12px;padding:12px;color:#d3dbfa;font-size:14px'> Estos artículos te los entregamos por WhatsApp en breve: <b>{h(names)}</b></p>"
        txt += [f"Te entregamos por WhatsApp: {names}", ""]
    wa_link = f"https://wa.me/{wa}" if wa else "#"
    html = (
        "<div style='background:#050914;padding:24px;font-family:Segoe UI,Arial,sans-serif'>"
        "<div style='max-width:520px;margin:auto;background:#0a1124;border:1px solid #1b2757;border-radius:20px;padding:26px;color:#f5f7ff'>"
        f"<div style='font-size:22px;font-weight:900;color:#8dff2e'>{h(store)}</div>"
        f"<h2 style='margin:14px 0 4px;font-size:20px'>¡Pago confirmado!</h2>"
        f"<p style='color:#98a3c7;margin:0 0 6px'>Aquí están tus accesos (pedido <b>{h(o['ref'])}</b>):</p>"
        f"{''.join(cards)}{manual_html}"
        "<p style='color:#98a3c7;font-size:13px;line-height:1.6'>• No cambies la contraseña ni el correo de la cuenta.<br>• Usa solo el perfil y el PIN asignados.<br>• Si algo no funciona, escríbenos y lo solucionamos.</p>"
        f"<p style='text-align:center;margin:20px 0 0'><a href='{h(wa_link)}' style='background:#25d366;color:#04210f;text-decoration:none;font-weight:800;padding:12px 22px;border-radius:999px;display:inline-block'> Soporte por WhatsApp</a></p>"
        "</div></div>"
    )
    txt += ["No cambies la contraseña ni el correo. Soporte: " + wa_link]
    return html, "\n".join(txt)


def alert_admin(subject, body):
    log_event("alerta", subject)
    if ADMIN_EMAIL and mail_ready():
        try:
            send_mail(ADMIN_EMAIL, "[MJ Streaming] " + subject, f"<pre style='font-family:inherit'>{h(body)}</pre>", body)
        except Exception as e:  # noqa: BLE001
            log_event("error", f"No se pudo avisar al admin: {e}")


def _deliver(ref):
    """Envía por correo las cuentas ya asignadas y todavía no enviadas. Reintentable (idempotente por cuenta)."""
    with _lock:
        ords, inv = load_ord(), load_inv()
        o = next((x for x in ords["orders"] if x["ref"] == ref), None)
        if not o or not o.get("paid"):
            return
        sent = set(o.get("enviadas", []))
        byid = {a["id"]: a for a in inv["items"]}
        delivered = [(ln, dec_acc(byid[c])) for ln in o["items"] for c in ln.get("cuentas", []) if c in byid and c not in sent]
        manual = [ln for ln in o["items"] if ln.get("manual") and not o.get("manual_avisado")]
        missing = sum(max(0, ln["qty"] - len(ln.get("cuentas", []))) for ln in o["items"] if not ln.get("manual"))
        ids = [c for ln in o["items"] for c in ln.get("cuentas", []) if c not in sent]
        data = load_data()
    if delivered or manual:
        try:
            html, txt = build_mail(o, delivered, manual, data.get("config", {}), data.get("products", []))
            send_mail(o["email"], f"Tus accesos · pedido {ref}", html, txt)
        except Exception as e:  # noqa: BLE001
            with _lock:
                ords = load_ord()
                o2 = next(x for x in ords["orders"] if x["ref"] == ref)
                o2.update(status="error_correo", error=str(e)[:300], intentos=o2.get("intentos", 0) + 1)
                _write(ORD_FILE, ords)
            alert_admin(f"No se pudo enviar el correo del pedido {ref}", f"Cliente: {o['email']}\nError: {e}\nReenvía desde /admin → Pedidos.")
            return
    with _lock:
        ords = load_ord()
        o2 = next(x for x in ords["orders"] if x["ref"] == ref)
        o2["enviadas"] = sorted(set(o2.get("enviadas", [])) | set(ids))
        if manual:
            o2["manual_avisado"] = True
        o2.pop("error", None)
        if missing:
            o2["status"] = "sin_stock"
        elif any(ln.get("manual") for ln in o2["items"]):
            o2["status"] = "manual"
        else:
            o2["status"] = "entregado"
            o2["delivered"] = int(time.time())
        _write(ORD_FILE, ords)
    if missing and not o.get("alerta_stock"):
        with _lock:
            ords = load_ord()
            next(x for x in ords["orders"] if x["ref"] == ref)["alerta_stock"] = True
            _write(ORD_FILE, ords)
        alert_admin(f"Pedido {ref} pagado SIN STOCK suficiente", f"Cliente: {o['email']}\nFaltan {missing} cuenta(s). Agrega inventario en /admin; el sistema las enviará solo, o usa Reenviar.")
    elif manual:
        alert_admin(f"Pedido {ref} requiere entrega manual", f"Cliente: {o['email']}\nArtículos sin inventario: " + ", ".join(f"{m['qty']}x {m['name']} ({m['plan']})" for m in manual))


_REF_LOCKS, _RL = {}, threading.Lock()


def deliver(ref):
    """Un solo envío a la vez por pedido: dos avisos simultáneos (webhook + consulta + reintento) nunca duplican el correo."""
    with _RL:
        lk = _REF_LOCKS.setdefault(ref, threading.Lock())
    with lk:
        _deliver(ref)


def deliver_async(ref):
    threading.Thread(target=deliver, args=(ref,), daemon=True).start()


def process_paid(ref, payment_id, amount, source):
    """Marca el pedido como pagado (idempotente), asigna cuentas y dispara el envío. Devuelve un texto de resultado."""
    with _lock:
        ords, inv = load_ord(), load_inv()
        o = next((x for x in ords["orders"] if x["ref"] == ref), None)
        if not o:
            return "pedido-desconocido"
        if o.get("paid"):
            return "duplicado"
        if amount is not None and int(amount) < int(o["total"]):
            o.update(status="monto_distinto", pago_recibido=int(amount), payment_id=payment_id)
            _write(ORD_FILE, ords)
            alert_admin(f"Pedido {ref}: monto pagado distinto", f"Esperado {o['total']}, recibido {amount}. No se entregó nada.")
            return "monto-distinto"
        o.update(paid=int(time.time()), payment_id=payment_id, status="pagado", via=source)
        assign(inv, o)
        _write(INV_FILE, inv)
        _write(ORD_FILE, ords)
    deliver_async(ref)
    return "ok"


# ─────────────────────────── Bold ───────────────────────────
def bold_call(method, path, body=None):
    req = urllib.request.Request(
        BOLD_API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "x-api-key " + BOLD_API_KEY, "Content-Type": "application/json", "User-Agent": "mj-streaming-tienda"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "ignore")
        raise RuntimeError(f"Bold respondió {e.code}: {detail}") from None


def base_url():
    if PUBLIC_URL:
        return PUBLIC_URL
    scheme = "https" if (os.environ.get("RENDER") or request.headers.get("X-Forwarded-Proto") == "https") else request.scheme
    return f"{scheme}://{request.host}"


def create_bold_link(o, base):
    desc = " + ".join(f"{ln['qty']}x {ln['name']}" for ln in o["items"])
    desc = ("Compra en MJ Streaming: " + desc)[:100]
    body = {
        "amount_type": "CLOSE",
        "amount": {"currency": "COP", "total_amount": o["total"], "tip_amount": 0},
        "reference": o["ref"],
        "description": desc,
        "payer_email": o["email"],
    }
    if base.startswith("https://"):        # Bold solo acepta callback_url con https
        body["callback_url"] = f"{base}/?pedido={o['ref']}"
    if BOLD_METHODS:
        body["payment_methods"] = BOLD_METHODS
    if BOLD_EXPIRE_MIN:
        ms = int((time.time() + BOLD_EXPIRE_MIN * 60) * 1000)
        body["expiration_date"] = ms * 1_000_000 if BOLD_EXPIRE_UNIT == "ns" else ms
    res = bold_call("POST", "/online/link/v1", body)
    pay = res.get("payload") or {}
    if res.get("errors") or not pay.get("url"):
        raise RuntimeError(f"Bold no devolvió el link: {json.dumps(res.get('errors'))[:200]}")
    return pay["payment_link"], pay["url"]


def verify_sig(raw, sig):
    # Bold firma con HMAC-SHA256 (hex) sobre el cuerpo crudo codificado en Base64. En pruebas la llave es vacía.
    if not BOLD_SECRET_KEY and not BOLD_TEST:
        return False
    key = BOLD_SECRET_KEY.encode() if not BOLD_TEST or BOLD_SECRET_KEY else b""
    mine = hmac.new(key, base64.b64encode(raw), hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(mine.encode(), (sig or "").strip().lower().encode())
    except Exception:  # noqa: BLE001
        return False


def reconcile(o):
    """Si el webhook se demora (Bold avisa hasta 10 min después), consulta el link en Bold y procesa si ya está PAID."""
    if o.get("paid") or o["status"] != "pendiente" or not o.get("link") or not BOLD_API_KEY:
        return
    if time.time() - o.get("revisado", 0) < 8:
        return
    with _lock:
        ords = load_ord()
        o2 = next(x for x in ords["orders"] if x["ref"] == o["ref"])
        o2["revisado"] = time.time()
        _write(ORD_FILE, ords)
    try:
        res = bold_call("GET", "/online/link/v1/" + urllib.parse.quote(o["link"]))
        info = res.get("payload") if isinstance(res.get("payload"), dict) else res
        if str(info.get("status", "")).upper() == "PAID":
            log_event("consulta", f"Pedido {o['ref']} figura PAID en Bold")
            process_paid(o["ref"], info.get("transaction_id") or o["link"], info.get("total"), "consulta")
    except Exception as e:  # noqa: BLE001
        log_event("error", f"Consulta Bold falló: {e}")


@app.post("/api/checkout")
def checkout():
    if not BOLD_API_KEY:
        return jsonify(error="El pago en línea aún no está configurado. Escríbenos por WhatsApp."), 503
    ip, now = client_ip(), time.time()
    CHK[ip] = [t for t in CHK.get(ip, []) if now - t < 600]
    if len(CHK[ip]) >= 12:
        return jsonify(error="Demasiados intentos. Espera unos minutos."), 429
    CHK[ip].append(now)
    b = request.get_json(silent=True) or {}
    email = s(b.get("email"), 200).lower()
    if not EMAIL_RE.match(email):
        return jsonify(error="Escribe un correo válido para enviarte tu compra."), 400
    d = load_data()
    try:
        pc = price_cart(d.get("config", {}), d.get("products", []), lst(b.get("items")), b.get("coupon"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if pc["total"] < 1000:
        return jsonify(error="El total es demasiado bajo para cobrar en línea."), 400
    ref = "MJ" + format(int(now), "x").upper() + secrets.token_hex(4).upper()
    o = {
        "ref": ref, "status": "pendiente", "email": email, "nombre": s(b.get("nombre"), 80), "notas": s(b.get("notas"), 300),
        "items": pc["lines"], "subtotal": pc["subtotal"], "d_tier": pc["d_tier"], "d_cup": pc["d_cup"], "cupon": pc["coupon"],
        "total": pc["total"], "created": int(now),
    }
    for ln in o["items"]:
        ln.pop("stock", None)
    with _lock:
        inv = load_inv()
        err = reserve(inv, o["items"], ref)
        if err:
            return jsonify(error=err), 409
        _write(INV_FILE, inv)
        ords = load_ord()
        for old in ords["orders"]:
            if old["status"] == "pendiente" and now - old["created"] > 2 * 86400:
                old["status"] = "expirado"
        ords["orders"].append(o)
        _write(ORD_FILE, ords)
    try:
        link, url = create_bold_link(o, base_url())
    except Exception as e:  # noqa: BLE001
        with _lock:
            inv, ords = load_inv(), load_ord()
            release(inv, ref)
            next(x for x in ords["orders"] if x["ref"] == ref).update(status="error_bold", error=str(e)[:300])
            _write(INV_FILE, inv)
            _write(ORD_FILE, ords)
        log_event("error", f"Crear link falló: {e}")
        return jsonify(error="No pudimos generar el link de pago. Inténtalo de nuevo o escríbenos por WhatsApp."), 502
    with _lock:
        ords = load_ord()
        next(x for x in ords["orders"] if x["ref"] == ref).update(link=link, url=url)
        _write(ORD_FILE, ords)
    return jsonify(url=url, ref=ref, total=o["total"])


CHK = {}


def mask(e):
    u, _, dom = e.partition("@")
    return (u[:2] + "***@" + dom) if u else e


@app.get("/api/pedido/<ref>")
def api_pedido(ref):
    if not re.match(r"^[A-Za-z0-9]{6,40}$", ref):
        abort(404)
    o = next((x for x in load_ord()["orders"] if x["ref"] == ref), None)
    if not o:
        abort(404)
    reconcile(o)
    o = next(x for x in load_ord()["orders"] if x["ref"] == ref)
    st = o["status"]
    if o.get("paid"):
        simple = "entregado" if st in ("entregado", "manual") else "pagado"
    elif st == "pendiente":
        simple = "rechazado" if o.get("rechazo") else "pendiente"
    elif st == "monto_distinto":
        simple = "revision"
    else:
        simple = "fallido"
    return jsonify(estado=simple, email=mask(o["email"]), total=o["total"], url=o.get("url", "") if simple in ("pendiente", "rechazado") else "")


@app.post("/api/bold/webhook")
def bold_webhook():
    raw = request.get_data()
    if not verify_sig(raw, request.headers.get("x-bold-signature", "")):
        log_event("webhook", "Firma inválida o BOLD_SECRET_KEY sin configurar", ok=False)
        return jsonify(error="firma"), 401
    try:
        ev = json.loads(raw)
    except ValueError:
        return jsonify(error="json"), 400
    typ, data = str(ev.get("type", "")), ev.get("data") if isinstance(ev.get("data"), dict) else {}
    ref = s((data.get("metadata") or {}).get("reference"), 80)
    pid = s(data.get("payment_id") or ev.get("subject"), 80)
    if not ref:      # respaldo: buscar el pedido por el id del link
        with _lock:
            o = next((x for x in load_ord()["orders"] if pid and x.get("link") == pid), None)
        ref = o["ref"] if o else ""
    res = "ignorado"
    if typ == "SALE_APPROVED" and ref:
        res = process_paid(ref, pid, (data.get("amount") or {}).get("total"), "webhook")
    elif typ == "SALE_REJECTED" and ref:
        with _lock:
            ords = load_ord()
            o = next((x for x in ords["orders"] if x["ref"] == ref), None)
            if o and not o.get("paid"):
                o["rechazo"] = int(time.time())
                _write(ORD_FILE, ords)
        res = "rechazo-registrado"
    elif typ == "VOID_APPROVED" and ref:
        with _lock:
            ords = load_ord()
            o = next((x for x in ords["orders"] if x["ref"] == ref), None)
            if o:
                o["anulado"] = int(time.time())
                _write(ORD_FILE, ords)
        alert_admin(f"Bold anuló el pago del pedido {ref}", "Revisa si debes recuperar las cuentas entregadas.")
        res = "anulacion-registrada"
    log_event("webhook", f"{typ} ref={ref or '?'} → {res}", ok=True)
    return jsonify(ok=True, result=res), 200


def repair_pass():
    """Repara lo que un reinicio o una caída pudo dejar a medias. Devuelve la lista de arreglos."""
    fixes, now, todo = [], time.time(), []
    with _lock:
        ords, inv = load_ord(), load_inv()
        for o in ords["orders"]:
            if o["status"] == "pendiente" and not o.get("link") and now - o["created"] > 300:
                o.update(status="error_bold", error="Se interrumpió antes de crear el link de pago")
                release(inv, o["ref"])
                fixes.append(f"Pedido {o['ref']}: sin link de pago, se liberaron sus cuentas")
            if o.get("paid") and o["status"] in ("error_correo", "sin_stock", "pagado") and o.get("intentos", 0) < 8 and (o["status"] != "pagado" or now - o["paid"] > 60):
                assign(inv, o)
                todo.append(o["ref"])
        for a in inv["items"]:
            if a["status"] == "reservada" and a.get("hasta", 0) < now:
                a.update(status="disponible")
                a.pop("order", None)
                a.pop("hasta", None)
        _write(INV_FILE, inv)
        _write(ORD_FILE, ords)
    for ref in todo:
        deliver(ref)
        fixes.append(f"Pedido {ref}: se reintentó la entrega")
    return fixes


def sweeper():
    """Al arrancar y cada 2 min: reintenta entregas pendientes y limpia reservas vencidas."""
    time.sleep(20)
    while True:
        try:
            repair_pass()
        except Exception as e:  # noqa: BLE001
            log_event("error", f"sweeper: {e}")
        time.sleep(120)


if not os.environ.get("MJ_NO_THREADS"):
    threading.Thread(target=sweeper, daemon=True).start()


# ─────────────────────────── admin: inventario y pedidos ───────────────────────────
def inv_view(a):
    return {"id": a["id"], "pid": a["pid"], "plan": a.get("plan", ""), **dec_acc(a), "status": a["status"],
            "order": a.get("order", ""), "created": a.get("created", 0), "sold": a.get("sold", 0)}


@app.get("/api/admin/inventario")
@admin_required
def admin_inv_get():
    inv, now = load_inv(), time.time()
    for a in inv["items"]:
        if a["status"] == "reservada" and a.get("hasta", 0) < now:
            a["status"] = "disponible"
    return jsonify(items=[inv_view(a) for a in inv["items"]], key_explicit=KEY_EXPLICIT)


def parse_lines(text):
    """Pegado masivo: columnas separadas por TAB (copiadas de Excel) o por | → cuenta, contraseña, perfil, pin."""
    out = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t") if "\t" in line else line.split("|") if "|" in line else line.split(":", 1)
        parts = [x.strip() for x in parts]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise ValueError(f"Línea inválida (cuenta y contraseña son obligatorias): {line[:60]}")
        parts += [""] * (4 - len(parts))
        out.append({"email": parts[0], "password": parts[1], "perfil": parts[2], "pin": parts[3]})
    return out


@app.post("/api/admin/inventario")
@admin_required
def admin_inv_add():
    b = request.get_json(silent=True) or {}
    pid = s(b.get("pid"), 60)
    if not any(p["id"] == pid for p in load_data().get("products", [])):
        return jsonify(error="Elige un producto de la lista"), 400
    try:
        rows = parse_lines(b.get("lines")) if b.get("lines") else [r for r in lst(b.get("rows")) if isinstance(r, dict)]
    except ValueError as e:
        return jsonify(error=str(e)), 400
    clean_rows = []
    for i, r in enumerate(rows, 1):
        email, pw = s(r.get("email"), 200), s(r.get("password"), 200)
        if not email and not pw and not s(r.get("perfil")) and not s(r.get("pin")):
            continue            # fila vacía
        if not email or not pw:
            return jsonify(error=f"Fila {i}: la cuenta y la contraseña son obligatorias"), 400
        clean_rows.append({"email": email, "password": pw, "perfil": s(r.get("perfil"), 60), "pin": s(r.get("pin"), 30)})
    if not clean_rows or len(clean_rows) > 500:
        return jsonify(error="Agrega entre 1 y 500 cuentas"), 400
    plan = s(b.get("plan"), 40)
    with _lock:
        inv = load_inv()
        have = {(a["pid"], dec_acc(a)["email"].lower(), dec_acc(a)["perfil"].lower()) for a in inv["items"] if a["pid"] == pid}
        added = skipped = 0
        for r in clean_rows:
            k = (pid, r["email"].lower(), r["perfil"].lower())
            if k in have:
                skipped += 1
                continue
            have.add(k)
            inv["items"].append({"id": secrets.token_hex(6), "pid": pid, "plan": plan, "enc": enc_acc(r["email"], r["password"], r["perfil"], r["pin"]),
                                 "status": "disponible", "created": int(time.time())})
            added += 1
        _write(INV_FILE, inv)
    return jsonify(ok=True, added=added, skipped=skipped)


@app.put("/api/admin/inventario/<aid>")
@admin_required
def admin_inv_put(aid):
    b = request.get_json(silent=True) or {}
    with _lock:
        inv = load_inv()
        a = next((x for x in inv["items"] if x["id"] == aid), None)
        if not a:
            return jsonify(error="No encontrada"), 404
        cur = dec_acc(a)
        a["enc"] = enc_acc(s(b.get("email", cur["email"]), 200), s(b.get("password", cur["password"]), 200),
                           s(b.get("perfil", cur["perfil"]), 60), s(b.get("pin", cur["pin"]), 30), s(b.get("extra", cur["extra"]), 300))
        if "plan" in b:
            a["plan"] = s(b["plan"], 40)
        if b.get("status") in ("disponible", "vendida", "bloqueada"):
            a["status"] = b["status"]
            if b["status"] != "vendida":
                a.pop("order", None)
                a.pop("hasta", None)
        _write(INV_FILE, inv)
    return jsonify(ok=True)


@app.delete("/api/admin/inventario/<aid>")
@admin_required
def admin_inv_del(aid):
    with _lock:
        inv = load_inv()
        inv["items"] = [x for x in inv["items"] if x["id"] != aid]
        _write(INV_FILE, inv)
    return jsonify(ok=True)


@app.get("/api/admin/pedidos")
@admin_required
def admin_ord_get():
    items = sorted(load_ord()["orders"], key=lambda o: o["created"], reverse=True)[:300]
    return jsonify(items=[{k: v for k, v in o.items() if k not in ("enviadas",)} for o in items])


def _order_or_404(ref):
    ords = load_ord()
    o = next((x for x in ords["orders"] if x["ref"] == ref), None)
    return ords, o


@app.post("/api/admin/pedidos/<ref>/reenviar")
@admin_required
def admin_ord_resend(ref):
    with _lock:
        ords, o = _order_or_404(ref)
        if not o or not o.get("paid"):
            return jsonify(error="El pedido no está pagado"), 400
        inv = load_inv()
        assign(inv, o)
        if (b := request.get_json(silent=True) or {}).get("todo"):
            o["enviadas"] = []              # reenvía también lo ya enviado
            o.pop("manual_avisado", None)
        _write(INV_FILE, inv)
        _write(ORD_FILE, ords)
    deliver(ref)
    o = _order_or_404(ref)[1]
    return jsonify(ok=o["status"] in ("entregado", "manual"), status=o["status"], error=o.get("error", ""))


@app.post("/api/admin/pedidos/<ref>/entregar")
@admin_required
def admin_ord_force(ref):
    """Para pruebas o si el webhook nunca llegó: confirma el pago a mano y entrega."""
    with _lock:
        o = _order_or_404(ref)[1]
        if not o:
            return jsonify(error="No existe"), 404
    r = process_paid(ref, "MANUAL-" + secrets.token_hex(3).upper(), None, "manual-admin")
    return jsonify(ok=r in ("ok", "duplicado"), result=r)


@app.post("/api/admin/pedidos/<ref>/cancelar")
@admin_required
def admin_ord_cancel(ref):
    with _lock:
        ords, o = _order_or_404(ref)
        if not o:
            return jsonify(error="No existe"), 404
        if o.get("paid"):
            return jsonify(error="El pedido ya está pagado"), 400
        inv = load_inv()
        release(inv, ref)
        o["status"] = "cancelado"
        _write(INV_FILE, inv)
        _write(ORD_FILE, ords)
    return jsonify(ok=True)


@app.post("/api/admin/pedidos/<ref>/completar")
@admin_required
def admin_ord_done(ref):
    with _lock:
        ords, o = _order_or_404(ref)
        if not o or not o.get("paid"):
            return jsonify(error="El pedido no está pagado"), 400
        o["status"] = "entregado"
        o["delivered"] = int(time.time())
        _write(ORD_FILE, ords)
    return jsonify(ok=True)


@app.post("/api/admin/auditoria")
@admin_required
def admin_audit():
    """Revisa la consistencia de pedidos e inventario, repara lo reparable y reporta lo que necesita tu atención."""
    fixes = repair_pass()
    issues = []
    ords, inv = load_ord(), load_inv()
    byacc = {a["id"]: a for a in inv["items"]}
    seen = {}
    for o in ords["orders"]:
        for ln in o["items"]:
            for c in ln.get("cuentas", []):
                if c not in byacc:
                    issues.append(f"Pedido {o['ref']}: una cuenta entregada ya no existe en el inventario")
                elif c in seen and seen[c] != o["ref"]:
                    issues.append(f"La misma cuenta está asignada a los pedidos {seen[c]} y {o['ref']}")
                seen[c] = o["ref"]
        if o.get("paid") and o["status"] in ("sin_stock", "error_correo"):
            issues.append(f"Pedido {o['ref']} ({o['email']}): pagado y sin entregar — {o['status'].replace('_', ' ')}")
        if o["status"] == "monto_distinto":
            issues.append(f"Pedido {o['ref']}: el monto pagado no coincide, revísalo en Bold")
    for a in inv["items"]:
        if a["status"] == "vendida" and a.get("order") and not any(o["ref"] == a["order"] for o in ords["orders"]) and not a["order"].startswith("MJ-MANUAL"):
            issues.append("Hay una cuenta marcada como vendida sin pedido asociado")
            break
    return jsonify(ok=not issues, fixes=fixes, issues=issues, revisados=len(ords["orders"]))


@app.post("/api/admin/correo-prueba")
@admin_required
def admin_mail_test():
    to = s((request.get_json(silent=True) or {}).get("to"), 200) or ADMIN_EMAIL
    if not EMAIL_RE.match(to):
        return jsonify(error="Escribe un correo de destino válido"), 400
    try:
        send_mail(to, "Prueba de correo · MJ Streaming", "<p>✅ El envío de correo funciona.</p>", "El envío de correo funciona.")
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)), 502
    return jsonify(ok=True)


@app.after_request
def headers(r):
    r.headers.setdefault("X-Content-Type-Options", "nosniff")
    r.headers.setdefault("Referrer-Policy", "same-origin")
    return r


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
