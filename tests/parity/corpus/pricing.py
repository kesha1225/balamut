"""Order prices: line totals, bulk discounts and tax."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

TAX_RATE = Decimal("0.20")
_CENT = Decimal("0.01")


def line_total(unit_price: Decimal, quantity: int) -> Decimal:
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    return unit_price * quantity


def bulk_discount(quantity: int) -> Decimal:
    """10% off from 5 items, 20% off from 40."""
    if quantity >= 40:
        return Decimal("0.20")
    if quantity >= 5:
        return Decimal("0.10")
    return Decimal(0)


def with_tax(amount: Decimal) -> Decimal:
    return (amount * (1 + TAX_RATE)).quantize(_CENT, rounding=ROUND_HALF_UP)


def order_total(unit_price: Decimal, quantity: int) -> Decimal:
    subtotal = line_total(unit_price, quantity)
    return with_tax(subtotal * (1 - bulk_discount(quantity)))
