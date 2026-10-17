import zlib
import psycopg2

PUERTOS = [7001, 7002, 7003]   # puerto de shard0, shard1, shard2
CLAVE = "andes12345"
BASE = "andes"

def shard(user_id, n=3):
    # Regla de reparto: el mismo cliente siempre cae en la misma base
    return zlib.crc32(user_id.encode()) % n

def conectar(i):
    return psycopg2.connect(host="localhost", port=PUERTOS[i],
                            user="admin", password=CLAVE, dbname=BASE)

# 1. Crear la tabla en cada base
for i in range(3):
    with conectar(i) as c, c.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS clientes (user_id text PRIMARY KEY)")

# 2. Insertar 100 clientes, cada uno en la base que le toca
for k in range(100):
    uid = f"user_{k:03d}"
    with conectar(shard(uid)) as c, c.cursor() as cur:
        cur.execute("INSERT INTO clientes VALUES (%s) ON CONFLICT DO NOTHING", (uid,))

# 3. Contar clientes por base
for i in range(3):
    with conectar(i) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM clientes")
        print(f"shard{i}: {cur.fetchone()[0]} clientes")

# 4. Buscar un cliente consultando UNA sola base
uid = "user_042"
i = shard(uid)
with conectar(i) as c, c.cursor() as cur:
    cur.execute("SELECT * FROM clientes WHERE user_id = %s", (uid,))
    print(f"{uid} esta en shard{i}: {cur.fetchone()}")
