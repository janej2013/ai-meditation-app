"""What the user can buy, and what each purchase grants.

Price ids are not secrets -- they are safe in code and in a plaintext env var,
unlike the API keys in ``stripe_client``. The map lives here so Checkout and
the webhook read the same definition: Checkout resolves a product key to a
price id, and the webhook resolves the price id on the paid line item back to
the credits it grants.

Callers pick a product by **key** (``pack_10``), never by price id. The key is
what the frontend sends, so a client cannot name an arbitrary Stripe price and
buy something the catalogue does not offer.

Override for a real Stripe account with the ``STRIPE_PRODUCTS`` environment
variable -- a JSON object keyed the same way:

    {"pack_10": {"price_id": "price_123", "kind": "credit_pack", "credits": 10}}
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

ProductKind = Literal["credit_pack", "subscription"]

PRODUCTS_ENV_VAR = "STRIPE_PRODUCTS"

# Stripe Checkout needs a different mode per product kind, and getting it wrong
# is a 400 from Stripe rather than a silent bug.
CHECKOUT_MODES: dict[ProductKind, str] = {
    "credit_pack": "payment",
    "subscription": "subscription",
}


@dataclass(frozen=True)
class Product:
    """One purchasable item.

    ``credits`` is what a successful payment grants: the whole pack for a
    credit pack, or one period's allowance for a subscription.
    """

    key: str
    price_id: str
    kind: ProductKind
    credits: int
    plan: str = "free"

    @property
    def checkout_mode(self) -> str:
        return CHECKOUT_MODES[self.kind]


# Placeholder ids until the Stripe products exist. They are deliberately
# obvious: a deployment that forgets STRIPE_PRODUCTS fails at Stripe with an
# unknown-price error rather than charging against something real.
_DEFAULT_PRODUCTS: tuple[Product, ...] = (
    Product(key="pack_10", price_id="price_placeholder_pack_10", kind="credit_pack", credits=10),
    Product(key="pack_30", price_id="price_placeholder_pack_30", kind="credit_pack", credits=30),
    Product(
        key="plan_monthly",
        price_id="price_placeholder_plan_monthly",
        kind="subscription",
        credits=20,
        plan="monthly",
    ),
)

_catalogue: dict[str, Product] | None = None


def _load_catalogue() -> dict[str, Product]:
    """Build the catalogue once, from the env var if present."""
    raw = os.environ.get(PRODUCTS_ENV_VAR)
    if not raw:
        return {product.key: product for product in _DEFAULT_PRODUCTS}

    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        logger.error(
            "%s is not valid JSON; falling back to the built-in catalogue", PRODUCTS_ENV_VAR
        )
        return {product.key: product for product in _DEFAULT_PRODUCTS}

    try:
        return {
            key: Product(
                key=key,
                price_id=entry["price_id"],
                kind=entry.get("kind", "credit_pack"),
                credits=int(entry.get("credits", 0)),
                plan=entry.get("plan", "free"),
            )
            for key, entry in document.items()
        }
    except (KeyError, TypeError, ValueError):
        # Same posture as invalid JSON: a malformed entry must not turn every
        # request into a 500 at first catalogue access.
        logger.error(
            "%s has a malformed entry; falling back to the built-in catalogue", PRODUCTS_ENV_VAR
        )
        return {product.key: product for product in _DEFAULT_PRODUCTS}


def catalogue() -> dict[str, Product]:
    """Every product, keyed by product key. Cached per container."""
    global _catalogue
    if _catalogue is None:
        _catalogue = _load_catalogue()
    return _catalogue


def reset_catalogue_cache() -> None:
    """Drop the cached catalogue. For tests; production loads once."""
    global _catalogue
    _catalogue = None


def by_key(key: str) -> Product | None:
    """Resolve what the client asked to buy."""
    return catalogue().get(key)


def by_price_id(price_id: str) -> Product | None:
    """Resolve what Stripe says was paid for.

    Returns None for a price the catalogue does not know -- a product created
    in the Stripe dashboard but never added here. The webhook treats that as
    "ignore and acknowledge" rather than an error, so Stripe stops retrying an
    event this deployment can never act on.
    """
    return next((p for p in catalogue().values() if p.price_id == price_id), None)
