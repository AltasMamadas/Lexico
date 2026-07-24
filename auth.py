"""
Autenticação leve: usuário + PIN numérico de 4 dígitos.
Token de sessão é assinado (itsdangerous, já vem com o Flask) e sem
expiração — não há sessão no banco, o token carrega a identidade.
"""
import os
import bcrypt
from itsdangerous import URLSafeSerializer, BadSignature, BadData

_SECRET = os.environ.get("SECRET_KEY")
if not _SECRET:
    raise RuntimeError("defina a env var SECRET_KEY")

_serializer = URLSafeSerializer(_SECRET, salt="boggle-perfil")


# O padrão do bcrypt (12) custa ~9,5s no 0.1 CPU do free tier do Render —
# medido: login levava 9,9s, dos quais 9,7s eram só o hash.
#
# Sobre a troca: o PIN tem 4 dígitos, ou seja 10.000 combinações. Quem obtiver
# o banco quebra qualquer PIN por força bruta independente do custo do hash —
# o elo fraco é o tamanho do PIN, não o número de rounds. A defesa real contra
# ataque online é o rate limit de login (5 tentativas/min por IP). Por isso
# vale trocar 7 segundos de espera por uma diferença de segurança marginal.
_ROUNDS = 10


def hash_pin(pin):
    return bcrypt.hashpw(pin.encode(), bcrypt.gensalt(rounds=_ROUNDS)).decode()


def checar_pin(pin, pin_hash):
    return bcrypt.checkpw(pin.encode(), pin_hash.encode())


def precisa_rehash(pin_hash):
    """
    True se o hash foi gerado com custo maior que o atual. O bcrypt guarda o
    custo dentro do próprio hash, então contas antigas continuariam pagando os
    9,5s para sempre; o login regrava o hash depois de autenticar.
    """
    try:
        return int(pin_hash.split("$")[2]) > _ROUNDS
    except (IndexError, ValueError):
        return False


def gerar_token(profile_id, username):
    return _serializer.dumps({"pid": str(profile_id), "u": username})


def verificar_token(token):
    """Retorna (profile_id, username) ou (None, None) se o token for inválido."""
    try:
        data = _serializer.loads(token)
    except (BadSignature, BadData):
        return None, None
    return data.get("pid"), data.get("u")
