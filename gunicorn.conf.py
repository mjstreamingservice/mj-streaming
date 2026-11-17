# Un solo proceso con varios hilos: los candados de inventario y pedidos viven en memoria,
# con varios procesos dos clientes podrían reservar la misma cuenta.
workers = 1
threads = 8
timeout = 60
