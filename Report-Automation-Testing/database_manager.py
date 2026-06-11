import os
import logging
from sqlalchemy import create_engine
from dotenv import load_dotenv
from urllib.parse import urlparse

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
DB_URL = os.getenv('DATABASE_URL')
if DB_URL:
    parsed_url = urlparse(DB_URL)
    DB_CONFIG = {
        'host': parsed_url.hostname or 'selericdb.postgres.database.azure.com',
        'port': parsed_url.port or 5432,
        'database': parsed_url.path.lstrip('/') or 'postgres',
        'user': parsed_url.username or 'admin_seleric',
        'password': parsed_url.password or 'Seleric789',
        'sslmode': 'require'
    }
else:
    DB_CONFIG = {
        'host': os.environ.get('DB_HOST', 'selericdb.postgres.database.azure.com'),
        'port': int(os.environ.get('DB_PORT', 5432)),
        'database': os.environ.get('DB_NAME', 'postgres'),
        'user': os.environ.get('DB_USER', 'admin_seleric'),
        'password': os.environ.get('DB_PASSWORD', 'Seleric789'),
        'sslmode': 'require'
    }

_engine = None

def get_db_engine():
    global _engine
    if _engine is not None:
        return _engine
    conn_str = (
        f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@"
        f"{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}?sslmode=require"
    )
    logger.info(f"Creating database engine for host={DB_CONFIG['host']}, db={DB_CONFIG['database']}, user={DB_CONFIG['user']}")
    _engine = create_engine(conn_str, isolation_level="AUTOCOMMIT")
    return _engine

def dispose_engine():
    global _engine
    if _engine is not None:
        logger.info("Disposing database engine.")
        _engine.dispose()
        _engine = None 