"""Limite de envios por IP, em memória (um processo: gunicorn -w 1), em janelas deslizantes de 1 hora e 24 horas.

Conta toda tentativa de envio (válida ou não). O IPv6 conta pela rede /64 (um único cliente costuma ter a rede
inteira, e trocar só o fim do endereço não zera o limite); o IPv4 mapeado em IPv6 conta como o IPv4. Some quando
o processo reinicia; o teto diário do serviço, que vem do banco, continua valendo.
"""

from collections import OrderedDict, deque
import ipaddress
import threading
import time

HORA = 3600
DIA = 24 * HORA
PREFIXO_DO_IPV6 = 64


def chave_do_endereco(endereco) -> str:
    """O endereço IPv4, ou a rede /64 do IPv6; o que não for IP conta como texto (até 64 caracteres)."""
    texto = str(endereco or "").strip()
    try:
        ip = ipaddress.ip_address(texto.split("%", 1)[0])
    except ValueError:
        return texto[:64] or "desconhecido"
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/{PREFIXO_DO_IPV6}", strict=False))
    return str(ip)


class LimitePorIp:
    def __init__(self, por_hora: int, por_dia: int, relogio=time.monotonic, maximo_de_enderecos: int = 10000):
        self._por_hora = por_hora
        self._por_dia = por_dia
        self._relogio = relogio
        self._maximo = maximo_de_enderecos
        self._envios = OrderedDict()
        self._trava = threading.Lock()

    def registrar(self, endereco: str):
        """Registra a tentativa e devolve None; ou, acima do limite, quantos segundos esperar (sem registrar)."""
        chave = chave_do_endereco(endereco)
        agora = self._relogio()
        with self._trava:
            envios = self._envios.get(chave)
            if envios is None:
                envios = self._envios[chave] = deque()
            self._envios.move_to_end(chave)
            while envios and agora - envios[0] >= DIA:
                envios.popleft()
            if len(envios) >= self._por_dia:
                return max(1, int(DIA - (agora - envios[0])) + 1)
            na_hora = [instante for instante in envios if agora - instante < HORA]
            if len(na_hora) >= self._por_hora:
                return max(1, int(HORA - (agora - na_hora[0])) + 1)
            envios.append(agora)
            while len(self._envios) > self._maximo:
                self._envios.popitem(last=False)
            return None
