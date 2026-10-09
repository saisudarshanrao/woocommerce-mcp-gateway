"""What the tools return.

WooCommerce's own JSON for a product or an order runs to hundreds of fields.
These models keep the ones that answer real questions, so a tool result stays
small enough to hand to a language model. They also become the output schema
that MCP clients see for each tool.
"""

from __future__ import annotations

import html
import re
from datetime import tzinfo
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

from woo_mcp.dates import from_store_gmt
from woo_mcp.woocommerce import StoreFacts


class Product(BaseModel):
    id: int
    name: str
    status: str = Field(description='"publish" means visible on the storefront.')
    sku: str
    price: str = Field(description="Current selling price, as a decimal string.")
    regular_price: str
    sale_price: str = Field(description="Empty when the product is not on sale.")
    currency: str
    stock_status: str
    total_sales: int
    permalink: str = Field(description="The product's page on the storefront.")
    short_description: str
    created_at: str | None = Field(description="In the store's timezone.")


class ProductList(BaseModel):
    products: list[Product]
    total: int = Field(description="Products matching the filters, across all pages.")
    page: int
    total_pages: int


class OrderItem(BaseModel):
    product_id: int
    name: str
    sku: str
    quantity: int
    total: str


class Order(BaseModel):
    id: int
    status: str
    total: str = Field(description="Order total including tax and shipping, as a decimal string.")
    currency: str
    created_at: str | None = Field(description="When the order was placed, in the store's timezone.")
    customer_name: str
    customer_email: str
    payment_method: str
    items: list[OrderItem]


class Address(BaseModel):
    name: str
    company: str
    address_1: str
    address_2: str
    city: str
    state: str
    postcode: str
    country: str
    phone: str


class OrderDetail(Order):
    billing: Address
    shipping: Address
    customer_note: str
    subtotal: str
    shipping_total: str
    discount_total: str
    total_tax: str
    paid_at: str | None
    completed_at: str | None


class Period(BaseModel):
    name: str
    timezone: str = Field(description="The store's timezone, which the period is measured in.")
    start: str | None = Field(description="Orders placed at or after this moment.")
    end: str | None = Field(description="Orders placed before this moment. Null means up to now.")


class OrderList(BaseModel):
    orders: list[Order]
    total: int = Field(description="Orders matching the filters, across all pages.")
    page: int
    total_pages: int
    period: Period
    listed_orders_value: str = Field(
        description="Sum of the totals of the orders listed on this page. It covers every matching order "
        "only when total_pages is 1."
    )
    currency: str


class OrderStatusChange(BaseModel):
    order: Order
    previous_status: str
    note_added: bool


def plain_text(value: str | None) -> str:
    """WooCommerce returns descriptions as HTML; a model wants the words."""
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()
    # Removing a tag can strand the punctuation that followed it: "tee</b>." -> "tee ."
    return re.sub(r"\s+([.,;:!?])", r"\1", text)


def product_from(raw: dict[str, Any], store: StoreFacts) -> Product:
    return Product(
        id=raw["id"],
        name=raw.get("name", ""),
        status=raw.get("status", ""),
        sku=raw.get("sku", ""),
        price=raw.get("price", ""),
        regular_price=raw.get("regular_price", ""),
        sale_price=raw.get("sale_price", ""),
        currency=store.currency,
        stock_status=raw.get("stock_status", ""),
        total_sales=int(raw.get("total_sales") or 0),
        permalink=raw.get("permalink", ""),
        short_description=plain_text(raw.get("short_description")),
        created_at=from_store_gmt(raw.get("date_created_gmt"), store.timezone),
    )


def _items(raw: dict[str, Any]) -> list[OrderItem]:
    return [
        OrderItem(
            product_id=item.get("product_id", 0),
            name=item.get("name", ""),
            sku=item.get("sku") or "",
            quantity=item.get("quantity", 0),
            total=item.get("total", ""),
        )
        for item in raw.get("line_items", [])
    ]


def _order_fields(raw: dict[str, Any], tz: tzinfo) -> dict[str, Any]:
    billing = raw.get("billing", {})
    return {
        "id": raw["id"],
        "status": raw.get("status", ""),
        "total": raw.get("total", ""),
        "currency": raw.get("currency", ""),
        "created_at": from_store_gmt(raw.get("date_created_gmt"), tz),
        "customer_name": f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip(),
        "customer_email": billing.get("email", ""),
        "payment_method": raw.get("payment_method_title") or raw.get("payment_method", ""),
        "items": _items(raw),
    }


def order_from(raw: dict[str, Any], store: StoreFacts) -> Order:
    return Order(**_order_fields(raw, store.timezone))


def _address(raw: dict[str, Any]) -> Address:
    return Address(
        name=f"{raw.get('first_name', '')} {raw.get('last_name', '')}".strip(),
        company=raw.get("company", ""),
        address_1=raw.get("address_1", ""),
        address_2=raw.get("address_2", ""),
        city=raw.get("city", ""),
        state=raw.get("state", ""),
        postcode=raw.get("postcode", ""),
        country=raw.get("country", ""),
        phone=raw.get("phone", ""),
    )


def order_detail_from(raw: dict[str, Any], store: StoreFacts) -> OrderDetail:
    subtotal = sum_decimals(item.get("subtotal", "0") for item in raw.get("line_items", []))
    return OrderDetail(
        **_order_fields(raw, store.timezone),
        billing=_address(raw.get("billing", {})),
        shipping=_address(raw.get("shipping", {})),
        customer_note=raw.get("customer_note", ""),
        subtotal=subtotal,
        shipping_total=raw.get("shipping_total", ""),
        discount_total=raw.get("discount_total", ""),
        total_tax=raw.get("total_tax", ""),
        paid_at=from_store_gmt(raw.get("date_paid_gmt"), store.timezone),
        completed_at=from_store_gmt(raw.get("date_completed_gmt"), store.timezone),
    )


def sum_decimals(values: Any) -> str:
    """Adds money amounts given as decimal strings, without float rounding."""
    total = Decimal("0")
    for value in values:
        try:
            total += Decimal(str(value))
        except InvalidOperation:
            continue
    return f"{total:.2f}"
