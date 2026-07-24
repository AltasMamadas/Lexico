"""
Rate limiter em memória com janela deslizante.
Suficiente para um único processo; se escalar para múltiplos workers,
migrar para Redis.
"""
import time
import threading

_lock = threading.Lock()
_buckets = {}  # chave -> [timestamps]

# Limites por categoria
LIMITS = {
    "login":       (5,  60),    # 5 tentativas por minuto
    # 6 por 10 min: amigos jogando na mesma casa saem pelo mesmo IP (NAT), e o
    # limite anterior (3 por 5 min) barrava um grupo criando contas junto.
    "criar_conta": (6,  600),
    "geral":       (60, 60),    # 60 req/min genérico
}


def _limpar_expirados():
    """Remove buckets que não têm timestamps recentes (chamado periodicamente)."""
    agora = time.time()
    expirados = [k for k, ts in _buckets.items() if ts and agora - ts[-1] > 600]
    for k in expirados:
        del _buckets[k]


def checar(chave, categoria="geral"):
    """Retorna True se a request é permitida, False se excedeu o limite."""
    max_req, janela = LIMITS.get(categoria, LIMITS["geral"])
    agora = time.time()
    with _lock:
        if len(_buckets) > 10000:
            _limpar_expirados()
        bucket = _buckets.setdefault(chave, [])
        # remove timestamps fora da janela
        corte = agora - janela
        while bucket and bucket[0] < corte:
            bucket.pop(0)
        if len(bucket) >= max_req:
            return False
        bucket.append(agora)
        return True


def tentativas_restantes(chave, categoria="geral"):
    max_req, janela = LIMITS.get(categoria, LIMITS["geral"])
    agora = time.time()
    with _lock:
        bucket = _buckets.get(chave, [])
        recentes = sum(1 for t in bucket if agora - t < janela)
        return max(0, max_req - recentes)
