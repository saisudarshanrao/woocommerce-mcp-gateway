"""The MCP server: six tools over one WooCommerce store.

Each tool is a plain async function. The SDK builds the tool's input schema
from the parameter type hints, its description from the docstring and its
output schema from the return type, so what a model is told about a tool is
exactly what the function accepts and returns.
"""

from __future__ import annotations

from importlib.metadata import version
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from woo_mcp.dates import Period as PeriodName
from woo_mcp.dates import parse_datetime, period_range, to_utc_param
from woo_mcp.models import (
    Order,
    OrderDetail,
    OrderList,
    OrderStatusChange,
    Period,
    Product,
    ProductList,
    order_detail_from,
    order_from,
    product_from,
    sum_decimals,
)
from woo_mcp.woocommerce import WooCommerce, WooCommerceError

OrderStatus = Literal["pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"]

# Hints for MCP clients about what a tool does. They are advice, not
# enforcement: a gateway may use them to decide who can call what, and a client
# may ask the user before running a tool that is not read-only.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ADDS_DATA = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
CHANGES_DATA = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)

# Optional text arguments default to "" rather than None. A nullable argument
# would appear in the tool's JSON Schema as anyOf[string, null], and not every
# model API accepts anyOf in a tool definition.

PerPage = Annotated[int, Field(ge=1, le=100, description="How many to return per page.")]
PageNumber = Annotated[int, Field(ge=1, description="Which page of results, starting at 1.")]

INSTRUCTIONS = """\
Tools for one WooCommerce store: its products and its orders.
Prices and totals are decimal strings in the store's currency, which every result names.
Dates and times are in the store's own timezone and carry their UTC offset.
For questions about today's or recent orders, pass list_orders a named period rather than computing dates.
"""


def build_server(woo: WooCommerce) -> MCPServer:
    """Builds the server around a WooCommerce client.

    The tools are closures over `woo`, so a test can hand in a client wired to
    a fake store and exercise the real tool code.
    """
    mcp = MCPServer(
        "woocommerce",
        title="WooCommerce store",
        version=version("woo-mcp"),
        instructions=INSTRUCTIONS,
    )

    async def call(action: str, request):
        """Awaits a store request, turning a failure into an error the model can read."""
        try:
            return await request
        except WooCommerceError as error:
            raise ToolError(f"Could not {action}. {error}") from error

    @mcp.tool(title="List products", annotations=READ_ONLY)
    async def list_products(
        search: Annotated[
            str, Field(description="Only products whose name, description or SKU contains this text.")
        ] = "",
        status: Annotated[
            Literal["any", "publish", "draft", "pending", "private"],
            Field(description='"publish" is what shoppers can see. "any" includes drafts.'),
        ] = "any",
        per_page: PerPage = 20,
        page: PageNumber = 1,
    ) -> ProductList:
        """List the store's products, newest first."""
        store = await call("read the store's settings", woo.store_facts())
        result = await call(
            "list products",
            woo.get_page(
                "/products",
                {
                    "search": search or None,
                    "status": status,
                    "per_page": per_page,
                    "page": page,
                    "orderby": "date",
                    "order": "desc",
                },
            ),
        )
        return ProductList(
            products=[product_from(raw, store) for raw in result.items],
            total=result.total,
            page=page,
            total_pages=result.total_pages,
        )

    @mcp.tool(title="Get product", annotations=READ_ONLY)
    async def get_product(
        product_id: Annotated[int, Field(ge=1, description="The product's numeric ID.")],
    ) -> Product:
        """Get one product by its ID."""
        store = await call("read the store's settings", woo.store_facts())
        raw = await call(f"get product {product_id}", woo.get(f"/products/{product_id}"))
        return product_from(raw, store)

    @mcp.tool(title="Create product", annotations=ADDS_DATA)
    async def create_product(
        name: Annotated[str, Field(min_length=1, max_length=200, description="The product name shoppers see.")],
        regular_price: Annotated[
            float, Field(gt=0, description="The price in the store's currency, for example 19.99.")
        ],
        description: Annotated[str, Field(description="Full description shown on the product page.")] = "",
        short_description: Annotated[str, Field(description="One or two lines shown next to the price.")] = "",
        sku: Annotated[str, Field(description="Stock keeping unit. Must be unique in the store.")] = "",
        status: Annotated[
            Literal["publish", "draft"],
            Field(description='"publish" puts it on the storefront at once. "draft" keeps it hidden.'),
        ] = "publish",
    ) -> Product:
        """Create a simple product. Published products appear on the storefront immediately."""
        store = await call("read the store's settings", woo.store_facts())
        body = {
            "name": name,
            "type": "simple",
            "regular_price": f"{regular_price:.2f}",
            "description": description,
            "short_description": short_description,
            "status": status,
        }
        if sku:
            body["sku"] = sku
        raw = await call(f"create the product {name!r}", woo.post("/products", body))
        return product_from(raw, store)

    @mcp.tool(title="List orders", annotations=READ_ONLY)
    async def list_orders(
        period: Annotated[
            PeriodName,
            Field(
                description="Which orders to include, by when they were placed, measured in the store's "
                'timezone. "last_7_days" is today and the six days before it.'
            ),
        ] = "all",
        status: Annotated[
            Literal["any", "pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"],
            Field(description='Only orders in this status. "processing" means paid or cash on delivery, not yet shipped.'),
        ] = "any",
        after: Annotated[
            str,
            Field(
                description="Only orders placed at or after this ISO 8601 date or date-time, for example "
                "2026-10-01 or 2026-10-01T09:00:00. Store time unless it carries an offset. Overrides period."
            ),
        ] = "",
        before: Annotated[
            str,
            Field(description="Only orders placed before this ISO 8601 date or date-time. Overrides period."),
        ] = "",
        per_page: PerPage = 20,
        page: PageNumber = 1,
    ) -> OrderList:
        """List orders, newest first. For "what came in today", use period="today"."""
        store = await call("read the store's settings", woo.store_facts())
        window = period_range(period, store.now())
        start, end = window.start, window.end
        try:
            if after:
                start = parse_datetime(after, store.timezone)
            if before:
                end = parse_datetime(before, store.timezone)
        except ValueError as error:
            raise ToolError(
                'after and before must be ISO 8601, such as "2026-10-01" or "2026-10-01T09:00:00".'
            ) from error

        result = await call(
            "list orders",
            woo.get_page(
                "/orders",
                {
                    "status": status,
                    # Sent in UTC and flagged as such, so the filter cannot be
                    # misread whatever timezone the store is set to.
                    "after": to_utc_param(start) if start else None,
                    "before": to_utc_param(end) if end else None,
                    "dates_are_gmt": True if (start or end) else None,
                    "per_page": per_page,
                    "page": page,
                    "orderby": "date",
                    "order": "desc",
                },
            ),
        )
        orders = [order_from(raw, store) for raw in result.items]
        return OrderList(
            orders=orders,
            total=result.total,
            page=page,
            total_pages=result.total_pages,
            period=Period(
                name="custom" if (after or before) else period,
                timezone=store.timezone_name,
                start=start.isoformat(timespec="seconds") if start else None,
                end=end.isoformat(timespec="seconds") if end else None,
            ),
            listed_orders_value=sum_decimals(order.total for order in orders),
            currency=store.currency,
        )

    @mcp.tool(title="Get order", annotations=READ_ONLY)
    async def get_order(
        order_id: Annotated[int, Field(ge=1, description="The order's numeric ID.")],
    ) -> OrderDetail:
        """Get one order in full: items, totals, customer and addresses."""
        store = await call("read the store's settings", woo.store_facts())
        raw = await call(f"get order {order_id}", woo.get(f"/orders/{order_id}"))
        return order_detail_from(raw, store)

    @mcp.tool(title="Update order status", annotations=CHANGES_DATA)
    async def update_order_status(
        order_id: Annotated[int, Field(ge=1, description="The order's numeric ID.")],
        status: Annotated[
            OrderStatus,
            Field(
                description='The new status. "completed" means fulfilled. "cancelled" returns stock. '
                '"refunded" only marks the order; it does not send money back.'
            ),
        ],
        note: Annotated[
            str,
            Field(description="A private note to record on the order, explaining the change. The customer does not see it."),
        ] = "",
    ) -> OrderStatusChange:
        """Change an order's status. The customer may be emailed about the change."""
        store = await call("read the store's settings", woo.store_facts())
        before_change = await call(f"get order {order_id}", woo.get(f"/orders/{order_id}"))
        updated = await call(
            f"set order {order_id} to {status}",
            woo.put(f"/orders/{order_id}", {"status": status}),
        )
        if note:
            await call(
                f"add the note to order {order_id}",
                woo.post(f"/orders/{order_id}/notes", {"note": note, "customer_note": False}),
            )
        return OrderStatusChange(
            order=order_from(updated, store),
            previous_status=before_change.get("status", ""),
            note_added=bool(note),
        )

    return mcp
