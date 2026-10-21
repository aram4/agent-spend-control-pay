import uuid
from collections import Counter

from sqlalchemy import text

from spendctl.db.engine import get_sessionmaker
from spendctl.db.models import TERMINAL_PURCHASE_STATES
from spendctl.providers.base import CardProvider, CardStatus


async def assert_budget_invariants(budget_ids: list[uuid.UUID] | None = None) -> None:
    where = "WHERE b.id = ANY(:ids)" if budget_ids is not None else ""
    query = text(f"""
        SELECT b.id, b.limit_amount, b.spent, b.reserved,
               COALESCE(SUM(r.amount) FILTER (WHERE r.status = 'held'), 0) AS held,
               COALESCE(SUM(r.captured_amount) FILTER (WHERE r.status = 'captured'), 0) AS captured
        FROM budgets b LEFT JOIN reservations r ON r.budget_id = b.id
        {where}
        GROUP BY b.id
    """)
    params = {"ids": budget_ids} if budget_ids is not None else {}
    async with get_sessionmaker()() as session:
        rows = (await session.execute(query, params)).all()

    violations = []
    for id_, limit, spent, reserved, held, captured in rows:
        if spent < 0 or reserved < 0 or spent + reserved > limit:
            violations.append(f"{id_}: spent {spent} + reserved {reserved} vs limit {limit}")
        if reserved != held:
            violations.append(f"{id_}: reserved {reserved} != held reservations {held}")
        if spent != captured:
            violations.append(f"{id_}: spent {spent} != captured reservations {captured}")
    assert not violations, "budget invariants violated:\n" + "\n".join(violations)


async def assert_one_reservation_per_purchase() -> None:
    async with get_sessionmaker()() as session:
        dupes = (
            await session.execute(
                text("SELECT purchase_id, COUNT(*) FROM reservations GROUP BY purchase_id HAVING COUNT(*) > 1")
            )
        ).all()
    assert not dupes, f"multiple reservations per purchase: {dupes}"


async def assert_card_invariants(provider: CardProvider) -> None:
    cards = await provider.list_cards()
    per_key = Counter(c.idem_key for c in cards)
    dupes = {k: n for k, n in per_key.items() if n > 1}
    assert not dupes, f"multiple provider cards per purchase: {dupes}"

    live = [c for c in cards if c.status is not CardStatus.CLOSED]
    async with get_sessionmaker()() as session:
        active = {
            wf
            for (wf,) in await session.execute(
                text("SELECT workflow_id FROM purchases WHERE NOT (state = ANY(:terminal))"),
                {"terminal": list(TERMINAL_PURCHASE_STATES)},
            )
        }
    orphans = [c.token for c in live if c.idem_key.removesuffix("-card") not in active]
    assert not orphans, f"open cards without an active purchase: {orphans}"


async def assert_all_invariants(
    budget_ids: list[uuid.UUID] | None = None, provider: CardProvider | None = None
) -> None:
    await assert_budget_invariants(budget_ids)
    await assert_one_reservation_per_purchase()
    if provider is not None:
        await assert_card_invariants(provider)
