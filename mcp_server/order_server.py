"""Order tools as an MCP server (stdio). Any MCP client (Claude Desktop, Claude Code, an agent framework)
can use the same order/ticket backend the WhatsApp agent uses.

Run:   STORE_BACKEND=dynamo TABLE_NAME=wa-agent python -m mcp_server.order_server
Local: STORE_BACKEND=memory python -m mcp_server.order_server
Claude Desktop config:
  {"mcpServers": {"acme-orders": {"command": "python", "args": ["-m", "mcp_server.order_server"],
                                   "env": {"STORE_BACKEND": "dynamo", "TABLE_NAME": "wa-agent"}}}}
"""
import time
import uuid

from mcp.server.fastmcp import FastMCP

from app.config import get_settings
from app.seed import seed_orders
from app.store import DynamoStore, MemoryStore

mcp = FastMCP("acme-orders")
_s = get_settings()
store = DynamoStore(_s.table_name, _s.region) if _s.store_backend == "dynamo" else MemoryStore()
if _s.store_backend != "dynamo":
    seed_orders(store)


@mcp.tool()
def lookup_order(order_id: str, customer_phone: str) -> dict:
    """Look up an order's status/tracking. The order must belong to customer_phone (WhatsApp number, digits only)."""
    order = store.get_order(order_id.strip().upper())
    if not order or order.get("phone") != customer_phone:
        return {"found": False, "message": "No order with that id for this phone number."}
    return {"found": True, **{k: v for k, v in order.items() if k != "phone"}}


@mcp.tool()
def create_ticket(customer_phone: str, subject: str, description: str, priority: str = "normal") -> dict:
    """Open a support ticket. priority: low | normal | high."""
    tid = "T-" + uuid.uuid4().hex[:8].upper()
    store.create_ticket({"ticket_id": tid, "phone": customer_phone, "subject": subject[:120],
                         "description": description[:1000],
                         "priority": priority if priority in ("low", "normal", "high") else "normal",
                         "status": "open", "created_at": int(time.time())})
    return {"ticket_id": tid, "status": "open"}


if __name__ == "__main__":
    mcp.run()
