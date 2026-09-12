"""Data-free health response contract for service supervision."""
from typing import Literal

from .models import ContractModel


class HealthStatus(ContractModel):
    status: Literal['ok', 'ready']
