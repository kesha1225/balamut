"""Shipping costs by weight."""

from __future__ import annotations

FREE_SHIPPING_FROM = 80


def shipping_cost(weight_kg: float, *, express: bool = False) -> float:
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    cost = 5.0 if weight_kg < 2 else 5.0 + (weight_kg - 2) * 1.5
    if express:
        cost *= 2
    return round(cost, 2)


def free_shipping(order_total: float) -> bool:
    return order_total >= FREE_SHIPPING_FROM
