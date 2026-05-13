# Radar Combustível — Trabalho Final: In-Memory Database

> Trabalho final da disciplina **In-Memory Database** do MBA em Engenharia de Dados — Turma 29AND  
> **Alunos:** David, Felipe, Leandro e Pablo

---

## Sobre o Projeto

Este projeto simula um sistema de monitoramento de preços de combustíveis em postos brasileiros ("Radar Combustível"). A arquitetura demonstra o uso combinado de bancos de dados **in-memory** (Redis) e orientado a documentos (MongoDB), com geração de dados sintéticos, pipeline de ingestão via Redis Streams e um dashboard interativo em Streamlit.

### Fluxo da Arquitetura

```
data-generator  →  MongoDB  →  data-pipeline (producer Redis)  →  Redis Streams
                                                                        ↓
                                                              consumer → MongoDB
                                                                        ↓
                                                              streamlit-app (dashboard)
```

---

## Estrutura do Repositório

```
.
├── data-generator/
│   └── seed_radar_combustivel.py      # Gera e insere dados sintéticos no MongoDB
├── data-pipeline/
│   ├── seed_radar_combustivel_redis.py  # Publica dados do MongoDB no Redis Stream
│   └── consumer_radar_combustivel_mongodb.py  # Consome o stream e persiste no MongoDB
├── streamlit-app/
│   └── dashboard.py                   # Dashboard interativo com Streamlit + Plotly
├── docker-compose.yml                 # Orquestração de todos os serviços
├── requirements.txt                   # Dependências Python
└── .env.local                         # Variáveis de ambiente para execução local
```

---

## Tecnologias e Dependências

### Infraestrutura (via Docker)

| Serviço | Imagem | Porta |
|---|---|---|
| MongoDB 7 (Replica Set) | `mongo:7` | `27017` |
| Redis Stack Server | `redis/redis-stack-server:latest` | `6379` / `8001` |
| Streamlit Dashboard | `python:3.11-slim` | `8501` |

### Python (requirements.txt)

| Pacote | Finalidade |
|---|---|
| `pymongo >= 4.6.0` | Conexão e operações com MongoDB |
| `redis >= 5.0.0` | Conexão com Redis (Streams, cache) |
| `Faker >= 24.0.0` | Geração de dados sintéticos |
| `python-dotenv >= 1.0.0` | Leitura de variáveis de ambiente |
| `streamlit` | Interface do dashboard |
| `plotly` | Gráficos interativos no dashboard |
| `pandas` | Manipulação de dados |

### Pré-requisitos do ambiente

- [Docker](https://docs.docker.com/get-docker/) e [Docker Compose](https://docs.docker.com/compose/install/) instalados
- Portas `27017`, `6379`, `8001` e `8501` disponíveis na máquina

---

## Variáveis de Ambiente

O arquivo `.env.local` contém as configurações padrão para execução local. **Não é necessário alterá-lo para rodar via Docker Compose**, pois os serviços já recebem as variáveis configuradas no `docker-compose.yml`.

```env
MONGO_URI=mongodb://localhost:27017/?directConnection=true
MONGO_DB=radar_combustivel
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=
```

> Para execução local (fora do Docker), aponte `MONGO_URI` e `REDIS_HOST` para os endereços corretos dos serviços.

---

## Como Executar

### 1. Clone o repositório

```bash
git clone https://github.com/leandrobologna-code/in-memory-database-trabalho-final.git
cd in-memory-database-trabalho-final
```

### 2. Suba todos os serviços com Docker Compose

O comando abaixo inicializa o MongoDB (com Replica Set), o Redis, popula os dados e sobe o dashboard — tudo em sequência, respeitando as dependências entre os serviços:

```bash
docker compose up
```

O Docker Compose executará automaticamente os seguintes passos na ordem correta:

1. **`mongo`** — Sobe o MongoDB com Replica Set habilitado
2. **`mongo-init`** — Inicializa o Replica Set (`rs0`)
3. **`redis`** — Sobe o Redis Stack Server
4. **`seed`** — Gera dados sintéticos e insere no MongoDB
5. **`seed-redis`** — Lê os dados do MongoDB e publica no Redis Stream
6. **`consumer`** — Consome o Redis Stream e persiste os dados processados no MongoDB
7. **`dashboard`** — Sobe o Streamlit na porta `8501`

### 3. Acesse o dashboard

Após todos os serviços subirem, abra no navegador:

```
http://localhost:8501
```

### 4. (Opcional) Interface do Redis

O Redis Stack Server expõe o **RedisInsight** na porta `8001`:

```
http://localhost:8001
```

### 5. Encerrar os serviços

```bash
docker compose down
```

Para remover também os volumes de dados persistidos:

```bash
docker compose down -v
```

---

## Executando Localmente (sem Docker)

Caso prefira rodar os scripts Python diretamente, com MongoDB e Redis já instalados na máquina:

```bash
# Instale as dependências
pip install -r requirements.txt

# 1. Gere os dados no MongoDB
python data-generator/seed_radar_combustivel.py

# 2. Publique os dados no Redis Stream
python data-pipeline/seed_radar_combustivel_redis.py

# 3. Consuma o stream (pode rodar em paralelo com o passo anterior)
python data-pipeline/consumer_radar_combustivel_mongodb.py

# 4. Suba o dashboard
streamlit run streamlit-app/dashboard.py
```

> Certifique-se de que as variáveis no `.env.local` estão apontando para os serviços corretos.

---

## Autores

| Nome | GitHub |
|---|---|
| Leandro Bologna | [@leandrobologna-code](https://github.com/leandrobologna-code) |
| David Leles | — |
| Felipe Oliveira | — |
| Pablo Dias | — |

---

*MBA em Engenharia de Dados — Turma 29ABD*