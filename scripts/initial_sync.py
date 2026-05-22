"""Populate the local SQLite cache from ServiceTitan. Run once after setup."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from lib.database import get_connection
from lib.servicetitan import ServiceTitanClient
from lib.sync import sync_all

load_dotenv()

client = ServiceTitanClient(
    app_key=os.environ["ST_APP_KEY"],
    tenant_id=os.environ["ST_TENANT_ID"],
    client_id=os.environ["ST_CLIENT_ID"],
    client_secret=os.environ["ST_CLIENT_SECRET"],
    environment=os.environ.get("ST_ENVIRONMENT", "production"),
)

conn = get_connection()
start = time.time()
stats = sync_all(client, conn, progress=lambda m: print(f"  · {m}"))
print(f"\nDone in {time.time() - start:.1f}s")
print(f"Stats: {stats}")
