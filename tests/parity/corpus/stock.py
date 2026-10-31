"""Stock reservations, committed through a unit of work."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol


class OutOfStockError(Exception):
    """Not enough items of a SKU."""


class Session(Protocol):
    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


@dataclass
class Warehouse:
    stock: dict[str, int]
    session: Session
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def reserve(self, sku: str, quantity: int) -> int:
        """Take ``quantity`` items of ``sku`` -> the items left. A failed commit puts the items back."""
        async with self._lock:
            available = self.stock.get(sku, 0)
            if quantity > available:
                raise OutOfStockError(sku)
            self.stock[sku] = available - quantity
            try:
                await self.session.commit()
            except Exception:
                self.stock[sku] = available
                await self.session.rollback()
                raise
            return self.stock[sku]
