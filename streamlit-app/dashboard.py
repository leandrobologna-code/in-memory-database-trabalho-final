"""
Dashboard — Plataforma Radar Combustível
========================================
Visualização dos dados servidos pelo Redis, demonstrando o uso de:
  - Hashes       → cadastro e métricas de postos
  - Sorted Sets  → rankings de preço, buscas e popularidade
  - Geo          → consulta por proximidade
  - Time Series  → evolução de preços ao longo do tempo
  - RediSearch   → busca full-text e filtros

Uso:
  streamlit run dashboard.py

Variáveis de ambiente:
  REDIS_HOST  (default: localhost)
  REDIS_PORT  (default: 6379)
"""

import os
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from redis import Redis
from redis.exceptions import ResponseError
from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv()

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

COMBUSTIVEIS = [
    "GASOLINA_COMUM",
    "GASOLINA_ADITIVADA",
    "ETANOL",
    "DIESEL_S10",
    "DIESEL_COMUM",
    "GNV",
]

COMBUSTIVEL_LABEL = {
    "GASOLINA_COMUM":    "Gasolina Comum",
    "GASOLINA_ADITIVADA":"Gasolina Aditivada",
    "ETANOL":            "Etanol",
    "DIESEL_S10":        "Diesel S10",
    "DIESEL_COMUM":      "Diesel Comum",
    "GNV":               "GNV",
}

# ---------------------------------------------------------------------------
# Conexão Redis (cached)
# ---------------------------------------------------------------------------

@st.cache_resource
def get_redis() -> Redis:
    return Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


# ---------------------------------------------------------------------------
# Funções de leitura Redis
# ---------------------------------------------------------------------------

def ranking_preco(redis: Redis, combustivel: str, top: int = 20) -> pd.DataFrame:
    """Sorted Set → ranking de menor preço por combustível."""
    key = f"ranking:preco:{combustivel}"
    rows = redis.zrange(key, 0, top - 1, withscores=True)
    if not rows:
        return pd.DataFrame()
    records = []
    for posto_id, preco in rows:
        hash_key = f"preco:{posto_id}:{combustivel}"
        data = redis.hgetall(hash_key)
        posto = redis.hgetall(f"posto:{posto_id}")
        records.append({
            "posto_id":     posto_id,
            "nome":         posto.get("nome_fantasia") or "—",
            "bandeira":     posto.get("bandeira", "—"),
            "cidade":       posto.get("cidade", "—"),
            "estado":       posto.get("estado", "—"),
            "preco_atual":  float(preco),
            "variacao_pct": float(data.get("variacao_pct", 0)),
        })
    return pd.DataFrame(records)


def ranking_buscas(redis: Redis, top: int = 15) -> pd.DataFrame:
    """Sorted Set → combustíveis mais buscados."""
    rows = redis.zrevrange("ranking:combustiveis:buscas", 0, top - 1, withscores=True)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([
        {"combustivel": COMBUSTIVEL_LABEL.get(c, c), "buscas": int(s)}
        for c, s in rows
    ])


def ranking_cidades(redis: Redis, top: int = 15) -> pd.DataFrame:
    """Sorted Set → cidades com mais buscas."""
    rows = redis.zrevrange("ranking:cidades:buscas", 0, top - 1, withscores=True)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{"cidade": c, "buscas": int(s)} for c, s in rows])


def ranking_postos_avaliacao(redis: Redis, top: int = 15) -> pd.DataFrame:
    """Sorted Set → postos mais bem avaliados."""
    rows = redis.zrevrange("ranking:postos:avaliacao", 0, top - 1, withscores=True)
    if not rows:
        return pd.DataFrame()
    records = []
    for posto_id, avg in rows:
        posto = redis.hgetall(f"posto:{posto_id}")
        records.append({
            "nome":        posto.get("nome_fantasia", "—"),
            "bandeira":    posto.get("bandeira", "—"),
            "cidade":      posto.get("cidade", "—"),
            "avaliacao":   round(float(avg), 2),
            "favoritos":   int(posto.get("favoritos", 0)),
            "checkins":    int(posto.get("checkins", 0)),
        })
    return pd.DataFrame(records)


def ranking_variacao(redis: Redis, top: int = 15) -> pd.DataFrame:
    """Sorted Set → postos com maior variação de preço."""
    rows = redis.zrevrange("ranking:variacao_preco", 0, top - 1, withscores=True)
    if not rows:
        return pd.DataFrame()
    records = []
    for key, variacao in rows:
        parts = key.split(":")
        if len(parts) < 2:
            continue
        posto_id    = parts[0]
        combustivel = parts[1]
        posto = redis.hgetall(f"posto:{posto_id}")
        records.append({
            "nome":         posto.get("nome_fantasia", "—"),
            "combustivel":  COMBUSTIVEL_LABEL.get(combustivel, combustivel),
            "variacao_abs": round(float(variacao), 2),
        })
    return pd.DataFrame(records)


def postos_geo(redis: Redis, sample: int = 500) -> pd.DataFrame:
    """Hash → coordenadas de postos para mapa."""
    keys = redis.keys("posto:*")[:sample]
    if not keys:
        return pd.DataFrame()
    pipe = redis.pipeline(transaction=False)
    for k in keys:
        pipe.hgetall(k)
    results = pipe.execute()
    records = []
    for data in results:
        loc = data.get("location", "")
        if not loc or "," not in loc:
            continue
        try:
            lng, lat = map(float, loc.split(","))
            records.append({
                "nome":       data.get("nome_fantasia", "—"),
                "bandeira":   data.get("bandeira", "—"),
                "cidade":     data.get("cidade", "—"),
                "estado":     data.get("estado", "—"),
                "avaliacao":  float(data.get("rating_avg", 0)),
                "lat":        lat,
                "lon":        lng,
            })
        except ValueError:
            continue
    return pd.DataFrame(records)


def time_series_preco(redis: Redis, posto_id: str, combustivel: str) -> pd.DataFrame:
    """Time Series → evolução de preço de um posto/combustível."""
    key = f"ts:preco:{posto_id}:{combustivel}"
    try:
        rows = redis.execute_command("TS.RANGE", key, "-", "+")
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([
            {"timestamp": pd.to_datetime(int(ts), unit="ms"), "preco": float(val)}
            for ts, val in rows
        ])
    except ResponseError:
        return pd.DataFrame()


def busca_postos_redisearch(redis: Redis, estado: str, combustivel: str, top: int = 10) -> pd.DataFrame:
    """RediSearch → postos filtrados por estado + preço do combustível."""
    try:
        query = f"@estado:{{{estado}}}"
        res = redis.ft("idx:postos").search(
            query,
            query_params={"SORTBY": "rating_avg", "DESC": "", "LIMIT": f"0 {top}"}
        )
        records = []
        for doc in res.docs:
            posto_id = doc.id.replace("posto:", "")
            preco_hash = redis.hgetall(f"preco:{posto_id}:{combustivel}")
            records.append({
                "nome":        getattr(doc, "nome_fantasia", "—"),
                "bandeira":    getattr(doc, "bandeira", "—"),
                "cidade":      getattr(doc, "cidade", "—"),
                "avaliacao":   float(getattr(doc, "rating_avg", 0)),
                "preco_atual": float(preco_hash.get("preco_atual", 0)) if preco_hash else None,
            })
        return pd.DataFrame(records)
    except Exception:
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Layout Streamlit
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Radar Combustível",
    page_icon="⛽",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Sans:wght@400;500&display=swap');

    html, body, [class*="css"] {
        font-family: 'DM Sans', sans-serif;
    }
    h1, h2, h3 {
        font-family: 'Syne', sans-serif !important;
    }
    .stMetric {
        background: #0f172a;
        border: 1px solid #1e293b;
        border-radius: 12px;
        padding: 16px !important;
    }
    .stMetric label {
        color: #94a3b8 !important;
        font-size: 0.75rem !important;
        text-transform: uppercase;
        letter-spacing: 0.08em;
    }
    .stMetric [data-testid="stMetricValue"] {
        color: #f8fafc !important;
        font-family: 'Syne', sans-serif !important;
        font-size: 1.8rem !important;
    }
    .block-container { padding-top: 2rem; }
    .section-label {
        font-size: 0.7rem;
        font-weight: 600;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: #f97316;
        margin-bottom: 4px;
    }
</style>
""", unsafe_allow_html=True)

# --- Sidebar ---
with st.sidebar:
    st.markdown("## ⛽ Radar Combustível")
    st.markdown("---")
    pagina = st.radio("Navegação", [
        "🏠 Visão Geral",
        "💰 Ranking de Preços",
        "🔍 Buscas & Tendências",
        "⭐ Popularidade",
        "📈 Evolução de Preços",
        "🗺️ Mapa de Postos",
    ])
    st.markdown("---")
    st.caption(f"Redis: `{REDIS_HOST}:{REDIS_PORT}`")

redis = get_redis()

# ============================================================
# PÁGINA: Visão Geral
# ============================================================
if pagina == "🏠 Visão Geral":
    st.markdown("# Radar Combustível")
    st.markdown("##### Pipeline MongoDB → Redis em tempo quase real")
    st.markdown("---")

    total_postos     = len(redis.keys("posto:*"))
    total_precos     = len(redis.keys("preco:*"))
    total_buscas     = int(redis.zscore("ranking:combustiveis:buscas", "GASOLINA_COMUM") or 0)
    indices          = redis.execute_command("FT._LIST")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Postos no Redis",       f"{total_postos:,}")
    c2.metric("Hashes de Preço",       f"{total_precos:,}")
    c3.metric("Buscas (Gasolina Com.)",f"{total_buscas:,}")
    c4.metric("Índices RediSearch",    len(indices))

    st.markdown("---")
    st.markdown("### Estruturas Redis utilizadas")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("""
| Estrutura | Chave | Finalidade |
|---|---|---|
| **Hash** | `posto:{id}` | Cadastro + métricas do posto |
| **Hash** | `preco:{id}:{comb}` | Preço atual por combustível |
| **Sorted Set** | `ranking:preco:{comb}` | Menor preço por combustível |
| **Sorted Set** | `ranking:variacao_preco` | Maior variação recente |
        """)
    with col2:
        st.markdown("""
| Estrutura | Chave | Finalidade |
|---|---|---|
| **Sorted Set** | `ranking:combustiveis:buscas` | Combustíveis mais buscados |
| **Sorted Set** | `ranking:cidades:buscas` | Cidades mais ativas |
| **Sorted Set** | `ranking:postos:avaliacao` | Postos mais bem avaliados |
| **Time Series** | `ts:preco:{id}:{comb}` | Evolução de preço |
        """)

    st.markdown("---")
    st.markdown("### Índices RediSearch ativos")
    for idx in indices:
        st.success(f"✅ `{idx}`")


# ============================================================
# PÁGINA: Ranking de Preços
# ============================================================
elif pagina == "💰 Ranking de Preços":
    st.markdown("# Ranking de Preços")
    st.markdown('<p class="section-label">Sorted Set — ranking:preco:{combustivel}</p>', unsafe_allow_html=True)
    st.markdown("---")

    col_sel, col_top = st.columns([3, 1])
    with col_sel:
        comb_sel = st.selectbox(
            "Combustível",
            COMBUSTIVEIS,
            format_func=lambda x: COMBUSTIVEL_LABEL.get(x, x)
        )
    with col_top:
        top_n = st.number_input("Top N", min_value=5, max_value=50, value=15)

    df = ranking_preco(redis, comb_sel, top_n)

    if df.empty:
        st.warning("Nenhum dado encontrado para esse combustível.")
    else:
        # KPIs
        k1, k2, k3 = st.columns(3)
        k1.metric("Menor preço",  f"R$ {df['preco_atual'].min():.3f}")
        k2.metric("Maior preço",  f"R$ {df['preco_atual'].max():.3f}")
        k3.metric("Média",        f"R$ {df['preco_atual'].mean():.3f}")

        st.markdown("---")

        fig = px.bar(
            df.sort_values("preco_atual"),
            x="preco_atual",
            y="nome",
            orientation="h",
            color="preco_atual",
            color_continuous_scale="RdYlGn_r",
            labels={"preco_atual": "Preço (R$)", "nome": "Posto"},
            title=f"Top {top_n} Menores Preços — {COMBUSTIVEL_LABEL[comb_sel]}",
            hover_data=["bandeira", "cidade", "estado", "variacao_pct"],
        )
        fig.update_layout(
            plot_bgcolor="#0f172a",
            paper_bgcolor="#0f172a",
            font_color="#f8fafc",
            coloraxis_showscale=False,
            yaxis={"categoryorder": "total ascending"},
            height=500,
        )
        st.plotly_chart(fig, use_container_width=True)

        st.markdown("### Tabela detalhada")
        st.caption("Clique em uma linha e copie o **posto_id** para consultar a evolução de preços.")

        # ----------------------------------------------------------------
        # CORREÇÃO: inclui posto_id na tabela e usa nome legível como label
        # Se o posto não foi encontrado no hash (nome == "—"), exibe o ID
        # truncado na coluna Posto para facilitar a leitura.
        # ----------------------------------------------------------------
        df_show = df[["posto_id", "nome", "bandeira", "cidade", "estado", "preco_atual", "variacao_pct"]].copy()
        df_show["nome"] = df_show.apply(
            lambda r: r["nome"] if r["nome"] != "—" else r["posto_id"],
            axis=1,
        )
        df_show.columns = ["posto_id", "Posto", "Bandeira", "Cidade", "UF", "Preço (R$)", "Variação (%)"]
        st.dataframe(
            df_show.style.format({"Preço (R$)": "{:.3f}", "Variação (%)": "{:+.2f}"}),
            use_container_width=True,
        )

        # Atalho rápido: seleciona posto para ir direto à Time Series
        st.markdown("---")
        st.markdown("### ⚡ Atalho — ver evolução de preço")
        posto_opcoes = {
            f"{row['Posto']} ({row['posto_id'][:8]}…)": row["posto_id"]
            for _, row in df_show.iterrows()
        }
        posto_escolhido_label = st.selectbox("Selecione um posto", list(posto_opcoes.keys()))
        posto_escolhido_id    = posto_opcoes[posto_escolhido_label]

        if st.button("📈 Ver evolução de preço deste posto"):
            st.session_state["ts_posto_id"]  = posto_escolhido_id
            st.session_state["ts_combustivel"] = comb_sel
            st.switch_page = "📈 Evolução de Preços"  # hint visual — navegação manual necessária
            st.info(f"**posto_id copiado:** `{posto_escolhido_id}`  \nVá para **📈 Evolução de Preços** e cole o ID acima.")

    st.markdown("---")
    st.markdown("### Maior variação recente")
    st.markdown('<p class="section-label">Sorted Set — ranking:variacao_preco</p>', unsafe_allow_html=True)
    df_var = ranking_variacao(redis, top=15)
    if not df_var.empty:
        fig2 = px.bar(
            df_var,
            x="variacao_abs",
            y="nome",
            orientation="h",
            color="combustivel",
            labels={"variacao_abs": "Variação Absoluta (%)", "nome": "Posto"},
            title="Postos com Maior Variação de Preço Recente",
        )
        fig2.update_layout(
            plot_bgcolor="#0f172a",
            paper_bgcolor="#0f172a",
            font_color="#f8fafc",
            height=450,
        )
        st.plotly_chart(fig2, use_container_width=True)


# ============================================================
# PÁGINA: Buscas & Tendências
# ============================================================
elif pagina == "🔍 Buscas & Tendências":
    st.markdown("# Buscas & Tendências")
    st.markdown('<p class="section-label">Sorted Sets — ranking:combustiveis:buscas | ranking:cidades:buscas</p>', unsafe_allow_html=True)
    st.markdown("---")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("### Combustíveis mais buscados")
        df_comb = ranking_buscas(redis)
        if df_comb.empty:
            st.info("Ainda sem dados de busca.")
        else:
            fig = px.bar(
                df_comb,
                x="buscas",
                y="combustivel",
                orientation="h",
                color="buscas",
                color_continuous_scale="Oranges",
                labels={"buscas": "Buscas", "combustivel": "Combustível"},
            )
            fig.update_layout(
                plot_bgcolor="#0f172a",
                paper_bgcolor="#0f172a",
                font_color="#f8fafc",
                coloraxis_showscale=False,
                yaxis={"categoryorder": "total ascending"},
                height=350,
            )
            st.plotly_chart(fig, use_container_width=True)

    with col2:
        st.markdown("### Cidades com mais buscas")
        df_cid = ranking_cidades(redis)
        if df_cid.empty:
            st.info("Ainda sem dados de busca por cidade.")
        else:
            fig2 = px.bar(
                df_cid,
                x="buscas",
                y="cidade",
                orientation="h",
                color="buscas",
                color_continuous_scale="Blues",
                labels={"buscas": "Buscas", "cidade": "Cidade"},
            )
            fig2.update_layout(
                plot_bgcolor="#0f172a",
                paper_bgcolor="#0f172a",
                font_color="#f8fafc",
                coloraxis_showscale=False,
                yaxis={"categoryorder": "total ascending"},
                height=350,
            )
            st.plotly_chart(fig2, use_container_width=True)

    st.markdown("---")
    st.markdown("### Combustíveis sem resultado (demanda não atendida)")
    st.markdown('<p class="section-label">Sorted Set — ranking:combustiveis:sem_resultado</p>', unsafe_allow_html=True)
    rows_sr = redis.zrevrange("ranking:combustiveis:sem_resultado", 0, -1, withscores=True)
    if rows_sr:
        df_sr = pd.DataFrame([
            {"combustivel": COMBUSTIVEL_LABEL.get(c, c), "buscas_sem_resultado": int(s)}
            for c, s in rows_sr
        ])
        fig3 = px.pie(
            df_sr,
            names="combustivel",
            values="buscas_sem_resultado",
            title="Distribuição de buscas sem resultado",
            color_discrete_sequence=px.colors.sequential.Reds_r,
        )
        fig3.update_layout(paper_bgcolor="#0f172a", font_color="#f8fafc")
        st.plotly_chart(fig3, use_container_width=True)
    else:
        st.info("Sem dados de buscas sem resultado ainda.")


# ============================================================
# PÁGINA: Popularidade
# ============================================================
elif pagina == "⭐ Popularidade":
    st.markdown("# Popularidade dos Postos")
    st.markdown('<p class="section-label">Sorted Sets — ranking:postos:avaliacao | ranking:postos:favoritos | ranking:postos:checkins</p>', unsafe_allow_html=True)
    st.markdown("---")

    df_aval = ranking_postos_avaliacao(redis, top=15)

    if df_aval.empty:
        st.warning("Nenhum dado de avaliação encontrado.")
    else:
        k1, k2, k3 = st.columns(3)
        k1.metric("Melhor avaliação", f"⭐ {df_aval['avaliacao'].max():.2f}")
        k2.metric("Mais favoritado",  f"❤️ {int(df_aval['favoritos'].max())}")
        k3.metric("Mais check-ins",   f"📍 {int(df_aval['checkins'].max())}")

        st.markdown("---")

        fig = px.scatter(
            df_aval,
            x="favoritos",
            y="avaliacao",
            size="checkins",
            color="bandeira",
            hover_name="nome",
            hover_data=["cidade", "checkins"],
            labels={"favoritos": "Favoritos", "avaliacao": "Avaliação média", "checkins": "Check-ins"},
            title="Postos: Avaliação × Favoritos (tamanho = check-ins)",
        )
        fig.update_layout(
            plot_bgcolor="#0f172a",
            paper_bgcolor="#0f172a",
            font_color="#f8fafc",
            height=450,
        )
        st.plotly_chart(fig, use_container_width=True)

        st.markdown("### Top 15 postos mais bem avaliados")
        st.dataframe(
            df_aval[["nome", "bandeira", "cidade", "avaliacao", "favoritos", "checkins"]]
            .rename(columns={
                "nome": "Posto", "bandeira": "Bandeira", "cidade": "Cidade",
                "avaliacao": "Avaliação", "favoritos": "Favoritos", "checkins": "Check-ins"
            })
            .style.format({"Avaliação": "{:.2f}"}),
            use_container_width=True,
        )


# ============================================================
# PÁGINA: Evolução de Preços (Time Series)
# ============================================================
elif pagina == "📈 Evolução de Preços":
    st.markdown("# Evolução de Preços")
    st.markdown('<p class="section-label">Time Series — ts:preco:{posto_id}:{combustivel}</p>', unsafe_allow_html=True)
    st.markdown("---")

    # ----------------------------------------------------------------
    # CORREÇÃO: pré-preenche o input se o usuário veio do Ranking
    # ----------------------------------------------------------------
    default_posto_id  = st.session_state.get("ts_posto_id", "")
    default_combustivel = st.session_state.get("ts_combustivel", COMBUSTIVEIS[0])

    st.info("Selecione um combustível e informe o **posto_id** (disponível na tabela da página 💰 Ranking de Preços).")

    col1, col2 = st.columns(2)
    with col1:
        posto_id_input = st.text_input(
            "ID do Posto (posto_id)",
            value=default_posto_id,
            placeholder="Ex: 683abc123def456789012345",
        )
    with col2:
        comb_index = COMBUSTIVEIS.index(default_combustivel) if default_combustivel in COMBUSTIVEIS else 0
        comb_ts = st.selectbox(
            "Combustível",
            COMBUSTIVEIS,
            index=comb_index,
            format_func=lambda x: COMBUSTIVEL_LABEL.get(x, x),
            key="ts_comb",
        )

    # Limpa o estado de sessão após usar
    if posto_id_input and "ts_posto_id" in st.session_state:
        del st.session_state["ts_posto_id"]
    if "ts_combustivel" in st.session_state:
        del st.session_state["ts_combustivel"]

    if posto_id_input:
        # Valida se o posto existe no Redis antes de tentar a Time Series
        posto_info = redis.hgetall(f"posto:{posto_id_input}")

        if not posto_info:
            st.error(
                f"Posto `{posto_id_input}` **não encontrado** no Redis.  \n"
                "Verifique se o `posto_id` foi copiado corretamente da tabela de Ranking de Preços "
                "(deve ser o valor da coluna **posto_id**, não o nome do posto)."
            )
        else:
            nome_posto = posto_info.get("nome_fantasia") or posto_id_input
            st.markdown(f"**Posto:** {nome_posto} &nbsp;·&nbsp; **Cidade:** {posto_info.get('cidade','—')} / {posto_info.get('estado','—')}")

            df_ts = time_series_preco(redis, posto_id_input, comb_ts)

            if df_ts.empty:
                st.warning(
                    "Nenhuma série temporal encontrada para esse posto/combustível.  \n"
                    "O consumer precisa estar rodando para acumular dados em tempo real."
                )
            else:
                fig = px.line(
                    df_ts,
                    x="timestamp",
                    y="preco",
                    markers=True,
                    labels={"timestamp": "Data/Hora", "preco": "Preço (R$)"},
                    title=f"Evolução do preço — {COMBUSTIVEL_LABEL[comb_ts]}",
                )
                fig.update_traces(line_color="#f97316", marker_color="#f97316")
                fig.update_layout(
                    plot_bgcolor="#0f172a",
                    paper_bgcolor="#0f172a",
                    font_color="#f8fafc",
                    height=400,
                )
                st.plotly_chart(fig, use_container_width=True)

                col_a, col_b, col_c = st.columns(3)
                col_a.metric("Mínimo",  f"R$ {df_ts['preco'].min():.3f}")
                col_b.metric("Máximo",  f"R$ {df_ts['preco'].max():.3f}")
                col_c.metric("Último",  f"R$ {df_ts['preco'].iloc[-1]:.3f}")
    else:
        st.markdown("#### Como encontrar o posto_id")
        st.markdown("1. Vá em **💰 Ranking de Preços**")
        st.markdown("2. Selecione o combustível desejado")
        st.markdown("3. Copie o valor da coluna **posto_id** na tabela detalhada")
        st.markdown("4. Cole aqui no campo acima")


# ============================================================
# PÁGINA: Mapa de Postos
# ============================================================
elif pagina == "🗺️ Mapa de Postos":
    st.markdown("# Mapa de Postos")
    st.markdown('<p class="section-label">Hash (location: lng,lat) — Geo Redis</p>', unsafe_allow_html=True)
    st.markdown("---")

    col1, col2 = st.columns([2, 1])
    with col1:
        estado_filtro = st.text_input("Filtrar por estado (UF)", placeholder="Ex: SP, RJ, MG")
    with col2:
        sample_n = st.slider("Quantidade de postos no mapa", 50, 1000, 300, step=50)

    df_geo = postos_geo(redis, sample=sample_n)

    if df_geo.empty:
        st.warning("Nenhum posto com localização encontrado no Redis.")
    else:
        if estado_filtro:
            df_geo = df_geo[df_geo["estado"].str.upper() == estado_filtro.upper()]

        st.markdown(f"**{len(df_geo)} postos exibidos**")

        fig = px.scatter_mapbox(
            df_geo,
            lat="lat",
            lon="lon",
            hover_name="nome",
            hover_data=["bandeira", "cidade", "estado", "avaliacao"],
            color="avaliacao",
            color_continuous_scale="RdYlGn",
            size_max=12,
            zoom=3.5,
            center={"lat": -15.8, "lon": -47.9},
            mapbox_style="carto-darkmatter",
            title="Distribuição geográfica dos postos",
            labels={"avaliacao": "Avaliação"},
        )
        fig.update_layout(
            paper_bgcolor="#0f172a",
            font_color="#f8fafc",
            height=600,
            margin={"r": 0, "t": 40, "l": 0, "b": 0},
        )
        st.plotly_chart(fig, use_container_width=True)

        st.markdown("---")
        st.markdown("### Busca por estado + combustível (RediSearch)")
        st.markdown('<p class="section-label">idx:postos — FT.SEARCH</p>', unsafe_allow_html=True)

        col_uf, col_comb = st.columns(2)
        with col_uf:
            uf_search = st.text_input("Estado (UF)", value="SP", key="uf_search")
        with col_comb:
            comb_search = st.selectbox(
                "Combustível",
                COMBUSTIVEIS,
                format_func=lambda x: COMBUSTIVEL_LABEL.get(x, x),
                key="comb_search"
            )

        if uf_search:
            df_search = busca_postos_redisearch(redis, uf_search.upper(), comb_search)
            if df_search.empty:
                st.info("Nenhum resultado encontrado.")
            else:
                st.dataframe(
                    df_search.rename(columns={
                        "nome": "Posto", "bandeira": "Bandeira",
                        "cidade": "Cidade", "avaliacao": "Avaliação",
                        "preco_atual": f"Preço {COMBUSTIVEL_LABEL[comb_search]} (R$)"
                    }).style.format({
                        "Avaliação": "{:.2f}",
                        f"Preço {COMBUSTIVEL_LABEL[comb_search]} (R$)": lambda x: f"{x:.3f}" if x else "—"
                    }),
                    use_container_width=True,
                )