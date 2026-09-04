from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from statistics import mean
from collections import defaultdict

@dataclass
class OrderRecord:
    order_id: str
    user_id: str
    amount: float
    status: str
    channel: str
    region: str
    app_version: str
    category: str
    created_at: str
    paid_at: str

class OrderStatus(Enum):
    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"

class OrderAnalytics:
    