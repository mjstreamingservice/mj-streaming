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
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file, session

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

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(("mj-secret:" + ADMIN_PASSWORD).encode()).hexdigest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER") or os.environ.get("FORCE_HTTPS")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
)
_lock = threading.RLock()


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
        "redes": {k: clean_url(redes.get(k)) if str(redes.get(k, "")).startswith("http") else "" for k in ("instagram", "tiktok", "facebook")},
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


def gh_restore():
    """Al arrancar sin datos locales (disco temporal), recupera catálogo e imágenes desde la rama de respaldo."""
    try:
        tree = gh("GET", f"/repos/{GH_REPO}/git/trees/{GH_BRANCH}?recursive=1")["tree"]
        for node in tree:
            if node["type"] != "blob" or not node["path"].startswith(GH_DIR + "/"):
                continue
            rel = node["path"][len(GH_DIR) + 1:]
            if rel != "catalogo.json" and not re.match(r"^img/[A-Za-z0-9_.-]+$", rel):
                continue
            blob = gh("GET", f"/repos/{GH_REPO}/git/blobs/{node['sha']}")
            dest = DATA_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(base64.b64decode(blob["content"]))
        SYNC.update(ok=True, msg="Datos restaurados desde GitHub", t=time.time())
    except Exception as e:  # noqa: BLE001
        SYNC.update(ok=False, msg=f"No se pudo restaurar desde GitHub: {e}", t=time.time())


if GH_ON and not CATALOG.exists():
    gh_restore()


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
def public_payload():
    d = with_images(load_data(), public=True)
    raw = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
    return raw, '"' + hashlib.sha1(raw.encode()).hexdigest()[:20] + '"'


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
    return jsonify(sync=SYNC, github=GH_ON)


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


@app.after_request
def headers(r):
    r.headers.setdefault("X-Content-Type-Options", "nosniff")
    r.headers.setdefault("Referrer-Policy", "same-origin")
    return r


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
