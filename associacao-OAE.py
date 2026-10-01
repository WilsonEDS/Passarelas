# -*- coding: utf-8 -*-
# =============================================================================
# ASSOCIACAO OAE x OAE - PASSARELAS_TRECHO-KM x LT1 (DATAS DE INSPECAO) - PyQGIS
# =============================================================================

# -----------------------------------------------------------------------------
# PARAMETROS DE MANUSEIO (camadas, atributos e buffer)
# -----------------------------------------------------------------------------
# Raio do buffer de busca, em metros, medido em CRS_METRICA.
RAIO_BUFFER_M = 50.0

# Camadas de entrada e de saida.
NOME_CAMADA_BASE = "Passarelas_trecho-km"
NOME_CAMADA_LT1 = "LT1_V10-2"
NOME_CAMADA_SAIDA = "Passarelas_trecho-km-insp"

# Todos os campos da LT1 cujo nome INICIA por este prefixo sao levados para a
# saida (ex.: "Data_Inspecao_Realizada 1º ciclo",
# "Data_Inspecao_Planejada_Inicial - 2º ciclo"). Comparacao exata, sensivel a
# maiusculas/minusculas e acentos.
PREFIXO_CAMPOS_LT1 = "Data_Inspecao"

# Campos de controle criados na saida.
CAMPO_DIST_LT1 = "Dist_LT1_m"
CAMPO_STATUS_LT1 = "Status_LT1"

# =============================================================================
#
# 1. OBJETIVO
# -----------------------------------------------------------------------------
# Para cada OAE (ponto) da camada NOME_CAMADA_BASE, identificar o ponto (OAE)
# mais proximo da camada NOME_CAMADA_LT1 dentro de um buffer de RAIO_BUFFER_M
# metros e levar, para a nova camada NOME_CAMADA_SAIDA, todos os atributos da
# LT1 cujo nome inicia por PREFIXO_CAMPOS_LT1.
#
# Nenhuma informacao e inventada: os valores sao copiados exatamente como
# estao na LT1 (inclusive NULL). Sem associacao, os campos ficam NULL.
#
#
# 2. DADOS DE ENTRADA
# -----------------------------------------------------------------------------
#   Ambas as camadas sao pontuais e estao no EPSG:4674 (SIRGAS 2000).
#   Como graus nao medem distancia, as medicoes sao feitas em CRS_METRICA
#   (EPSG:5880 - SIRGAS 2000 / Brazil Polyconic); a saida permanece no SRC
#   original da camada base.
#
#
# 3. DADOS DE SAIDA
# -----------------------------------------------------------------------------
#   Camada em memoria NOME_CAMADA_SAIDA com:
#       - todos os atributos da camada base;
#       - todos os campos "Data_Inspecao*" da LT1, na ordem da LT1, com o
#         mesmo nome e tipo;
#       - Dist_LT1_m (real): distancia, em metros, ate o ponto LT1 associado.
#         NULL quando nao associado.
#       - Status_LT1 (texto): resultado da associacao (ver secao 4).
#
#
# 4. CRITERIO DE ASSOCIACAO (VIZINHO MAIS PROXIMO MUTUO, 1:1)
# -----------------------------------------------------------------------------
#   Uma passarela P e associada ao ponto L da LT1 somente quando:
#     - L e o UNICO ponto LT1 mais proximo de P dentro do buffer; e
#     - P e a UNICA passarela mais proxima de L dentro do buffer.
#   Assim, cada ponto LT1 alimenta no maximo uma passarela, e o resultado nao
#   depende da ordem de leitura das feicoes.
#
#   Status_LT1 (mutuamente exclusivos, avaliados nesta ordem):
#     GEOMETRIA_INVALIDA                       ponto da passarela nulo,
#                                              multiponto ou falha de reprojecao.
#     SEM_PONTO_NO_BUFFER                      nenhum ponto LT1 a ate
#                                              RAIO_BUFFER_M metros.
#     EMPATE_ENTRE_PONTOS_LT1                  dois ou mais pontos LT1 a mesma
#                                              menor distancia da passarela.
#     PONTO_LT1_EQUIDISTANTE_DE_PASSARELAS     o ponto LT1 mais proximo esta a
#                                              mesma menor distancia de duas
#                                              ou mais passarelas.
#     PONTO_LT1_MAIS_PROXIMO_DE_OUTRA_PASSARELA o ponto LT1 mais proximo tem
#                                              outra passarela mais proxima.
#     ASSOCIADO                                associacao mutua e unica.
#
# =============================================================================

import math
from collections import Counter

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)


# -----------------------------------------------------------------------------
# 0) CONFIGURACAO TECNICA
# -----------------------------------------------------------------------------
CRS_METRICA = QgsCoordinateReferenceSystem("EPSG:5880")
# Tolerancia estritamente numerica para reconhecer distancias iguais.
TOLERANCIA_EMPATE_M = 1e-8

STATUS_VALIDOS = (
    "ASSOCIADO",
    "SEM_PONTO_NO_BUFFER",
    "EMPATE_ENTRE_PONTOS_LT1",
    "PONTO_LT1_EQUIDISTANTE_DE_PASSARELAS",
    "PONTO_LT1_MAIS_PROXIMO_DE_OUTRA_PASSARELA",
    "GEOMETRIA_INVALIDA",
)


# -----------------------------------------------------------------------------
# 1) FUNCOES AUXILIARES
# -----------------------------------------------------------------------------
def geometria_pontual_metrica(feicao, transformacao):
    """Clona e reprojeta o ponto de uma feicao para CRS_METRICA.

    Retorna:
        QgsGeometry pontual em CRS_METRICA, ou None quando a geometria for
        nula, nao pontual, multiponto com mais de um ponto, nao reprojetavel
        ou com coordenadas nao finitas.

    Hipotese metodologica:
        Escolher um ponto dentre varios de um MultiPoint seria arbitrar a
        posicao da OAE; por isso so se aceita MultiPoint com um unico ponto.
    """
    geometria = feicao.geometry()
    if geometria is None or geometria.isNull() or geometria.isEmpty():
        return None
    if QgsWkbTypes.geometryType(geometria.wkbType()) != QgsWkbTypes.PointGeometry:
        return None

    geometria = QgsGeometry(geometria)
    if transformacao is not None:
        try:
            geometria.transform(transformacao)
        except QgsCsException:
            return None

    if geometria.isMultipart():
        pontos = geometria.asMultiPoint()
        if len(pontos) != 1:
            return None
        ponto = pontos[0]
    else:
        ponto = geometria.asPoint()
    if not all(math.isfinite(v) for v in (ponto.x(), ponto.y())):
        return None
    return QgsGeometry.fromPointXY(QgsPointXY(ponto))


def pontos_metricos(camada):
    """Reprojeta todos os pontos de uma camada e monta seu indice espacial.

    Retorna:
        (geometrias, indice, invalidos)
        geometrias: dict fid -> QgsGeometry em CRS_METRICA (apenas validas).
        indice: QgsSpatialIndex sobre essas geometrias.
        invalidos: conjunto de fids com geometria invalida.
    """
    transformacao = None
    if camada.crs() != CRS_METRICA:
        transformacao = QgsCoordinateTransform(
            camada.crs(), CRS_METRICA, projeto.transformContext()
        )
    geometrias = {}
    invalidos = set()
    indice = QgsSpatialIndex()
    for feicao in camada.getFeatures():
        geometria = geometria_pontual_metrica(feicao, transformacao)
        if geometria is None:
            invalidos.add(feicao.id())
            continue
        geometrias[feicao.id()] = geometria
        auxiliar = QgsFeature(feicao.id())
        auxiliar.setGeometry(geometria)
        indice.addFeature(auxiliar)
    return geometrias, indice, invalidos


def mais_proximos(geometria, indice, geometrias_alvo, raio):
    """Encontra os pontos alvo mais proximos dentro do buffer.

    Retorna:
        (fids_mais_proximos, menor_distancia)
        fids_mais_proximos: lista ordenada de fids a menor distancia (mais de
            um elemento significa empate); vazia se nada estiver no buffer.
        menor_distancia: float ou None.

    Hipotese metodologica:
        O envelope de semilado igual ao raio contem o circulo do buffer, de
        modo que o indice nao descarta candidatos; a distancia real decide a
        admissao. O minimo e calculado sobre o conjunto completo, e nao pela
        primeira feicao lida.
    """
    ponto = geometria.asPoint()
    envelope = QgsRectangle(ponto.x() - raio, ponto.y() - raio,
                            ponto.x() + raio, ponto.y() + raio)
    candidatos = []
    for fid in indice.intersects(envelope):
        distancia = geometrias_alvo[fid].distance(geometria)
        if math.isfinite(distancia) and 0 <= distancia <= raio:
            candidatos.append((fid, distancia))
    if not candidatos:
        return [], None
    menor = min(d for _, d in candidatos)
    fids = sorted(fid for fid, d in candidatos if abs(d - menor) <= TOLERANCIA_EMPATE_M)
    return fids, menor


# -----------------------------------------------------------------------------
# 2) CAMADAS E CAMPOS
# -----------------------------------------------------------------------------
projeto = QgsProject.instance()

if (isinstance(RAIO_BUFFER_M, bool)
        or not isinstance(RAIO_BUFFER_M, (int, float))
        or not math.isfinite(RAIO_BUFFER_M)
        or RAIO_BUFFER_M <= 0):
    raise Exception("RAIO_BUFFER_M precisa ser um numero finito e positivo (metros).")
raio_buffer = float(RAIO_BUFFER_M)

camadas = {}
for nome in (NOME_CAMADA_BASE, NOME_CAMADA_LT1):
    encontradas = projeto.mapLayersByName(nome)
    if not encontradas:
        raise Exception(f'Camada "{nome}" nao encontrada no projeto.')
    # Nome duplicado impediria saber qual camada foi processada.
    if len(encontradas) != 1:
        raise Exception(f'Ha mais de uma camada com o nome "{nome}".')
    camada = encontradas[0]
    if not camada.isValid() or not camada.crs().isValid():
        raise Exception(f'Camada "{nome}" ou seu SRC invalido.')
    if camada.geometryType() != QgsWkbTypes.PointGeometry:
        raise Exception(f'A camada "{nome}" precisa ser pontual.')
    camadas[nome] = camada

camada_base = camadas[NOME_CAMADA_BASE]
camada_lt1 = camadas[NOME_CAMADA_LT1]

if not CRS_METRICA.isValid():
    raise Exception("CRS_METRICA invalida.")

campos_copiados = [
    QgsField(campo) for campo in camada_lt1.fields()
    if campo.name().startswith(PREFIXO_CAMPOS_LT1)
]
if not campos_copiados:
    raise Exception(
        f'Nenhum campo de "{NOME_CAMADA_LT1}" inicia por "{PREFIXO_CAMPOS_LT1}". '
        "Campos existentes: " + ", ".join(c.name() for c in camada_lt1.fields())
    )
nomes_copiados = [campo.name() for campo in campos_copiados]
idx_lt1_copiados = [camada_lt1.fields().indexOf(nome) for nome in nomes_copiados]

# Colisao de nomes duplicaria colunas e tornaria ambiguo qual delas contem o
# resultado desta execucao.
campos_novos = nomes_copiados + [CAMPO_DIST_LT1, CAMPO_STATUS_LT1]
colisoes = sorted(set(campos_novos) & {c.name() for c in camada_base.fields()})
if colisoes:
    raise Exception(
        f'A camada "{NOME_CAMADA_BASE}" ja possui campos reservados para a saida: '
        + ", ".join(colisoes)
    )


# -----------------------------------------------------------------------------
# 3) PONTOS EM SRC METRICO E VIZINHOS MAIS PROXIMOS NOS DOIS SENTIDOS
# -----------------------------------------------------------------------------
geom_base, indice_base, invalidos_base = pontos_metricos(camada_base)
geom_lt1, indice_lt1, invalidos_lt1 = pontos_metricos(camada_lt1)

# Passarela -> pontos LT1 mais proximos.
proximos_da_base = {
    fid: mais_proximos(geometria, indice_lt1, geom_lt1, raio_buffer)
    for fid, geometria in geom_base.items()
}
# Ponto LT1 -> passarelas mais proximas. Calculado apenas para os pontos LT1
# que sao o mais proximo de alguma passarela, unicos que podem ser associados.
lt1_candidatos = {
    fids[0] for fids, _ in proximos_da_base.values() if len(fids) == 1
}
proximos_do_lt1 = {
    fid: mais_proximos(geom_lt1[fid], indice_base, geom_base, raio_buffer)
    for fid in lt1_candidatos
}


# -----------------------------------------------------------------------------
# 4) CAMADA DE SAIDA
# -----------------------------------------------------------------------------
tipo_geometria_saida = QgsWkbTypes.displayString(camada_base.wkbType())
if camada_base.wkbType() in (QgsWkbTypes.Unknown, QgsWkbTypes.NoGeometry):
    raise Exception("Tipo de geometria da camada base indefinido.")

# A saida permanece no SRC original da camada base (EPSG:4674).
saida = QgsVectorLayer(
    f"{tipo_geometria_saida}?crs={camada_base.crs().authid()}",
    NOME_CAMADA_SAIDA,
    "memory",
)
if not saida.isValid():
    raise Exception("Falha ao criar a camada de saida em memoria.")

provedor_saida = saida.dataProvider()
provedor_saida.addAttributes(camada_base.fields())
provedor_saida.addAttributes(
    campos_copiados
    + [
        QgsField(CAMPO_DIST_LT1, QVariant.Double, len=20, prec=3),
        QgsField(CAMPO_STATUS_LT1, QVariant.String, len=60),
    ]
)
saida.updateFields()

QTD_CAMPOS_NOVOS = saida.fields().count() - camada_base.fields().count()
if QTD_CAMPOS_NOVOS != len(campos_novos):
    raise RuntimeError("Estrutura da camada de saida divergente da esperada.")

idx_saida_copiados = [saida.fields().indexOf(nome) for nome in nomes_copiados]
idx_dist = saida.fields().indexOf(CAMPO_DIST_LT1)
idx_status = saida.fields().indexOf(CAMPO_STATUS_LT1)

# Atributos da LT1 lidos uma unica vez, somente para os pontos associaveis.
atributos_lt1 = {
    feicao.id(): [feicao.attributes()[i] for i in idx_lt1_copiados]
    for feicao in camada_lt1.getFeatures()
    if feicao.id() in lt1_candidatos
}


# -----------------------------------------------------------------------------
# 5) ASSOCIACAO
# -----------------------------------------------------------------------------
contagem = Counter()
lt1_usados = set()
novas_feicoes = []

for base in camada_base.getFeatures():
    nova = QgsFeature(saida.fields())
    nova.setGeometry(QgsGeometry(base.geometry()))
    nova.setAttributes(base.attributes() + [None] * QTD_CAMPOS_NOVOS)

    fid_base = base.id()
    if fid_base in invalidos_base:
        status = "GEOMETRIA_INVALIDA"
    else:
        fids_lt1, distancia = proximos_da_base[fid_base]
        if not fids_lt1:
            status = "SEM_PONTO_NO_BUFFER"
        elif len(fids_lt1) > 1:
            status = "EMPATE_ENTRE_PONTOS_LT1"
        else:
            fid_lt1 = fids_lt1[0]
            fids_base_do_lt1, _ = proximos_do_lt1[fid_lt1]
            if len(fids_base_do_lt1) > 1:
                status = "PONTO_LT1_EQUIDISTANTE_DE_PASSARELAS"
            elif fids_base_do_lt1 != [fid_base]:
                status = "PONTO_LT1_MAIS_PROXIMO_DE_OUTRA_PASSARELA"
            else:
                status = "ASSOCIADO"
                # Copia fiel: nenhum valor e convertido, completado ou estimado.
                for idx, valor in zip(idx_saida_copiados, atributos_lt1[fid_lt1]):
                    nova.setAttribute(idx, valor)
                nova.setAttribute(idx_dist, round(distancia, 3))
                if fid_lt1 in lt1_usados:
                    raise RuntimeError("Ponto LT1 associado a mais de uma passarela.")
                lt1_usados.add(fid_lt1)

    if status not in STATUS_VALIDOS:
        raise RuntimeError(f"Status interno invalido: {status}")
    nova.setAttribute(idx_status, status)
    contagem[status] += 1
    contagem["TOTAL"] += 1
    novas_feicoes.append(nova)

if (not provedor_saida.addFeatures(novas_feicoes)[0]
        or saida.featureCount() != contagem["TOTAL"]):
    raise RuntimeError("Falha na gravacao das feicoes na camada de saida.")
saida.updateExtents()
projeto.addMapLayer(saida)


# -----------------------------------------------------------------------------
# 6) RESUMO E CONFERENCIA
# -----------------------------------------------------------------------------
soma_status = sum(contagem[status] for status in STATUS_VALIDOS)
if soma_status != contagem["TOTAL"] or len(lt1_usados) != contagem["ASSOCIADO"]:
    raise RuntimeError("Inconsistencia nas somatorias do resumo.")

total_lt1 = camada_lt1.featureCount()
print("=" * 72)
print(f"RESUMO - Associacao {NOME_CAMADA_BASE} x {NOME_CAMADA_LT1}")
print("=" * 72)
for status in STATUS_VALIDOS:
    print(f"{status + ':':44} {contagem[status]}")
print(f"{'Soma dos status:':44} {soma_status}")
print(f"{'Total de OAEs em ' + NOME_CAMADA_BASE + ':':44} {contagem['TOTAL']}")
print()
print(f"{'Pontos em ' + NOME_CAMADA_LT1 + ':':44} {total_lt1}")
print(f"{'  usados (associados):':44} {len(lt1_usados)}")
print(f"{'  nao usados:':44} {total_lt1 - len(lt1_usados)}")
print(f"{'  com geometria invalida:':44} {len(invalidos_lt1)}")
print()
print(f"Campos copiados de {NOME_CAMADA_LT1} ({len(nomes_copiados)}):")
for nome in nomes_copiados:
    print(f"  {nome}")
print(f"Camada criada: {NOME_CAMADA_SAIDA}")
print()
print("PARAMETROS DESTA EXECUCAO")
print(f"  RAIO_BUFFER_M:       {RAIO_BUFFER_M}")
print(f"  PREFIXO_CAMPOS_LT1:  {PREFIXO_CAMPOS_LT1}")
print(f"  CRS metrico:         {CRS_METRICA.authid()}")
print(f"  TOLERANCIA_EMPATE_M: {TOLERANCIA_EMPATE_M}")
print("=" * 72)
