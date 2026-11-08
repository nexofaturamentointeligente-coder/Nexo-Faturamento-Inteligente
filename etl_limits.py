"""SEC-005: limites finitos compartilhados pelo ETL e pelos uploads."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields
import os


@dataclass(frozen=True)
class ETLLimits:
    max_file_bytes: int = 20 * 1024 * 1024
    max_sheets: int = 16
    max_zip_entries: int = 256
    max_uncompressed_bytes: int = 32 * 1024 * 1024
    max_zip_ratio: int = 200
    max_rows: int = 100_000
    max_columns: int = 128
    max_cells_per_sheet: int = 500_000
    max_cells_per_file: int = 1_000_000
    max_field_chars: int = 16_384
    max_record_chars: int = 131_072
    timeout_seconds: int = 60
    memory_mb: int = 384
    # A Curva ABC traz todos os produtos: cerca de 170 bytes por produto no resultado.
    max_output_bytes: int = 4 * 1024 * 1024

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if type(value) is not int or not 1 <= value <= _HARD_CAPS[item.name]:
                raise ValueError("Configuração inválida de limite ETL: " + item.name)
        if self.memory_mb < 128:
            raise ValueError("Configuração inválida de limite ETL: memory_mb")

    @classmethod
    def from_mapping(cls, mapping=None):
        if mapping is None:
            return cls()
        if not isinstance(mapping, dict) or set(mapping) - set(_HARD_CAPS):
            raise ValueError("Configuração inválida dos limites ETL.")
        return cls(**mapping)


# Tetos que nenhuma configuração ultrapassa. Células por aba: 500 mil é trava rígida (o padrão
# e o teto são iguais), porque o worker isolado tem 384 MiB e lê a aba inteira em memória.
_HARD_CAPS = {
    "max_file_bytes": 20 * 1024 * 1024, "max_sheets": 64,
    "max_zip_entries": 1024, "max_uncompressed_bytes": 64 * 1024 * 1024,
    "max_zip_ratio": 1000, "max_rows": 200_000, "max_columns": 256,
    "max_cells_per_sheet": 500_000, "max_cells_per_file": 2_000_000,
    "max_field_chars": 32_768, "max_record_chars": 1024 * 1024,
    "timeout_seconds": 120, "memory_mb": 512,
    "max_output_bytes": 4 * 1024 * 1024,
}


def limits_from_environment():
    values = {}
    for name in _HARD_CAPS:
        raw = os.environ.get("ETL_" + name.upper())
        if raw is not None:
            # Não imprimir o valor fornecido pelo ambiente em erros.
            if not raw.isascii() or not raw.isdecimal():
                raise ValueError("Configuração inválida de limite ETL: " + name)
            try:
                values[name] = int(raw)
            except ValueError:
                raise ValueError("Configuração inválida de limite ETL: " + name) from None
    return ETLLimits.from_mapping(values)


# O filho recebe a política explicitamente, sem herdar segredos/configuração.
_ACTIVE_LIMITS = ContextVar("nexo_etl_limits", default=None)


def get_limits():
    selected = _ACTIVE_LIMITS.get()
    if selected is not None:
        return selected
    from flask import current_app, has_app_context
    if has_app_context():
        configured = current_app.config.get("ETL_LIMITS", {})
        if isinstance(configured, ETLLimits):
            return configured
        return ETLLimits.from_mapping(configured)
    return limits_from_environment()


@contextmanager
def using_limits(limits):
    token = _ACTIVE_LIMITS.set(limits)
    try:
        yield
    finally:
        _ACTIVE_LIMITS.reset(token)
