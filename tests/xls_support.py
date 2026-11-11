"""Gerador mínimo de planilhas .xls FICTÍCIAS (BIFF8 em contêiner OLE2), só com a biblioteca padrão.

Serve aos testes de etl_biff. Monta, byte a byte, o contêiner (cabeçalho, FAT, DIFAT, diretório e
mini-fluxo) e os registros da pasta de trabalho, e devolve o mapa de onde cada parte ficou, para um
teste poder corromper um ponto exato do arquivo. Não é um gravador de planilhas de uso geral: grava só
o que um leitor precisa para achar as células, mais o que os relatórios de ERP costumam trazer em volta
delas (linhas, mesclas, desenhos), que o leitor tem de pular.

São três camadas:
  1. registros do BIFF8: `registro` e as funções de célula (`numero`, `rk`, `varios_rk`, `rotulo`,
     `texto_compartilhado`, `booleano`, `erro`, `formula`, `branco`, `brancos`) e `tabela_de_textos`,
     que parte o SST em registros CONTINUE no tamanho pedido;
  2. `Pasta` e `Aba`: a pasta de trabalho (formatos, textos compartilhados, abas) e o fluxo "Workbook";
  3. `conteiner`: o arquivo OLE2, com setores de 512 ou de 4096 bytes, na ordem pedida.

Todos os dados são inventados. Sem import do app, de config nem de biblioteca de planilha.
"""

from datetime import datetime
import struct

ASSINATURA = bytes.fromhex("d0cf11e0a1b11ae1")
LIVRE, FIM_DE_CADEIA, SETOR_DA_FAT, SETOR_DA_DIFAT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC
SEM_ENTRADA = 0xFFFFFFFF
CORTE_DO_MINI_FLUXO = 4096
MINI_SETOR = 64
MAIOR_REGISTRO = 8224

BOF, EOF, CONTINUE = 0x0809, 0x000A, 0x003C
FILEPASS, CODEPAGE, DATEMODE = 0x002F, 0x0042, 0x0022
FORMAT, XF, BOUNDSHEET, SST = 0x041E, 0x00E0, 0x0085, 0x00FC
DIMENSIONS, ROW, MERGEDCELLS, OBJ = 0x0200, 0x0208, 0x00E5, 0x005D
LABELSST, LABEL, RSTRING, NUMBER, RK, MULRK = 0x00FD, 0x0204, 0x00D6, 0x0203, 0x027E, 0x00BD
BLANK, MULBLANK, BOOLERR, FORMULA, STRING, SHRFMLA = 0x0201, 0x00BE, 0x0205, 0x0006, 0x0207, 0x04BC
BIFF8, BIFF5 = 0x0600, 0x0500
GLOBAIS, PLANILHA, GRAFICO, FOLHA_DE_MACRO = 0x0005, 0x0010, 0x0020, 0x0040
# Tipo da aba no registro BOUNDSHEET -> espécie do subfluxo no BOF.
ESPECIE_DA_ABA = {0: PLANILHA, 1: FOLHA_DE_MACRO, 2: GRAFICO, 6: 0x0006}
PREENCHIMENTO = 0x0FEE  # identificador sem uso no BIFF8: registro que todo leitor pula pelo tamanho

FORMATO_DE_DATA = "dd/mm/yy"
FORMATO_DE_DATA_E_HORA = "dd/MM/yyyy hh:mm"
FORMATO_DE_DINHEIRO = "#,##0.00;(#,##0.00)"
FORMATO_DE_QUANTIDADE = "0.0"
FORMATO_DE_PAGINA = "000 de "  # a paginação "N de M" de um ERP: tem a letra "d" solta e NÃO é data


# =====================================================================================================================
# 1. Registros do BIFF8
# =====================================================================================================================
def registro(identificador, dados=b""):
    """Um registro: identificador, tamanho dos dados (16 bits cada) e os dados."""
    return struct.pack("<HH", identificador, len(dados)) + dados


def bof(especie, versao=BIFF8):
    return registro(BOF, struct.pack("<HHHHII", versao, especie, 0x0DBB, 0x07CC, 0x000000C1, 0x00000306))


FIM = registro(EOF)


def unidades(valor):
    """As unidades UTF-16 do texto (o BIFF8 conta o comprimento nelas)."""
    bruto = valor.encode("utf-16-le", "surrogatepass")
    return struct.unpack(f"<{len(bruto) // 2}H", bruto)


def texto(valor, *, largo=None, bytes_do_comprimento=2):
    """Texto dentro de um registro: comprimento, byte de opções e caracteres, com um byte por caractere
    quando todos cabem em Latin-1 (ou quando `largo` é False) e em UTF-16 no resto dos casos."""
    partes = unidades(valor)
    if largo is None:
        largo = any(unidade > 0xFF for unidade in partes)
    cabeca = struct.pack("<H" if bytes_do_comprimento == 2 else "<B", len(partes)) + bytes([1 if largo else 0])
    return cabeca + (struct.pack(f"<{len(partes)}H", *partes) if largo else bytes(partes))


def numero(linha, coluna, valor, xf=0):
    return registro(NUMBER, struct.pack("<HHHd", linha, coluna, xf, valor))


def rk_de_inteiro(inteiro, centesimos=False):
    """RK com um inteiro de 30 bits com sinal; com `centesimos`, o leitor divide por 100."""
    return (inteiro << 2) & 0xFFFFFFFC | 2 | (1 if centesimos else 0)


def rk_de_real(real, centesimos=False):
    """RK com os 30 bits altos de um double (o número precisa ter os 34 bits baixos zerados)."""
    bits = struct.unpack("<Q", struct.pack("<d", real))[0]
    if bits & 0x3FFFFFFFF:
        raise ValueError("o número não cabe em um RK")
    return (bits >> 32) & 0xFFFFFFFC | (1 if centesimos else 0)


def rk(linha, coluna, compacto, xf=0):
    return registro(RK, struct.pack("<HHHI", linha, coluna, xf, compacto))


def varios_rk(linha, coluna, compactos, xf=0, ultima=None):
    """MULRK: números compactos em colunas seguidas de uma mesma linha."""
    dados = struct.pack("<HH", linha, coluna) + b"".join(struct.pack("<HI", xf, c) for c in compactos)
    return registro(MULRK, dados + struct.pack("<H", coluna + len(compactos) - 1 if ultima is None else ultima))


def texto_compartilhado(linha, coluna, indice, xf=0):
    return registro(LABELSST, struct.pack("<HHHI", linha, coluna, xf, indice))


def rotulo(linha, coluna, valor, xf=0, *, largo=None, identificador=LABEL):
    """LABEL (ou RSTRING): o texto guardado no próprio registro da célula."""
    return registro(identificador, struct.pack("<HHH", linha, coluna, xf) + texto(valor, largo=largo))


def booleano(linha, coluna, valor, xf=0):
    return registro(BOOLERR, struct.pack("<HHHBB", linha, coluna, xf, 1 if valor else 0, 0))


def erro(linha, coluna, codigo=0x07, xf=0):
    """Célula com erro (0x07 é #DIV/0!)."""
    return registro(BOOLERR, struct.pack("<HHHBB", linha, coluna, xf, codigo, 1))


class Erro:
    """Resultado de erro de uma fórmula."""

    def __init__(self, codigo=0x07):
        self.codigo = codigo


def formula(linha, coluna, valor, xf=0, *, largo=None, com_texto=True, limite=MAIOR_REGISTRO):
    """FORMULA com o valor guardado: número, booleano, Erro, texto (que vai num registro STRING logo em
    seguida, partido em CONTINUE quando passa de `limite`) ou "" (texto vazio, sem STRING)."""
    seguinte = b""
    if isinstance(valor, bool):
        resultado = struct.pack("<BBBBHH", 1, 0, 1 if valor else 0, 0, 0, 0xFFFF)
    elif isinstance(valor, Erro):
        resultado = struct.pack("<BBBBHH", 2, 0, valor.codigo, 0, 0, 0xFFFF)
    elif valor == "":
        resultado = struct.pack("<BBBBHH", 3, 0, 0, 0, 0, 0xFFFF)
    elif isinstance(valor, str):
        resultado = struct.pack("<BBBBHH", 0, 0, 0, 0, 0, 0xFFFF)
        if com_texto:
            seguinte = texto_de_formula(valor, largo=largo, limite=limite)
    else:
        resultado = struct.pack("<d", valor)
    expressao = b"\x1e\x00\x00"  # a constante inteira 0: o leitor não interpreta a expressão
    cabeca = struct.pack("<HHH", linha, coluna, xf) + resultado + struct.pack("<HIH", 0, 0, len(expressao))
    return registro(FORMULA, cabeca + expressao) + seguinte


def texto_de_formula(valor, *, largo=None, limite=MAIOR_REGISTRO):
    """O registro STRING de uma fórmula de texto e os CONTINUE em que ele continua."""
    partes = unidades(valor)
    if largo is None:
        largo = any(unidade > 0xFF for unidade in partes)
    largura = 2 if largo else 1
    opcao = bytes([1 if largo else 0])
    corpo = struct.pack(f"<{len(partes)}H", *partes) if largo else bytes(partes)
    cabem = (limite - 3) // largura * largura
    saida = registro(STRING, struct.pack("<H", len(partes)) + opcao + corpo[:cabem])
    corpo = corpo[cabem:]
    cabem = (limite - 1) // largura * largura
    while corpo:
        saida += registro(CONTINUE, opcao + corpo[:cabem])
        corpo = corpo[cabem:]
    return saida


def branco(linha, coluna, xf=0):
    """BLANK: célula em branco que só tem formatação."""
    return registro(BLANK, struct.pack("<HHH", linha, coluna, xf))


def brancos(linha, coluna, quantas, xf=0):
    """MULBLANK: células em branco com formatação em colunas seguidas."""
    dados = struct.pack("<HH", linha, coluna) + struct.pack("<H", xf) * quantas
    return registro(MULBLANK, dados + struct.pack("<H", coluna + quantas - 1))


def linha_da_aba(linha, primeira=0, depois_da_ultima=19):
    """ROW: a descrição de uma linha (altura e colunas usadas)."""
    return registro(ROW, struct.pack("<HHHHHHI", linha, primeira, depois_da_ultima, 255, 0, 0, 0x0100))


def dimensoes(linhas, colunas):
    return registro(DIMENSIONS, struct.pack("<IIHHH", 0, linhas, 0, colunas, 0))


def mesclas(intervalos):
    """MERGEDCELLS: (primeira linha, última linha, primeira coluna, última coluna) de cada mescla."""
    return registro(MERGEDCELLS, struct.pack("<H", len(intervalos))
                    + b"".join(struct.pack("<HHHH", *intervalo) for intervalo in intervalos))


def desenho(tamanho=44):
    """OBJ: um objeto desenhado (a linha traçada de um relatório). Só bytes, sem nada para executar."""
    return registro(OBJ, bytes([1, 0, 0, 0, 1, 0]) + bytes(range(tamanho - 6)))


def preenchimento(tamanho):
    """Registros sem significado que somam exatamente `tamanho` bytes (múltiplo de 4, no mínimo 4)."""
    partes = []
    while tamanho:
        dados = min(tamanho - 4, MAIOR_REGISTRO)  # 8224 é múltiplo de 4: o que sobra sempre cabe num registro
        partes.append(registro(PREENCHIMENTO, bytes(dados)))
        tamanho -= 4 + dados
    return b"".join(partes)


class Texto:
    """Um texto da tabela compartilhada e como gravá-lo: `largo` força UTF-16 (True) ou um byte por
    caractere (False); `trechos` acrescenta trechos de formatação (texto rico); `fonetico`, um bloco de
    leitura fonética."""

    def __init__(self, valor, *, largo=None, trechos=0, fonetico=b""):
        self.valor, self.largo, self.trechos, self.fonetico = valor, largo, trechos, fonetico


def tabela_de_textos(textos, *, limite=MAIOR_REGISTRO, quantidade=None, largo_na_continuacao=None):
    """O registro SST e os CONTINUE em que ele continua, cada um com até `limite` bytes de dados.

    As regras são as do formato: o cabeçalho de um texto e cada trecho de formatação nunca se partem; os
    caracteres se partem entre caracteres, e o registro seguinte começa por um byte de opção; o bloco
    fonético se parte em qualquer ponto, sem byte de opção. `largo_na_continuacao` troca a largura dos
    caracteres depois de um corte (True: UTF-16; False: um byte, se couber). `quantidade` mente no
    cabeçalho sobre quantos textos a tabela tem."""
    declarados = len(textos) if quantidade is None else quantidade
    prontos, atual = [], bytearray(struct.pack("<II", declarados, declarados))

    def fechar():
        prontos.append(bytes(atual))
        atual.clear()

    for item in textos:
        if not isinstance(item, Texto):
            item = Texto(item)
        partes = unidades(item.valor)
        largo = item.largo if item.largo is not None else any(unidade > 0xFF for unidade in partes)
        cabeca = struct.pack("<HB", len(partes), (1 if largo else 0) | (8 if item.trechos else 0)
                             | (4 if item.fonetico else 0))
        if item.trechos:
            cabeca += struct.pack("<H", item.trechos)
        if item.fonetico:
            cabeca += struct.pack("<I", len(item.fonetico))
        if len(atual) + len(cabeca) > limite:
            fechar()
        atual += cabeca
        posicao = 0
        while posicao < len(partes):
            cabem = (limite - len(atual)) // (2 if largo else 1)
            if cabem <= 0:
                fechar()
                if largo_na_continuacao is not None:
                    largo = largo_na_continuacao or any(unidade > 0xFF for unidade in partes[posicao:])
                atual.append(1 if largo else 0)
                continue
            pedaco = partes[posicao:posicao + cabem]
            atual += struct.pack(f"<{len(pedaco)}H", *pedaco) if largo else bytes(pedaco)
            posicao += len(pedaco)
        for trecho in range(item.trechos):
            if len(atual) + 4 > limite:
                fechar()
            atual += struct.pack("<HH", trecho, 0)
        fonetico = item.fonetico
        while fonetico:
            if len(atual) >= limite:
                fechar()
            cabem = limite - len(atual)
            atual += fonetico[:cabem]
            fonetico = fonetico[cabem:]
    if atual or not prontos:
        fechar()
    return b"".join(registro(CONTINUE if indice else SST, dados) for indice, dados in enumerate(prontos))


# =====================================================================================================================
# 2. Pasta de trabalho
# =====================================================================================================================
def serie(valor, sistema_1904=False):
    """O número de série do Excel para uma data (a parte inteira são os dias; a fração, a hora)."""
    passado = valor - (datetime(1904, 1, 1) if sistema_1904 else datetime(1899, 12, 30))
    return passado.days + (passado.seconds + passado.microseconds / 1_000_000) / 86400


class Aba:
    """Uma aba da pasta: os registros das suas células, na ordem em que vão para o arquivo."""

    def __init__(self, pasta, nome, tipo, visibilidade):
        self.pasta, self.nome, self.tipo, self.visibilidade = pasta, nome, tipo, visibilidade
        self.registros = []

    def por(self, linha, coluna, valor, formato=None):
        """Grava uma célula pelo tipo do valor: str vai para a tabela de textos (LABELSST), bool vira
        BOOLERR, int vira RK, float vira NUMBER, datetime vira NUMBER com formato de data e None vira
        BLANK (célula em branco com formatação). `formato` é o texto de um formato numérico personalizado
        ou o número de um formato embutido do Excel."""
        pasta = self.pasta
        if isinstance(valor, datetime) and formato is None:
            formato = FORMATO_DE_DATA if valor.time() == datetime.min.time() else FORMATO_DE_DATA_E_HORA
        xf = pasta.xf(formato) if formato is not None else 0
        if valor is None:
            dados = branco(linha, coluna, xf)
        elif isinstance(valor, bool):
            dados = booleano(linha, coluna, valor, xf)
        elif isinstance(valor, str):
            dados = texto_compartilhado(linha, coluna, pasta.texto(valor), xf)
        elif isinstance(valor, datetime):
            dados = numero(linha, coluna, serie(valor, pasta.sistema_1904), xf)
        elif isinstance(valor, int) and -(1 << 29) <= valor < (1 << 29):
            dados = rk(linha, coluna, rk_de_inteiro(valor), xf)
        else:
            dados = numero(linha, coluna, float(valor), xf)
        self.registros.append(dados)
        return self

    def bruto(self, *registros):
        """Acrescenta registros já montados (os testes de estrutura usam)."""
        self.registros.extend(registros)
        return self

    def subfluxo(self):
        return bof(ESPECIE_DA_ABA[self.tipo]) + b"".join(self.registros) + FIM


class Pasta:
    """Uma pasta de trabalho fictícia: formatos numéricos, textos compartilhados e abas."""

    def __init__(self, *, sistema_1904=False):
        self.sistema_1904 = sistema_1904
        self.textos = []
        self._indice_do_texto = {}
        self.formatos = {}            # índice (a partir de 164) -> texto do formato personalizado
        self.xfs = [0]                # formato numérico de cada XF; o XF 0 é o formato Geral
        self._xf_do_formato = {0: 0}
        self.abas = []

    def xf(self, formato):
        """O índice do XF com o formato numérico dado (criado na primeira vez)."""
        if formato not in self._xf_do_formato:
            indice = formato
            if isinstance(formato, str):
                indice = 164 + len(self.formatos)
                self.formatos[indice] = formato
            self.xfs.append(indice)
            self._xf_do_formato[formato] = len(self.xfs) - 1
        return self._xf_do_formato[formato]

    def texto(self, valor):
        """O índice do texto na tabela compartilhada (cada texto entra uma vez)."""
        if valor not in self._indice_do_texto:
            self._indice_do_texto[valor] = len(self.textos)
            self.textos.append(valor)
        return self._indice_do_texto[valor]

    def aba(self, nome="Plan1", *, tipo=0, visibilidade=0):
        """Nova aba. `tipo`: 0 planilha, 1 folha de macro, 2 gráfico; `visibilidade`: 0 visível, 1 oculta."""
        nova = Aba(self, nome, tipo, visibilidade)
        self.abas.append(nova)
        return nova

    def globais(self, deslocamentos, *, sst=None, antes_de_tudo=b"", antes_do_fim=b"", versao=BIFF8):
        """O subfluxo de globais. `sst` troca a tabela de textos por registros já montados; `antes_de_tudo`
        entra logo depois do BOF (é onde fica o FILEPASS de um arquivo cifrado)."""
        partes = [bof(GLOBAIS, versao), antes_de_tudo, registro(CODEPAGE, struct.pack("<H", 1200)),
                  registro(DATEMODE, struct.pack("<H", 1 if self.sistema_1904 else 0))]
        partes.extend(registro(FORMAT, struct.pack("<H", indice) + texto(formato))
                      for indice, formato in self.formatos.items())
        partes.extend(registro(XF, struct.pack("<HHHBBBBIIH", 0, formato, 0, 0, 0, 0, 0, 0, 0, 0))
                      for formato in self.xfs)
        partes.extend(registro(BOUNDSHEET, struct.pack("<IBB", deslocamento, aba.visibilidade, aba.tipo)
                               + texto(aba.nome, bytes_do_comprimento=1))
                      for aba, deslocamento in zip(self.abas, deslocamentos))
        partes.append(tabela_de_textos(self.textos) if sst is None else sst)
        partes.extend((antes_do_fim, FIM))
        return b"".join(partes)

    def fluxo(self, *, minimo=CORTE_DO_MINI_FLUXO, deslocamentos=None, **opcoes):
        """Os bytes do fluxo "Workbook": os globais e um subfluxo por aba. Abaixo de `minimo` bytes o
        fluxo é completado, dentro dos globais, com registros sem significado: com o padrão ele fica em
        setores comuns, como nos arquivos de verdade; com minimo=0 uma pasta pequena vai para o
        mini-fluxo. `deslocamentos` troca o que os globais dizem sobre onde cada aba começa."""
        subfluxos = [aba.subfluxo() for aba in self.abas]
        enchimento = b""
        for _ in range(2):
            opcoes_dos_globais = dict(opcoes, antes_do_fim=enchimento + opcoes.get("antes_do_fim", b""))
            posicao = len(self.globais([0] * len(subfluxos), **opcoes_dos_globais))
            reais = []
            for subfluxo in subfluxos:
                reais.append(posicao)
                posicao += len(subfluxo)
            if posicao >= minimo:
                break
            enchimento = preenchimento(-(-(minimo - posicao) // 4) * 4)
        if deslocamentos is None:
            deslocamentos = reais
        return self.globais(deslocamentos, **opcoes_dos_globais) + b"".join(subfluxos)

    def arquivo(self, *, setor=512, fat_no_cabecalho=109, por_setor_da_difat=None, ordem=None, outros=(),
                **opcoes):
        """O arquivo .xls: o fluxo "Workbook" (mais os fluxos de `outros`) dentro do contêiner OLE2."""
        return conteiner([("Workbook", self.fluxo(**opcoes)), *outros], setor=setor, ordem=ordem,
                         fat_no_cabecalho=fat_no_cabecalho, por_setor_da_difat=por_setor_da_difat)


PRODUTOS = ("LIXA FICTÍCIA GRÃO 80", "ADESIVO FICTÍCIO P/ LAMINAÇÃO 880G", "SOLVENTE FICTÍCIO 0,9L",
            "MASSA DE REPARO FICTÍCIA 1KG", "ESMALTE FICTÍCIO BRANCO 3,6L", "VERNIZ FICTÍCIO 3,0L")
COLUNAS_DO_RELATORIO = (0, 1, 2, 4, 14, 16, 17)


def relatorio_paginado(paginas=2, itens_por_pagina=3):
    """Uma pasta com um relatório fictício no desenho paginado de um ERP de varejo, e o que um leitor
    tem de achar nela: {(linha, coluna): valor} só das células com valor.

    A planilha é a imagem de uma página impressa: os dados ficam espalhados pelas colunas 0, 1, 2, 4, 14,
    16 e 17, com uma linha em branco entre um item e outro, o cabeçalho repetido a cada página, células em
    branco formatadas (inclusive na coluna 18, depois da última coluna com valor), mesclas, linhas
    desenhadas e um registro DIMENSIONS que declara mais linhas e colunas do que as que têm valor."""
    pasta = Pasta()
    aba = pasta.aba("Report")
    esperado, intervalos = {}, []
    linha = 0

    def celula(coluna, valor, formato=None):
        aba.por(linha, coluna, valor, formato)
        esperado[(linha, coluna)] = float(valor) if type(valor) is int else valor

    linha = 1
    aba.bruto(linha_da_aba(linha))
    celula(4, "Relatório Fictício de Vendas por Cliente")
    celula(14, "Página ...:")
    celula(16, datetime(2026, 11, 9, 17, 3, 38))
    celula(17, 1, FORMATO_DE_PAGINA)
    linha = 2
    aba.bruto(linha_da_aba(linha))
    celula(4, "Período de  : 01/01/26 á 31/01/26 - Andamento: Todas vendas")
    linha = 4
    item = quantidade_total = valor_total = 0
    for _ in range(paginas):
        aba.bruto(linha_da_aba(linha))
        for coluna, titulo in zip(COLUNAS_DO_RELATORIO, ("Data", "Ven.", "Nr. Doc.", "Produto", "Valor Unit.",
                                                           "Qtde", "Valor Total")):
            celula(coluna, titulo)
        linha += 1
        for _ in range(itens_por_pagina):
            unitario = 3.8 + 2.75 * item
            quantidade = 1.0 + item % 3 / 2
            total = round(unitario * quantidade + 0.0081, 4)
            aba.bruto(linha_da_aba(linha))
            celula(0, datetime(2026, 1, 5 + item % 20))
            celula(1, 38 - item % 4)
            celula(2, f"P9-{item + 1:05d}  NFC9-{item + 1:05d}")
            aba.bruto(branco(linha, 3))
            celula(4, PRODUTOS[item % len(PRODUTOS)])
            aba.bruto(brancos(linha, 5, 9))
            celula(14, unitario, FORMATO_DE_DINHEIRO)
            aba.bruto(branco(linha, 15))
            celula(16, quantidade, FORMATO_DE_QUANTIDADE)
            celula(17, total, FORMATO_DE_DINHEIRO)
            aba.bruto(branco(linha, 18), desenho())
            intervalos.append((linha, linha, 4, 13))
            quantidade_total += quantidade
            valor_total += total
            item += 1
            linha += 2  # uma linha em branco depois de cada item
        aba.bruto(linha_da_aba(linha))
        celula(2, "Software fictício de gestão - contato@example.invalid")
        linha += 2
    aba.bruto(linha_da_aba(linha))
    celula(0, "Total Geral >>>")
    celula(16, quantidade_total, FORMATO_DE_QUANTIDADE)
    celula(17, round(valor_total, 4), FORMATO_DE_DINHEIRO)
    aba.bruto(linha_da_aba(linha + 1), brancos(linha + 1, 0, 19), linha_da_aba(linha + 2), branco(linha + 2, 18),
              mesclas(intervalos))
    aba.registros.insert(0, dimensoes(linha + 3, 19))
    return pasta, esperado


class Sorteio:
    """Gerador congruente simples: dá a mesma sequência em qualquer versão do Python, para os testes de
    arquivo corrompido serem sempre os mesmos."""

    def __init__(self, semente):
        self._estado = semente

    def abaixo(self, limite):
        """Um inteiro de 0 a limite - 1."""
        self._estado = (self._estado * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        return (self._estado >> 33) % limite


MODOS_DE_MUTACAO = ("byte do cabeçalho", "bytes soltos", "truncado", "setor trocado", "inteiro extremo",
                    "tamanho de registro")
_EXTREMOS = (b"\xff\xff\xff\xff", b"\x00\x00\x00\x00", b"\xfe\xff\xff\xff", b"\xff\xff\xff\x7f", b"\x01\x00\x00\x00")
_TAMANHOS = (b"\xff\xff", b"\x00\x00", b"\x20\x20", b"\x21\x20")


def mutar(original, sorteio, modo):
    """Uma cópia corrompida do arquivo: um byte do cabeçalho, até oito bytes soltos, um corte, um setor
    zerado ou com lixo, um inteiro de 32 bits trocado por um valor extremo (ponteiros e tamanhos) ou dois
    bytes trocados por um tamanho de registro absurdo."""
    dados = bytearray(original)
    if modo == 0:
        dados[sorteio.abaixo(512)] = sorteio.abaixo(256)
    elif modo == 1:
        for _ in range(1 + sorteio.abaixo(8)):
            dados[sorteio.abaixo(len(dados))] = sorteio.abaixo(256)
    elif modo == 2:
        del dados[1 + sorteio.abaixo(len(dados) - 1):]
    elif modo == 3:
        setor = 1 + sorteio.abaixo(len(dados) // 512 - 1)
        lixo = bytes(512) if sorteio.abaixo(2) else bytes(sorteio.abaixo(256) for _ in range(512))
        dados[setor * 512:(setor + 1) * 512] = lixo
    elif modo == 4:
        posicao = sorteio.abaixo(len(dados) - 4)
        dados[posicao:posicao + 4] = _EXTREMOS[sorteio.abaixo(len(_EXTREMOS))]
    else:
        posicao = 512 + sorteio.abaixo(len(dados) - 514)
        dados[posicao:posicao + 2] = _TAMANHOS[sorteio.abaixo(len(_TAMANHOS))]
    return bytes(dados)


# =====================================================================================================================
# 3. Contêiner OLE2
# =====================================================================================================================
class Arquivo:
    """Os bytes de um arquivo OLE2 e o mapa de onde cada parte ficou."""

    def __init__(self, setor):
        self.setor = setor
        self.dados = bytearray()
        self.cadeias = {}           # nome do fluxo (ou "diretorio", "minifat", "raiz") -> setores, na ordem
        self.setores_da_fat = []
        self.setores_da_difat = []
        self.entradas = {}          # caminho do fluxo -> número da entrada no diretório

    def __bytes__(self):
        return bytes(self.dados)

    def posicao_do_setor(self, numero):
        return (numero + 1) * self.setor

    def posicao_na_fat(self, numero):
        """Onde está, no arquivo, a entrada da FAT que diz qual setor vem depois do setor `numero`."""
        por_setor = self.setor // 4
        return self.posicao_do_setor(self.setores_da_fat[numero // por_setor]) + 4 * (numero % por_setor)

    def posicao_da_entrada(self, caminho, campo=0):
        """Onde está, no arquivo, o campo (deslocamento dentro da entrada) da entrada de diretório do
        fluxo dado; "" é a raiz."""
        numero = self.entradas[caminho]
        por_setor = self.setor // 128
        setor = self.cadeias["diretorio"][numero // por_setor]
        return self.posicao_do_setor(setor) + 128 * (numero % por_setor) + campo

    def trocar(self, posicao, formato, *valores):
        """Regrava bytes do arquivo (struct.pack_into) e devolve o próprio arquivo."""
        struct.pack_into(formato, self.dados, posicao, *valores)
        return self


def _entrada(nome, tipo, *, esquerda=SEM_ENTRADA, direita=SEM_ENTRADA, filha=SEM_ENTRADA, inicio=0, tamanho=0):
    bruto = (nome + "\x00").encode("utf-16-le")
    entrada = bytearray(128)
    entrada[:len(bruto)] = bruto
    struct.pack_into("<HBB", entrada, 0x40, len(bruto), tipo, 1)
    struct.pack_into("<III", entrada, 0x44, esquerda, direita, filha)
    struct.pack_into("<IQ", entrada, 0x74, inicio, tamanho)
    return bytes(entrada)


def _embaralhar(quantos):
    """Permutação fixa de range(quantos) (gerador congruente: a mesma em qualquer versão do Python)."""
    ordem, semente = list(range(quantos)), 20261110
    for indice in range(quantos - 1, 0, -1):
        semente = (semente * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        outro = (semente >> 33) % (indice + 1)
        ordem[indice], ordem[outro] = ordem[outro], ordem[indice]
    return ordem


def conteiner(fluxos, *, setor=512, fat_no_cabecalho=109, por_setor_da_difat=None, ordem=None):
    """Monta um arquivo OLE2 com os fluxos dados, uma lista de (caminho, bytes).

    "Nome" é um fluxo da raiz; "Pasta/Nome" é um fluxo dentro de uma pasta (um nível só). Fluxo com menos
    de 4096 bytes vai para o mini-fluxo, como o formato manda. `ordem` muda a posição dos setores no
    arquivo: None (cada cadeia em sequência: os fluxos, o mini-fluxo, a mini-FAT, o diretório, a FAT e a
    DIFAT), "invertida", "embaralhada" ou uma função que recebe o total de setores e devolve, para cada
    setor dessa sequência, a posição dele no arquivo. `fat_no_cabecalho` limita quantos setores da FAT o
    cabeçalho lista (o resto vai para setores da DIFAT, cada um com até `por_setor_da_difat` entradas).
    Devolve um `Arquivo`."""
    por_setor = setor // 4
    por_setor_da_difat = por_setor - 1 if por_setor_da_difat is None else por_setor_da_difat
    arquivo = Arquivo(setor)

    # Diretório: a raiz, depois cada fluxo ou pasta da raiz, com os fluxos de uma pasta logo depois dela.
    nomes, tipos, conteudos, pais = ["Root Entry"], [5], [b""], [None]
    pastas = {}
    for caminho, dados in fluxos:
        pasta, _, nome = caminho.rpartition("/")
        pai = 0
        if pasta:
            if pasta not in pastas:
                pastas[pasta] = len(nomes)
                nomes.append(pasta)
                tipos.append(1)
                conteudos.append(b"")
                pais.append(0)
            pai = pastas[pasta]
        arquivo.entradas[caminho] = len(nomes)
        nomes.append(nome)
        tipos.append(2)
        conteudos.append(bytes(dados))
        pais.append(pai)
    arquivo.entradas[""] = 0
    filhas = {}
    for indice, pai in enumerate(pais):
        if pai is not None:
            filhas.setdefault(pai, []).append(indice)

    # Mini-fluxo: os fluxos pequenos, em mini-setores de 64 bytes encadeados pela mini-FAT.
    mini_fluxo, minifat, inicio_no_mini = bytearray(), [], {}
    for indice, dados in enumerate(conteudos):
        if tipos[indice] == 2 and 0 < len(dados) < CORTE_DO_MINI_FLUXO:
            quantos = -(-len(dados) // MINI_SETOR)
            inicio_no_mini[indice] = len(minifat)
            minifat.extend(list(range(len(minifat) + 1, len(minifat) + quantos)) + [FIM_DE_CADEIA])
            mini_fluxo += dados.ljust(quantos * MINI_SETOR, b"\x00")
    minifat_bruta = b"".join(struct.pack("<I", proximo) for proximo in minifat)

    # Cadeias de setores comuns, numeradas primeiro em ordem lógica.
    logicas = {}   # nome da cadeia -> (primeiro setor lógico, quantidade)
    total = 0

    def reservar(nome, tamanho):
        nonlocal total
        quantos = -(-tamanho // setor)
        logicas[nome] = (total, quantos)
        total += quantos

    for indice, dados in enumerate(conteudos):
        if tipos[indice] == 2 and len(dados) >= CORTE_DO_MINI_FLUXO:
            reservar(indice, len(dados))
    reservar("raiz", len(mini_fluxo))
    reservar("minifat", len(minifat_bruta))
    reservar("diretorio", 128 * len(nomes))
    da_fat = 1
    while True:
        da_difat = -(-max(0, da_fat - fat_no_cabecalho) // por_setor_da_difat)
        precisa = -(-(total + da_fat + da_difat) // por_setor)
        if precisa == da_fat:
            break
        da_fat = precisa
    reservar("fat", da_fat * setor)
    reservar("difat", da_difat * setor)
    if ordem == "invertida":
        fisico = list(range(total - 1, -1, -1))
    elif ordem == "embaralhada":
        fisico = _embaralhar(total)
    elif callable(ordem):
        fisico = list(ordem(total))
    else:
        fisico = list(range(total))

    def setores(nome):
        primeiro, quantos = logicas[nome]
        return [fisico[logico] for logico in range(primeiro, primeiro + quantos)]

    def inicio(nome):
        cadeia = setores(nome)
        return cadeia[0] if cadeia else FIM_DE_CADEIA

    # FAT e DIFAT.
    fat = [LIVRE] * (da_fat * por_setor)
    for nome in logicas:
        cadeia = setores(nome)
        if nome == "fat":
            marca = SETOR_DA_FAT
        elif nome == "difat":
            marca = SETOR_DA_DIFAT
        else:
            marca = None
        for posicao, numero in enumerate(cadeia):
            fat[numero] = marca if marca else (cadeia[posicao + 1] if posicao + 1 < len(cadeia) else FIM_DE_CADEIA)
    arquivo.setores_da_fat, arquivo.setores_da_difat = setores("fat"), setores("difat")
    no_cabecalho = arquivo.setores_da_fat[:fat_no_cabecalho]
    restantes = arquivo.setores_da_fat[fat_no_cabecalho:]
    difat_bruta = b""
    for posicao, numero in enumerate(arquivo.setores_da_difat):
        lote = restantes[posicao * por_setor_da_difat:(posicao + 1) * por_setor_da_difat]
        lote = lote + [LIVRE] * (por_setor - 1 - len(lote))
        seguinte = arquivo.setores_da_difat[posicao + 1] if posicao + 1 < da_difat else FIM_DE_CADEIA
        difat_bruta += struct.pack(f"<{por_setor}I", *lote, seguinte)

    # Diretório: as irmãs ficam numa fila (cada uma aponta a seguinte como irmã da direita).
    entradas = []
    for indice, nome in enumerate(nomes):
        irmas = filhas.get(pais[indice], []) if pais[indice] is not None else []
        depois = irmas[irmas.index(indice) + 1:] if indice in irmas else []
        direita = depois[0] if depois else SEM_ENTRADA
        filha = filhas[indice][0] if filhas.get(indice) else SEM_ENTRADA
        if tipos[indice] == 5:
            entradas.append(_entrada(nome, 5, filha=filha, inicio=inicio("raiz"), tamanho=len(mini_fluxo)))
        elif tipos[indice] == 1:
            entradas.append(_entrada(nome, 1, direita=direita, filha=filha))
        elif indice in inicio_no_mini:
            entradas.append(_entrada(nome, 2, direita=direita, inicio=inicio_no_mini[indice],
                                     tamanho=len(conteudos[indice])))
        elif conteudos[indice]:
            entradas.append(_entrada(nome, 2, direita=direita, inicio=inicio(indice), tamanho=len(conteudos[indice])))
        else:
            entradas.append(_entrada(nome, 2, direita=direita, inicio=FIM_DE_CADEIA))
    diretorio = b"".join(entradas)

    # Cabeçalho (ocupa o primeiro setor inteiro) e os setores, na ordem física.
    cabecalho = bytearray(setor)
    cabecalho[:8] = ASSINATURA
    struct.pack_into("<5H", cabecalho, 0x18, 0x003E, 3 if setor == 512 else 4, 0xFFFE, setor.bit_length() - 1, 6)
    struct.pack_into("<9I", cabecalho, 0x28, 0 if setor == 512 else logicas["diretorio"][1], da_fat,
                     inicio("diretorio"), 0, CORTE_DO_MINI_FLUXO, inicio("minifat"), logicas["minifat"][1],
                     inicio("difat"), da_difat)
    struct.pack_into("<109I", cabecalho, 0x4C, *no_cabecalho, *[LIVRE] * (109 - len(no_cabecalho)))
    conteudo_de = {"raiz": bytes(mini_fluxo), "minifat": minifat_bruta.ljust(logicas["minifat"][1] * setor, b"\xff"),
                   "diretorio": diretorio, "difat": difat_bruta,
                   "fat": struct.pack(f"<{len(fat)}I", *fat)}
    pronto = [b""] * total
    for nome in logicas:
        dados = conteudo_de[nome] if isinstance(nome, str) else conteudos[nome]
        cadeia = setores(nome)
        for posicao, numero in enumerate(cadeia):
            pronto[numero] = dados[posicao * setor:(posicao + 1) * setor].ljust(setor, b"\x00")
        chave = nome if isinstance(nome, str) else next(c for c, n in arquivo.entradas.items() if n == nome)
        arquivo.cadeias[chave] = cadeia
    arquivo.dados = bytearray(bytes(cabecalho) + b"".join(pronto))
    return arquivo
