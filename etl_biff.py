"""Leitor de planilha .xls do Excel 97-2003 (BIFF8 dentro de um contêiner OLE2), só com a biblioteca padrão.

É a leitura do formato em que os ERPs legados exportam relatórios. O leitor abre o contêiner, percorre
o fluxo da pasta de trabalho uma vez, registro a registro, e devolve as células COM VALOR da aba mais
preenchida, com o tipo que o arquivo guarda (texto, número, booleano ou data). Não executa nada do
arquivo, não segue referência externa, não abre outro fluxo do contêiner e não grava em disco.

O que lê
  1. Contêiner OLE2: cabeçalho, setores de 512 e de 4096 bytes, FAT (com os setores extras da DIFAT),
     diretório e o fluxo "Workbook", em setores comuns ou, quando tem menos de 4096 bytes, no mini-fluxo
     (mini-FAT). Macros, objetos embutidos e propriedades do documento ficam sem abrir.
  2. Globais da pasta: BOF, FILEPASS, DATEMODE, FORMAT, XF, BOUNDSHEET, SST e CONTINUE.
  3. Células: LABELSST, LABEL, RSTRING, NUMBER, RK, MULRK, BOOLERR e FORMULA (só o valor guardado; o
     texto de uma fórmula vem no registro STRING seguinte). BLANK e MULBLANK, que são células em branco
     com formatação, não contam em nada. Todo outro registro (desenhos, mesclas, larguras, impressão) é
     pulado pelo tamanho.

Valores
  - texto: str (texto vazio conta como célula vazia);
  - número: float, também quando é inteiro (quem chama converte); infinito e NaN contam como vazio;
  - booleano: bool; erro (#DIV/0!, #N/D) e fórmula sem valor guardado: vazio;
  - data e data com hora: datetime.datetime, arredondada ao milissegundo, quando a célula tem formato
    de data e o número de série cai na faixa de datas: de 01/03/1900 (série 61) a 31/12/9999 no sistema
    1900 e de 02/01/1904 (série 1) a 31/12/9999 no sistema 1904. Fora da faixa (hora sem data, duração,
    número com formato de data por engano) o valor continua número.

Recusas (LeituraRecusada, sempre com código e mensagem fixos, sem dado do arquivo)
  - "invalid": não é um contêiner OLE2, ou a estrutura está quebrada ou incoerente (arquivo truncado,
    ciclo ou cruzamento nas cadeias de setores, registro maior que o BIFF8 permite, subfluxo sem fim,
    aba fora do fluxo, índice de texto ou de formato que não existe, coluna fora do formato);
  - "unsupported": variante que o leitor não lê (Excel 5.0/95 e anteriores, arquivo com senha);
  - "sheets", "rows", "columns", "cells" e "field_chars": os limites de `ler`, aplicados durante a
    leitura ("cells" também quando a área da aba escolhida, linhas x colunas, passa de `max_area`);
    "empty": arquivo vazio ou sem nenhuma célula com valor.

A memória acompanha as células com valor e o tamanho do arquivo: ficam na memória a FAT, a tabela de
textos (os bytes como vieram; cada texto só é decodificado quando uma célula o usa) e as células da
aba em leitura e da melhor aba até ali. A matriz devolvida é retangular e custa linhas x colunas: por isso
`max_area`, quando dado, é conferido ANTES de ela ser montada (duas células distantes num arquivo de
poucos bytes não ocupam a área inteira).
"""

from __future__ import annotations

from array import array
from collections import namedtuple
from datetime import datetime, timedelta
import struct

# --- Contêiner OLE2 (Compound File Binary) ---------------------------------------------------------------------
_ASSINATURA_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FIM_DE_CADEIA = 0xFFFFFFFE
_SETOR_LIVRE = 0xFFFFFFFF
_MAIOR_SETOR = 0xFFFFFFFA            # acima disto o número é um marcador (fim, livre, FAT, DIFAT), não um setor
_SEM_ENTRADA = 0xFFFFFFFF            # entrada do diretório sem irmã ou sem filha
_CORTE_DO_MINI_FLUXO = 4096          # fluxo menor que isto fica no mini-fluxo, em mini-setores de 64 bytes
_MINI_SETOR = 64
_ENTRADA = 128                       # bytes de uma entrada do diretório
_BLOCO = 64 * 1024                   # o fluxo é lido em blocos de até 64 KiB (alguns milhares de registros)
# Planilha de Excel antigo gravada sem contêiner: o arquivo começa pelo registro BOF (identificador, tamanho).
_BOF_SEM_CONTEINER = frozenset(((0x0009, 4), (0x0209, 6), (0x0409, 6), (0x0809, 8), (0x0809, 16)))

# --- Registros do BIFF8 que o leitor interpreta ----------------------------------------------------------------
_BOF, _EOF, _CONTINUE = 0x0809, 0x000A, 0x003C
_FILEPASS, _DATEMODE = 0x002F, 0x0022
_FORMAT, _XF, _BOUNDSHEET, _SST = 0x041E, 0x00E0, 0x0085, 0x00FC
_LABELSST, _LABEL, _RSTRING = 0x00FD, 0x0204, 0x00D6
_NUMBER, _RK, _MULRK = 0x0203, 0x027E, 0x00BD
_BOOLERR, _FORMULA, _STRING = 0x0205, 0x0006, 0x0207
_SHRFMLA, _ARRAY, _TABLE = 0x04BC, 0x0221, 0x0236     # podem vir entre a FORMULA e o seu STRING
_BOF_ANTIGOS = (0x0009, 0x0209, 0x0409)               # BIFF2, BIFF3 e BIFF4
_DOS_GLOBAIS = frozenset((_BOF, _EOF, _CONTINUE, _FILEPASS, _DATEMODE, _FORMAT, _XF, _BOUNDSHEET, _SST,
                          *_BOF_ANTIGOS))
_DA_ABA = frozenset((_BOF, _EOF, _CONTINUE, _LABELSST, _LABEL, _RSTRING, _NUMBER, _RK, _MULRK, _BOOLERR,
                     _FORMULA, _STRING, _SHRFMLA, _ARRAY, _TABLE))
_BIFF8, _BIFF5 = 0x0600, 0x0500
_SUBFLUXO_DE_GLOBAIS, _SUBFLUXO_DE_PLANILHA = 0x0005, 0x0010
_MAIOR_REGISTRO = 8224               # bytes de dados de um registro do BIFF8
_MAIOR_COLUNA = 255                  # a planilha do BIFF8 vai da coluna 0 à 255 (a linha cabe em 16 bits)
_MAIOR_NUMERO = 1.7976931348623157e308
_SEM_LIMITE = 0xFFFF                 # texto que não é de célula (formato numérico) não tem limite de caracteres

# Formatos numéricos embutidos do Excel que mostram data ou hora (os demais índices são de número ou texto).
_FORMATOS_EMBUTIDOS_DE_DATA = frozenset((*range(14, 23), *range(27, 37), *range(45, 48), *range(50, 59),
                                         *range(71, 82)))
_LETRAS_DE_DATA = frozenset("dmyhsDMYHS")
_MARCAS_DE_DIGITO = frozenset("0#?")
_XF_DE_NUMERO, _XF_DE_DATA, _XF_INEXISTENTE = 0, 1, 2

_CABECALHO = struct.Struct("<HH")
_U16 = struct.Struct("<H").unpack_from
_U32 = struct.Struct("<I").unpack_from
_DOUBLE = struct.Struct("<d").unpack
_DOIS_U32 = struct.Struct("<II")
_CELULA = struct.Struct("<HHH").unpack_from              # linha, coluna e formato (XF)
_CELULA_COM_NUMERO = struct.Struct("<HHHd").unpack_from
_CELULA_COM_U32 = struct.Struct("<HHHI").unpack_from     # LABELSST (índice do texto) e RK (número compacto)
_CELULA_COM_RESULTADO = struct.Struct("<HHH8s").unpack_from
_XF_E_RK = struct.Struct("<HI").unpack_from

# --- Mensagens (fixas: nunca levam dado do arquivo) ------------------------------------------------------------
_ARQUIVO_VAZIO = "O arquivo está vazio."
_NAO_E_XLS = "O conteúdo do arquivo não é o de uma planilha .xls do Excel 97-2003."
_CONTEINER = "A estrutura do arquivo .xls é inválida ou o arquivo está truncado."
_SEM_PASTA = "O arquivo .xls não contém uma pasta de trabalho do Excel."
_PASTA = "A planilha .xls contém registros inválidos ou está truncada."
_EXCEL_95 = ("Planilha no formato do Excel 5.0/95, que não é lido. Abra o arquivo, salve como .xlsx "
             "e envie novamente.")
_ANTIGO = ("Planilha em formato do Excel anterior ao 97-2003, que não é lido. Abra o arquivo, salve como .xlsx "
           "e envie novamente.")
_CIFRADO = ("A planilha .xls está protegida por senha, e o conteúdo não pode ser lido. Remova a senha, salve "
            "como .xlsx e envie novamente.")
_ABAS = "O XLS excede o limite de abas permitido."
_LINHAS = "O XLS excede o limite de linhas permitido."
_COLUNAS = "O XLS excede o limite de colunas permitido."
_CELULAS = "Uma aba do XLS excede o orçamento de células permitido."
_AREA = "Uma aba do XLS excede a área de células permitida."
_CARACTERES = "O XLS contém uma célula acima do limite de caracteres permitido."
_SEM_DADOS = "O XLS não contém dados."

_Limites = namedtuple("_Limites", "abas linhas colunas celulas caracteres area")


class LeituraRecusada(Exception):
    """Arquivo que o leitor recusa. O código e a mensagem são fixos e nunca levam dado do arquivo."""

    def __init__(self, codigo, mensagem):
        self.codigo = codigo
        self.mensagem = mensagem
        super().__init__(mensagem)


def _recusar(codigo, mensagem):
    raise LeituraRecusada(codigo, mensagem)


class _Prazo:
    """Chama a verificação de prazo de quem pediu a leitura e lembra se a exceção em curso saiu dela:
    essa passa adiante como veio; qualquer outra falha inesperada da leitura vira recusa."""

    __slots__ = ("_funcao", "no_chamador")

    def __init__(self, funcao):
        self._funcao = funcao
        self.no_chamador = False

    def __call__(self):
        if self._funcao is not None:
            self.no_chamador = True
            self._funcao()
            self.no_chamador = False


# =====================================================================================================================
# Contêiner OLE2
# =====================================================================================================================
def _recusar_o_que_nao_e_ole2(inicio):
    """Sem a assinatura do contêiner, ou o arquivo é uma planilha de Excel antigo gravada sem contêiner
    (começa pelo registro BOF), ou não é uma planilha .xls."""
    if len(inicio) >= 4 and _CABECALHO.unpack_from(inicio) in _BOF_SEM_CONTEINER:
        _recusar("unsupported", _ANTIGO)
    _recusar("invalid", _NAO_E_XLS)


class _Fluxo:
    """O fluxo "Workbook" de um contêiner OLE2, entregue em blocos a partir de qualquer posição.

    Só o cabeçalho, a FAT, o diretório e esse fluxo são lidos. Todo número de setor é conferido contra
    o tamanho do arquivo, e cada setor só pode ser usado uma vez: um ciclo numa cadeia, ou duas cadeias
    que se cruzam, viram recusa em vez de laço."""

    def __init__(self, arquivo, prazo):
        self._arquivo = arquivo
        self._prazo = prazo
        arquivo.seek(0, 2)
        self._bytes = arquivo.tell()
        arquivo.seek(0)
        cabecalho = arquivo.read(512)
        if not self._bytes:
            _recusar("empty", _ARQUIVO_VAZIO)
        if len(cabecalho) < 512 or not cabecalho.startswith(_ASSINATURA_OLE2):
            _recusar_o_que_nao_e_ole2(cabecalho)
        ordem, expoente, expoente_do_mini = struct.unpack_from("<3H", cabecalho, 0x1C)
        (inicio_do_diretorio, _, corte, inicio_da_minifat, _, inicio_da_difat,
         setores_da_difat) = struct.unpack_from("<7I", cabecalho, 0x30)
        if ordem != 0xFFFE or expoente not in (9, 12) or expoente_do_mini != 6 or corte != _CORTE_DO_MINI_FLUXO:
            _recusar("invalid", _CONTEINER)
        self._setor = 1 << expoente
        self._inteiros = struct.Struct(f"<{self._setor // 4}I").unpack
        # O cabeçalho ocupa o primeiro setor; o setor 0 vem logo depois dele. O último pode vir incompleto.
        self._total = (self._bytes - 1) // self._setor
        self._usados = bytearray(self._total)
        self._fat = self._ler_fat(cabecalho, inicio_da_difat, setores_da_difat)
        diretorio = b"".join(self._ler_setor(numero, incompleto=True)
                             for numero in self._cadeia(inicio_do_diretorio))
        inicio, tamanho, inicio_da_raiz, tamanho_da_raiz = self._localizar(diretorio)
        if not 0 < tamanho <= self._bytes:
            _recusar("invalid", _CONTEINER)
        self.tamanho = tamanho
        self._pequeno = self._setores_do_fluxo = None
        if tamanho < _CORTE_DO_MINI_FLUXO:
            self._pequeno = self._ler_do_mini_fluxo(inicio, inicio_da_raiz, tamanho_da_raiz, inicio_da_minifat)
        else:
            self._setores_do_fluxo = self._cadeia(inicio, -(-tamanho // self._setor))

    def _usar(self, numero):
        """Marca o setor como usado; recusa o que fica fora do arquivo e o que já foi usado."""
        if numero >= self._total or self._usados[numero]:
            _recusar("invalid", _CONTEINER)
        self._usados[numero] = 1

    def _ler_setor(self, numero, incompleto=False):
        self._arquivo.seek((numero + 1) * self._setor)
        dados = self._arquivo.read(self._setor)
        if len(dados) != self._setor:
            if not incompleto:
                _recusar("invalid", _CONTEINER)
            dados = dados.ljust(self._setor, b"\x00")  # só o diretório: entrada zerada é entrada sem uso
        return dados

    def _ler_fat(self, cabecalho, inicio_da_difat, setores_da_difat):
        """A tabela de alocação: para cada setor, o setor seguinte da mesma cadeia. Os setores que a
        guardam estão listados na DIFAT: 109 no cabeçalho e os demais em setores encadeados."""
        da_fat = []

        def listar(numeros):
            for numero in numeros:
                if numero < _FIM_DE_CADEIA:  # "fim" e "livre" só preenchem a lista
                    self._usar(numero)
                    da_fat.append(numero)

        listar(struct.unpack_from("<109I", cabecalho, 0x4C))
        proximo = inicio_da_difat
        # Sem setores extras, o começo da cadeia deveria ser "fim"; há gravadores que deixam "livre" ou zero.
        if setores_da_difat or proximo not in (0, _FIM_DE_CADEIA, _SETOR_LIVRE):
            while proximo <= _MAIOR_SETOR:
                self._usar(proximo)
                numeros = self._inteiros(self._ler_setor(proximo))
                listar(numeros[:-1])
                proximo = numeros[-1]
        if not da_fat:
            _recusar("invalid", _CONTEINER)
        fat = array("I")
        for indice, numero in enumerate(da_fat):
            if not indice & 0x3FF:
                self._prazo()
            fat.extend(self._inteiros(self._ler_setor(numero)))
        return fat

    def _cadeia(self, inicio, quantos=None):
        """Os setores de uma cadeia da FAT, do primeiro ao fim ou até completar `quantos`. O laço para
        porque cada volta marca um setor novo como usado."""
        fat, cadeia, numero = self._fat, array("I"), inicio
        while numero != _FIM_DE_CADEIA and (quantos is None or len(cadeia) < quantos):
            self._usar(numero)
            if numero >= len(fat):
                _recusar("invalid", _CONTEINER)
            cadeia.append(numero)
            numero = fat[numero]
        if quantos is not None and len(cadeia) < quantos:
            _recusar("invalid", _CONTEINER)  # o fluxo declara mais bytes do que a cadeia tem
        return cadeia

    def _localizar(self, diretorio):
        """Acha o fluxo da pasta de trabalho entre as entradas da raiz do diretório. Devolve o primeiro
        setor e o tamanho dele e os da raiz (que guarda o mini-fluxo).

        As entradas formam uma árvore de irmãs; ela é percorrida com uma pilha e cada entrada só é
        visitada uma vez. O que está dentro de pastas (objeto embutido, projeto de macros) não é visto."""
        quantas = len(diretorio) // _ENTRADA
        if not quantas or diretorio[0x42] != 5:
            _recusar("invalid", _CONTEINER)
        visitadas = bytearray(quantas)
        visitadas[0] = 1
        pilha = [_U32(diretorio, 0x4C)[0]]
        achados = {}
        while pilha:
            numero = pilha.pop()
            if numero == _SEM_ENTRADA:
                continue
            if numero >= quantas or visitadas[numero]:
                _recusar("invalid", _CONTEINER)
            visitadas[numero] = 1
            base = numero * _ENTRADA
            pilha.extend(struct.unpack_from("<II", diretorio, base + 0x44))
            comprimento = _U16(diretorio, base + 0x40)[0]
            if diretorio[base + 0x42] != 2 or not 2 <= comprimento <= 64 or comprimento & 1:
                continue  # só fluxos com nome
            nome = diretorio[base:base + comprimento - 2].decode("utf-16-le", "replace").lower()
            if nome in ("workbook", "book", "encryptedpackage"):
                if nome in achados:
                    _recusar("invalid", _CONTEINER)
                achados[nome] = struct.unpack_from("<IQ", diretorio, base + 0x74)
        if "workbook" not in achados:
            if "book" in achados:
                _recusar("unsupported", _EXCEL_95)
            if "encryptedpackage" in achados:  # pasta do Excel 2007 em diante gravada com senha
                _recusar("unsupported", _CIFRADO)
            _recusar("invalid", _SEM_PASTA)
        inicio, tamanho = achados["workbook"]
        inicio_da_raiz, tamanho_da_raiz = struct.unpack_from("<IQ", diretorio, 0x74)
        if self._setor == 512:  # com setores de 512 bytes o tamanho tem 32 bits; a metade alta pode vir com lixo
            tamanho &= 0xFFFFFFFF
            tamanho_da_raiz &= 0xFFFFFFFF
        return inicio, tamanho, inicio_da_raiz, tamanho_da_raiz

    def _ler_do_mini_fluxo(self, inicio, inicio_da_raiz, tamanho_da_raiz, inicio_da_minifat):
        """O fluxo inteiro (menos de 4096 bytes), tirado dos mini-setores de 64 bytes que ficam dentro do
        fluxo da raiz e são encadeados pela mini-FAT."""
        if tamanho_da_raiz > self._bytes:
            _recusar("invalid", _CONTEINER)
        setor = self._setor
        minifat = array("I")
        for numero in self._cadeia(inicio_da_minifat):
            minifat.extend(self._inteiros(self._ler_setor(numero)))
        recipiente = self._cadeia(inicio_da_raiz, -(-tamanho_da_raiz // setor))
        capacidade = len(recipiente) * setor
        partes, vistos, numero, restante = [], set(), inicio, self.tamanho
        while restante > 0:  # no máximo 64 voltas: cada uma lê ao menos um byte de um fluxo de até 4095
            if numero >= len(minifat) or numero in vistos or (numero + 1) * _MINI_SETOR > capacidade:
                _recusar("invalid", _CONTEINER)
            vistos.add(numero)
            indice, dentro = divmod(numero * _MINI_SETOR, setor)
            pedir = min(_MINI_SETOR, restante)
            self._arquivo.seek((recipiente[indice] + 1) * setor + dentro)
            dados = self._arquivo.read(pedir)
            if len(dados) != pedir:
                _recusar("invalid", _CONTEINER)
            partes.append(dados)
            restante -= pedir
            numero = minifat[numero]
        return b"".join(partes)

    def blocos(self, desde):
        """Gera os bytes do fluxo a partir da posição `desde`, em blocos de até 64 KiB, na ordem da
        cadeia de setores (setores vizinhos no arquivo saem numa leitura só)."""
        if self._pequeno is not None:
            if desde < self.tamanho:
                yield self._pequeno[desde:]
            return
        arquivo, setor, cadeia = self._arquivo, self._setor, self._setores_do_fluxo
        indice, dentro = divmod(desde, setor)
        restante = self.tamanho - desde
        por_bloco = _BLOCO // setor
        while restante > 0:
            proximo = indice + 1
            limite = min(len(cadeia), indice + por_bloco)
            while proximo < limite and cadeia[proximo] == cadeia[proximo - 1] + 1:
                proximo += 1
            pedir = min(restante, (proximo - indice) * setor - dentro)
            arquivo.seek((cadeia[indice] + 1) * setor + dentro)
            dados = arquivo.read(pedir)
            if len(dados) != pedir:
                _recusar("invalid", _CONTEINER)
            yield dados
            restante -= pedir
            indice, dentro = proximo, 0


# =====================================================================================================================
# Registros e textos do BIFF8
# =====================================================================================================================
def _registros(blocos, desde, interesse, prazo):
    """Gera (identificador, bloco, início dos dados no bloco, tamanho dos dados, posição do registro no
    fluxo) dos registros cujo identificador está em `interesse`; os outros são pulados pelo tamanho.

    Quem consome lê os dados com struct.unpack_from(bloco, início), sem fatiar. O prazo é conferido a
    cada bloco. Se o fluxo acaba no meio de um registro, o gerador só termina: quem consome recusa o
    arquivo por não ter visto o EOF do subfluxo."""
    cabecalho = _CABECALHO.unpack_from
    base, resto = desde, b""
    for bloco in blocos:
        prazo()
        if resto:
            bloco = resto + bloco
        posicao, fim = 0, len(bloco)
        while posicao + 4 <= fim:
            identificador, tamanho = cabecalho(bloco, posicao)
            if tamanho > _MAIOR_REGISTRO:
                _recusar("invalid", _PASTA)
            seguinte = posicao + 4 + tamanho
            if seguinte > fim:
                break
            if identificador in interesse:
                yield identificador, bloco, posicao + 4, tamanho, base + posicao
            posicao = seguinte
        resto = bloco[posicao:]
        base += posicao


def _conferir_bof(registro, posicao, especie):
    """O subfluxo começa na posição dada por um BOF do BIFF8 e da espécie esperada (globais ou planilha)?"""
    if registro is None:
        _recusar("invalid", _PASTA)
    identificador, bloco, o, n, inicio = registro
    if inicio != posicao:
        _recusar("invalid", _PASTA)
    if identificador in _BOF_ANTIGOS:
        _recusar("unsupported", _ANTIGO)
    if identificador != _BOF or n < 4:
        _recusar("invalid", _PASTA)
    versao, encontrada = struct.unpack_from("<HH", bloco, o)
    if versao == _BIFF5:
        _recusar("unsupported", _EXCEL_95)
    if versao != _BIFF8 or encontrada != especie:
        _recusar("invalid", _PASTA)


def _caracteres(dados, o, fim, largo, faltam, pedacos):
    """Tira de dados[o:fim] até `faltam` caracteres (um byte cada, ou dois quando `largo`) e os junta a
    `pedacos`, se for uma lista. Devolve (caracteres que ainda faltam, posição depois do que foi lido)."""
    disponivel = fim - o
    if disponivel < 0:
        _recusar("invalid", _PASTA)
    if largo:
        tirados = min(faltam, disponivel >> 1)
        if tirados < faltam and disponivel & 1:
            _recusar("invalid", _PASTA)  # um caractere de dois bytes não se parte entre dois registros
        ate = o + 2 * tirados
    else:
        tirados = min(faltam, disponivel)
        ate = o + tirados
    if tirados and pedacos is not None:
        pedacos.append((largo, dados[o:ate]))
    return faltam - tirados, ate


def _decodificar(pedacos):
    """O texto guardado em um ou mais pedaços (largo?, bytes): Latin-1 de um byte por caractere ou
    UTF-16. Com mais de um pedaço largo a decodificação é feita de uma vez, porque um par substituto
    pode ficar partido entre dois registros. Código inválido vira U+FFFD."""
    if len(pedacos) == 1:
        largo, dados = pedacos[0]
        return dados.decode("utf-16-le", "replace") if largo else dados.decode("latin-1")
    if not any(largo for largo, _ in pedacos):
        return b"".join(dados for _, dados in pedacos).decode("latin-1")
    return b"".join(dados if largo else dados.decode("latin-1").encode("utf-16-le")
                    for largo, dados in pedacos).decode("utf-16-le", "replace")


def _texto_do_registro(bloco, o, fim, max_caracteres):
    """Começo de um texto guardado no próprio registro: dois bytes de comprimento, um de opções e os
    caracteres que couberem. Devolve (caracteres que faltam, pedaços lidos)."""
    if o + 3 > fim:
        _recusar("invalid", _PASTA)
    caracteres, opcoes = bloco[o] | bloco[o + 1] << 8, bloco[o + 2]
    if caracteres > max_caracteres:
        _recusar("field_chars", _CARACTERES)
    o += 3 + (2 if opcoes & 0x08 else 0) + (4 if opcoes & 0x04 else 0)  # contagens de formatação e de fonético
    pedacos = []
    faltam, _ = _caracteres(bloco, o, fim, opcoes & 0x01, caracteres, pedacos)
    return faltam, pedacos


class _Textos:
    """Tabela de textos compartilhados (o registro SST e as suas continuações).

    Guarda os bytes dos registros e onde começa cada texto; um texto só é decodificado quando uma
    célula o usa, e uma vez só. Assim a memória acompanha o tamanho do arquivo e os textos usados, e
    não a quantidade de textos que a tabela diz ter."""

    def __init__(self, partes, max_caracteres, prazo):
        self._partes = partes
        self._parte = array("I")       # registro (SST ou CONTINUE) em que cada texto começa
        self._posicao = array("H")     # posição do texto dentro desse registro
        self._com_valor = bytearray()  # 1 quando o texto tem ao menos um caractere
        self._prontos = {}
        self.quantidade = 0
        if not partes:
            return
        if len(partes[0]) < 8:
            _recusar("invalid", _PASTA)
        quantidade = _U32(partes[0], 4)[0]
        if quantidade > (sum(map(len, partes)) - 8) // 3:  # cada texto ocupa ao menos três bytes
            _recusar("invalid", _PASTA)
        parte, posicao = 0, 8
        for indice in range(quantidade):
            if not indice & 0xFFF:
                prazo()
            self._parte.append(parte)
            self._posicao.append(posicao)
            parte, posicao, caracteres = self._percorrer(parte, posicao, None)
            if caracteres > max_caracteres:
                _recusar("field_chars", _CARACTERES)
            self._com_valor.append(caracteres > 0)
        self.quantidade = quantidade

    def _percorrer(self, parte, posicao, pedacos):
        """Atravessa o texto que começa em (parte, posição) e junta os seus caracteres a `pedacos`, se
        for uma lista. Devolve (parte, posição) do que vem depois e a quantidade de caracteres.

        O cabeçalho do texto nunca se parte entre dois registros. Os caracteres podem continuar no
        registro seguinte, que então começa por um byte de opção dizendo se dali em diante cada caractere
        tem um ou dois bytes. As listas de formatação e de fonético, que vêm depois dos caracteres,
        continuam no registro seguinte sem esse byte. Cada volta dos dois laços avança um registro."""
        partes = self._partes
        if posicao == len(partes[parte]):  # o texto anterior acabou junto com o registro
            parte, posicao = parte + 1, 0
            if parte == len(partes):
                _recusar("invalid", _PASTA)
        dados = partes[parte]
        fim = len(dados)
        if posicao + 3 > fim:
            _recusar("invalid", _PASTA)
        caracteres, opcoes = dados[posicao] | dados[posicao + 1] << 8, dados[posicao + 2]
        posicao += 3
        depois = 0  # bytes das listas de formatação (4 por trecho) e de fonético
        if opcoes & 0x08:
            if posicao + 2 > fim:
                _recusar("invalid", _PASTA)
            depois = 4 * (dados[posicao] | dados[posicao + 1] << 8)
            posicao += 2
        if opcoes & 0x04:
            if posicao + 4 > fim:
                _recusar("invalid", _PASTA)
            depois += _U32(dados, posicao)[0]
            posicao += 4
        largo = opcoes & 0x01
        faltam = caracteres
        while faltam:
            faltam, posicao = _caracteres(dados, posicao, fim, largo, faltam, pedacos)
            if faltam:
                parte += 1
                if parte == len(partes) or not partes[parte]:
                    _recusar("invalid", _PASTA)
                dados = partes[parte]
                fim = len(dados)
                largo, posicao = dados[0] & 0x01, 1
        while depois > fim - posicao:
            depois -= fim - posicao
            parte += 1
            if parte == len(partes):
                _recusar("invalid", _PASTA)
            fim, posicao = len(partes[parte]), 0
        return parte, posicao + depois, caracteres

    def texto(self, indice):
        """O texto de número `indice`; None quando ele é vazio."""
        pronto = self._prontos.get(indice)
        if pronto is None:
            if indice >= self.quantidade:
                _recusar("invalid", _PASTA)
            if not self._com_valor[indice]:
                return None
            pedacos = []
            self._percorrer(self._parte[indice], self._posicao[indice], pedacos)
            pronto = self._prontos[indice] = _decodificar(pedacos)
        return pronto

    def presenca(self, indice):
        """True quando o texto de número `indice` tem valor, sem decodificá-lo (leitura só de contagem)."""
        if indice >= self.quantidade:
            _recusar("invalid", _PASTA)
        return True if self._com_valor[indice] else None


def _e_formato_de_data(formato):
    """O formato numérico personalizado mostra data ou hora?

    Fora de aspas, de colchetes e de caractere escapado (o que vem depois de barra invertida, sublinhado
    ou asterisco), conta as letras de data (d, m, y, h, s) e as marcas de dígito (0, #, ?): é data
    quando há mais letras de data que marcas de dígito. "dd/mm/yy", "[h]:mm" e "mm:ss.0" são datas;
    "#,##0.00", "0.0", '0 "dias"' e o "000 de " da paginação de um ERP (um "d" solto depois de três
    zeros) não são. É a regra da biblioteca xlrd, que o pandas usa para ler .xls."""
    datas = digitos = 0
    entre_aspas = entre_colchetes = escapado = False
    for caractere in formato:
        if entre_aspas:
            entre_aspas = caractere != '"'
        elif escapado:
            escapado = False
        elif entre_colchetes:
            entre_colchetes = caractere != "]"
        elif caractere == '"':
            entre_aspas = True
        elif caractere in "\\_*":
            escapado = True
        elif caractere == "[":
            entre_colchetes = True
        elif caractere in _LETRAS_DE_DATA:
            datas += 1
        elif caractere in _MARCAS_DE_DIGITO:
            digitos += 1
    return datas > digitos


def _numero_do_rk(rk):
    """O número de um RK: inteiro de 30 bits com sinal ou os 30 bits altos de um double, com um bit que
    manda dividir por 100."""
    if rk & 2:
        numero = rk >> 2
        if numero & 0x20000000:
            numero -= 0x40000000
        numero = float(numero)
    else:
        numero = _DOUBLE(_DOIS_U32.pack(0, rk & 0xFFFFFFFC))[0]
    return numero / 100.0 if rk & 1 else numero


def _leitor_de_numeros(tipos_de_xf, sistema_1904, materializar):
    """Devolve a função que dá o valor de uma célula numérica a partir do seu formato (XF) e do número."""
    if sistema_1904:
        base, primeira, limite = datetime(1904, 1, 1), 1.0, 2957004.0
    else:  # a série 60 é o 29/02/1900 que não existiu; a partir da 61 a conta fecha com a base em 30/12/1899
        base, primeira, limite = datetime(1899, 12, 30), 61.0, 2958466.0

    def valor(xf, numero):
        tipo = tipos_de_xf[xf]
        if tipo:
            if tipo == _XF_INEXISTENTE:
                _recusar("invalid", _PASTA)
            if primeira <= numero < limite:
                dias = int(numero)
                try:
                    return base + timedelta(days=dias, milliseconds=round((numero - dias) * 86400000.0))
                except OverflowError:  # o arredondamento passou de 31/12/9999
                    return numero
        return numero if -_MAIOR_NUMERO <= numero <= _MAIOR_NUMERO else None

    def presenca(xf, numero):
        if tipos_de_xf[xf] == _XF_INEXISTENTE:
            _recusar("invalid", _PASTA)
        return True if -_MAIOR_NUMERO <= numero <= _MAIOR_NUMERO else None

    return valor if materializar else presenca


# =====================================================================================================================
# Células de uma aba
# =====================================================================================================================
class _Celulas:
    """As células com valor de uma aba: {linha: {coluna: valor}}.

    Só o que tem valor ocupa memória: uma célula sozinha na última linha e na última coluna custa o
    mesmo que qualquer outra. A área (linhas x colunas) só é ocupada pela matriz devolvida."""

    __slots__ = ("linhas", "contagem", "_max_linhas", "_max_colunas", "_max_celulas", "_indice", "_atual")

    def __init__(self, limites):
        self.linhas = {}
        self.contagem = 0
        self._max_linhas, self._max_colunas, self._max_celulas = limites.linhas, limites.colunas, limites.celulas
        self._indice, self._atual = -1, None

    def guardar(self, linha, coluna, valor):
        """Registra o valor de uma célula, com os limites conferidos na hora. Vale o último registro de
        uma mesma célula; None (erro, texto vazio) apaga o valor que ela tinha."""
        if linha == self._indice:
            celulas = self._atual
        else:
            celulas = self.linhas.get(linha)
            if celulas is None:
                if valor is None:
                    return
                if linha >= self._max_linhas:
                    _recusar("rows", _LINHAS)
                celulas = self.linhas[linha] = {}
            self._indice, self._atual = linha, celulas
        if valor is None:
            if celulas.pop(coluna, None) is not None:
                self.contagem -= 1
            return
        if coluna not in celulas:
            if coluna > _MAIOR_COLUNA:
                _recusar("invalid", _PASTA)
            if coluna >= self._max_colunas:
                _recusar("columns", _COLUNAS)
            self.contagem += 1
            if self.contagem > self._max_celulas:
                _recusar("cells", _CELULAS)
        celulas[coluna] = valor

    def fechar(self):
        """Devolve (linhas, colunas) contadas até a última célula com valor."""
        ocupadas = [indice for indice, celulas in self.linhas.items() if celulas]  # um valor apagado esvazia a linha
        if not ocupadas:
            return 0, 0
        return max(ocupadas) + 1, max(max(celulas) for celulas in self.linhas.values() if celulas) + 1

    def matriz(self, linhas, colunas, prazo):
        """A matriz retangular linhas x colunas, com None nas células vazias (as linhas saem da memória
        de trabalho à medida que entram na matriz)."""
        valores = []
        for indice in range(linhas):
            if not indice & 0x3FF:
                prazo()
            linha = [None] * colunas
            celulas = self.linhas.pop(indice, None)
            if celulas:
                for coluna, valor in celulas.items():
                    linha[coluna] = valor
            valores.append(linha)
        return valores


# =====================================================================================================================
# Globais e abas
# =====================================================================================================================
def _ler_globais(fluxo, limites, prazo, materializar):
    """Lê o subfluxo de globais, do BOF ao EOF. Devolve os deslocamentos das planilhas na ordem em que
    o arquivo as lista (abas ocultas contam; gráfico, folha de macro e módulo não), a tabela de textos,
    a função que dá o valor das células numéricas e a posição em que os globais terminam."""
    planilhas = []
    formatos = {}        # índice do formato numérico -> é formato de data?
    xfs = array("H")     # índice do formato numérico de cada XF
    partes = []          # dados do SST e das suas continuações
    continua_em = -1     # posição em que um CONTINUE ainda pertence ao SST
    sistema_1904 = False
    registros = _registros(fluxo.blocos(0), 0, _DOS_GLOBAIS, prazo)
    _conferir_bof(next(registros, None), 0, _SUBFLUXO_DE_GLOBAIS)
    for identificador, bloco, o, n, inicio in registros:
        if identificador == _CONTINUE:
            if inicio == continua_em:
                partes.append(bloco[o:o + n])
                continua_em = inicio + 4 + n
        elif identificador == _XF:
            if n < 4 or len(xfs) > 0xFFFF:
                _recusar("invalid", _PASTA)
            xfs.append(_U16(bloco, o + 2)[0])
        elif identificador == _FORMAT:
            if n < 5:
                _recusar("invalid", _PASTA)
            faltam, pedacos = _texto_do_registro(bloco, o + 2, o + n, _SEM_LIMITE)
            if faltam:
                _recusar("invalid", _PASTA)
            formatos[_U16(bloco, o)[0]] = _e_formato_de_data(_decodificar(pedacos)) if pedacos else False
        elif identificador == _BOUNDSHEET:
            if n < 6:
                _recusar("invalid", _PASTA)
            if bloco[o + 5] == 0:  # planilha; os outros tipos (macro, gráfico, módulo) não são lidos
                if len(planilhas) >= limites.abas:
                    _recusar("sheets", _ABAS)
                planilhas.append(_U32(bloco, o)[0])
        elif identificador == _SST:
            if partes:
                _recusar("invalid", _PASTA)
            partes.append(bloco[o:o + n])
            continua_em = inicio + 4 + n
        elif identificador == _DATEMODE:
            if n < 2:
                _recusar("invalid", _PASTA)
            sistema_1904 = _U16(bloco, o)[0] != 0
        elif identificador == _FILEPASS:
            _recusar("unsupported", _CIFRADO)
        elif identificador == _EOF:
            fim = inicio + 4 + n
            break
        else:  # outro BOF dentro dos globais
            _recusar("invalid", _PASTA)
    else:
        _recusar("invalid", _PASTA)  # o fluxo acabou sem o EOF dos globais
    tipos_de_xf = bytearray((_XF_INEXISTENTE,)) * 0x10000
    for indice, formato in enumerate(xfs):
        e_data = formatos.get(formato)
        if e_data is None:
            e_data = formato in _FORMATOS_EMBUTIDOS_DE_DATA
        tipos_de_xf[indice] = _XF_DE_DATA if e_data else _XF_DE_NUMERO
    textos = _Textos(partes, limites.caracteres, prazo)
    return planilhas, textos, _leitor_de_numeros(bytes(tipos_de_xf), sistema_1904, materializar), fim


def _ler_aba(fluxo, deslocamento, de_texto, de_numero, limites, prazo, materializar):
    """Lê o subfluxo de uma planilha, do BOF no deslocamento dado ao seu EOF. Devolve as células com
    valor e a posição em que o subfluxo termina."""
    registros = _registros(fluxo.blocos(deslocamento), deslocamento, _DA_ABA, prazo)
    _conferir_bof(next(registros, None), deslocamento, _SUBFLUXO_DE_PLANILHA)
    celulas = _Celulas(limites)
    guardar = celulas.guardar
    max_caracteres = limites.caracteres
    pendente = None   # fórmula de texto à espera do seu STRING: (linha, coluna, posição em que ele tem de vir)
    em_curso = None   # texto de STRING que continua em CONTINUE: [linha, coluna, faltam, pedaços, posição]
    for identificador, bloco, o, n, inicio in registros:
        if identificador == _NUMBER:
            if n < 14:
                _recusar("invalid", _PASTA)
            linha, coluna, xf, numero = _CELULA_COM_NUMERO(bloco, o)
            guardar(linha, coluna, de_numero(xf, numero))
        elif identificador == _LABELSST:
            if n < 10:
                _recusar("invalid", _PASTA)
            linha, coluna, _, indice = _CELULA_COM_U32(bloco, o)
            guardar(linha, coluna, de_texto(indice))
        elif identificador == _RK:
            if n < 10:
                _recusar("invalid", _PASTA)
            linha, coluna, xf, rk = _CELULA_COM_U32(bloco, o)
            guardar(linha, coluna, de_numero(xf, _numero_do_rk(rk)))
        elif identificador == _MULRK:  # linha, primeira coluna, (XF, RK) de cada coluna, última coluna
            quantos, sobra = divmod(n - 6, 6)
            if n < 12 or sobra:
                _recusar("invalid", _PASTA)
            linha, coluna = _CABECALHO.unpack_from(bloco, o)
            if _U16(bloco, o + n - 2)[0] != coluna + quantos - 1:
                _recusar("invalid", _PASTA)
            for posicao in range(o + 4, o + n - 2, 6):
                xf, rk = _XF_E_RK(bloco, posicao)
                guardar(linha, coluna, de_numero(xf, _numero_do_rk(rk)))
                coluna += 1
        elif identificador == _FORMULA:  # só o valor guardado; a expressão não é lida
            if n < 14:
                _recusar("invalid", _PASTA)
            linha, coluna, xf, resultado = _CELULA_COM_RESULTADO(bloco, o)
            if resultado[6] != 0xFF or resultado[7] != 0xFF:
                guardar(linha, coluna, de_numero(xf, _DOUBLE(resultado)[0]))
            elif resultado[0] == 1:
                guardar(linha, coluna, resultado[2] != 0)
            else:  # 0: texto, que vem no registro STRING; 2: erro; 3: texto vazio
                guardar(linha, coluna, None)
                if resultado[0] == 0:
                    pendente = (linha, coluna, inicio + 4 + n)
        elif identificador == _EOF:
            if em_curso is not None:
                _recusar("invalid", _PASTA)  # o texto de uma fórmula ficou pela metade
            return celulas, inicio + 4 + n
        elif identificador == _BOF:  # subfluxo embutido (gráfico): pulado inteiro, até o seu EOF
            abertos = 1
            for identificador, bloco, o, n, inicio in registros:
                if identificador == _BOF:
                    abertos += 1
                elif identificador == _EOF:
                    abertos -= 1
                    if not abertos:
                        break
            else:
                _recusar("invalid", _PASTA)
        elif identificador == _STRING:
            if pendente is not None and inicio == pendente[2]:
                if em_curso is not None:
                    _recusar("invalid", _PASTA)
                faltam, pedacos = _texto_do_registro(bloco, o, o + n, max_caracteres)
                if faltam:
                    em_curso = [pendente[0], pendente[1], faltam, pedacos, inicio + 4 + n]
                elif pedacos:
                    guardar(pendente[0], pendente[1], _decodificar(pedacos) if materializar else True)
                pendente = None
        elif identificador == _CONTINUE:
            if em_curso is not None and inicio == em_curso[4]:
                if not n:
                    _recusar("invalid", _PASTA)
                em_curso[2], _ = _caracteres(bloco, o + 1, o + n, bloco[o] & 0x01, em_curso[2], em_curso[3])
                em_curso[4] = inicio + 4 + n
                if not em_curso[2]:
                    guardar(em_curso[0], em_curso[1], _decodificar(em_curso[3]) if materializar else True)
                    em_curso = None
            elif pendente is not None and inicio == pendente[2]:
                pendente = (pendente[0], pendente[1], inicio + 4 + n)
        elif identificador == _BOOLERR:  # valor e, no byte seguinte, se o valor é um código de erro
            if n < 8:
                _recusar("invalid", _PASTA)
            linha, coluna, _ = _CELULA(bloco, o)
            guardar(linha, coluna, None if bloco[o + 7] else bloco[o + 6] != 0)
        elif identificador == _LABEL or identificador == _RSTRING:
            if n < 9:
                _recusar("invalid", _PASTA)
            linha, coluna, _ = _CELULA(bloco, o)
            faltam, pedacos = _texto_do_registro(bloco, o + 6, o + n, max_caracteres)
            if faltam:
                _recusar("invalid", _PASTA)
            guardar(linha, coluna, (_decodificar(pedacos) if materializar else True) if pedacos else None)
        elif pendente is not None and inicio == pendente[2]:  # SHRFMLA, ARRAY ou TABLE antes do STRING
            pendente = (pendente[0], pendente[1], inicio + 4 + n)
    _recusar("invalid", _PASTA)  # o fluxo acabou sem o EOF da aba


def _ler(arquivo, limites, prazo, materializar):
    fluxo = _Fluxo(arquivo, prazo)
    planilhas, textos, de_numero, fim = _ler_globais(fluxo, limites, prazo, materializar)
    de_texto = textos.texto if materializar else textos.presenca
    escolhida = indice_da_escolhida = None
    no_arquivo = 0
    # As abas são lidas na ordem em que estão no fluxo, cada uma depois do fim da anterior.
    for deslocamento, indice in sorted(zip(planilhas, range(len(planilhas)))):
        if not fim <= deslocamento < fluxo.tamanho:
            _recusar("invalid", _PASTA)
        celulas, fim = _ler_aba(fluxo, deslocamento, de_texto, de_numero, limites, prazo, materializar)
        no_arquivo += celulas.contagem
        if (escolhida is None or celulas.contagem > escolhida.contagem
                or (celulas.contagem == escolhida.contagem and indice < indice_da_escolhida)):
            escolhida, indice_da_escolhida = celulas, indice
    if escolhida is None or not escolhida.contagem:
        _recusar("empty", _SEM_DADOS)
    linhas, colunas = escolhida.fechar()
    # A área é conferida antes de a matriz existir: é a matriz, e não a leitura, que custa linhas x colunas.
    if limites.area is not None and linhas * colunas > limites.area:
        _recusar("cells", _AREA)
    return {"abas": len(planilhas), "aba": indice_da_escolhida, "linhas": linhas, "colunas": colunas,
            "celulas": escolhida.contagem, "celulas_no_arquivo": no_arquivo,
            "valores": escolhida.matriz(linhas, colunas, prazo) if materializar else None}


def ler(caminho, *, max_abas, max_linhas, max_colunas, max_celulas, max_caracteres, max_area=None,
        verificar=None, materializar=True) -> dict:
    """Lê a planilha .xls em `caminho` e devolve um dicionário com:

      - "abas": quantas planilhas a pasta tem (as ocultas contam; gráfico e folha de macro, não);
      - "aba": o índice, entre as planilhas, da escolhida: a de mais células com valor (no empate, a primeira);
      - "linhas" e "colunas": o tamanho da aba escolhida, até a última célula com valor;
      - "celulas": as células com valor da aba escolhida; "celulas_no_arquivo": as de todas as abas;
      - "valores": com `materializar`, a lista das linhas, todas com "colunas" valores e None nas células
        vazias; cada valor é str, float, bool ou datetime.datetime. Sem `materializar`, None: a leitura
        confere e conta tudo sem decodificar os textos nem montar a matriz.

    Os limites valem durante a leitura: `max_abas` planilhas, `max_linhas` e `max_colunas` até a última
    célula com valor, `max_celulas` células COM VALOR por aba (não a área) e `max_caracteres` por texto.
    `max_area`, se dado, é o teto de linhas x colunas da aba escolhida, conferido antes de a matriz ser
    montada (com ou sem `materializar`).
    `verificar`, se dado, é chamada sem argumentos a cada bloco de até 64 KiB do fluxo (alguns milhares de
    registros) e a cada alguns milhares de textos e de linhas; a exceção que ela levantar passa adiante.

    Levanta LeituraRecusada (atributos `codigo` e `mensagem`) para todo arquivo que não é lido; nenhuma
    outra exceção sai por causa do conteúdo do arquivo. MemoryError e OSError passam adiante."""
    prazo = _Prazo(verificar)
    limites = _Limites(max_abas, max_linhas, max_colunas, max_celulas, max_caracteres, max_area)
    try:
        with open(caminho, "rb") as arquivo:
            return _ler(arquivo, limites, prazo, materializar)
    except (LeituraRecusada, MemoryError, OSError):
        raise
    except Exception:
        if prazo.no_chamador:
            raise
        raise LeituraRecusada("invalid", _PASTA) from None
