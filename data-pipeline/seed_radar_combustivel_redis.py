"""
Seed Redis — Plataforma Radar Combustível
=========================================
Carrega snapshots do MongoDB, popula hashes no Redis e cria
índices RediSearch prontos para leitura rápida.

Estruturas criadas:
  Hashes:
    posto:{posto_id}          → dados cadastrais + métricas agregadas
    preco:{posto_id}:{comb}   → preço atual por combustível por posto

  Time Series:
    ts:preco:{posto_id}:{comb} → evolução de preço (criadas vazias, preenchidas pelo consumer)

  Índices RediSearch:
    idx:postos                → busca por nome, bandeira, cidade, estado, avaliação, geo
    idx:precos                → busca por combustível e faixa de preço

Uso:
  1) docker compose up -d
  2) python seed_radar_combustivel.py   (popula o MongoDB)
  3) python seed_redis_radar_combustivel.py  (este script)

Variáveis de ambiente:
  MONGO_URI   (default: mongodb://localhost:27017/?directConnection=true)
  DB_NAME     (default: radar_combustivel)
  REDIS_HOST  (default: localhost)
  REDIS_PORT  (default: 6379)
"""

from __future__ import annotations

import os
import sys
from typing import Dict

from dotenv import load_dotenv
from pymongo import MongoClient
from redis import Redis
from redis.commands.search.field import (
    GeoField,
    NumericField,
    TagField,
    TextField,
)
from redis.commands.search.index_definition import IndexDefinition, IndexType

load_dotenv(".env.local")
load_dotenv()

MONGO_URI  = os.getenv("MONGO_URI",  "mongodb://localhost:27017/?directConnection=true")
DB_NAME    = os.getenv("DB_NAME",    "radar_combustivel")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))


# ---------------------------------------------------------------------------
# Snapshots MongoDB (aggregation)
# ---------------------------------------------------------------------------

def load_posto_snapshot() -> Dict[str, dict]:
    """
    Snapshot de cada posto com:
      - dados cadastrais (nome, bandeira, cidade, estado)
      - último preço por combustível (do evento mais recente)
      - média de avaliação
      - contagem de favoritos e check-ins
      - coordenadas geográficas
    """
    mongo = MongoClient(MONGO_URI)
    db    = mongo[DB_NAME]

    # --- Dados cadastrais dos postos ---
    postos = {
        str(p["_id"]): p
        for p in db.postos.find({}, {
            "cnpj": 1, "nome_fantasia": 1, "bandeira": 1,
            "endereco": 1, "location": 1, "ativo": 1,
        })
    }

    # --- Último preço por posto/combustível ---
    pipeline_preco = [
        {"$sort": {"ocorrido_em": -1}},
        {
            "$group": {
                "_id": {"posto_id": "$posto_id", "combustivel": "$combustivel"},
                "preco_atual":    {"$first": "$preco_novo"},
                "variacao_pct":   {"$first": "$variacao_pct"},
                "ocorrido_em":    {"$first": "$ocorrido_em"},
            }
        },
    ]
    precos_por_posto: Dict[str, dict] = {}
    for row in db.eventos_preco.aggregate(pipeline_preco):
        posto_id    = str(row["_id"]["posto_id"])
        combustivel = row["_id"]["combustivel"]
        precos_por_posto.setdefault(posto_id, {})[combustivel] = {
            "preco_atual":  row["preco_atual"],
            "variacao_pct": row["variacao_pct"],
        }

    # --- Média de avaliação por posto ---
    pipeline_aval = [
        {"$match": {"tipo": "avaliacao", "nota": {"$ne": None}}},
        {
            "$group": {
                "_id":          "$posto_id",
                "rating_avg":   {"$avg": "$nota"},
                "rating_count": {"$sum": 1},
            }
        },
    ]
    avaliacoes = {
        str(row["_id"]): {
            "rating_avg":   round(row["rating_avg"], 2),
            "rating_count": row["rating_count"],
        }
        for row in db.avaliacoes_interacoes.aggregate(pipeline_aval)
    }

    # --- Contagem de favoritos e check-ins ---
    pipeline_inter = [
        {"$match": {"tipo": {"$in": ["favorito", "check_in"]}}},
        {
            "$group": {
                "_id":      "$posto_id",
                "favoritos": {"$sum": {"$cond": [{"$eq": ["$tipo", "favorito"]},  1, 0]}},
                "checkins":  {"$sum": {"$cond": [{"$eq": ["$tipo", "check_in"]},  1, 0]}},
            }
        },
    ]
    interacoes = {
        str(row["_id"]): {"favoritos": row["favoritos"], "checkins": row["checkins"]}
        for row in db.avaliacoes_interacoes.aggregate(pipeline_inter)
    }

    # --- Localização (geo) por posto ---
    localizacoes = {
        str(loc["posto_id"]): loc
        for loc in db.localizacoes_postos.find({}, {"posto_id": 1, "bairro": 1, "municipio": 1, "uf": 1})
    }

    # --- Monta snapshot consolidado ---
    snapshot = {}
    for posto_id, posto in postos.items():
        endereco = posto.get("endereco", {})
        geo      = posto.get("location", {})
        coords   = geo.get("coordinates", [0, 0])   # [lng, lat]
        loc      = localizacoes.get(posto_id, {})
        aval     = avaliacoes.get(posto_id, {"rating_avg": 0.0, "rating_count": 0})
        inter    = interacoes.get(posto_id, {"favoritos": 0, "checkins": 0})

        snapshot[posto_id] = {
            "posto_id":     posto_id,
            "cnpj":         posto.get("cnpj", ""),
            "nome_fantasia": posto.get("nome_fantasia", ""),
            "bandeira":     posto.get("bandeira", ""),
            "cidade":       endereco.get("cidade", loc.get("municipio", "")),
            "estado":       endereco.get("estado", loc.get("uf", "")),
            "bairro":       endereco.get("bairro", loc.get("bairro", "")),
            "cep":          endereco.get("cep", ""),
            "ativo":        1 if posto.get("ativo", True) else 0,
            "lng":          coords[0],
            "lat":          coords[1],
            "rating_avg":   aval["rating_avg"],
            "rating_count": aval["rating_count"],
            "favoritos":    inter["favoritos"],
            "checkins":     inter["checkins"],
            "precos":       precos_por_posto.get(posto_id, {}),
        }

    mongo.close()
    return snapshot


def load_preco_snapshot() -> list[dict]:
    """
    Retorna lista com o preço mais recente de cada combinação posto/combustível.
    Usado para popular os hashes preco:{posto_id}:{combustivel}.
    """
    mongo = MongoClient(MONGO_URI)
    db    = mongo[DB_NAME]
    pipeline = [
        {"$sort": {"ocorrido_em": -1}},
        {
            "$group": {
                "_id": {"posto_id": "$posto_id", "combustivel": "$combustivel"},
                "preco_atual":    {"$first": "$preco_novo"},
                "preco_anterior": {"$first": "$preco_anterior"},
                "variacao_pct":   {"$first": "$variacao_pct"},
                "fonte":          {"$first": "$fonte"},
                "ocorrido_em":    {"$first": "$ocorrido_em"},
            }
        },
    ]
    rows = list(db.eventos_preco.aggregate(pipeline))
    mongo.close()
    return rows


# ---------------------------------------------------------------------------
# Seed Redis
# ---------------------------------------------------------------------------

def seed_posto_hashes(redis: Redis, snapshot: Dict[str, dict]) -> None:
    """Popula hashes posto:{posto_id} com dados cadastrais e métricas."""
    print(f"[REDIS] Populando {len(snapshot)} hashes de postos...")
    pipe = redis.pipeline(transaction=False)
    for posto_id, data in snapshot.items():
        key = f"posto:{posto_id}"
        pipe.hset(key, mapping={
            "posto_id":     data["posto_id"],
            "nome_fantasia": data["nome_fantasia"],
            "bandeira":     data["bandeira"],
            "cidade":       data["cidade"],
            "estado":       data["estado"],
            "bairro":       data["bairro"],
            "cep":          data["cep"],
            "ativo":        data["ativo"],
            "location":     f"{data['lng']},{data['lat']}",   # formato GEO Redis: lng,lat
            "rating_avg":   data["rating_avg"],
            "rating_count": data["rating_count"],
            "favoritos":    data["favoritos"],
            "checkins":     data["checkins"],
        })
    pipe.execute()
    print(f"[REDIS] Hashes de postos criados.")


def seed_preco_hashes(redis: Redis, rows: list[dict]) -> None:
    """Popula hashes preco:{posto_id}:{combustivel} com preço atual."""
    print(f"[REDIS] Populando {len(rows)} hashes de preços...")
    pipe = redis.pipeline(transaction=False)
    for row in rows:
        posto_id    = str(row["_id"]["posto_id"])
        combustivel = row["_id"]["combustivel"]
        key = f"preco:{posto_id}:{combustivel}"
        pipe.hset(key, mapping={
            "posto_id":       posto_id,
            "combustivel":    combustivel,
            "preco_atual":    float(row["preco_atual"]),
            "preco_anterior": float(row.get("preco_anterior") or row["preco_atual"]),
            "variacao_pct":   float(row.get("variacao_pct") or 0.0),
            "fonte":          row.get("fonte", ""),
            "atualizado_em":  str(row.get("ocorrido_em", "")),
        })
        # Sorted Set: menor preço por combustível
        pipe.zadd(f"ranking:preco:{combustivel}", {posto_id: float(row["preco_atual"])})
    pipe.execute()
    print(f"[REDIS] Hashes de preços criados.")


def seed_time_series(redis: Redis, snapshot: Dict[str, dict]) -> None:
    """Cria Time Series vazias para cada posto/combustível (preenchidas pelo consumer)."""
    COMBUSTIVEIS = (
        "GASOLINA_COMUM", "GASOLINA_ADITIVADA", "ETANOL",
        "DIESEL_S10", "DIESEL_COMUM", "GNV",
    )
    print(f"[REDIS] Criando Time Series...")
    count = 0
    for posto_id in snapshot:
        for comb in COMBUSTIVEIS:
            ts_key = f"ts:preco:{posto_id}:{comb}"
            try:
                redis.execute_command(
                    "TS.CREATE", ts_key,
                    "RETENTION", 604800000,     # 7 dias em ms
                    "DUPLICATE_POLICY", "LAST",
                    "LABELS",
                    "posto_id",    posto_id,
                    "combustivel", comb,
                    "metric",      "preco",
                )
                count += 1
            except Exception:
                pass   # já existe — ignora
    print(f"[REDIS] {count} Time Series criadas.")


# ---------------------------------------------------------------------------
# Índices RediSearch
# ---------------------------------------------------------------------------

def create_indexes(redis: Redis) -> None:
    """Cria (ou recria) os índices RediSearch para postos e preços."""

    # --- idx:postos ---
    try:
        redis.execute_command("FT.DROPINDEX", "idx:postos", "DD")
    except Exception:
        pass

    redis.ft("idx:postos").create_index(
        fields=[
            TextField("nome_fantasia", weight=2.0),
            TagField("bandeira"),
            TagField("cidade"),
            TagField("estado"),
            TagField("bairro"),
            NumericField("rating_avg",   sortable=True),
            NumericField("rating_count", sortable=True),
            NumericField("favoritos",    sortable=True),
            NumericField("checkins",     sortable=True),
            NumericField("ativo",        sortable=True),
            GeoField("location"),
        ],
        definition=IndexDefinition(prefix=["posto:"], index_type=IndexType.HASH),
    )
    print("[REDIS] Índice idx:postos criado.")

    # --- idx:precos ---
    try:
        redis.execute_command("FT.DROPINDEX", "idx:precos", "DD")
    except Exception:
        pass

    redis.ft("idx:precos").create_index(
        fields=[
            TagField("combustivel"),
            TagField("posto_id"),
            NumericField("preco_atual",  sortable=True),
            NumericField("variacao_pct", sortable=True),
            TagField("fonte"),
        ],
        definition=IndexDefinition(prefix=["preco:"], index_type=IndexType.HASH),
    )
    print("[REDIS] Índice idx:precos criado.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"[SEED] Conectando ao MongoDB: {MONGO_URI}")
    print(f"[SEED] Conectando ao Redis:   {REDIS_HOST}:{REDIS_PORT}")

    redis = Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    redis.ping()
    print("[SEED] Conexões OK.")

    print("[SEED] Carregando snapshot de postos do MongoDB...")
    snapshot = load_posto_snapshot()
    print(f"[SEED] {len(snapshot)} postos carregados.")

    print("[SEED] Carregando snapshot de preços do MongoDB...")
    preco_rows = load_preco_snapshot()
    print(f"[SEED] {len(preco_rows)} combinações posto/combustível carregadas.")

    seed_posto_hashes(redis, snapshot)
    seed_preco_hashes(redis, preco_rows)
    seed_time_series(redis, snapshot)
    create_indexes(redis)

    print(f"\n[SEED] Concluído!")
    print(f"  postos populados : {len(snapshot)}")
    print(f"  hashes de preço  : {len(preco_rows)}")
    print(f"  índices criados  : idx:postos, idx:precos")
    return 0


if __name__ == "__main__":
    sys.exit(main())