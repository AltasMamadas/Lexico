"""
Servidor Boggle multiplayer v3.
Multi-sala com nome e senha opcional. Série de vitórias (1/3/5/7).
Modo cooperativo (caça com palavras partilhadas) e competitivo.
"""
import os, time, random, threading, secrets, logging
from dotenv import load_dotenv
load_dotenv()  # carrega .env local antes de importar módulos que leem env vars

# O psycopg_pool reporta falha de conexão no logger dele e segue tentando em
# background; sem configurar logging o gunicorn descarta essas mensagens e a
# única coisa visível vira o PoolTimeout, que não diz a causa.
logging.basicConfig(level=logging.INFO)
logging.getLogger("psycopg.pool").setLevel(logging.DEBUG)
from flask import Flask, request, jsonify, send_from_directory
import game_core as gc
import auth, db
import achievements as ach
import rate_limit as rl
import sanitize as san
import logger as log

# ids de avatares válidos (a arte vive no frontend; aqui só validamos o id)
AVATARES_VALIDOS = {f"a{i}" for i in range(1, 13)}

app = Flask(__name__, static_folder="static")
LOCK = threading.Lock()


@app.errorhandler(Exception)
def _erro_json(e):
    """
    Rotas /api/ devem sempre responder JSON. O handler padrão do Flask devolve
    HTML, que estoura no res.json() do cliente e deixa a tela presa em
    "Carregando..." — o erro fica invisível em vez de aparecer.
    """
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "erro": e.description}), e.code
        return e
    log.error("erro_nao_tratado", rota=request.path, tipo=type(e).__name__, msg=str(e)[:200])
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "erro": "erro interno no servidor"}), 500
    raise e

# -------- pool de salas --------
# sala_id -> {"id", "nome", "senha_hash", "estado", "criada_em", "vazia_desde"}
salas = {}

SOBREV_TEMPO_INICIAL = 90
SOBREV_FASES = [(0, 4, 4), (30, 4, 6), (60, 6, 6)]
SOBREV_EMBARALHA_APOS = 90
SOBREV_EMBARALHA_INTERVALO = 20
SOBREV_AVISO = 5

# tempo (s) sem sinal do jogador antes de removê-lo da sala
TIMEOUT_LOBBY = 20
TIMEOUT_JOGO = 40
# grace period (s) que uma sala fica vazia antes de ser deletada
GRACE_SALA_VAZIA = 60


def estado_inicial():
    return {
        "fase": "lobby",
        "config": {
            "tamanho": 4,
            "dificuldade": "medio",
            "duracao": 180,
            "modo_categoria": "competitivo",  # cooperativo | competitivo
            "modo": "individual",              # individual | times | sobrevivencia | caca
            "n_partidas": 1,                   # série: primeiro a ganhar N partidas
        },
        "grade": [],
        "colunas": 4,
        "linhas": 4,
        "grade_info": {},
        "grade_palavras": [],
        "resumo": {},
        "jogadores": {},
        "host": None,
        "inicio": 0,
        "fim": 0,
        "sobrev": {
            "fase_grade": 0, "prox_embaralho": 0,
            "embaralhou_em": 0, "cresceu_em": 0, "celulas_novas": [],
        },
        "placar": {},
        "placar_times": {},
        "rodada": 0,
        "vitorias": {},
        "historico": [],
        "ranking": {},
        "celulas_bloqueadas": {},  # restricao: {idx: timestamp_expira}
        "disputa": {},             # duelo: {word: {achador, expira}}
        "bonus_duelo": {},         # duelo: {nome: pts_bonus}
        "eventos": [],             # notificações da sala (entrou/saiu)
        "evento_seq": 0,
        "chat": [],                # mensagens do lobby chat
        "chat_seq": 0,
    }


# Conquistas recém-desbloqueadas esperando entrega ao jogador no próximo poll.
# {profile_id: [achievement_id, ...]} — lock próprio pra não disputar com o LOCK do jogo.
NOTIF_ACH = {}
NOTIF_LOCK = threading.Lock()

EVENTO_JANELA = 15  # segundos que um evento fica disponível para entrega


def _add_evento(estado, tipo, nome, texto):
    estado["evento_seq"] += 1
    estado["eventos"].append({
        "id": estado["evento_seq"], "tipo": tipo, "nome": nome,
        "texto": texto, "ts": time.time(),
    })
    estado["eventos"] = estado["eventos"][-20:]


def _gerar_sala_id():
    while True:
        sid = secrets.token_hex(3).upper()
        if sid not in salas:
            return sid


def _autenticar():
    cab = request.headers.get("Authorization", "")
    if not cab.startswith("Bearer "):
        return None, None
    return auth.verificar_token(cab[7:])


def _exigir_login():
    pid, nome = _autenticar()
    if not nome:
        return None, None, (jsonify({"erro": "nao autenticado"}), 401)
    return pid, nome, None


def _sala_ou_erro(sala_id):
    if not sala_id:
        return None, (jsonify({"erro": "sala_id ausente"}), 400)
    sala = salas.get(sala_id)
    if not sala:
        return None, (jsonify({"erro": "sala nao encontrada"}), 404)
    return sala, None


def _avatar_de(pid):
    if not pid:
        return "a1"
    try:
        return db.obter_avatar(pid)
    except Exception:
        return "a1"


def _eh_host(nome, estado):
    return bool(nome) and estado["host"] == nome


def _passar_host(estado):
    if estado["host"] in estado["jogadores"]:
        return
    estado["host"] = next(iter(estado["jogadores"]), None)


def _expandir_grade(grade, li_ant, co_ant, li_novo, co_novo):
    nova = [None] * (li_novo * co_novo)
    for r in range(li_ant):
        for c in range(co_ant):
            nova[r * co_novo + c] = grade[r * co_ant + c]
    novos = []
    for i, v in enumerate(nova):
        if v is None:
            nova[i] = (random.choice(gc.VOGAIS) if random.random() < 0.40
                       else random.choice(gc.POOL_CONS))
            novos.append(i)
    return nova, novos


def bonus_tempo(palavra):
    n = len(palavra)
    if n < 3: return 0
    if n == 3: return 1
    return 3 + (n - 4) * 2


def _fase_sobrev(decorrido):
    atual = SOBREV_FASES[0]; idx = 0
    for i, (t, li, co) in enumerate(SOBREV_FASES):
        if decorrido >= t:
            atual = (t, li, co); idx = i
    return idx, atual[1], atual[2]


def _aplicar_sobrevivencia(estado):
    if estado["config"]["modo"] != "sobrevivencia" or estado["fase"] != "jogando":
        return
    agora = time.time()
    decorrido = agora - estado["inicio"]
    idx, li, co = _fase_sobrev(decorrido)
    if idx != estado["sobrev"]["fase_grade"]:
        li_ant, co_ant = estado["linhas"], estado["colunas"]
        estado["sobrev"]["fase_grade"] = idx
        grade, novos = _expandir_grade(estado["grade"], li_ant, co_ant, li, co)
        estado["grade"] = grade
        estado["linhas"], estado["colunas"] = li, co
        estado["grade_palavras"] = sorted(
            gc.palavras_da_grade(grade, li, co), key=lambda w: (-len(w), w))
        estado["grade_info"] = {
            "palavras": len(estado["grade_palavras"]),
            "maior": len(estado["grade_palavras"][0]) if estado["grade_palavras"] else 0,
        }
        estado["sobrev"]["cresceu_em"] = agora
        estado["sobrev"]["celulas_novas"] = novos
        estado["sobrev"]["prox_embaralho"] = 0
    if decorrido >= SOBREV_EMBARALHA_APOS:
        if estado["sobrev"]["prox_embaralho"] == 0:
            estado["sobrev"]["prox_embaralho"] = agora + SOBREV_EMBARALHA_INTERVALO
        elif agora >= estado["sobrev"]["prox_embaralho"]:
            g = estado["grade"][:]
            random.shuffle(g)
            estado["grade"] = g
            li, co = estado["linhas"], estado["colunas"]
            estado["grade_palavras"] = sorted(
                gc.palavras_da_grade(g, li, co), key=lambda w: (-len(w), w))
            estado["sobrev"]["embaralhou_em"] = agora
            estado["sobrev"]["prox_embaralho"] = agora + SOBREV_EMBARALHA_INTERVALO


def _checar_eliminados(estado):
    if estado["config"]["modo"] != "sobrevivencia" or estado["fase"] != "jogando":
        return
    agora = time.time()
    algum_vivo = False
    for j in estado["jogadores"].values():
        if j.get("vivo", True):
            if agora >= j.get("fim_individual", 0):
                j["vivo"] = False
            else:
                algum_vivo = True
    if not algum_vivo and estado["jogadores"]:
        estado["fim"] = agora - 1


def _reset_para_lobby(estado, full=False):
    estado["fase"] = "lobby"
    estado["grade"] = []
    estado["inicio"] = 0
    estado["fim"] = 0
    estado["placar"] = {}
    estado["placar_times"] = {}
    estado["celulas_bloqueadas"] = {}
    estado["disputa"] = {}
    estado["bonus_duelo"] = {}
    for j in estado["jogadores"].values():
        j["palavras"] = set()
    if full:
        estado["rodada"] = 0
        estado["vitorias"] = {}
        estado["historico"] = []


def _vencedor_da_partida(estado):
    if estado["config"]["modo"] == "times":
        if not estado["placar_times"]:
            return None
        return max(estado["placar_times"].items(), key=lambda kv: kv[1])[0]
    else:
        if not estado["placar"]:
            return None
        return max(estado["placar"].items(), key=lambda kv: kv[1]["pontos"])[0]


def _checar_fim(estado):
    """Se a partida acabou, resolve o placar e muda de fase. Retorna a lista de
    partidas a persistir no banco: [(perfil_id, dados)]. A persistência em si é
    feita FORA do LOCK pelo chamador (é I/O de rede ao Supabase — não pode
    segurar o lock global)."""
    if estado["fase"] != "jogando" or time.time() < estado["fim"]:
        return []
    jogadores_palavras = {n: j["palavras"] for n, j in estado["jogadores"].items()}
    modo = estado["config"]["modo"]
    if modo == "times":
        times = {n: (j["time"] or "Sem time") for n, j in estado["jogadores"].items()}
        ind, pt = gc.resolver_placar_times(jogadores_palavras, times)
        estado["placar"] = ind
        estado["placar_times"] = pt
    else:
        estado["placar"] = gc.resolver_placar(jogadores_palavras)
        estado["placar_times"] = {}

    if modo == "duelo":
        for word, d in list(estado["disputa"].items()):
            estado["bonus_duelo"][d["achador"]] = estado["bonus_duelo"].get(d["achador"], 0) + 2
        estado["disputa"] = {}
        for nome_b, bonus in estado["bonus_duelo"].items():
            if nome_b in estado["placar"]:
                estado["placar"][nome_b]["pontos"] += bonus

    for nome, dados in estado["placar"].items():
        r = estado["ranking"].setdefault(nome, {"total": 0, "melhor": 0, "partidas": 0})
        r["total"] += dados["pontos"]
        r["partidas"] += 1
        if dados["pontos"] > r["melhor"]:
            r["melhor"] = dados["pontos"]

    todas = estado.get("grade_palavras", [])
    achadas_grupo = set()
    for j in estado["jogadores"].values():
        achadas_grupo |= j["palavras"]
    faltaram = [w for w in todas if w not in achadas_grupo]
    estado["resumo"] = {
        "total": len(todas),
        "achadas": len([w for w in todas if w in achadas_grupo]),
        "faltaram": len(faltaram),
        "maior": todas[0] if todas else "",
        "maior_achada": max(achadas_grupo, key=len) if achadas_grupo else "",
        "faltaram_lista": faltaram[:60],
        "achadas_lista": sorted(achadas_grupo, key=lambda w: (-len(w), w)),
    }

    venc = _vencedor_da_partida(estado)
    duracao = time.time() - estado["inicio"]
    pendentes = []
    for nome, dados_placar in estado["placar"].items():
        j = estado["jogadores"].get(nome)
        if not j or not j.get("perfil_id"):
            continue
        palavras = j["palavras"]
        if modo == "times":
            ganhou = (j.get("time") or "Sem time") == venc
        else:
            ganhou = nome == venc
        hist = {}
        for w in palavras:
            hist[len(w)] = hist.get(len(w), 0) + 1
        pendentes.append((j["perfil_id"], {
            "mode": modo,
            "team": j.get("time") if modo == "times" else None,
            "score": dados_placar["pontos"],
            "words_found": len(palavras),
            "longest_word": max(palavras, key=len) if palavras else None,
            "avg_word_length": (sum(len(w) for w in palavras) / len(palavras)) if palavras else 0,
            "words_per_second": (len(palavras) / duracao) if duracao > 0 else 0,
            "won": ganhou,
            "duration_seconds": duracao,
            "exclusivas_count": len(dados_placar.get("exclusivas", [])),
            "word_len_hist": hist,
        }))

    if venc is not None:
        estado["vitorias"][venc] = estado["vitorias"].get(venc, 0) + 1
        pontos_venc = (estado["placar_times"].get(venc, 0) if modo == "times"
                       else estado["placar"].get(venc, {}).get("pontos", 0))
        estado["historico"].append({"rodada": estado["rodada"], "vencedor": venc, "pontos": pontos_venc})
        estado["historico"] = estado["historico"][-5:]

    # série: primeiro a ganhar n_partidas
    alvo = estado["config"]["n_partidas"]
    max_vit = max(estado["vitorias"].values(), default=0) if estado["vitorias"] else 0
    if alvo <= 1 or max_vit >= alvo:
        estado["fase"] = "fim_campeonato"
    else:
        estado["fase"] = "resultado"

    return pendentes


def _limpar_ausentes(sala):
    estado = sala["estado"]
    agora = time.time()
    if estado["fase"] != "jogando":
        fora = [n for n, j in estado["jogadores"].items() if agora - j["visto"] > TIMEOUT_LOBBY]
        for n in fora:
            del estado["jogadores"][n]
            _add_evento(estado, "saiu", n, f"{n} saiu da sala")
    elif (estado["jogadores"] and
          all(agora - j["visto"] > TIMEOUT_JOGO for j in estado["jogadores"].values())):
        _reset_para_lobby(estado, full=True)
        estado["host"] = None
        sala["vazia_desde"] = agora
        return
    if estado["host"] not in estado["jogadores"]:
        _passar_host(estado)
    if not estado["jogadores"] and estado["fase"] != "jogando":
        _reset_para_lobby(estado, full=True)
        estado["host"] = None
        sala.setdefault("vazia_desde", agora)
    elif estado["jogadores"]:
        sala.pop("vazia_desde", None)


def _limpar_salas_vazias():
    agora = time.time()
    para_remover = [
        sid for sid, sala in salas.items()
        if not sala["estado"]["jogadores"]
        and (agora - sala.get("vazia_desde", agora)) > GRACE_SALA_VAZIA
    ]
    for sid in para_remover:
        del salas[sid]


# ---- rotas de perfil ----

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/dbcheck")
def dbcheck():
    """
    Diagnóstico TEMPORÁRIO. O psycopg_pool engole a exceção de conexão e só
    expõe PoolTimeout, que não diz nada sobre a causa. Aqui testamos DNS, TCP
    e Postgres em separado para isolar em que camada quebra.
    Remover assim que a conexão do Render estiver resolvida.
    """
    import socket
    import psycopg
    from urllib.parse import urlparse

    url = os.environ.get("SUPABASE_DB_URL", "")
    p = urlparse(url)
    host, porta = p.hostname, (p.port or 5432)
    out = {"host": host, "porta": porta, "usuario": p.username}

    try:
        infos = socket.getaddrinfo(host, porta, 0, socket.SOCK_STREAM)
        out["dns"] = sorted({i[4][0] for i in infos})
    except Exception as e:
        out["dns"] = f"{type(e).__name__}: {e}"

    try:
        t = time.time()
        s = socket.create_connection((host, porta), timeout=8)
        s.close()
        out["tcp"] = "ok em %.2fs" % (time.time() - t)
    except Exception as e:
        out["tcp"] = f"{type(e).__name__}: {e}"

    try:
        t = time.time()
        with psycopg.connect(url, connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("select 1")
        out["postgres"] = "ok em %.2fs" % (time.time() - t)
    except Exception as e:
        out["postgres"] = f"{type(e).__name__}: {str(e)[:300]}"

    return jsonify(out)


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "salas_ativas": len(salas),
        "jogadores_online": sum(len(s["estado"]["jogadores"]) for s in salas.values()),
        "metricas": log.get_counts(),
    })


@app.route("/api/perfil/criar", methods=["POST"])
def perfil_criar():
    ip = request.remote_addr or "?"
    if not rl.checar(f"criar:{ip}", "criar_conta"):
        log.warn("rate_limit", rota="criar", ip=ip)
        return jsonify({"ok": False, "motivo": "muitas tentativas, aguarde"}), 429
    data = request.json or {}
    username, err = san.nome_usuario(data.get("username"))
    if err:
        return jsonify({"ok": False, "motivo": err}), 400
    p, err = san.pin(data.get("pin"))
    if err:
        return jsonify({"ok": False, "motivo": err}), 400
    if db.buscar_perfil(username):
        return jsonify({"ok": False, "motivo": "usuario ja existe"}), 409
    pid = db.criar_perfil(username, auth.hash_pin(p))
    token = auth.gerar_token(pid, username)
    log.info("perfil_criado", username=username)
    log.count("perfis_criados")
    return jsonify({"ok": True, "token": token, "username": username, "avatar": "a1"})


@app.route("/api/perfil/login", methods=["POST"])
def perfil_login():
    ip = request.remote_addr or "?"
    data = request.json or {}
    username = (data.get("username") or "").strip()[:16]
    chave_rl = f"login:{ip}:{username}"
    if not rl.checar(chave_rl, "login"):
        log.warn("rate_limit", rota="login", ip=ip, user=username)
        return jsonify({"ok": False, "motivo": "muitas tentativas, aguarde 1 minuto"}), 429
    p, err = san.pin(data.get("pin"))
    if err:
        return jsonify({"ok": False, "motivo": err}), 400
    perfil = db.buscar_perfil(username)
    if not perfil or not auth.checar_pin(p, perfil["pin_hash"]):
        log.info("login_falhou", ip=ip, user=username)
        log.count("login_falhas")
        return jsonify({"ok": False, "motivo": "usuario ou pin incorretos"}), 401
    token = auth.gerar_token(perfil["id"], username)
    log.info("login_ok", user=username)
    log.count("logins")
    return jsonify({"ok": True, "token": token, "username": username,
                    "avatar": perfil.get("avatar", "a1"),
                    "bio": perfil.get("bio", ""),
                    "tem_pergunta": bool(perfil.get("security_question"))})


@app.route("/api/perfil/stats")
def perfil_stats():
    pid, nome, erro = _exigir_login()
    if erro: return erro
    stats = db.obter_stats(pid)
    if not stats:
        return jsonify({"ok": False, "motivo": "perfil nao encontrado"}), 404
    return jsonify({"ok": True,
                    "stats": {k: (str(v) if hasattr(v, '__float__') else v) for k, v in stats.items()},
                    "username": nome})


@app.route("/api/perfil/avatar", methods=["POST"])
def perfil_avatar():
    pid, _, erro = _exigir_login()
    if erro: return erro
    data = request.json or {}
    avatar = (data.get("avatar") or "").strip()
    if avatar not in AVATARES_VALIDOS:
        return jsonify({"ok": False, "motivo": "avatar invalido"}), 400
    try:
        db.set_avatar(pid, avatar)
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500
    return jsonify({"ok": True, "avatar": avatar})


@app.route("/api/perfil/editar", methods=["POST"])
def perfil_editar():
    pid, _, erro = _exigir_login()
    if erro: return erro
    data = request.json or {}
    bio = data.get("bio")
    if bio is not None:
        bio = bio.strip()[:120]
    try:
        db.atualizar_perfil(pid, bio=bio)
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/perfil/seguranca", methods=["POST"])
def perfil_seguranca():
    """Define a pergunta de segurança para recuperação de PIN."""
    pid, _, erro = _exigir_login()
    if erro: return erro
    data = request.json or {}
    pergunta = (data.get("pergunta") or "").strip()[:100]
    resposta = (data.get("resposta") or "").strip().lower()
    if not pergunta or len(resposta) < 2:
        return jsonify({"ok": False, "motivo": "pergunta e resposta obrigatórias (resposta mín 2 chars)"}), 400
    db.set_security_question(pid, pergunta, auth.hash_pin(resposta))
    return jsonify({"ok": True})


@app.route("/api/perfil/recuperar", methods=["POST"])
def perfil_recuperar():
    """Recupera o PIN usando a resposta de segurança."""
    ip = request.remote_addr or "?"
    if not rl.checar(f"recuperar:{ip}", "login"):
        return jsonify({"ok": False, "motivo": "muitas tentativas, aguarde"}), 429
    data = request.json or {}
    username = (data.get("username") or "").strip()[:16]
    resposta = (data.get("resposta") or "").strip().lower()
    novo_pin, err = san.pin(data.get("novo_pin"))
    if err:
        return jsonify({"ok": False, "motivo": err}), 400
    perfil = db.buscar_perfil(username)
    if not perfil or not perfil.get("security_question"):
        return jsonify({"ok": False, "motivo": "usuário não encontrado ou sem pergunta de segurança"}), 404
    if not perfil.get("security_answer_hash") or not auth.checar_pin(resposta, perfil["security_answer_hash"]):
        log.info("recuperacao_falhou", ip=ip, user=username)
        return jsonify({"ok": False, "motivo": "resposta incorreta"}), 401
    db.reset_pin(username, auth.hash_pin(novo_pin))
    token = auth.gerar_token(perfil["id"], username)
    log.info("pin_recuperado", user=username)
    return jsonify({"ok": True, "token": token, "username": username,
                    "avatar": perfil.get("avatar", "a1")})


@app.route("/api/perfil/pergunta", methods=["POST"])
def perfil_pergunta():
    """Retorna a pergunta de segurança de um usuário (para a tela de recuperação)."""
    data = request.json or {}
    username = (data.get("username") or "").strip()[:16]
    perfil = db.buscar_perfil(username)
    if not perfil or not perfil.get("security_question"):
        return jsonify({"ok": False, "motivo": "usuário não encontrado ou sem pergunta"}), 404
    return jsonify({"ok": True, "pergunta": perfil["security_question"]})


@app.route("/api/perfil/achievements")
def perfil_achievements():
    pid, _, erro = _exigir_login()
    if erro: return erro
    try:
        desbloqueados = db.obter_achievements(pid)
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500
    return jsonify({"ok": True,
                    "catalogo": ach.catalogo(),
                    "desbloqueados": desbloqueados})


@app.route("/api/amigos", methods=["GET"])
def listar_amigos():
    pid, _, erro = _exigir_login()
    if erro: return erro
    amigos = db.listar_amigos(pid)
    # marcar quem está online (em alguma sala)
    online_set = set()
    with LOCK:
        for sid, estado in salas.items():
            for jn in estado.get("jogadores", {}):
                online_set.add(jn)
    for a in amigos:
        a["online"] = a["username"] in online_set
    return jsonify({"ok": True, "amigos": amigos})


@app.route("/api/amigos/adicionar", methods=["POST"])
def adicionar_amigo():
    pid, _, erro = _exigir_login()
    if erro: return erro
    data = request.json or {}
    username = (data.get("username") or "").strip()[:16]
    if not username:
        return jsonify({"ok": False, "motivo": "username obrigatorio"}), 400
    ok, err = db.enviar_pedido_amizade(pid, username)
    if err:
        return jsonify({"ok": False, "motivo": err}), 400
    return jsonify({"ok": True})


@app.route("/api/amigos/aceitar", methods=["POST"])
def aceitar_amigo():
    pid, _, erro = _exigir_login()
    if erro: return erro
    data = request.json or {}
    fid = (data.get("friendship_id") or "").strip()
    if not fid:
        return jsonify({"ok": False, "motivo": "friendship_id obrigatorio"}), 400
    ok = db.aceitar_amizade(pid, fid)
    if not ok:
        return jsonify({"ok": False, "motivo": "pedido nao encontrado"}), 404
    return jsonify({"ok": True})


@app.route("/api/amigos/remover", methods=["POST"])
def remover_amigo():
    pid, _, erro = _exigir_login()
    if erro: return erro
    data = request.json or {}
    username = (data.get("username") or "").strip()[:16]
    if not username:
        return jsonify({"ok": False, "motivo": "username obrigatorio"}), 400
    db.remover_amizade(pid, username)
    return jsonify({"ok": True})


@app.route("/api/perfil/historico")
def perfil_historico():
    pid, _, erro = _exigir_login()
    if erro: return erro
    try:
        hist = db.historico_partidas(pid)
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})
    return jsonify({"ok": True, "historico": hist})


@app.route("/api/leaderboard")
def leaderboard():
    _, _, erro = _exigir_login()
    if erro: return erro
    modo = (request.args.get("modo") or "").strip()
    modos_validos = {"individual", "times", "sobrevivencia", "duelo", "restricao", "caca"}
    if modo and modo in modos_validos:
        data = db.obter_leaderboard_modo(modo)
    else:
        data = db.obter_leaderboard()
    resultado = [{k: float(v) if hasattr(v, '__float__') else v for k, v in row.items()}
                 for row in data]
    return jsonify({"ok": True, "data": resultado})


# ---- rotas de sala ----

@app.route("/api/salas")
def listar_salas():
    _, _, erro = _exigir_login()
    if erro: return erro
    with LOCK:
        for sala in list(salas.values()):
            _limpar_ausentes(sala)
        _limpar_salas_vazias()
        lista = [{
            "id": sala["id"],
            "nome": sala["nome"],
            "jogadores": len(sala["estado"]["jogadores"]),
            "fase": sala["estado"]["fase"],
            "tem_senha": bool(sala["senha_hash"]),
            "modo_categoria": sala["estado"]["config"]["modo_categoria"],
            "modo": sala["estado"]["config"]["modo"],
        } for sala in salas.values()]
    return jsonify({"ok": True, "salas": lista})


@app.route("/api/sala/criar", methods=["POST"])
def criar_sala():
    pid, nome, erro = _exigir_login()
    if erro: return erro
    data = request.json or {}
    nome_sala, err = san.nome_sala(data.get("nome"))
    if err:
        return jsonify({"ok": False, "motivo": err}), 400
    senha = (data.get("senha") or "").strip()
    senha_hash = auth.hash_pin(senha) if senha else None
    avatar = _avatar_de(pid)
    with LOCK:
        sid = _gerar_sala_id()
        e = estado_inicial()
        e["jogadores"][nome] = {
            "palavras": set(), "visto": time.time(), "time": None,
            "vivo": True, "fim_individual": 0, "ultima_palavra": 0,
            "perfil_id": pid, "avatar": avatar,
        }
        e["host"] = nome
        salas[sid] = {
            "id": sid, "nome": nome_sala, "senha_hash": senha_hash,
            "estado": e, "criada_em": time.time(),
        }
    log.info("sala_criada", sala=sid, nome=nome_sala, host=nome)
    log.count("salas_criadas")
    return jsonify({"ok": True, "sala_id": sid, "sala_nome": nome_sala, "nome": nome})


@app.route("/api/sala/<sala_id>/entrar", methods=["POST"])
def entrar_sala(sala_id):
    pid, nome, erro = _exigir_login()
    if erro: return erro
    data = request.json or {}
    avatar = _avatar_de(pid)
    with LOCK:
        sala, err = _sala_ou_erro(sala_id)
        if err: return err
        estado = sala["estado"]
        # verifica senha se for novo na sala
        if nome not in estado["jogadores"] and sala["senha_hash"]:
            senha = (data.get("senha") or "").strip()
            if not auth.checar_pin(senha, sala["senha_hash"]):
                return jsonify({"ok": False, "motivo": "senha incorreta"}), 403
        if nome not in estado["jogadores"]:
            estado["jogadores"][nome] = {
                "palavras": set(), "visto": time.time(), "time": None,
                "vivo": True, "fim_individual": 0, "ultima_palavra": 0,
                "perfil_id": pid, "avatar": avatar,
            }
            _add_evento(estado, "entrou", nome, f"{nome} entrou na sala")
        else:
            estado["jogadores"][nome]["visto"] = time.time()
            estado["jogadores"][nome]["perfil_id"] = pid
            estado["jogadores"][nome]["avatar"] = avatar
        if estado["host"] is None or estado["host"] not in estado["jogadores"]:
            estado["host"] = nome
        sala.pop("vazia_desde", None)
    return jsonify({"ok": True, "nome": nome, "sala_nome": sala["nome"],
                    "host": estado["host"] == nome})


@app.route("/api/sala/<sala_id>/sair", methods=["POST"])
def sair(sala_id):
    _, nome, erro = _exigir_login()
    if erro: return erro
    with LOCK:
        sala, err = _sala_ou_erro(sala_id)
        if err: return err
        estado = sala["estado"]
        if nome in estado["jogadores"]:
            del estado["jogadores"][nome]
            _add_evento(estado, "saiu", nome, f"{nome} saiu da sala")
        _passar_host(estado)
        if not estado["jogadores"]:
            _reset_para_lobby(estado, full=True)
            estado["ranking"] = {}
            estado["host"] = None
            sala["vazia_desde"] = time.time()
    return jsonify({"ok": True})


@app.route("/api/sala/<sala_id>/chat", methods=["POST"])
def chat_sala(sala_id):
    _, nome, erro = _exigir_login()
    if erro: return erro
    data = request.json or {}
    texto = (data.get("texto") or "").strip()[:200]
    if not texto:
        return jsonify({"ok": False, "motivo": "mensagem vazia"}), 400
    with LOCK:
        sala, err = _sala_ou_erro(sala_id)
        if err: return err
        estado = sala["estado"]
        if nome not in estado["jogadores"]:
            return jsonify({"ok": False, "motivo": "nao esta na sala"}), 403
        estado["chat_seq"] += 1
        msg = {"id": estado["chat_seq"], "nome": nome, "texto": texto, "ts": time.time()}
        estado["chat"].append(msg)
        if len(estado["chat"]) > 50:
            estado["chat"] = estado["chat"][-50:]
    return jsonify({"ok": True})


@app.route("/api/sala/<sala_id>/config", methods=["POST"])
def config_sala(sala_id):
    data = request.json or {}
    _, nome, erro = _exigir_login()
    if erro: return erro
    with LOCK:
        sala, err = _sala_ou_erro(sala_id)
        if err: return err
        estado = sala["estado"]
        if not _eh_host(nome, estado):
            return jsonify({"ok": False, "motivo": "so o host configura"})
        if estado["fase"] not in ("lobby", "fim_campeonato"):
            return jsonify({"ok": False, "motivo": "so no lobby"})
        c = estado["config"]
        muda_modo = (
            ("modo" in data and data["modo"] != c["modo"])
            or ("modo_categoria" in data and data["modo_categoria"] != c["modo_categoria"])
            or ("tamanho" in data and int(data["tamanho"]) != c["tamanho"])
            or ("dificuldade" in data and data["dificuldade"] != c["dificuldade"])
            or ("duracao" in data and int(data["duracao"]) != c["duracao"])
        )
        if "tamanho" in data:
            c["tamanho"] = max(3, min(10, int(data["tamanho"])))
        if "dificuldade" in data and data["dificuldade"] in ("facil", "medio", "dificil"):
            c["dificuldade"] = data["dificuldade"]
        if "duracao" in data:
            c["duracao"] = max(30, min(600, int(data["duracao"])))
        if "modo_categoria" in data and data["modo_categoria"] in ("cooperativo", "competitivo"):
            c["modo_categoria"] = data["modo_categoria"]
            if data["modo_categoria"] == "cooperativo":
                c["modo"] = "caca"
        if "modo" in data and data["modo"] in ("individual", "times", "sobrevivencia", "restricao", "duelo"):
            if c["modo_categoria"] == "competitivo":
                c["modo"] = data["modo"]
        if "n_partidas" in data and int(data["n_partidas"]) in (1, 3, 5, 7):
            c["n_partidas"] = int(data["n_partidas"])
        if muda_modo:
            estado["ranking"] = {}
            estado["historico"] = []
            estado["vitorias"] = {}
    return jsonify({"ok": True, "config": estado["config"]})


@app.route("/api/sala/<sala_id>/time", methods=["POST"])
def set_time(sala_id):
    data = request.json or {}
    _, nome, erro = _exigir_login()
    if erro: return erro
    t = (data.get("time", "") or "").strip()[:16] or None
    with LOCK:
        sala, err = _sala_ou_erro(sala_id)
        if err: return err
        if nome in sala["estado"]["jogadores"]:
            sala["estado"]["jogadores"][nome]["time"] = t
    return jsonify({"ok": True})


@app.route("/api/sala/<sala_id>/iniciar", methods=["POST"])
def iniciar(sala_id):
    _, nome, erro = _exigir_login()
    if erro: return erro
    with LOCK:
        sala, err = _sala_ou_erro(sala_id)
        if err: return err
        estado = sala["estado"]
        if not _eh_host(nome, estado):
            return jsonify({"ok": False, "motivo": "so o host inicia"})
        if estado["fase"] == "fim_campeonato":
            _reset_para_lobby(estado, full=True)
        if estado["fase"] == "lobby":
            estado["rodada"] = 0
            estado["vitorias"] = {}
            estado["historico"] = []
        estado["fase"] = "jogando"
        modo = estado["config"]["modo"]
        dif = estado["config"].get("dificuldade", "medio")
        if modo == "sobrevivencia":
            _, li, co = _fase_sobrev(0)
            estado["sobrev"] = {"fase_grade": 0, "prox_embaralho": 0,
                                "embaralhou_em": 0, "cresceu_em": 0, "celulas_novas": []}
        else:
            li = co = estado["config"]["tamanho"]
        grade, qtd, maior = gc.gerar_grade(li, dificuldade=dif, colunas=co)
        estado["grade"] = grade
        estado["linhas"], estado["colunas"] = li, co
        estado["grade_info"] = {"palavras": qtd, "maior": maior}
        todas = gc.palavras_da_grade(grade, li, co)
        estado["grade_palavras"] = sorted(todas, key=lambda w: (-len(w), w))
        agora = time.time()
        estado["inicio"] = agora
        estado["fim"] = agora + (60 * 60 if modo in ("sobrevivencia", "caca")
                                 else estado["config"]["duracao"])
        estado["rodada"] += 1
        for j in estado["jogadores"].values():
            j["palavras"] = set()
            j["vivo"] = True
            j["fim_individual"] = agora + SOBREV_TEMPO_INICIAL
            j["ultima_palavra"] = agora
        estado["placar"] = {}
        estado["placar_times"] = {}
    return jsonify({"ok": True})


@app.route("/api/sala/<sala_id>/nova", methods=["POST"])
def nova(sala_id):
    _, nome, erro = _exigir_login()
    if erro: return erro
    with LOCK:
        sala, err = _sala_ou_erro(sala_id)
        if err: return err
        estado = sala["estado"]
        if estado["jogadores"] and not _eh_host(nome, estado):
            return jsonify({"ok": False, "motivo": "so o host encerra"})
        _reset_para_lobby(estado, full=True)
    return jsonify({"ok": True})


@app.route("/api/sala/<sala_id>/submeter", methods=["POST"])
def submeter(sala_id):
    data = request.json or {}
    _, nome, erro = _exigir_login()
    if erro: return erro
    caminho, err = san.caminho(data.get("caminho", []))
    if err:
        return jsonify({"ok": False, "motivo": err}), 400
    with LOCK:
        sala, err = _sala_ou_erro(sala_id)
        if err: return err
        estado = sala["estado"]
        if estado["fase"] != "jogando":
            return jsonify({"ok": False, "motivo": "fora de partida"})
        if nome not in estado["jogadores"]:
            return jsonify({"ok": False, "motivo": "desconhecido"})
        j = estado["jogadores"][nome]
        modo = estado["config"]["modo"]
        if modo == "sobrevivencia" and not j.get("vivo", True):
            return jsonify({"ok": False, "motivo": "sem tempo"})
        ok, w = gc.validar_submissao(estado["grade"], caminho,
                                      linhas=estado["linhas"], colunas=estado["colunas"])
        if not ok:
            return jsonify({"ok": False, "palavra": w})
        agora = time.time()
        ja = w in j["palavras"]
        j["palavras"].add(w)
        j["visto"] = agora
        if not ja:
            j["ultima_palavra"] = agora
        resp = {"ok": True, "palavra": w, "base": gc.pontos_base(w), "repetida": ja}

        if not ja:
            if modo == "restricao":
                for idx in caminho:
                    estado["celulas_bloqueadas"][idx] = agora + 5
            elif modo == "duelo":
                for word_exp, d in list(estado["disputa"].items()):
                    if d["expira"] < agora:
                        estado["bonus_duelo"][d["achador"]] = estado["bonus_duelo"].get(d["achador"], 0) + 2
                        del estado["disputa"][word_exp]
                if w in estado["disputa"]:
                    del estado["disputa"][w]
                    resp["defendeu"] = True
                else:
                    estado["disputa"][w] = {"achador": nome, "expira": agora + 10}
                    resp["em_disputa"] = True

        if modo == "sobrevivencia" and not ja:
            ganho = bonus_tempo(w)
            j["fim_individual"] = j.get("fim_individual", agora) + ganho
            resp["ganho_tempo"] = ganho
        if modo == "caca":
            modo_cat = estado["config"]["modo_categoria"]
            if modo_cat == "cooperativo":
                coletivas = set()
                for jj in estado["jogadores"].values():
                    coletivas |= jj["palavras"]
                total = len(estado["grade_palavras"])
                achou = len(coletivas)
            else:
                total = len(estado["grade_palavras"])
                achou = len(j["palavras"])
            resp["progresso"] = {"achadas": achou, "total": total}
            if achou >= total and total > 0:
                estado["fim"] = agora - 1
        return jsonify(resp)


@app.route("/api/sala/<sala_id>/dica", methods=["POST"])
def dica(sala_id):
    _, nome, erro = _exigir_login()
    if erro: return erro
    with LOCK:
        sala, err = _sala_ou_erro(sala_id)
        if err: return err
        estado = sala["estado"]
        if estado["fase"] != "jogando":
            return jsonify({"ok": False, "motivo": "fora de partida"})
        if estado["config"]["modo"] not in ("individual", "times", "caca"):
            return jsonify({"ok": False, "motivo": "modo sem dicas"})
        if nome not in estado["jogadores"]:
            return jsonify({"ok": False, "motivo": "desconhecido"})
        modo_cat = estado["config"]["modo_categoria"]
        if modo_cat == "cooperativo":
            ja = set()
            for jj in estado["jogadores"].values():
                ja |= jj["palavras"]
        else:
            ja = estado["jogadores"][nome]["palavras"]
        candidatas = [w for w in estado["grade_palavras"] if len(w) == 3 and w not in ja]
        if not candidatas:
            return jsonify({"ok": True, "palavra": None, "motivo": "achou todas de 3 letras"})
        escolhida = random.choice(candidatas)
        caminho = gc.caminho_da_palavra(estado["grade"], escolhida,
                                        linhas=estado["linhas"], colunas=estado["colunas"])
        return jsonify({"ok": True, "palavra": escolhida, "caminho": caminho})


@app.route("/api/sala/<sala_id>/estado")
def get_estado(sala_id):
    pid_poll, nome, erro = _exigir_login()
    if erro: return erro
    with LOCK:
        sala, err = _sala_ou_erro(sala_id)
        if err: return err
        estado = sala["estado"]
        agora = time.time()
        if nome in estado["jogadores"]:
            estado["jogadores"][nome]["visto"] = agora
        _aplicar_sobrevivencia(estado)
        _checar_eliminados(estado)
        pendentes_persistencia = _checar_fim(estado)
        _limpar_ausentes(sala)

        modo = estado["config"]["modo"]
        modo_cat = estado["config"]["modo_categoria"]
        jogando = estado["fase"] == "jogando"

        restante = 0
        if jogando:
            if modo == "sobrevivencia" and nome in estado["jogadores"]:
                fim_ind = estado["jogadores"][nome].get("fim_individual", agora)
                restante = max(0, int(round(fim_ind - agora)))
            elif modo == "caca":
                restante = -1
            else:
                restante = max(0, int(estado["fim"] - agora))

        minhas = []
        vivo = True
        if nome in estado["jogadores"]:
            minhas = sorted(estado["jogadores"][nome]["palavras"])
            vivo = estado["jogadores"][nome].get("vivo", True)

        # união das palavras do grupo — computada no máximo uma vez por request
        # (reusada por palavras_coletivas e pelo progresso da caça cooperativa)
        coletivas = None
        if jogando and modo_cat == "cooperativo":
            coletivas = set()
            for j in estado["jogadores"].values():
                coletivas |= j["palavras"]
        palavras_coletivas = sorted(coletivas) if coletivas is not None else None

        jogadores_info = []
        for n, j in estado["jogadores"].items():
            info = {"nome": n, "time": j["time"], "avatar": j.get("avatar", "a1"),
                    "cor": j.get("cor")}
            if modo == "sobrevivencia":
                info["vivo"] = j.get("vivo", True)
                info["restante"] = max(0, int(round(j.get("fim_individual", agora) - agora)))
                info["palavras"] = len(j["palavras"])
            elif modo == "caca":
                info["palavras"] = len(j["palavras"])
            jogadores_info.append(info)

        embaralhou_ha = None; cresceu_ha = None; celulas_novas = []
        if modo == "sobrevivencia" and jogando:
            t = estado["sobrev"].get("embaralhou_em", 0)
            if t and (agora - t) <= SOBREV_AVISO:
                embaralhou_ha = round(agora - t, 1)
            tc = estado["sobrev"].get("cresceu_em", 0)
            if tc and (agora - tc) <= 6:
                cresceu_ha = round(agora - tc, 1)
                celulas_novas = estado["sobrev"].get("celulas_novas", [])

        resp = {
            "fase": estado["fase"],
            "config": estado["config"],
            "sala_nome": sala["nome"],
            "host": estado["host"],
            "sou_host": estado["host"] == nome,
            "rodada": estado["rodada"],
            "grade": estado["grade"] if jogando else [],
            "linhas": estado["linhas"],
            "colunas": estado["colunas"],
            "restante": restante,
            "vivo": vivo,
            "jogadores": jogadores_info,
            "minhas_palavras": minhas,
        }
        # Campos de placar/série só mudam entre partidas — durante "jogando" o
        # frontend não os lê (só em mostrarResultado/mostrarFim, nas fases
        # resultado/fim_campeonato). Omiti-los durante o jogo enxuga o poll.
        if not jogando:
            resp["placar"] = estado["placar"]
            resp["placar_times"] = estado["placar_times"]
            resp["vitorias"] = estado["vitorias"]
            resp["historico"] = estado["historico"]
            resp["ranking"] = estado["ranking"]
            if estado["fase"] in ("resultado", "fim_campeonato"):
                resp["resumo"] = estado["resumo"]
        if palavras_coletivas is not None:
            resp["palavras_coletivas"] = palavras_coletivas
        if modo == "restricao" and jogando:
            estado["celulas_bloqueadas"] = {k: v for k, v in estado["celulas_bloqueadas"].items() if v > agora}
            resp["celulas_bloqueadas"] = list(estado["celulas_bloqueadas"].keys())
        if modo == "duelo" and jogando:
            for word_exp, d in list(estado["disputa"].items()):
                if d["expira"] < agora:
                    estado["bonus_duelo"][d["achador"]] = estado["bonus_duelo"].get(d["achador"], 0) + 2
                    del estado["disputa"][word_exp]
            resp["disputa"] = [
                {"palavra": w, "achador": d["achador"], "expira_em": round(d["expira"] - agora, 1)}
                for w, d in estado["disputa"].items()
            ]
            resp["bonus_duelo"] = estado["bonus_duelo"]
        if embaralhou_ha is not None:
            resp["embaralhou_ha"] = embaralhou_ha
            resp["aviso_embaralho"] = SOBREV_AVISO
        if cresceu_ha is not None:
            resp["cresceu_ha"] = cresceu_ha
            resp["celulas_novas"] = celulas_novas
        if modo == "caca" and jogando:
            achadas = len(coletivas) if modo_cat == "cooperativo" else len(minhas)
            resp["progresso"] = {"achadas": achadas, "total": len(estado["grade_palavras"])}
        # eventos recentes da sala (entrou/saiu) — o cliente deduplica por id
        recentes = [e for e in estado["eventos"] if agora - e["ts"] <= EVENTO_JANELA]
        if recentes:
            resp["eventos"] = [{"id": e["id"], "tipo": e["tipo"],
                                "nome": e["nome"], "texto": e["texto"]} for e in recentes]

        # chat do lobby
        if estado["chat"]:
            resp["chat"] = [{"id":m["id"],"nome":m["nome"],"texto":m["texto"]} for m in estado["chat"][-30:]]

        # conquistas recém-desbloqueadas deste jogador (entrega única)
        if pid_poll:
            with NOTIF_LOCK:
                ids = NOTIF_ACH.pop(str(pid_poll), None)
            if ids:
                vistos, lista = set(), []
                for aid in ids:
                    if aid in vistos:
                        continue
                    vistos.add(aid)
                    a = ach.BY_ID.get(aid)
                    if a:
                        lista.append({"id": a[0], "nome": a[1], "desc": a[2], "icone": a[3]})
                if lista:
                    resp["conquistas_novas"] = lista

        payload = jsonify(resp)  # serializa ainda sob o LOCK (estado consistente)

    # persistência ao Supabase FORA do LOCK — I/O de rede não pode bloquear
    # os polls das outras salas. Roda no máximo uma vez por partida.
    for perfil_id, dados in pendentes_persistencia:
        try:
            novos = db.persistir_partida(perfil_id, dados)
            log.count("partidas_persistidas")
            if novos:
                log.info("achievements_desbloqueados", perfil=str(perfil_id), novos=novos)
                with NOTIF_LOCK:
                    NOTIF_ACH.setdefault(str(perfil_id), []).extend(novos)
        except Exception as e:
            log.error("persistir_partida_falhou", perfil=str(perfil_id), erro=str(e))
    return payload


@app.route("/api/sala/<sala_id>/palavras", methods=["GET", "POST"])
def palavras_extras(sala_id):
    _, _, erro = _exigir_login()
    if erro: return erro
    if request.method == "GET":
        return jsonify({"palavras": sorted(gc.EXTRAS)})
    data = request.json or {}
    texto = (data.get("palavras") or "").upper()
    acao = data.get("acao", "add")
    novas = [p.strip() for p in texto.replace(",", " ").replace("\n", " ").split()]
    novas = ["".join(c for c in p if c.isalpha()) for p in novas]
    novas = [p for p in novas if 3 <= len(p) <= 16]
    aplicadas, ignoradas = [], []
    with LOCK:
        if acao == "remover":
            aplicadas = gc.remover_palavras_batch(novas)
            ignoradas = [p for p in novas if p not in aplicadas]
        else:
            for p in novas:
                (aplicadas if gc.adicionar_palavra(p) else ignoradas).append(p)
    return jsonify({"ok": True, "acao": acao, "aplicadas": aplicadas,
                    "ignoradas": ignoradas, "total_extras": len(gc.EXTRAS)})


@app.route("/api/sala/<sala_id>/zerar_ranking", methods=["POST"])
def zerar_ranking(sala_id):
    _, _, erro = _exigir_login()
    if erro: return erro
    with LOCK:
        sala, err = _sala_ou_erro(sala_id)
        if err: return err
        sala["estado"]["ranking"] = {}
    return jsonify({"ok": True})


def _loop_limpeza():
    """Limpeza proativa: remove jogadores ausentes e salas vazias mesmo quando
    ninguém está pollando (senão salas abandonadas ficariam abertas p/ sempre)."""
    while True:
        time.sleep(30)
        try:
            with LOCK:
                for sala in list(salas.values()):
                    _limpar_ausentes(sala)
                _limpar_salas_vazias()
        except Exception as e:
            print(f"[limpeza] erro: {e}")


# Sob gunicorn (--workers>1) cada worker roda seu próprio loop, mas como o
# estado é por-processo isso é o comportamento correto.
threading.Thread(target=_loop_limpeza, daemon=True).start()


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    dev = os.environ.get("FLASK_ENV") != "production"
    app.run(host="0.0.0.0", port=porta, debug=dev)
