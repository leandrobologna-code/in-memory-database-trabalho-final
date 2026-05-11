"""
Seed MongoDB — Plataforma Radar Combustível
==========================================
Coleções (conforme escopo do trabalho final):
  - postos: cadastro de postos com endereço e geo (GeoJSON Point).
  - eventos_preco: eventos de atualização de preço por posto/combustível.
  - buscas_usuarios: buscas e filtros utilizados pelos usuários.
  - avaliacoes_interacoes: avaliações, favoritos e outras interações.
  - localizacoes_postos: documento de localização indexável (geo + IBGE).

Uso:
  1) docker compose up -d
  2) pip install -r requirements.txt
  3) python seed_radar_combustivel.py

Variáveis de ambiente (opcional):
  MONGO_URI   (default: mongodb://localhost:27017/?directConnection=true)
  DB_NAME     (default: radar_combustivel)
  SEED        (default: 42)
  BATCH_SIZE  (default: 5000)
  N           (default: 100000) — registros por coleção
"""

from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, List, Sequence

from bson import ObjectId
from faker import Faker
from pymongo import ASCENDING, GEOSPHERE, MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

# ---------------------------------------------------------------------------
# Constantes de domínio — Radar Combustível (Brasil)
# ---------------------------------------------------------------------------

COMBUSTIVEIS = (
    "GASOLINA_COMUM",
    "GASOLINA_ADITIVADA",
    "ETANOL",
    "DIESEL_S10",
    "DIESEL_COMUM",
    "GNV",
)

BANDEIRAS = (
    "Ipiranga",
    "Shell",
    "BR",
    "Raízen",
    "Ale",
    "Boxter",
    "Petrobras",
    "Rede independente",
)

UFS = (
    "SP",
    "RJ",
    "MG",
    "PR",
    "RS",
    "BA",
    "PE",
    "CE",
    "DF",
    "GO",
)

TIPOS_INTERACAO = ("avaliacao", "favorito", "compartilhamento", "denuncia", "check_in")

# Approx. bounding box Brasil (lng, lat) para pontos plausíveis
BR_LNG_RANGE = (-73.5, -34.8)
BR_LAT_RANGE = (-33.8, 5.3)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def chunked(seq: Sequence[Any], size: int) -> Iterable[List[Any]]:
    for i in range(0, len(seq), size):
        yield list(seq[i : i + size])


def make_fake_geo(fake: Faker) -> dict[str, Any]:
    lng = random.uniform(*BR_LNG_RANGE)
    lat = random.uniform(*BR_LAT_RANGE)
    return {"type": "Point", "coordinates": [round(lng, 6), round(lat, 6)]}


def cnpj_like(fake: Faker) -> str:
    digits = "".join(str(random.randint(0, 9)) for _ in range(14))
    return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:14]}"


# ---------------------------------------------------------------------------
# Estruturas de documento (MongoDB) — uma função por entidade
# ---------------------------------------------------------------------------


def doc_posto(fake: Faker, oid: ObjectId) -> dict[str, Any]:
    cidade = fake.city()
    estado = random.choice(UFS)
    geo = make_fake_geo(fake)
    return {
        "_id": oid,
        "cnpj": cnpj_like(fake),
        "nome_fantasia": f"Posto {fake.company()}",
        "bandeira": random.choice(BANDEIRAS),
        "endereco": {
            "logradouro": fake.street_name(),
            "numero": str(random.randint(1, 9999)),
            "bairro": fake.bairro() if hasattr(fake, "bairro") else fake.city_suffix(),
            "cep": fake.postcode(),
            "cidade": cidade,
            "estado": estado,
        },
        "telefone": fake.phone_number()[:20],
        "ativo": random.random() > 0.03,
        "location": geo,
        "created_at": fake.date_time_between(start_date="-5y", end_date="now", tzinfo=timezone.utc),
        "updated_at": utc_now(),
    }


def doc_evento_preco(
    fake: Faker,
    posto_ids: Sequence[ObjectId],
) -> dict[str, Any]:
    posto_id = random.choice(posto_ids)
    comb = random.choice(COMBUSTIVEIS)
    preco_novo = round(random.uniform(4.5, 8.9), 3)
    preco_ant = round(max(3.0, preco_novo + random.uniform(-0.8, 0.8)), 3)
    ocorrido = fake.date_time_between(start_date="-90d", end_date="now", tzinfo=timezone.utc)
    return {
        "_id": ObjectId(),
        "posto_id": posto_id,
        "combustivel": comb,
        "preco_anterior": preco_ant,
        "preco_novo": preco_novo,
        "variacao_pct": round((preco_novo - preco_ant) / preco_ant * 100, 4) if preco_ant else 0.0,
        "unidade": "BRL_L",
        "fonte": random.choice(("app_usuario", "api_anp", "operador_posto", "crawler")),
        "ocorrido_em": ocorrido,
        "revisado": random.random() > 0.15,
    }


def doc_busca(fake: Faker) -> dict[str, Any]:
    return {
        "_id": ObjectId(),
        "usuario_id": fake.uuid4(),
        "session_id": fake.uuid4(),
        "tipo_combustivel": random.choice(COMBUSTIVEIS),
        "cidade": fake.city(),
        "estado": random.choice(UFS),
        "raio_km": random.choice((1, 2, 3, 5, 10, 15)),
        "filtros": {
            "apenas_abertos": random.random() > 0.5,
            "ordenacao": random.choice(("preco", "distancia", "avaliacao")),
        },
        "geo_centro": make_fake_geo(fake),
        "consultado_em": fake.date_time_between(start_date="-180d", end_date="now", tzinfo=timezone.utc),
        "resultado_count": random.randint(0, 120),
        "latencia_ms": random.randint(8, 450),
    }


def doc_avaliacao_interacao(fake: Faker, posto_ids: Sequence[ObjectId]) -> dict[str, Any]:
    tipo = random.choice(TIPOS_INTERACAO)
    nota = random.randint(1, 5) if tipo == "avaliacao" else None
    return {
        "_id": ObjectId(),
        "posto_id": random.choice(posto_ids),
        "usuario_id": fake.uuid4(),
        "tipo": tipo,
        "nota": nota,
        "comentario": fake.text(max_nb_chars=180) if tipo == "avaliacao" and random.random() > 0.4 else None,
        "created_at": fake.date_time_between(start_date="-2y", end_date="now", tzinfo=timezone.utc),
        "util_count": random.randint(0, 42) if tipo == "avaliacao" else 0,
    }


def doc_localizacao_posto(
    fake: Faker,
    posto_id: ObjectId,
) -> dict[str, Any]:
    geo = make_fake_geo(fake)
    return {
        "_id": ObjectId(),
        "posto_id": posto_id,
        "municipio": fake.city(),
        "bairro": fake.bairro() if hasattr(fake, "bairro") else f"Bairro {random.randint(1, 200)}",
        "uf": random.choice(UFS),
        "codigo_ibge": str(random.randint(1100000, 5300000)),
        "geo": geo,
        "atualizado_em": utc_now() - timedelta(days=random.randint(0, 30)),
    }


# ---------------------------------------------------------------------------
# Índices sugeridos (consultas e pipeline)
# ---------------------------------------------------------------------------


def ensure_indexes(db) -> None:
    db.postos.create_index([("location", GEOSPHERE)])
    db.postos.create_index([("endereco.estado", ASCENDING), ("endereco.cidade", ASCENDING)])
    db.eventos_preco.create_index([("posto_id", ASCENDING), ("ocorrido_em", ASCENDING)])
    db.eventos_preco.create_index([("combustivel", ASCENDING), ("ocorrido_em", ASCENDING)])
    db.buscas_usuarios.create_index([("consultado_em", ASCENDING)])
    db.buscas_usuarios.create_index([("estado", ASCENDING), ("cidade", ASCENDING)])
    db.avaliacoes_interacoes.create_index([("posto_id", ASCENDING), ("created_at", ASCENDING)])
    db.localizacoes_postos.create_index([("posto_id", ASCENDING)], unique=True)
    db.localizacoes_postos.create_index([("geo", GEOSPHERE)])


def insert_batches(col: Collection, docs: List[dict[str, Any]], batch_size: int) -> int:
    n = 0
    for batch in chunked(docs, batch_size):
        col.insert_many(batch, ordered=False)
        n += len(batch)
        print(f"  {col.name}: {n} inseridos...", flush=True)
    return n


def main() -> int:
    mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017/?directConnection=true")
    db_name = os.environ.get("DB_NAME", "radar_combustivel")
    seed = int(os.environ.get("SEED", "42"))
    batch_size = int(os.environ.get("BATCH_SIZE", "5000"))
    n_target = int(os.environ.get("N", "100000"))

    random.seed(seed)
    Faker.seed(seed)
    fake = Faker("pt_BR")

    print(f"Conectando: {mongo_uri} / {db_name}")
    print(f"Alvo: {n_target} documentos por coleção (5 coleções). SEED={seed}")

    try:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=15000)
        client.admin.command("ping")
    except PyMongoError as e:
        print(f"Erro ao conectar ao MongoDB: {e}", file=sys.stderr)
        return 1

    db = client[db_name]

    # Limpa coleções para reexecução idempotente do seed
    for name in (
        "postos",
        "eventos_preco",
        "buscas_usuarios",
        "avaliacoes_interacoes",
        "localizacoes_postos",
    ):
        db[name].drop()

    print("Gerando IDs de postos e documentos...")
    posto_ids = [ObjectId() for _ in range(n_target)]

    postos = [doc_posto(fake, oid) for oid in posto_ids]
    localizacoes = [doc_localizacao_posto(fake, pid) for pid in posto_ids]

    print("Inserindo postos...")
    insert_batches(db.postos, postos, batch_size)
    postos.clear()

    print("Inserindo localizacoes_postos (1:1 com postos)...")
    insert_batches(db.localizacoes_postos, localizacoes, batch_size)
    localizacoes.clear()

    print("Gerando e inserindo eventos_preco...")
    eventos = [doc_evento_preco(fake, posto_ids) for _ in range(n_target)]
    insert_batches(db.eventos_preco, eventos, batch_size)
    eventos.clear()

    print("Gerando e inserindo buscas_usuarios...")
    buscas = [doc_busca(fake) for _ in range(n_target)]
    insert_batches(db.buscas_usuarios, buscas, batch_size)
    buscas.clear()

    print("Gerando e inserindo avaliacoes_interacoes...")
    avaliacoes = [doc_avaliacao_interacao(fake, posto_ids) for _ in range(n_target)]
    insert_batches(db.avaliacoes_interacoes, avaliacoes, batch_size)
    avaliacoes.clear()

    print("Criando índices...")
    ensure_indexes(db)

    total = sum(db[c].estimated_document_count() for c in (
        "postos",
        "eventos_preco",
        "buscas_usuarios",
        "avaliacoes_interacoes",
        "localizacoes_postos",
    ))
    print(f"Concluído. Total aproximado de documentos: {total}")
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
