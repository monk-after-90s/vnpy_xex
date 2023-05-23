from enum import Enum
from vnpy.trader.constant import Exchange

# Extract original exchange name list
exchange_names = [e.name for e in Exchange]

# Add crypto currency exchanges
exchange_names.extend([
    "XEX",
])

# Generate new enum class
Exchange = Enum("Exchange", zip(exchange_names, exchange_names))
import vnpy.trader.constant

vnpy.trader.constant.Exchange = Exchange

import importlib_metadata

from .xex_gateway import XEXSpotGateway

try:
    __version__ = importlib_metadata.version("vnpy_xex")
except importlib_metadata.PackageNotFoundError:
    __version__ = "dev"
