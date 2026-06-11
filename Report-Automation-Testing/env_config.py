import os
import logging
from typing import Dict, Optional, Any
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from dotenv import load_dotenv
import json
from urllib.parse import urlparse

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DatabaseEnvConfig:
    """
    Environment configuration manager that loads environment variables from database
    with fallback to local environment variables.
    """
    
    def __init__(self, table_name: str = "env_config", auto_load: bool = True):
        """
        Initialize the database environment configuration.
        
        Args:
            table_name: Name of the database table storing environment variables
            auto_load: Whether to automatically load config on initialization
        """
        self.table_name = table_name
        self._config_cache: Dict[str, Any] = {}
        self._engine = None
        
        # Load local environment variables first
        load_dotenv()
        
        if auto_load:
            self.load_from_database()
    
    def _get_database_engine(self):
        """Get or create database engine."""
        if self._engine is None:
            # Use existing database configuration pattern
            DB_URL = os.getenv('DATABASE_URL')
            if DB_URL:
                parsed_url = urlparse(DB_URL)
                db_config = {
                    'host': parsed_url.hostname or '72.61.228.168',
                    'port': parsed_url.port or 5432,
                    'database': parsed_url.path.lstrip('/') or 'seleric_db',
                    'user': parsed_url.username or 'admin_seleric',
                    'password': parsed_url.password or 'SelericDB246',
                    'sslmode': 'require'
                }
            else:
                db_config = {
                    'host': os.environ.get('DB_HOST', '72.61.228.168'),
                    'port': int(os.environ.get('DB_PORT', 5432)),
                    'database': os.environ.get('DB_NAME', 'seleric_db'),
                    'user': os.environ.get('DB_USER', 'admin_seleric'),
                    'password': os.environ.get('DB_PASSWORD', 'SelericDB246'),
                    'sslmode': 'require'
                }
            
            conn_str = (
                f"postgresql://{db_config['user']}:{db_config['password']}@"
                f"{db_config['host']}:{db_config['port']}/{db_config['database']}?sslmode=require"
            )
            
            try:
                self._engine = create_engine(conn_str, isolation_level="AUTOCOMMIT")
                logger.info(f"Database engine created for {db_config['host']}")
            except Exception as e:
                logger.error(f"Failed to create database engine: {e}")
                self._engine = None
        
        return self._engine
    
    def create_config_table(self):
        """Check if the env_config table exists."""
        engine = self._get_database_engine()
        if not engine:
            logger.error("Cannot check table: database engine not available")
            return False
        
        try:
            with engine.connect() as conn:
                # Check if table exists
                check_table_sql = text(f"""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_name = '{self.table_name}'
                    );
                """)
                result = conn.execute(check_table_sql)
                table_exists = result.scalar()
                
                if table_exists:
                    logger.info(f"Configuration table '{self.table_name}' exists")
                    return True
                else:
                    logger.error(f"Configuration table '{self.table_name}' does not exist")
                    return False
                    
        except SQLAlchemyError as e:
            logger.error(f"Failed to check configuration table: {e}")
            return False
    
    def load_from_database(self) -> bool:
        """
        Load environment variables from database.
        
        Returns:
            bool: True if successful, False otherwise
        """
        engine = self._get_database_engine()
        if not engine:
            logger.warning("Database engine not available, using local environment only")
            return False
        
        try:
            # Check if table exists
            if not self.create_config_table():
                logger.error("env_config table not found. Please ensure the table exists.")
                return False
            
            with engine.connect() as conn:
                # Query for environment variables using the new schema
                query = text(f"""
                    SELECT key_name, key_value, service_name 
                    FROM {self.table_name} 
                    ORDER BY service_name, key_name
                """)
                
                result = conn.execute(query)
                
                # Load variables into cache and environment
                loaded_count = 0
                for row in result:
                    key_name, key_value, service_name = row
                    if key_name and key_value:
                        self._config_cache[key_name] = key_value
                        os.environ[key_name] = key_value
                        loaded_count += 1
                
                logger.info(f"Loaded {loaded_count} environment variables from database")
                return True
                
        except SQLAlchemyError as e:
            logger.error(f"Failed to load from database: {e}")
            return False
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        Get environment variable value.
        
        Args:
            key: Environment variable name
            default: Default value if not found
            
        Returns:
            The environment variable value or default
        """
        # Check cache first
        if key in self._config_cache:
            return self._config_cache[key]
        
        # Check environment variables
        value = os.getenv(key, default)
        if value is not None:
            self._config_cache[key] = value
        
        return value
    
    def set(self, key: str, value: Any, service_name: str = "default", description: str = None) -> bool:
        """
        Set environment variable in database.
        
        Args:
            key: Environment variable name
            value: Environment variable value
            service_name: Service name (e.g., 'Google Ads', 'Facebook Ads', 'Shopify')
            description: Optional description (not used in new schema)
            
        Returns:
            bool: True if successful, False otherwise
        """
        engine = self._get_database_engine()
        if not engine:
            logger.error("Cannot set variable: database engine not available")
            return False
        
        try:
            with engine.connect() as conn:
                # Upsert the configuration using the new schema
                upsert_sql = text(f"""
                    INSERT INTO {self.table_name} (service_name, key_name, key_value)
                    VALUES (:service_name, :key_name, :key_value)
                    ON CONFLICT (service_name, key_name) 
                    DO UPDATE SET 
                        key_value = EXCLUDED.key_value
                """)
                
                conn.execute(upsert_sql, {
                    "service_name": service_name,
                    "key_name": key,
                    "key_value": str(value)
                })
                conn.commit()
                
                # Update cache and environment
                self._config_cache[key] = str(value)
                os.environ[key] = str(value)
                
                logger.info(f"Set environment variable '{key}' in database for service '{service_name}'")
                return True
                
        except SQLAlchemyError as e:
            logger.error(f"Failed to set environment variable '{key}': {e}")
            return False
    
    def delete(self, key: str, service_name: str = None) -> bool:
        """
        Delete environment variable from database.
        
        Args:
            key: Environment variable name
            service_name: Service name (optional, will delete all matching keys if not specified)
            
        Returns:
            bool: True if successful, False otherwise
        """
        engine = self._get_database_engine()
        if not engine:
            logger.error("Cannot delete variable: database engine not available")
            return False
        
        try:
            with engine.connect() as conn:
                if service_name:
                    delete_sql = text(f"DELETE FROM {self.table_name} WHERE key_name = :key AND service_name = :service_name")
                    result = conn.execute(delete_sql, {"key": key, "service_name": service_name})
                else:
                    delete_sql = text(f"DELETE FROM {self.table_name} WHERE key_name = :key")
                    result = conn.execute(delete_sql, {"key": key})
                
                conn.commit()
                
                # Remove from cache
                self._config_cache.pop(key, None)
                
                logger.info(f"Deleted environment variable '{key}' from database")
                return result.rowcount > 0
                
        except SQLAlchemyError as e:
            logger.error(f"Failed to delete environment variable '{key}': {e}")
            return False
    
    def list_all(self) -> Dict[str, Any]:
        """
        List all environment variables from database.
        
        Returns:
            Dict containing all environment variables
        """
        engine = self._get_database_engine()
        if not engine:
            logger.warning("Database engine not available")
            return {}
        
        try:
            with engine.connect() as conn:
                query = text(f"""
                    SELECT service_name, key_name, key_value
                    FROM {self.table_name}
                    ORDER BY service_name, key_name
                """)
                
                result = conn.execute(query)
                configs = {}
                
                for row in result:
                    service_name, key_name, key_value = row
                    configs[key_name] = {
                        'value': key_value,
                        'service_name': service_name
                    }
                
                return configs
                
        except SQLAlchemyError as e:
            logger.error(f"Failed to list environment variables: {e}")
            return {}
    
    def get_by_service(self, service_name: str) -> Dict[str, Any]:
        """
        Get all environment variables for a specific service.
        
        Args:
            service_name: Name of the service
            
        Returns:
            Dict containing environment variables for the service
        """
        engine = self._get_database_engine()
        if not engine:
            logger.warning("Database engine not available")
            return {}
        
        try:
            with engine.connect() as conn:
                query = text(f"""
                    SELECT key_name, key_value
                    FROM {self.table_name}
                    WHERE service_name = :service_name
                    ORDER BY key_name
                """)
                
                result = conn.execute(query, {"service_name": service_name})
                configs = {}
                
                for row in result:
                    key_name, key_value = row
                    configs[key_name] = key_value
                
                return configs
                
        except SQLAlchemyError as e:
            logger.error(f"Failed to get variables for service '{service_name}': {e}")
            return {}
    
    def export_to_json(self, filepath: str) -> bool:
        """
        Export all environment variables to JSON file.
        
        Args:
            filepath: Path to export file
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            configs = self.list_all()
            with open(filepath, 'w') as f:
                json.dump(configs, f, indent=2, default=str)
            
            logger.info(f"Exported environment variables to {filepath}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to export to JSON: {e}")
            return False
    
    def import_from_json(self, filepath: str) -> bool:
        """
        Import environment variables from JSON file.
        
        Args:
            filepath: Path to import file
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            with open(filepath, 'r') as f:
                configs = json.load(f)
            
            success_count = 0
            for key, config in configs.items():
                service_name = config.get('service_name', 'default')
                if self.set(key, config['value'], service_name):
                    success_count += 1
            
            logger.info(f"Imported {success_count} environment variables from {filepath}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to import from JSON: {e}")
            return False
    
    def reload(self) -> bool:
        """
        Reload environment variables from database.
        
        Returns:
            bool: True if successful, False otherwise
        """
        self._config_cache.clear()
        return self.load_from_database()
    
    def get_database_config(self) -> Dict[str, Any]:
        """
        Get database configuration for the current environment.
        
        Returns:
            Dict containing database configuration
        """
        return {
            'host': self.get('DB_HOST', '72.61.228.168'),
            'port': int(self.get('DB_PORT', 5432)),
            'database': self.get('DB_NAME', 'seleric_db'),
            'user': self.get('DB_USER', 'admin_seleric'),
            'password': self.get('DB_PASSWORD', 'SelericDB246'),
            'sslmode': 'require'
        }
    
    def get_api_config(self) -> Dict[str, Any]:
        """
        Get API configuration for the current environment.
        
        Returns:
            Dict containing API configuration
        """
        return {
            'facebook_app_id': self.get('FACEBOOK_APP_ID'),
            'facebook_app_secret': self.get('FACEBOOK_APP_SECRET'),
            'facebook_access_token': self.get('FACEBOOK_ACCESS_TOKEN'),
            'facebook_ad_account_id': self.get('FACEBOOK_AD_ACCOUNT_ID'),
            'shopify_store': self.get('SHOPIFY_STORE'),
            'shopify_api_key': self.get('SHOPIFY_API_KEY'),
            'shopify_api_secret': self.get('SHOPIFY_API_SECRET'),
            'shopify_password': self.get('SHOPIFY_PASSWORD'),
            'shopify_api_version': self.get('SHOPIFY_API_VERSION', '2023-01'),
            'google_ads_developer_token': self.get('GOOGLE_ADS_DEVELOPER_TOKEN'),
            'google_ads_login_customer_id': self.get('GOOGLE_ADS_LOGIN_CUSTOMER_ID'),
            'google_ads_customer_id': self.get('GOOGLE_ADS_CUSTOMER_ID'),
            'azure_client_id': self.get('AZURE_CLIENT_ID'),
            'azure_tenant_id': self.get('AZURE_TENANT_ID'),
            'azure_client_secret': self.get('AZURE_CLIENT_SECRET'),
        }

# Global instance for easy access
env_config = DatabaseEnvConfig()

# Convenience functions
def get_env(key: str, default: Any = None) -> Any:
    """Get environment variable value."""
    return env_config.get(key, default)

def set_env(key: str, value: Any, service_name: str = "default", description: str = None) -> bool:
    """Set environment variable in database."""
    return env_config.set(key, value, service_name, description)

def reload_env() -> bool:
    """Reload environment variables from database."""
    return env_config.reload()

def get_service_config(service_name: str) -> Dict[str, Any]:
    """Get all configuration for a specific service."""
    return env_config.get_by_service(service_name)

if __name__ == "__main__":
    # Example usage
    print("Environment Configuration Manager")
    print("=" * 40)
    
    # Test loading
    print(f"Database connection available: {env_config._get_database_engine() is not None}")
    
    # Show some current values
    print("\nCurrent environment variables:")
    for key in ['DB_HOST', 'DB_USER', 'FACEBOOK_APP_ID', 'SHOPIFY_STORE']:
        value = get_env(key)
        print(f"  {key}: {value}")
    
    # Show all database configs
    print("\nAll database configurations:")
    all_configs = env_config.list_all()
    for key, config in all_configs.items():
        print(f"  {key}: {config['value']} ({config['service_name']})") 