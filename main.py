import os
import re
import secrets
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Boolean, Text, func
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session

# ---------------------------------------------------------------------------
# CREDENCIAIS FIXAS DE ACESSO
# ---------------------------------------------------------------------------
USUARIO_FIXO = "qcsoftware"
SENHA_FIXA = "Qcsoftware@2026"

SESSOES_ATIVAS = set()

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO DE CONEXÃO E INFRAESTRUTURA - POSTGRESQL (HOSTINGER / COOLIFY)
# ---------------------------------------------------------------------------
usuario = "fabricio"
senha_segura = urllib.parse.quote_plus("Myfab@123")
host = "179.198.119.225"
porta = "5432"
banco = "qcsoftware"

SQLALCHEMY_DATABASE_URL = (
    f"postgresql+psycopg2://{usuario}:{senha_segura}@{host}:{porta}/{banco}?sslmode=disable"
)

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# MODELOS DE TABELAS (SQLALCHEMY ORM)
# ---------------------------------------------------------------------------
class MapaMaquinas(Base):
    __tablename__ = "maquinas_mapa"

    id_maquinas = Column(Integer, primary_key=True, index=True)
    nome = Column(String(100), nullable=False)
    x = Column(Integer, default=0)
    y = Column(Integer, default=0)
    status = Column(String(30), default="Normal")
    ativo = Column(Boolean, default=True)


class HistoricoOcorrencias(Base):
    __tablename__ = "maquinas_historico_ocorrencias"

    id = Column(Integer, primary_key=True, autoincrement=True)
    id_maquinas = Column(Integer, index=True)
    data_hora = Column(String(50))
    colaborador = Column(String(150))
    problema = Column(Text)


# ---------------------------------------------------------------------------
# GERENCIAMENTO E INICIALIZAÇÃO DO BANCO
# ---------------------------------------------------------------------------
def inicializar_banco():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        total_maquinas = db.query(MapaMaquinas).count()
        if total_maquinas == 0:
            print("Semeando 60 ativos industriais com status inicial 'Normal'...")
            maquinas_iniciais = [
                MapaMaquinas(id_maquinas=i, nome=f"MÁQ - {i:02d}", status="Normal", ativo=True)
                for i in range(1, 61)
            ]
            db.add_all(maquinas_iniciais)
            db.commit()
            print("Tabela maquinas_mapa configurada com sucesso!")
    except Exception as e:
        print(f"Erro ao inicializar dados: {e}")
        db.rollback()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    inicializar_banco()
    yield


app = FastAPI(title="Q.C Software - API MapaMaquinas", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# SCHEMAS DE VALIDAÇÃO DE ENTRADA (PYDANTIC)
# ---------------------------------------------------------------------------
class LoginInput(BaseModel):
    username: str
    senha: str


class OcorrenciaInput(BaseModel):
    data_hora: str
    colaborador: str
    problema: str


class ResolucaoInput(BaseModel):
    data_hora: str
    colaborador: str
    comentario: str


# ---------------------------------------------------------------------------
# AUTENTICAÇÃO
# ---------------------------------------------------------------------------
def verificar_token(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Não autenticado")
    token = authorization.split(" ", 1)[1]
    if token not in SESSOES_ATIVAS:
        raise HTTPException(status_code=401, detail="Sessão inválida ou expirada")
    return token


@app.post("/api/login")
def realizar_login(dados: LoginInput):
    usuario_valido = secrets.compare_digest(dados.username, USUARIO_FIXO)
    senha_valida = secrets.compare_digest(dados.senha, SENHA_FIXA)

    if not (usuario_valido and senha_valida):
        raise HTTPException(status_code=401, detail="Usuário ou senha incorretos")

    token = secrets.token_hex(32)
    SESSOES_ATIVAS.add(token)

    return {
        "token": token,
        "usuario": USUARIO_FIXO,
    }


@app.post("/api/logout")
def realizar_logout(token: str = Depends(verificar_token)):
    SESSOES_ATIVAS.discard(token)
    return {"status": "desconectado"}


# ---------------------------------------------------------------------------
# ROTA DO DASHBOARD GERENCIAL
# ---------------------------------------------------------------------------
def _parse_data_br(texto_data: str) -> Optional[datetime]:
    if not texto_data:
        return None
    for fmt in ("%d/%m/%Y, %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(texto_data.strip(), fmt)
        except ValueError:
            continue
    return None


@app.get("/api/dashboard/status", dependencies=[Depends(verificar_token)])
def obter_status_dashboard(db: Session = Depends(get_db)):
    try:
        resultado = (
            db.query(
                func.count(MapaMaquinas.id_maquinas).label("total"),
                func.count(func.nullif(MapaMaquinas.status != "Normal", True)).label("normal"),
                func.count(func.nullif(MapaMaquinas.status != "Parado", True)).label("parado"),
                func.count(func.nullif(MapaMaquinas.status != "Manutenção", True)).label("manutencao"),
            )
            .filter(MapaMaquinas.ativo == True)
            .first()
        )

        maquinas = db.query(MapaMaquinas).filter(MapaMaquinas.ativo == True).all()
        mapa_nomes = {m.id_maquinas: m.nome for m in maquinas}
        mapa_status = {m.id_maquinas: m.status for m in maquinas}

        ocorrencias = db.query(HistoricoOcorrencias).all()
        agora = datetime.now()

        qtd_paradas_por_maq: dict[int, int] = {}
        qtd_manutencao_por_maq: dict[int, int] = {}
        dias_paradas_lista: list[float] = []
        dias_manutencao_lista: list[float] = []

        for oc in ocorrencias:
            id_m = oc.id_maquinas
            status_atual_m = mapa_status.get(id_m, "Normal")
            texto_prob = oc.problema or ""

            qtd_paradas_por_maq[id_m] = qtd_paradas_por_maq.get(id_m, 0) + 1

            dt_inicio = _parse_data_br(oc.data_hora)
            match_resolvido = re.search(r"\[RESOLVIDO em ([^\]p]+?)(?:\s+por\s+|\])", texto_prob)
            dt_fim = _parse_data_br(match_resolvido.group(1)) if match_resolvido else None

            if dt_inicio:
                fim_efetivo = dt_fim if dt_fim else agora
                duracao_dias = max((fim_efetivo - dt_inicio).total_seconds() / 86400.0, 0.0)

                if dt_fim:
                    qtd_manutencao_por_maq[id_m] = qtd_manutencao_por_maq.get(id_m, 0) + 1
                    dias_manutencao_lista.append(duracao_dias)
                    dias_paradas_lista.append(duracao_dias)
                else:
                    if status_atual_m == "Manutenção":
                        qtd_manutencao_por_maq[id_m] = qtd_manutencao_por_maq.get(id_m, 0) + 1
                        dias_manutencao_lista.append(duracao_dias)
                    else:
                        dias_paradas_lista.append(duracao_dias)

        for m in maquinas:
            if m.status == "Manutenção" and m.id_maquinas not in qtd_manutencao_por_maq:
                qtd_manutencao_por_maq[m.id_maquinas] = 1

        top_paradas = sorted(
            [
                {"id": k, "nome": mapa_nomes.get(k, f"MÁQ - {k:02d}"), "quantidade": v}
                for k, v in qtd_paradas_por_maq.items()
            ],
            key=lambda x: x["quantidade"],
            reverse=True,
        )[:5]

        top_manutencao = sorted(
            [
                {"id": k, "nome": mapa_nomes.get(k, f"MÁQ - {k:02d}"), "quantidade": v}
                for k, v in qtd_manutencao_por_maq.items()
            ],
            key=lambda x: x["quantidade"],
            reverse=True,
        )[:5]

        media_dias_parada = (
            round(sum(dias_paradas_lista) / len(dias_paradas_lista), 2)
            if dias_paradas_lista
            else 0.0
        )
        media_dias_manutencao = (
            round(sum(dias_manutencao_lista) / len(dias_manutencao_lista), 2)
            if dias_manutencao_lista
            else 0.0
        )

        return {
            "total": resultado.total or 0,
            "normal": resultado.normal or 0,
            "parado": resultado.parado or 0,
            "manutencao": resultado.manutencao or 0,
            "media_dias_parada": media_dias_parada,
            "media_dias_manutencao": media_dias_manutencao,
            "top_paradas": top_paradas,
            "top_manutencao": top_manutencao,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar indicadores: {str(e)}")


# ---------------------------------------------------------------------------
# ROTAS DA API MapaMaquinas
# ---------------------------------------------------------------------------
@app.get("/api/mapamaquinas", dependencies=[Depends(verificar_token)])
def obter_maquinas(db: Session = Depends(get_db)):
    maquinas = (
        db.query(MapaMaquinas)
        .filter(MapaMaquinas.ativo == True)
        .order_by(MapaMaquinas.id_maquinas.asc())
        .all()
    )
    return [
        {
            "id_maquinas": m.id_maquinas,
            "nome": m.nome,
            "x": m.x,
            "y": m.y,
            "status": m.status,
        }
        for m in maquinas
    ]


@app.get("/api/mapamaquinas/{id_maquina}/ocorrencia", dependencies=[Depends(verificar_token)])
def obter_ultima_ocorrencia(id_maquina: int, db: Session = Depends(get_db)):
    ocorrencia = (
        db.query(HistoricoOcorrencias)
        .filter(HistoricoOcorrencias.id_maquinas == id_maquina)
        .order_by(HistoricoOcorrencias.id.desc())
        .first()
    )

    if not ocorrencia or "[RESOLVIDO" in (ocorrencia.problema or ""):
        return {"tem_ocorrencia": False}

    return {
        "tem_ocorrencia": True,
        "data_hora": ocorrencia.data_hora,
        "colaborador": ocorrencia.colaborador,
        "problema": ocorrencia.problema,
    }


@app.post("/api/mapamaquinas/{id_maquina}/ocorrencia", dependencies=[Depends(verificar_token)])
def registrar_ocorrencia(id_maquina: int, dados: OcorrenciaInput, db: Session = Depends(get_db)):
    maquina = db.query(MapaMaquinas).filter(MapaMaquinas.id_maquinas == id_maquina).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Ativo não localizado no Q.C Software")

    nova_ocorrencia = HistoricoOcorrencias(
        id_maquinas=id_maquina,
        data_hora=dados.data_hora,
        colaborador=dados.colaborador,
        problema=dados.problema,
    )
    db.add(nova_ocorrencia)

    maquina.status = "Parado"

    db.commit()
    return {"status": "sucesso", "novo_status": maquina.status}


@app.post("/api/mapamaquinas/{id_maquina}/manutencao", dependencies=[Depends(verificar_token)])
def colocar_em_manutencao(id_maquina: int, db: Session = Depends(get_db)):
    maquina = db.query(MapaMaquinas).filter(MapaMaquinas.id_maquinas == id_maquina).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Ativo não localizado")

    maquina.status = "Manutenção"
    db.commit()
    return {"status": "sucesso", "novo_status": maquina.status}


@app.post("/api/mapamaquinas/{id_maquina}/resolver", dependencies=[Depends(verificar_token)])
def resolver_ocorrencia(id_maquina: int, dados: ResolucaoInput, db: Session = Depends(get_db)):
    maquina = db.query(MapaMaquinas).filter(MapaMaquinas.id_maquinas == id_maquina).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Máquina não localizada")

    ultima_ocorrencia = (
        db.query(HistoricoOcorrencias)
        .filter(HistoricoOcorrencias.id_maquinas == id_maquina)
        .order_by(HistoricoOcorrencias.id.desc())
        .first()
    )

    if ultima_ocorrencia:
        texto_solucao = f"\n\n[RESOLVIDO em {dados.data_hora} por {dados.colaborador}]: {dados.comentario}"
        ultima_ocorrencia.problema = (ultima_ocorrencia.problema or "") + texto_solucao

    maquina.status = "Normal"

    db.commit()
    return {"status": "sucesso", "novo_status": maquina.status}


# ---------------------------------------------------------------------------
# ROTAS DE RELATÓRIOS E WORKFLOW OPERACIONAL
# ---------------------------------------------------------------------------
@app.get("/api/relatorios/historico", dependencies=[Depends(verificar_token)])
def obter_relatorio_historico(status: Optional[str] = None, db: Session = Depends(get_db)):
    """Retorna o histórico completo cruzando ocorrências com o status atual da máquina"""
    query = (
        db.query(HistoricoOcorrencias, MapaMaquinas)
        .outerjoin(MapaMaquinas, HistoricoOcorrencias.id_maquinas == MapaMaquinas.id_maquinas)
        .order_by(HistoricoOcorrencias.id.desc())
    )

    if status and status != "TODOS":
        query = query.filter(MapaMaquinas.status == status)

    registros = query.all()
    resultado = []
    for oc, maq in registros:
        texto_problema = oc.problema or ""
        resolvido = "[RESOLVIDO" in texto_problema
        resultado.append({
            "id_ocorrencia": oc.id,
            "id_maquinas": oc.id_maquinas,
            "nome_maquina": maq.nome if maq else f"MÁQ - {oc.id_maquinas:02d}",
            "status_maquina": maq.status if maq else "Desconhecido",
            "data_hora": oc.data_hora,
            "colaborador": oc.colaborador,
            "problema": texto_problema,
            "situacao": "Resolvido" if resolvido else "Em Aberto"
        })
    return resultado


@app.get("/api/workflow/maquinas", dependencies=[Depends(verificar_token)])
def obter_workflow_maquinas(db: Session = Depends(get_db)):
    """Retorna as máquinas separadas por estágio do workflow (Normal, Parado, Manutenção)"""
    maquinas = (
        db.query(MapaMaquinas)
        .filter(MapaMaquinas.ativo == True)
        .order_by(MapaMaquinas.id_maquinas.asc())
        .all()
    )

    workflow = {"Normal": [], "Parado": [], "Manutenção": []}

    for m in maquinas:
        ultima_oc = (
            db.query(HistoricoOcorrencias)
            .filter(HistoricoOcorrencias.id_maquinas == m.id_maquinas)
            .order_by(HistoricoOcorrencias.id.desc())
            .first()
        )
        detalhe_oc = None
        if ultima_oc and "[RESOLVIDO" not in (ultima_oc.problema or ""):
            detalhe_oc = {
                "data_hora": ultima_oc.data_hora,
                "colaborador": ultima_oc.colaborador,
                "problema": ultima_oc.problema
            }

        item = {
            "id_maquinas": m.id_maquinas,
            "nome": m.nome,
            "status": m.status,
            "ocorrencia_aberta": detalhe_oc
        }

        if m.status in workflow:
            workflow[m.status].append(item)
        else:
            workflow["Normal"].append(item)

    return workflow


# ---------------------------------------------------------------------------
# ROTA INICIAL E INTERFACE ESTÁTICA
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.get("/")
def pagina_inicial():
    caminho_login = os.path.join(BASE_DIR, "login.html")
    if not os.path.exists(caminho_login):
        raise HTTPException(status_code=404, detail="Arquivo login.html não encontrado")
    return FileResponse(caminho_login)


app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="raiz")

if __name__ == "__main__":
    import uvicorn

    porta = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=porta, reload=True)