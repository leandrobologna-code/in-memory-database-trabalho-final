"""
Consumer MongoDB → Redis — Plataforma Radar Combustível
=======================================================
Escuta Change Streams das coleções do Radar Combustível e mantém
o Redis atualizado em tempo real com rankings, métricas e hashes
prontos para leitura rápida.

Coleções monitoradas:
  - eventos_preco         → rankings de preço, variação, hash por posto/combustível
  - buscas_usuarios       → ranking de combustíveis mais buscados, bairros/cidades quentes
  - avaliacoes_interacoes → média de avaliação por posto, ranking de favoritos

Uso:
  1) docker compose up -d
  2) pip install -r requirements.txt
  3) python consumer_radar_combustivel.py [--skip-backfill]

Variáveis de ambiente:
  MONGO_URI   (default: mongodb://localhost:27017/?directConnection=true)
  DB_NAME     (default: radar_combustivel)
  REDIS_HOST  (default: localhost)
  REDIS_PORT  (default: 6379)
"""

from __future__ import annotations

import argparse
import os
import threading
import time
from typing import Any, Dict

from dotenv import load_dotenv
from pymongo import MongoClient
from redis import Redis
from redis.exceptions import ResponseError

load_dotenv(".env.local")
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/?directConnection=true")
DB_NAME   = os.getenv("DB_NAME",   "radar_combustivel")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

# ---------------------------------------------------------------------------
# Helpers de chave Redis
# ---------------------------------------------------------------------------

def posto_hash_key(posto_id: str) -> str:
    """Hash com dados e contadores do posto."""
    return f"posto:{posto_id}"


def preco_hash_key(posto_id: str, combustivel: str) -> str:
    """Hash com o preço atual de um combustível em um posto."""
    return f"preco:{posto_id}:{combustivel}"


def ts_key_preco(posto_id: str, combustivel: str) -> str:
    """Time Series de evolução de preço."""
    return f"ts:preco:{posto_id}:{combustivel}"


# ---------------------------------------------------------------------------
# Time Series helper (mesmo padrão do consumer de referência)
# ---------------------------------------------------------------------------

def ensure_ts_add(
    redis: Redis,
    key: str,
    ts: int,
    value: float,
    labels: Dict[str, str],
) -> None:
    try:
        redis.execute_command("TS.ADD", key, ts, value, "ON_DUPLICATE", "LAST")
    except ResponseError as exc:
        msg = str(exc).lower()
        if "key does not exist" not in msg and "tsdb: the key does not exist" not in msg:
            raise
        redis.execute_command(
            "TS.CREATE",
            key,
            "RETENTION", 604800000,       # 7 dias em ms
            "DUPLICATE_POLICY", "LAST",
            "LABELS",
            *sum(([k, v] for k, v in labels.items()), []),
        )
        redis.execute_command("TS.ADD", key, ts, value, "ON_DUPLICATE", "LAST")


# ---------------------------------------------------------------------------
# Handlers por coleção
# ---------------------------------------------------------------------------

def handle_evento_preco(redis: Redis, doc: Dict[str, Any]) -> None:
    """
    Atualiza:
      - Hash com preço atual por posto/combustível
      - Ranking de menor preço por combustível (Sorted Set)
      - Ranking de maior variação recente (Sorted Set, valor absoluto)
      - Time Series de preço por posto/combustível
    """
    posto_id    = str(doc["posto_id"])   # ObjectId → string
    combustivel = doc["combustivel"]
    preco_novo  = float(doc["preco_novo"])
    variacao    = float(doc.get("variacao_pct", 0.0))
    ocorrido_em = doc.get("ocorrido_em")

    # --- Hash com preço atual ---
    hash_key = preco_hash_key(posto_id, combustivel)
    redis.hset(hash_key, mapping={
        "posto_id":       posto_id,
        "combustivel":    combustivel,
        "preco_atual":    preco_novo,
        "preco_anterior": doc.get("preco_anterior", preco_novo),
        "variacao_pct":   variacao,
        "fonte":          doc.get("fonte", ""),
        "atualizado_em":  str(ocorrido_em),
    })

    # --- Ranking de menor preço por combustível ---
    # Sorted Set: score = preco_novo (menor score = mais barato = melhor posição)
    ranking_preco_key = f"ranking:preco:{combustivel}"
    redis.zadd(ranking_preco_key, {posto_id: preco_novo})
    print(f"[PRECO] {combustivel} | posto {posto_id} | R$ {preco_novo:.3f}")

    # --- Ranking de maior variação absoluta (últimas atualizações) ---
    variacao_abs = abs(variacao)
    redis.zadd("ranking:variacao_preco", {f"{posto_id}:{combustivel}": variacao_abs})
    print(f"[PRECO] variação {variacao:+.2f}% | {combustivel} | posto {posto_id}")

    # --- Time Series ---
    if ocorrido_em:
        ts_ms = int(ocorrido_em.timestamp() * 1000)
        ensure_ts_add(
            redis,
            ts_key_preco(posto_id, combustivel),
            ts_ms,
            preco_novo,
            {"posto_id": posto_id, "combustivel": combustivel, "metric": "preco"},
        )


def handle_busca(redis: Redis, doc: Dict[str, Any]) -> None:
    """
    Atualiza:
      - Ranking de combustíveis mais buscados (Sorted Set)
      - Ranking de cidades com mais buscas (Sorted Set)
      - Ranking de estados com mais buscas (Sorted Set)
      - Contador de buscas sem resultado (quando resultado_count == 0)
    """
    combustivel = doc.get("tipo_combustivel", "")
    cidade      = doc.get("cidade", "")
    estado      = doc.get("estado", "")
    resultado   = int(doc.get("resultado_count", 0))

    if combustivel:
        score = redis.zincrby("ranking:combustiveis:buscas", 1, combustivel)
        print(f"[BUSCA] combustível: {combustivel} → {int(float(score))} buscas")

    if cidade:
        redis.zincrby("ranking:cidades:buscas", 1, cidade)

    if estado:
        redis.zincrby("ranking:estados:buscas", 1, estado)

    # Buscas sem resultado indicam demanda não atendida — útil para o negócio
    if resultado == 0 and combustivel:
        redis.zincrby("ranking:combustiveis:sem_resultado", 1, combustivel)


def handle_avaliacao_interacao(redis: Redis, doc: Dict[str, Any]) -> None:
    """
    Atualiza:
      - Média de avaliação por posto (Hash)
      - Ranking de postos mais bem avaliados (Sorted Set)
      - Ranking de postos mais favoritados (Sorted Set)
      - Ranking de postos com mais check-ins (Sorted Set)
      - Contador de denúncias por posto
    """
    posto_id = str(doc["posto_id"])
    tipo     = doc.get("tipo", "")
    hash_key = posto_hash_key(posto_id)

    if tipo == "avaliacao":
        nota = doc.get("nota")
        if nota is not None:
            redis.hincrbyfloat(hash_key, "rating_sum", float(nota))
            redis.hincrby(hash_key, "rating_count", 1)

            rating_sum   = float(redis.hget(hash_key, "rating_sum")   or 0.0)
            rating_count = int(redis.hget(hash_key, "rating_count")   or 1)
            avg = round(rating_sum / max(rating_count, 1), 2)
            redis.hset(hash_key, "stars", avg)

            # Ranking de melhores avaliações
            redis.zadd("ranking:postos:avaliacao", {posto_id: avg})
            print(f"[AVAL] posto {posto_id} | média: {avg} ({rating_count} avaliações)")

    elif tipo == "favorito":
        score = redis.zincrby("ranking:postos:favoritos", 1, posto_id)
        redis.hincrby(hash_key, "favoritos", 1)
        print(f"[FAV] posto {posto_id} → {int(float(score))} favoritos")

    elif tipo == "check_in":
        score = redis.zincrby("ranking:postos:checkins", 1, posto_id)
        redis.hincrby(hash_key, "checkins", 1)
        print(f"[CHECKIN] posto {posto_id} → {int(float(score))} check-ins")

    elif tipo == "denuncia":
        redis.hincrby(hash_key, "denuncias", 1)
        print(f"[DENUNCIA] posto {posto_id}")


# ---------------------------------------------------------------------------
# Backfill — processa documentos já existentes no MongoDB
# ---------------------------------------------------------------------------

def backfill(db, redis: Redis, limit: int = 50_000) -> None:
    handlers = {
        "eventos_preco":          handle_evento_preco,
        "buscas_usuarios":        handle_busca,
        "avaliacoes_interacoes":  handle_avaliacao_interacao,
    }
    for col_name, handler in handlers.items():
        print(f"[BACKFILL] Processando {col_name}...")
        count = 0
        for doc in db[col_name].find({}).limit(limit):
            try:
                handler(redis, doc)
                count += 1
            except Exception as exc:
                print(f"[BACKFILL] Erro em {col_name}: {exc}")
        print(f"[BACKFILL] {col_name}: {count} documentos processados.")


# ---------------------------------------------------------------------------
# Watch — escuta Change Stream de uma coleção em uma thread separada
# ---------------------------------------------------------------------------

def watch_collection(db, redis: Redis, col_name: str, handler) -> None:
    print(f"[STREAM] Iniciando watch em '{col_name}'...")
    col = db[col_name]
    while True:
        try:
            pipeline = [{"$match": {"operationType": "insert"}}]
            with col.watch(pipeline, full_document="updateLookup") as stream:
                for change in stream:
                    try:
                        handler(redis, change["fullDocument"])
                    except Exception as exc:
                        print(f"[STREAM] Erro ao processar evento de {col_name}: {exc}")
        except Exception as exc:
            print(f"[STREAM] Reconectando '{col_name}' após erro: {exc}")
            time.sleep(2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Consumer MongoDB Change Stream → Redis | Radar Combustível"
    )
    parser.add_argument(
        "--skip-backfill",
        action="store_true",
        help="Pula o processamento de documentos já existentes.",
    )
    args = parser.parse_args()

    print(f"[CONSUMER] Conectando ao MongoDB: {MONGO_URI}")
    print(f"[CONSUMER] Conectando ao Redis:   {REDIS_HOST}:{REDIS_PORT}")

    mongo = MongoClient(MONGO_URI)
    redis = Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    db    = mongo[DB_NAME]

    # Verifica conexões
    mongo.admin.command("ping")
    redis.ping()
    print("[CONSUMER] Conexões OK.")

    if not args.skip_backfill:
        backfill(db, redis)

    # Uma thread por coleção monitorada
    collections_to_watch = {
        "eventos_preco":         handle_evento_preco,
        "buscas_usuarios":       handle_busca,
        "avaliacoes_interacoes": handle_avaliacao_interacao,
    }

    threads = []
    for col_name, handler in collections_to_watch.items():
        t = threading.Thread(
            target=watch_collection,
            args=(db, redis, col_name, handler),
            daemon=True,
            name=f"watch-{col_name}",
        )
        t.start()
        threads.append(t)

    print("[CONSUMER] Aguardando eventos em tempo real... (Ctrl+C para encerrar)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[CONSUMER] Encerrando.")


if __name__ == "__main__":
    main()