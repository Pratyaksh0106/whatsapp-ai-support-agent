"""Seed demo orders into DynamoDB.  Usage: python -m scripts.seed"""
from app.config import get_settings
from app.seed import seed_orders
from app.store import DynamoStore

s = get_settings()
seed_orders(DynamoStore(s.table_name, s.region))
print("seeded orders")
