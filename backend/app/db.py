from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings


pool = ConnectionPool(
    conninfo=settings.database_url,
    kwargs={"row_factory": dict_row},
    open=True,
)
