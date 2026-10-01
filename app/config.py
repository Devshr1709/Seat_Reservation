import os
import re

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/seats")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "admin-secret")
POOL_MAX = int(os.environ.get("POOL_MAX", "15"))
DEFAULT_LIMIT = int(os.environ.get("PER_USER_LIMIT", "4"))
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:@/+=-]{1,256}$")
