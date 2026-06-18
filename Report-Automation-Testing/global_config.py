#!/usr/bin/env python3
"""
Global Configuration Loader
This module loads all environment variables from the database once and makes them available globally.
"""

import os
import logging
import tempfile
from typing import Dict, Any, Optional
from env_config import env_config, get_env, set_env

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class GlobalConfig:
    """
    Global configuration manager that loads all environment variables once
    and provides them globally without repeated database calls.
    """
    
    def __init__(self):
        """Initialize the global configuration."""
        self._config_cache: Dict[str, Any] = {}
        self._service_configs: Dict[str, Dict[str, Any]] = {}
        self._is_loaded = False
        
    def load_all_configs(self) -> bool:
        """
        Load all configurations from database once.
        
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            logger.info("Loading all configurations from database...")
            
            # Load all environment variables
            all_configs = env_config.list_all()
            
            # Populate cache
            for key_name, config in all_configs.items():
                self._config_cache[key_name] = config['value']
                # Also set in environment for compatibility
                os.environ[key_name] = config['value']
            
            # Load service-specific configurations
            services = ['Google Ads', 'Facebook Ads', 'Shopify', 'Azure OpenAI']
            for service in services:
                service_config = env_config.get_by_service(service)
                if service_config:
                    self._service_configs[service] = service_config
            
            self._is_loaded = True
            logger.info(f"Loaded {len(self._config_cache)} environment variables globally")
            return True
            
        except Exception as e:
            logger.error(f"Failed to load global configurations: {e}")
            return False
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        Get environment variable value from global cache.
        
        Args:
            key: Environment variable name
            default: Default value if not found
            
        Returns:
            The environment variable value or default
        """
        # Ensure configs are loaded
        if not self._is_loaded:
            self.load_all_configs()
        
        return self._config_cache.get(key, default)
    
    def get_service_config(self, service_name: str) -> Dict[str, Any]:
        """
        Get all configuration for a specific service from global cache.
        
        Args:
            service_name: Name of the service
            
        Returns:
            Dict containing environment variables for the service
        """
        # Ensure configs are loaded
        if not self._is_loaded:
            self.load_all_configs()
        
        return self._service_configs.get(service_name, {})
    
    def get_google_ads_config(self) -> Dict[str, Any]:
        """Get Google Ads configuration."""
        return self.get_service_config('Google Ads')
    
    def get_facebook_ads_config(self) -> Dict[str, Any]:
        """Get Facebook Ads configuration."""
        return self.get_service_config('Facebook Ads')
    
    def get_shopify_config(self) -> Dict[str, Any]:
        """Get Shopify configuration."""
        return self.get_service_config('Shopify')
    
    def get_azure_openai_config(self) -> Dict[str, Any]:
        """Get Azure OpenAI configuration."""
        return self.get_service_config('Azure OpenAI')
    
    def reload(self) -> bool:
        """
        Reload all configurations from database.
        
        Returns:
            bool: True if successful, False otherwise
        """
        self._config_cache.clear()
        self._service_configs.clear()
        self._is_loaded = False
        return self.load_all_configs()
    
    def list_all_keys(self) -> Dict[str, Any]:
        """
        List all available configuration keys.
        
        Returns:
            Dict containing all configuration keys and their service names
        """
        if not self._is_loaded:
            self.load_all_configs()
        
        result = {}
        for key, value in self._config_cache.items():
            # Find which service this key belongs to
            service_name = "Unknown"
            for service, config in self._service_configs.items():
                if key in config:
                    service_name = service
                    break
            
            result[key] = {
                'value': value,
                'service': service_name
            }
        
        return result
    
    def get_database_config(self) -> Dict[str, Any]:
        """Get database configuration."""
        return {
            'host': self.get('DB_HOST', '72.61.228.168'),
            'port': int(self.get('DB_PORT', 5432)),
            'database': self.get('DB_NAME', 'seleric_db'),
            'user': self.get('DB_USER', 'admin_seleric'),
            'password': self.get('DB_PASSWORD', 'SelericDB246'),
            'sslmode': 'require'
        }
    
    def get_api_config(self) -> Dict[str, Any]:
        """Get API configuration."""
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

# Global instance
global_config = GlobalConfig()

# Convenience functions for global access
def get_global_config(key: str, default: Any = None) -> Any:
    """Get environment variable from global cache."""
    return global_config.get(key, default)

def get_google_ads_config() -> Dict[str, Any]:
    """Get Google Ads configuration."""
    return global_config.get_google_ads_config()

def get_facebook_ads_config() -> Dict[str, Any]:
    """Get Facebook Ads configuration."""
    return global_config.get_facebook_ads_config()

def get_shopify_config() -> Dict[str, Any]:
    """Get Shopify configuration."""
    return global_config.get_shopify_config()

def get_azure_openai_config() -> Dict[str, Any]:
    """Get Azure OpenAI configuration."""
    return global_config.get_azure_openai_config()

def reload_global_config() -> bool:
    """Reload all global configurations."""
    return global_config.reload()

def list_all_configs() -> Dict[str, Any]:
    """List all available configurations."""
    return global_config.list_all_keys()

def get_temp_dir() -> str:
    """
    Get temporary directory path in a cross-platform way.
    For Azure Functions on Linux, uses /tmp (ephemeral storage).
    For local development, uses system temp directory.
    
    Returns:
        str: Path to temporary directory
    """
    # Check if we're in Azure Functions (Linux) environment
    # Azure Functions sets WEBSITE_INSTANCE_ID environment variable
    if os.environ.get('WEBSITE_INSTANCE_ID'):
        # Azure Functions Linux environment - use /tmp
        return '/tmp'
    elif os.name == 'posix' and os.path.exists('/tmp'):
        # Linux/Unix environment - use /tmp
        return '/tmp'
    else:
        # Windows or other - use system temp directory
        return tempfile.gettempdir()

def get_report_dir() -> str:
    """
    Get report directory path for file writes (Excel, PDF, etc.).
    In Azure Functions (WEBSITE_INSTANCE_ID set), always uses /tmp/reports so writes succeed.
    Otherwise uses REPORT_DIR env if set, or temp_dir/reports.
    
    Returns:
        str: Path to report directory (writable; /tmp/reports in Azure)
    """
    # Azure Functions: only /tmp is writable; use /tmp/reports for all report outputs
    if os.environ.get('WEBSITE_INSTANCE_ID'):
        return os.path.join('/tmp', 'reports')
    # Optional override for local/dev
    report_dir = os.environ.get('REPORT_DIR')
    if report_dir:
        return report_dir
    # Fallback to temp_dir/reports
    temp_dir = get_temp_dir()
    return os.path.join(temp_dir, 'reports')

# Auto-load configurations when module is imported
def initialize_global_config():
    """Initialize global configuration on module import."""
    try:
        success = global_config.load_all_configs()
        if success:
            logger.info("Global configuration loaded successfully")
        else:
            logger.warning("Failed to load global configuration, using local environment")
    except Exception as e:
        logger.error(f"Error initializing global configuration: {e}")

# Initialize on import
initialize_global_config()

if __name__ == "__main__":
    # Example usage
    print("Global Configuration Manager")
    print("=" * 40)
    
    # Show loaded configurations
    print(f"Total configurations loaded: {len(global_config._config_cache)}")
    
    # Show service configurations
    print("\nService Configurations:")
    for service in ['Google Ads', 'Facebook Ads', 'Shopify', 'Azure OpenAI']:
        config = global_config.get_service_config(service)
        print(f"  {service}: {len(config)} variables")
    
    # Show some example values
    print("\nExample Values:")
    example_keys = [
        'TH_GOOGLE_ADS_CUSTOMER_ID',
        'TH_SHOPIFY_STORE',
        'Socialpepper_FB_AD_ACCOUNT_ID',
        'AZURE_OPENAI_API_KEY',
        'AZURE_OPENAI_ENDPOINT',
        'AZURE_DEPLOYMENT',
    ]
    
    for key in example_keys:
        value = get_global_config(key)
        if value:
            masked_value = value[:10] + "..." if len(str(value)) > 10 else value
            print(f"  {key}: {masked_value}")
        else:
            print(f"  {key}: Not found")
    
    # Show all available keys
    print("\nAll Available Keys:")
    all_configs = list_all_configs()
    for key, config in list(all_configs.items())[:10]:  # Show first 10
        print(f"  {key} ({config['service']})")
    
    if len(all_configs) > 10:
        print(f"  ... and {len(all_configs) - 10} more") 