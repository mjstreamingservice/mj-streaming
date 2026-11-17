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


---

# 💳 Pagos automáticos con Bold + entrega por correo

**Flujo:** el cliente agrega al carrito → *Pagar ahora* → escribe su correo → `/api/checkout` reserva las cuentas y crea el link de Bold → el cliente paga en Bold → Bold avisa a `/api/bold/webhook` (evento `SALE_APPROVED`, firmado) → el sistema toma las cuentas, las marca vendidas y las envía al correo. Si el aviso se demora, la página de retorno (`/?pedido=...`) consulta el link en Bold y entrega igual.

## Variables de entorno en Render
| Variable | Para qué |
|---|---|
| `BOLD_API_KEY` | Llave de identidad de Bold (crea los links). |
| `BOLD_SECRET_KEY` | Llave secreta de Bold (verifica la firma del webhook). Sin ella el webhook rechaza todo. |
| `BOLD_TEST=1` | Solo en pruebas: la firma usa llave vacía. **Quítala en producción.** |
| `PUBLIC_URL` | `https://tudominio.com` (se usa en el `callback_url`). |
| `INVENTORY_KEY` | Frase larga y secreta que cifra las cuentas. **Guárdala aparte: sin ella no se pueden descifrar.** |
| `RESEND_API_KEY` + `MAIL_FROM` | Correo vía Resend (recomendado; dominio verificado, ej. `MJ Streaming <ventas@tudominio.com>`). |
| `SMTP_HOST/PORT/USER/PASS` + `MAIL_FROM` | Alternativa SMTP (Render gratis suele bloquear SMTP saliente; usa Resend). |
| `ADMIN_EMAIL` | (opcional) recibe avisos: sin stock, correo fallido, monto distinto. |
| `RESERVA_MIN` | (opcional, 30) minutos que se reservan las cuentas mientras se paga. |
| `BOLD_METHODS` | (opcional) `PSE,NEQUI,CREDIT_CARD,BOTON_BANCOLOMBIA` para limitar medios. |
| `BOLD_EXPIRE_MIN` | (opcional) vencimiento del link en minutos. La doc de Bold dice nanosegundos pero su ejemplo parece milisegundos: si lo usas, prueba y ajusta `BOLD_EXPIRE_UNIT` (`ns` o `ms`). |

## En el panel de Bold
Panel de comercios → Integraciones → **Webhooks → Configurar webhook** → pega `https://tudominio.com/api/bold/webhook` (la URL exacta aparece en `/admin` → *Pagos Bold*) y activa *Venta aprobada*.

## Inventario
`/admin` → **Inventario**: pega cuentas (`correo|clave|nota`), una por línea, por producto (y plan opcional). Los productos con inventario muestran el stock real en la tienda y se despachan solos; los demás se marcan "entrega manual" (el cliente recibe un correo de confirmación y tú un aviso). En **Pedidos** puedes reenviar, confirmar un pago a mano o cancelar.

## Importante
- Las pruebas de Bold (sandbox) **no envían webhook**: para probar el flujo completo usa *Confirmar pago manual* en Pedidos o haz una compra real pequeña.
- Render gratis duerme el servicio: el primer aviso puede tardar; Bold reintenta (15 min, 1 h, 4 h, 8 h, 24 h) y el sistema es idempotente.
- Disco temporal: sin un disco persistente, inventario y pedidos solo sobreviven si defines `INVENTORY_KEY` y el respaldo en GitHub (`GITHUB_TOKEN`/`GITHUB_REPO`); se guardan cifrados. Lo más seguro es un *Persistent Disk* de Render con `DATA_DIR` apuntando a él.

---

# 🎨 Temas, inventario por campos y arte

- **/admin → Temas:** Navidad (clásica, nórdica, dorada), Brujitas y Halloween (2), Amor y Amistad (2), Año Nuevo, Black Friday, Día de la Madre y del Padre. Cada una trae colores, efectos animados, luces/adornos y mensaje propio. Se pueden programar por fechas.
- **/admin → Inventario:** cada cuenta tiene Cuenta, Contraseña, Perfil y PIN en casillas separadas (o pegando columnas desde Excel). Las repetidas se omiten solas.
- **/admin → Pedidos → Auditar y reparar:** revisa pedidos e inventario y arregla lo que un reinicio haya dejado a medias.
- **gunicorn.conf.py:** fija 1 proceso con 8 hilos (el inventario se reserva en memoria). Si en Render tienes `-w 2` o más en el Start Command, quítalo.
- El arte de cada plataforma se genera dentro de `index.html`; si subes una imagen propia en el panel, esa tiene prioridad.
