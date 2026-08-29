from datetime import timedelta

from argon2 import PasswordHasher
from argon2.low_level import Type

# OWASP's current minimum Argon2id profile (19 MiB, two iterations, one lane).
# A dedicated one-token login worker limiter serializes password verification;
# the global throttle row lock independently preserves that bound inside the
# repository transaction. This profile benchmarks at roughly 25 ms in the
# Python 3.14 API development environment used for this change.
PASSWORD_MEMORY_KIB = 19_456
PASSWORD_TIME_COST = 2
PASSWORD_PARALLELISM = 1
PASSWORD_HASH_BYTES = 32
PASSWORD_SALT_BYTES = 16
ADMIN_PASSWORD_MIN_BYTES = 20
ADMIN_PASSWORD_MAX_BYTES = 1_024

PASSWORD_HASHER = PasswordHasher(
    time_cost=PASSWORD_TIME_COST,
    memory_cost=PASSWORD_MEMORY_KIB,
    parallelism=PASSWORD_PARALLELISM,
    hash_len=PASSWORD_HASH_BYTES,
    salt_len=PASSWORD_SALT_BYTES,
    type=Type.ID,
)

# A normal Argon2id verifier used only to equalize syntactically valid unknown
# logins. Its input has no authentication meaning and grants no access.
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=19456,t=2,p=1$"
    "/NugjqiIU8py8h7Kcv6dlw$"
    "9TlyDzyIYzAEWKLYHTm4IwspVxK5I1zBnamce16nuyQ"
)

SESSION_SECRET_BYTES = 32
CSRF_SECRET_BYTES = 32
SESSION_IDLE_LIFETIME = timedelta(minutes=30)
SESSION_ABSOLUTE_LIFETIME = timedelta(hours=24)
SESSION_LAST_USE_COALESCE = timedelta(minutes=5)
SESSION_PRUNE_LIMIT = 100

THROTTLE_WINDOW = timedelta(minutes=10)
SOURCE_FAILURE_LIMIT = 10
SOURCE_BLOCK_LIFETIME = timedelta(minutes=15)
GLOBAL_FAILURE_LIMIT = 100
GLOBAL_BLOCK_LIFETIME = timedelta(minutes=5)
GLOBAL_BUCKET_KEY = "instance"
MAX_SOURCE_KEY_LENGTH = 128
MAX_RETRY_AFTER_SECONDS = 15 * 60
THROTTLE_PRUNE_LIMIT = 100
