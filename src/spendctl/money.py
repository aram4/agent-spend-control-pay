from decimal import Decimal

CENT = Decimal("0.01")


def to_money(value: Decimal | int | str) -> Decimal:
    if isinstance(value, float):
        raise TypeError("money must be Decimal, int, or str, never float")
    amount = Decimal(value)
    if amount != amount.quantize(CENT):
        raise ValueError(f"money has sub-cent precision: {value}")
    return amount.quantize(CENT)
