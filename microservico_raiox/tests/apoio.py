"""Apoio dos testes: configuração de teste, planilhas montadas em memória e dublês do banco e do Storage."""

import io
import zipfile

from armazenamento import ErroDeArmazenamento
from configuracao import Configuracao
from repositorio import ErroDeBanco, TetoDoDiaAtingido

ORIGEM = "https://landing.exemplo.com.br"
_PLANILHA = "application/vnd.openxmlformats-officedocument.spreadsheetml."
PRINCIPAL = _PLANILHA + "sheet.main+xml"
PRINCIPAL_COM_MACRO = "application/vnd.ms-excel.sheet.macroEnabled.main+xml"
PRINCIPAL_DE_MODELO = _PLANILHA + "template.main+xml"
TIPO_DA_ABA = _PLANILHA + "worksheet+xml"
TIPO_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RELACAO = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="{principal}"/>'
    '<Override PartName="/{aba}" ContentType="' + TIPO_DA_ABA + '"/>{tipos}</Types>')
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="' + RELACAO + 'officeDocument" Target="xl/workbook.xml"/>{relacoes}'
    '</Relationships>')
_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="Vendas" sheetId="1" r:id="rId1"/></sheets>{nomes}</workbook>')
_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="' + RELACAO + 'worksheet" Target="{alvo}"/>{relacoes}</Relationships>')


def configuracao(**troca) -> Configuracao:
    base = {"database_url": "postgresql://usuario@banco.exemplo/nexo",
            "supabase_url": "https://abcdefghijklmnopqrst.supabase.co", "supabase_key": "chave-de-teste",
            "origens": (ORIGEM,), "termos_versao": "termos-teste-v1", "proxies_confiaveis": 0}
    base.update(troca)
    return Configuracao(**base)


def folha(celulas='<c r="A1" t="inlineStr"><is><t>Produto</t></is></c><c r="B1"><v>10</v></c>', antes="",
          depois="") -> str:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'{antes}<sheetData><row r="1">{celulas}</row></sheetData>{depois}</worksheet>')


def override(parte: str, tipo: str) -> str:
    return f'<Override PartName="/{parte}" ContentType="{tipo}"/>'


def default(extensao: str, tipo: str) -> str:
    return f'<Default Extension="{extensao}" ContentType="{tipo}"/>'


def relacao(identificador: str, tipo: str, alvo: str, externa: bool = False) -> str:
    tipo = tipo if "://" in tipo else RELACAO + tipo
    modo = ' TargetMode="External"' if externa else ""
    return f'<Relationship Id="{identificador}" Type="{tipo}" Target="{alvo}"{modo}/>'


def xlsx(principal=PRINCIPAL, planilha=None, extras=None, tipos="", relacoes="", relacoes_da_raiz="",
         nomes_definidos="", aba="xl/worksheets/sheet1.xml", content_types=None, comprimir=zipfile.ZIP_DEFLATED,
         partes=None) -> bytes:
    """Uma planilha .xlsx mínima e válida; os parâmetros acrescentam (ou trocam) partes, tipos e relações.
    `partes`, quando dado, substitui as partes de mesmo nome antes de gravar (inclusive as do esqueleto)."""
    alvo_da_aba = aba[len("xl/"):] if aba.startswith("xl/") else "/" + aba
    conteudo = {
        "[Content_Types].xml": content_types if content_types is not None else _CONTENT_TYPES.format(
            principal=principal, aba=aba, tipos=tipos),
        "_rels/.rels": _RELS.format(relacoes=relacoes_da_raiz),
        "xl/workbook.xml": _WORKBOOK.format(
            nomes=f"<definedNames>{nomes_definidos}</definedNames>" if nomes_definidos else ""),
        "xl/_rels/workbook.xml.rels": _WORKBOOK_RELS.format(alvo=alvo_da_aba, relacoes=relacoes),
        aba: planilha if planilha is not None else folha(),
    }
    conteudo.update(extras or {})
    conteudo.update(partes or {})
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w", comprimir) as pacote:
        for nome, dados in conteudo.items():
            pacote.writestr(nome, dados)
    return memoria.getvalue()


def com_bit_de_cifra(dados: bytes, nome: str) -> bytes:
    """O pacote com a parte `nome` marcada como cifrada no diretório central (bit 0 das flags)."""
    nome_em_bytes = nome.encode()
    posicao = 0
    while True:
        posicao = dados.index(b"PK\x01\x02", posicao)
        tamanho_do_nome = int.from_bytes(dados[posicao + 28:posicao + 30], "little")
        if dados[posicao + 46:posicao + 46 + tamanho_do_nome] == nome_em_bytes:
            flags = int.from_bytes(dados[posicao + 8:posicao + 10], "little") | 0x1
            return dados[:posicao + 8] + flags.to_bytes(2, "little") + dados[posicao + 10:]
        posicao += 4


CSV = "Codigo;Produto;Quantidade;Total\n1;Café 500g;10;-12,50\n2;Arroz 5kg;5;50,00\n".encode("utf-8")


class RepositorioFalso:
    """O banco: conta os leads do dia, grava (com o teto conferido na gravação) e confere se um id existe."""

    def __init__(self, hoje=0, falhar_ao_gravar=False, falhar_ao_contar=False, gravar_e_falhar=False,
                 falhar_ao_conferir=False, leads_que_chegam_antes=0):
        self.hoje = hoje
        self.falhar_ao_gravar = falhar_ao_gravar
        self.falhar_ao_contar = falhar_ao_contar
        self.gravar_e_falhar = gravar_e_falhar
        self.falhar_ao_conferir = falhar_ao_conferir
        self.leads_que_chegam_antes = leads_que_chegam_antes
        self.gravados = []
        self.contagens = 0
        self.conferencias = 0
        self.pedidos_de_gravacao = []

    def leads_desde(self, instante):
        self.contagens += 1
        if self.falhar_ao_contar:
            raise ErroDeBanco("o banco não respondeu (OperationalError)")
        return self.hoje

    def gravar(self, lead, teto, desde):
        self.pedidos_de_gravacao.append({"teto": teto, "desde": desde})
        # Leads de outros envios que chegaram entre a primeira contagem e a gravação.
        self.hoje += self.leads_que_chegam_antes
        self.leads_que_chegam_antes = 0
        if self.falhar_ao_gravar:
            raise ErroDeBanco("o banco recusou a operação (IntegrityError)")
        if self.hoje >= teto:
            raise TetoDoDiaAtingido()
        self.gravados.append(lead)
        self.hoje += 1
        if self.gravar_e_falhar:
            # O COMMIT foi aplicado e a resposta se perdeu.
            raise ErroDeBanco("o banco recusou a operação (OperationalError)")

    def existe(self, lead_id):
        self.conferencias += 1
        if self.falhar_ao_conferir:
            raise ErroDeBanco("o banco não respondeu (OperationalError)")
        return any(lead["id"] == lead_id for lead in self.gravados)

    def conferir_tabela(self):
        if self.falhar_ao_contar:
            raise ErroDeBanco("o banco recusou a operação (UndefinedColumn)")


class ArmazenamentoFalso:
    def __init__(self, falhar_no_envio=None, falhar_ao_apagar=False, gravar_e_falhar_no_envio=None,
                 erro_no_envio=None, bucket_publico=False, falhar_no_bucket=False):
        self.objetos = {}
        self.apagados = []
        self.envios = 0
        self._falhar_no_envio = falhar_no_envio
        self._falhar_ao_apagar = falhar_ao_apagar
        self._gravar_e_falhar_no_envio = gravar_e_falhar_no_envio
        self._erro_no_envio = erro_no_envio
        self._bucket_publico = bucket_publico
        self._falhar_no_bucket = falhar_no_bucket

    def conferir_bucket(self):
        if self._falhar_no_bucket:
            raise ErroDeArmazenamento("o Storage respondeu 403", 403)
        return not self._bucket_publico

    def enviar(self, caminho, conteudo, tipo):
        self.envios += 1
        if self._falhar_no_envio == self.envios:
            raise self._erro_no_envio or ErroDeArmazenamento("o Storage respondeu 500", 500)
        self.objetos[caminho] = (conteudo, tipo)
        if self._gravar_e_falhar_no_envio == self.envios:
            # O Storage gravou, mas a resposta não chegou no prazo.
            raise ErroDeArmazenamento("o Storage não respondeu (TimeoutError)")

    def apagar(self, caminho):
        if self._falhar_ao_apagar:
            raise ErroDeArmazenamento("o Storage respondeu 500", 500)
        self.apagados.append(caminho)
        return self.objetos.pop(caminho, None) is not None
