import os
import json
import logging
from pathlib import Path
from datetime import datetime

class MasterResource:
    """
    Centralized Resource Manager for the GCPPythonCode ecosystem.
    Directs all projects to the MasterConfiguration directory.
    """
    
    # Absolute path to the MasterConfiguration directory
    MASTER_ROOT = Path(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration")
    
    @classmethod
    def get_kite_config(cls):
        """Load Kite credentials from the master config folder."""
        config_path = cls.MASTER_ROOT / 'config' / 'kite_config.json'
        if not config_path.exists():
            raise FileNotFoundError(f"Master Kite configuration NOT FOUND at {config_path}")
            
        with open(config_path, 'r') as f:
            return json.load(f)

    @classmethod
    def get_instruments_path(cls):
        """Get the absolute path to the master valid_instruments.csv file."""
        data_path = cls.MASTER_ROOT / 'data' / 'valid_instruments.csv'
        data_path = cls.MASTER_ROOT / 'data' / 'valid_instruments.csv'
        return str(data_path)

    @classmethod
    def setup_shared_logger(cls, app_name):
        """Setup a logger that writes to the central master logs directory.

        Each call from a fresh process gets its own timestamped log file:
            <app_name>_DDMonYYYY_HH_MM_SS.log   e.g. dhan_sl_monitor_12Apr2026_09_15_30.log
        """
        log_dir = cls.MASTER_ROOT / 'logs'
        log_dir.mkdir(exist_ok=True)

        logger = logging.getLogger(app_name)
        logger.setLevel(logging.INFO)

        # Only add handlers once per process (prevents duplicate lines if
        # setup_shared_logger is called multiple times for the same name).
        if not logger.handlers:
            timestamp = datetime.now().strftime("%d%b%Y_%H_%M_%S")
            log_file  = log_dir / f"{app_name}_{timestamp}.log"

            # File handler — new file per process restart
            fh = logging.FileHandler(log_file, encoding='utf-8')
            fh.setFormatter(logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            ))
            logger.addHandler(fh)

            # Console handler
            ch = logging.StreamHandler()
            ch.setFormatter(logging.Formatter(
                '%(name)s - %(levelname)s - %(message)s'
            ))
            logger.addHandler(ch)

        return logger
    @classmethod
    def get_shared_db_path(cls):
        """Get the absolute path to the central signals database (Webhooks)."""
        db_path = cls.MASTER_ROOT / 'data' / 'shared_market_data.db'
        return str(db_path)

    @classmethod
    def get_trading_db_path(cls):
        """Get the absolute path to the main trading signals database (Telegram)."""
        db_path = cls.MASTER_ROOT / 'data' / 'trading.db'
        return str(db_path)

    @classmethod
    def get_telegram_config(cls):
        """Load Telegram credentials from the master config folder."""
        config_path = cls.MASTER_ROOT / 'config' / 'telegram_config.json'
        if not config_path.exists():
            return None
        with open(config_path, 'r') as f:
            return json.load(f)

    @classmethod
    def get_claude_key(cls):
        """Load Claude API key from the master config folder."""
        key_path = cls.MASTER_ROOT / 'config' / 'claude_api_key.txt'
        if not key_path.exists():
            return None
        with open(key_path, 'r') as f:
            return f.read().strip()

    @classmethod
    def get_parsing_rules_path(cls):
        """Get the absolute path to the parsing_rules_enhanced_v2.json file."""
        config_path = cls.MASTER_ROOT / 'config' / 'parsing_rules_enhanced_v2.json'
        config_path = cls.MASTER_ROOT / 'config' / 'parsing_rules_enhanced_v2.json'
        return str(config_path)

    @classmethod
    def get_parquet_path(cls):
        """Get the absolute path to the valid_instruments.parquet file."""
        data_path = cls.MASTER_ROOT / 'data' / 'valid_instruments.parquet'
        data_path = cls.MASTER_ROOT / 'data' / 'valid_instruments.parquet'
        return str(data_path)

    @classmethod
    def get_sl_config_path(cls):
        """Get the absolute path to sl_config.json (SL monitor configuration)."""
        return str(cls.MASTER_ROOT / 'config' / 'sl_config.json')

    @classmethod
    def get_sl_exits_path(cls):
        """Get the absolute path to sl_exits.json (daily SL exit blacklist, shared between sl_monitor and order_placer)."""
        return str(cls.MASTER_ROOT / 'data' / 'sl_exits.json')

# Shortcut functions for easy importing
def get_kite_config():
    return MasterResource.get_kite_config()

def get_instruments_path():
    return MasterResource.get_instruments_path()

def get_shared_db_path():
    return MasterResource.get_shared_db_path()

def get_trading_db_path():
    return MasterResource.get_trading_db_path()

def get_telegram_config():
    return MasterResource.get_telegram_config()

def get_claude_key():
    return MasterResource.get_claude_key()

def get_parsing_rules_path():
    return MasterResource.get_parsing_rules_path()

def get_parquet_path():
    return MasterResource.get_parquet_path()

def get_sl_config_path():
    return MasterResource.get_sl_config_path()

def get_sl_exits_path():
    return MasterResource.get_sl_exits_path()

def setup_logger(app_name):
    return MasterResource.setup_shared_logger(app_name)
