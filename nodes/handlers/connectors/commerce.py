"""Commerce connectors: Stripe, Shopify."""
from __future__ import annotations

from typing import Any

from ..base import FieldConfig, FieldType
from ..rest_base import ConnectorError, RestConnectorNode



class StripeNode(RestConnectorNode):
    """Customers, charges and payment links in Stripe."""

    node_type = "stripe"
    name = "Stripe"
    description = "Manage Stripe customers, payments and refunds"
    icon = "💳"
    color = "#635bff"

    credential_slug = "stripe"
    auth_style = "bearer"
    base_url = "https://api.stripe.com/v1"

    fields = [
        FieldConfig(name="credential", label="Stripe Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="stripe"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["create_customer", "get_customer", "list_customers",
                             "create_payment_link", "list_charges", "refund_charge"],
                    default="create_customer"),
        FieldConfig(name="email", label="Email", field_type=FieldType.STRING, required=False),
        FieldConfig(name="name", label="Name", field_type=FieldType.STRING, required=False),
        FieldConfig(name="customer_id", label="Customer ID", field_type=FieldType.STRING,
                    required=False),
        FieldConfig(name="price_id", label="Price ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="quantity", label="Quantity", field_type=FieldType.NUMBER,
                    required=False, default=1),
        FieldConfig(name="charge_id", label="Charge / PaymentIntent ID", field_type=FieldType.STRING,
                    required=False),
        FieldConfig(name="amount", label="Refund Amount (minor units)", field_type=FieldType.NUMBER,
                    required=False,
                    description="Leave blank to refund in full. In paise/cents."),
        FieldConfig(name="limit", label="Limit", field_type=FieldType.NUMBER, required=False, default=25),
    ]
    static_output_fields = ["id", "object", "created"]

    async def run_operation(self, operation, config, secret, context):
        # Stripe's API is form-encoded, not JSON, despite answering with JSON.
        form_headers = {"Content-Type": "application/x-www-form-urlencoded"}
        limit = min(int(config.get("limit") or 25), 100)

        if operation == "create_customer":
            email = config.get("email", "").strip()
            if not email:
                raise ConnectorError("Email is required to create a customer.")
            form: dict[str, Any] = {"email": email}
            if config.get("name"):
                form["name"] = config["name"]
            return await self.call("POST", "/customers", secret=secret,
                                   data=form, headers=form_headers)

        if operation == "get_customer":
            customer_id = config.get("customer_id", "").strip()
            if not customer_id:
                raise ConnectorError("Customer ID is required.")
            return await self.call("GET", f"/customers/{customer_id}", secret=secret)

        if operation == "list_customers":
            data = await self.call("GET", "/customers", secret=secret, params={"limit": limit})
            return (data or {}).get("data", [])

        if operation == "create_payment_link":
            price_id = config.get("price_id", "").strip()
            if not price_id:
                raise ConnectorError("Price ID is required.")
            # Nested list params use Stripe's bracket syntax.
            form = {
                "line_items[0][price]": price_id,
                "line_items[0][quantity]": str(int(config.get("quantity") or 1)),
            }
            return await self.call("POST", "/payment_links", secret=secret,
                                   data=form, headers=form_headers)

        if operation == "list_charges":
            params: dict[str, Any] = {"limit": limit}
            if config.get("customer_id"):
                params["customer"] = config["customer_id"]
            data = await self.call("GET", "/charges", secret=secret, params=params)
            return (data or {}).get("data", [])

        if operation == "refund_charge":
            charge_id = config.get("charge_id", "").strip()
            if not charge_id:
                raise ConnectorError("Charge or PaymentIntent ID is required.")
            # Stripe distinguishes the two by prefix and rejects the wrong field.
            key = "payment_intent" if charge_id.startswith("pi_") else "charge"
            form = {key: charge_id}
            if config.get("amount") not in (None, ""):
                form["amount"] = str(int(config["amount"]))
            return await self.call("POST", "/refunds", secret=secret,
                                   data=form, headers=form_headers)

        raise NotImplementedError(operation)


class ShopifyNode(RestConnectorNode):
    """Orders, products and customers in Shopify."""

    node_type = "shopify"
    name = "Shopify"
    description = "Manage Shopify orders, products and customers"
    icon = "🛍️"
    color = "#96bf48"

    credential_slug = "shopify"
    credential_key = "accessToken"
    auth_style = "header"
    auth_header = "X-Shopify-Access-Token"

    fields = [
        FieldConfig(name="credential", label="Shopify Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="shopify"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["list_orders", "get_order", "list_products",
                             "create_product", "list_customers", "update_inventory"],
                    default="list_orders"),
        FieldConfig(name="order_id", label="Order ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="product_title", label="Product Title", field_type=FieldType.STRING,
                    required=False),
        FieldConfig(name="product_price", label="Price", field_type=FieldType.STRING, required=False),
        FieldConfig(name="inventory_item_id", label="Inventory Item ID", field_type=FieldType.STRING,
                    required=False),
        FieldConfig(name="location_id", label="Location ID", field_type=FieldType.STRING,
                    required=False),
        FieldConfig(name="available", label="Available Quantity", field_type=FieldType.NUMBER,
                    required=False),
        FieldConfig(name="status", label="Order Status", field_type=FieldType.SELECT,
                    options=["any", "open", "closed", "cancelled"],
                    required=False, default="any"),
        FieldConfig(name="api_version", label="API Version", field_type=FieldType.STRING,
                    required=False, default="2024-10"),
        FieldConfig(name="limit", label="Limit", field_type=FieldType.NUMBER, required=False, default=50),
    ]
    static_output_fields = ["id", "name", "created_at", "total_price"]

    async def run_operation(self, operation, config, secret, context):
        creds = await context.get_credential(config.get("credential")) or {}
        shop = (creds.get("shopDomain") or creds.get("shop_domain") or "").strip()
        if not shop:
            raise ConnectorError("Shopify credential has no shop domain.")
        shop = shop.replace("https://", "").replace("http://", "").rstrip("/")
        if not shop.endswith(".myshopify.com"):
            shop = f"{shop}.myshopify.com"

        version = config.get("api_version") or "2024-10"
        base = f"https://{shop}/admin/api/{version}"
        limit = min(int(config.get("limit") or 50), 250)

        if operation == "list_orders":
            data = await self.call("GET", f"{base}/orders.json", secret=secret,
                                   params={"limit": limit,
                                           "status": config.get("status") or "any"})
            return (data or {}).get("orders", [])

        if operation == "get_order":
            order_id = config.get("order_id", "").strip()
            if not order_id:
                raise ConnectorError("Order ID is required.")
            data = await self.call("GET", f"{base}/orders/{order_id}.json", secret=secret)
            return (data or {}).get("order", {})

        if operation == "list_products":
            data = await self.call("GET", f"{base}/products.json", secret=secret,
                                   params={"limit": limit})
            return (data or {}).get("products", [])

        if operation == "create_product":
            title = config.get("product_title", "").strip()
            if not title:
                raise ConnectorError("Product title is required.")
            product: dict[str, Any] = {"title": title}
            if config.get("product_price"):
                product["variants"] = [{"price": str(config["product_price"])}]
            data = await self.call("POST", f"{base}/products.json", secret=secret,
                                   json_body={"product": product})
            return (data or {}).get("product", {})

        if operation == "list_customers":
            data = await self.call("GET", f"{base}/customers.json", secret=secret,
                                   params={"limit": limit})
            return (data or {}).get("customers", [])

        if operation == "update_inventory":
            item_id = config.get("inventory_item_id", "").strip()
            location_id = config.get("location_id", "").strip()
            if not item_id or not location_id:
                raise ConnectorError("Inventory item ID and location ID are required.")
            if config.get("available") in (None, ""):
                raise ConnectorError("Available quantity is required.")
            data = await self.call(
                "POST", f"{base}/inventory_levels/set.json", secret=secret,
                json_body={
                    "inventory_item_id": int(item_id),
                    "location_id": int(location_id),
                    "available": int(config["available"]),
                },
            )
            return (data or {}).get("inventory_level", {})

        raise NotImplementedError(operation)
