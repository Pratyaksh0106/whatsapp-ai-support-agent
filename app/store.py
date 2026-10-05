"""State storage. DynamoDB single-table design; MemoryStore for tests/evals/local."""
from __future__ import annotations
import json
import time
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

DAY = 86400


def _blank_state() -> dict:
    return {"history": [], "handoff": False, "low_conf_streak": 0}


class MemoryStore:
    def __init__(self):
        self.states, self.orders, self.tickets = {}, {}, {}
        self.seen, self.counters, self.spend = set(), {}, {}

    def get_state(self, cid): return json.loads(json.dumps(self.states.get(cid) or _blank_state()))
    def put_state(self, cid, st): self.states[cid] = json.loads(json.dumps(st))
    def get_order(self, oid): return self.orders.get(oid)
    def put_order(self, order): self.orders[order["order_id"]] = order
    def create_ticket(self, t): self.tickets[t["ticket_id"]] = t

    def seen_message(self, mid):
        if mid in self.seen:
            return True
        self.seen.add(mid)
        return False

    def incr(self, key, ttl_s):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def add_spend(self, day, usd):
        self.spend[day] = self.spend.get(day, 0.0) + usd
        return self.spend[day]

    def get_spend(self, day): return self.spend.get(day, 0.0)


class DynamoStore:
    """Keys: ORDER#id / TICKET#id / CONV#cid(STATE) / DEDUPE#msgid / RL#key / SPEND#day. TTL attr: ttl."""

    def __init__(self, table_name: str, region: str, table=None):
        self.t = table or boto3.resource("dynamodb", region_name=region).Table(table_name)

    def _get(self, pk, sk="META"):
        item = self.t.get_item(Key={"pk": pk, "sk": sk}).get("Item")
        return json.loads(item["data"]) if item else None

    def _put(self, pk, data, sk="META", ttl=None, **kw):
        item = {"pk": pk, "sk": sk, "data": json.dumps(data)}
        if ttl:
            item["ttl"] = int(time.time()) + ttl
        self.t.put_item(Item=item, **kw)

    def get_state(self, cid): return self._get(f"CONV#{cid}", "STATE") or _blank_state()
    def put_state(self, cid, st): self._put(f"CONV#{cid}", st, "STATE", ttl=7 * DAY)
    def get_order(self, oid): return self._get(f"ORDER#{oid}")
    def put_order(self, order): self._put(f"ORDER#{order['order_id']}", order)
    def create_ticket(self, t): self._put(f"TICKET#{t['ticket_id']}", t)

    def seen_message(self, mid):
        try:
            self._put(f"DEDUPE#{mid}", {}, ttl=DAY, ConditionExpression="attribute_not_exists(pk)")
            return False
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return True
            raise

    def incr(self, key, ttl_s):
        r = self.t.update_item(
            Key={"pk": f"RL#{key}", "sk": "META"},
            UpdateExpression="ADD cnt :one SET #t = :ttl",
            ExpressionAttributeNames={"#t": "ttl"},
            ExpressionAttributeValues={":one": 1, ":ttl": int(time.time()) + ttl_s},
            ReturnValues="UPDATED_NEW",
        )
        return int(r["Attributes"]["cnt"])

    def add_spend(self, day, usd):
        r = self.t.update_item(
            Key={"pk": f"SPEND#{day}", "sk": "META"},
            UpdateExpression="ADD usd :v SET #t = :ttl",
            ExpressionAttributeNames={"#t": "ttl"},
            ExpressionAttributeValues={":v": Decimal(str(round(usd, 6))), ":ttl": int(time.time()) + 3 * DAY},
            ReturnValues="UPDATED_NEW",
        )
        return float(r["Attributes"]["usd"])

    def get_spend(self, day):
        item = self.t.get_item(Key={"pk": f"SPEND#{day}", "sk": "META"}).get("Item")
        return float(item["usd"]) if item else 0.0
