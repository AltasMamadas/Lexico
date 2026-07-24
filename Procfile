# workers=1 é obrigatório: o estado das salas vive em memória (dict `salas` em
# app.py). Com mais de um worker cada processo teria suas próprias salas e os
# jogadores seriam espalhados entre elas.
# threads=8 é o que destrava a concorrência: no padrão (sync, 1 thread) o app
# atende uma requisição por vez, então qualquer I/O lento congela todo mundo.
web: gunicorn app:app --worker-class gthread --workers 1 --threads 8 --timeout 60
