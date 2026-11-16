# MJ Streaming · tienda + panel de administración

Tienda de streaming (HTML) servida por Python (Flask) con un panel `/admin` para editar
productos, precios, stock, imágenes, WhatsApp, descuentos, preguntas y términos **sin tocar código**.
Los cambios se publican al instante y la tienda abierta por los clientes se actualiza sola (cada 20 s).

## Estructura
```
app.py                    servidor (Flask)
public/index.html         la tienda
public/admin.html         el panel
catalogo.default.json     catálogo inicial (de prueba)
static/img/               imágenes por plataforma (opcional, ver LEEME.txt)
requirements.txt
```

## Probar en tu PC
```
pip install -r requirements.txt
set ADMIN_PASSWORD=tu_clave_larga        (Windows)   |   export ADMIN_PASSWORD=tu_clave_larga   (Linux/Mac)
python app.py
```
Tienda: http://localhost:5000 · Panel: http://localhost:5000/admin

## Publicar en Render (con tu dominio)
1. Sube esta carpeta a un repositorio de GitHub.
2. Render → New → Web Service → elige el repo.
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT`
3. Environment: `ADMIN_PASSWORD` = una contraseña larga.
4. Settings → Custom Domains: agrega tu dominio y sigue las instrucciones de DNS.

## Que no se pierdan tus ediciones (importante en Render)
El disco de Render gratis es temporal: se borra al reiniciar. Elige una opción:

**A. Disco persistente de Render** (plan de pago): agrega un Disk montado en `/var/data` y la variable `DATA_DIR=/var/data`.

**B. Respaldo automático en GitHub** (gratis):
1. GitHub → Settings → Developer settings → Fine-grained tokens → token con acceso a TU repositorio y permiso *Contents: Read and write*.
2. En Render agrega: `GITHUB_TOKEN` = el token, `GITHUB_REPO` = `usuario/repositorio`.
3. Cada vez que publicas, el catálogo y las imágenes se guardan en la rama `data` (se crea sola). Render solo
   despliega la rama principal, así que guardar no reinicia la tienda. Al reiniciar, el servidor recupera todo desde esa rama.
   El panel muestra el estado del respaldo arriba (☁).

Además, en el panel → Respaldo puedes descargar `catalogo.json` y ver/restaurar las últimas 40 versiones.

## Imágenes de cada plataforma
- Desde el panel: Productos → ✏️ Editar → 📤 Subir imagen (se redimensiona sola).
- O copia archivos a `static/img/` con el id del producto (`netflix.png`, `max.webp`…). Ver `static/img/LEEME.txt`.

## Notas
- Usa `--workers 1` (el catálogo se guarda en un archivo).
- La sesión del panel dura 14 días; tras 8 intentos fallidos de contraseña se bloquea 10 minutos.
- Si abres `public/index.html` directamente (sin servidor) funciona con los datos de prueba incluidos, pero sin panel.
