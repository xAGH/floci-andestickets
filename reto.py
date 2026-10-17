import zlib

def shard(user_id, n):
    return zlib.crc32(user_id.encode()) % n

clientes = [f"user_{k:03d}" for k in range(100)]
movidos = [u for u in clientes if shard(u, 3) != shard(u, 4)]
print("Se mueven de base:", len(movidos), "de", len(clientes))
for n in (3, 4):
    print(f"Con {n} bases:", [sum(1 for u in clientes if shard(u, n) == s) for s in range(n)])
