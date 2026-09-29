"""
Configuración de gunicorn para la API.

Gunicorn lee este archivo solo desde el directorio de trabajo (/app en la
imagen). Los argumentos del comando de la task definition —``--workers 2
--timeout 300``— siguen mandando sobre lo que declaren; esto agrega lo que el
comando no dice.

El 2026-09-29 la alarma ``ecofilia-unhealthy-hosts`` saltó por esto: cada
worker ``sync`` atiende un pedido por vez, y dos pedidos pesados (el listado
completo de «mis documentos», 1,7 MB, pedido de nuevo después de cada borrado
o edición) ocupaban los dos workers de una tarea con la CPU al 100%. El health
check del balanceador —un ``SELECT 1``— esperaba detrás de ellos, vencía a
los 5 segundos dos veces seguidas, y ECS mataba la tarea con esos pedidos
adentro.
"""

# Hilos por worker. Con más de uno, gunicorn pasa solo de ``sync`` a
# ``gthread``: un pedido pesado ya no bloquea al health check ni a los pedidos
# livianos que llegan detrás. Son tres y no más porque los hilos comparten la
# memoria del proceso: varios listados grandes a la vez en el mismo worker
# suman su pico.
threads = 3

# Reciclar cada worker después de una cantidad de pedidos. Python no le
# devuelve al sistema la memoria de un pico: sin esto, un solo listado grande
# deja la tarea al 87% para siempre, y el autoscaling por memoria agrega tareas
# que no bajan la memoria de las que ya existen. El jitter evita que los dos
# workers se reinicien juntos.
max_requests = 500
max_requests_jitter = 100
