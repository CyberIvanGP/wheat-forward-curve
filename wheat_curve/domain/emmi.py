"""EMMI-разложение произвольного срока на соседние стандартные узлы.

См. docs/METHODOLOGY_IMPLEMENTATION.md §4.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from wheat_curve.domain.models import Curve, DecomposedObservation, Side
from wheat_curve.domain.tenors import STANDARD_TENORS, StandardNode


def find_bracket(maturity: date, nodes: list[StandardNode]) -> tuple[StandardNode | None, StandardNode, StandardNode | None]:
    """Возвращает (exact|None, TL|None, TR|None).

    Если maturity совпадает с узлом -> (node, None, None).
    Иначе -> (None, TL, TR), либо TL/TR может отсутствовать, если maturity вне диапазона.
    """
    for n in nodes:
        if n.maturity_date == maturity:
            return n, None, None  # type: ignore[return-value]
    left = None
    right = None
    for n in nodes:
        if n.maturity_date < maturity:
            if left is None or n.maturity_date > left.maturity_date:
                left = n
        if n.maturity_date > maturity:
            if right is None or n.maturity_date < right.maturity_date:
                right = n
    return None, left, right  # type: ignore[return-value]


def decompose_observation(
    *,
    source_id: str,
    maturity: date,
    price: Decimal,
    quantity: Decimal,
    side: Side | None,
    reference_curve: Curve,
    standard_nodes_list: list[StandardNode],
) -> list[DecomposedObservation]:
    """Разложить одно наблюдение (сделка или заявка) по EMMI на стандартные узлы.

    Если maturity точно совпадает со стандартным узлом — одно наблюдение без изменений.
    Иначе — два синтетических наблюдения на TL и TR с сохранённым delta к reference curve.
    """
    exact, left, right = find_bracket(maturity, standard_nodes_list)
    if exact is not None:
        return [
            DecomposedObservation(
                tenor=exact.tenor,
                price=price,
                quantity=quantity,
                side=side,
                source_id=source_id,
                original_maturity=maturity,
                original_price=price,
                original_quantity=quantity,
                weight=Decimal("1"),
            )
        ]

    if left is None or right is None:
        # Срок вне диапазона стандартных узлов (< 1D или > 3Y) — наблюдение не используется.
        return []

    tl_days = (right.maturity_date - left.maturity_date).days
    if tl_days <= 0:
        return []
    t_days = (maturity - left.maturity_date).days
    wl = Decimal(right.maturity_date.toordinal() - maturity.toordinal()) / Decimal(tl_days)
    wr = Decimal(maturity.toordinal() - left.maturity_date.toordinal()) / Decimal(tl_days)

    fref_tl = reference_curve.get(left.tenor)
    fref_tr = reference_curve.get(right.tenor)
    if fref_tl is None or fref_tr is None:
        return []

    fref_t = wl * fref_tl + wr * fref_tr
    delta = price - fref_t
    price_l = fref_tl + delta
    price_r = fref_tr + delta

    q_l = quantity * wl
    q_r = quantity * wr

    return [
        DecomposedObservation(
            tenor=left.tenor,
            price=price_l,
            quantity=q_l,
            side=side,
            source_id=source_id,
            original_maturity=maturity,
            original_price=price,
            original_quantity=quantity,
            weight=wl,
        ),
        DecomposedObservation(
            tenor=right.tenor,
            price=price_r,
            quantity=q_r,
            side=side,
            source_id=source_id,
            original_maturity=maturity,
            original_price=price,
            original_quantity=quantity,
            weight=wr,
        ),
    ]
