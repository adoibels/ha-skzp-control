"""Ładuje moduły integracji z minimalnymi atrapami Home Assistanta."""
import sys
import types
from pathlib import Path
from unittest.mock import Mock, AsyncMock
ROOT = Path(__file__).resolve().parents[1] / 'custom_components' / 'skzp_control'
pkg = types.ModuleType('skzp_control'); pkg.__path__ = [str(ROOT)]
sys.modules['skzp_control'] = pkg
class HAError(Exception):
    def __init__(self, **kwargs):
        self.key = kwargs.get('translation_key')
        self.kwargs = kwargs
        super().__init__(self.key)
for name in ('homeassistant', 'homeassistant.components', 'homeassistant.components.persistent_notification', 'homeassistant.config_entries', 'homeassistant.const', 'homeassistant.core', 'homeassistant.exceptions', 'homeassistant.helpers', 'homeassistant.helpers.entity_registry', 'homeassistant.helpers.device_registry', 'homeassistant.helpers.translation'):
    mod = types.ModuleType(name); mod.__path__ = []
    sys.modules[name] = mod
    if '.' in name:
        parent, child = name.rsplit('.', 1); setattr(sys.modules[parent], child, mod)
sys.modules['homeassistant.config_entries'].ConfigEntry = object
sys.modules['homeassistant.core'].HomeAssistant = object
sys.modules['homeassistant.const'].CONF_HOST = 'host'
sys.modules['homeassistant.const'].CONF_PORT = 'port'
for name in ('ConfigEntryError', 'ConfigEntryNotReady', 'HomeAssistantError'):
    setattr(sys.modules['homeassistant.exceptions'], name, HAError)
sys.modules['homeassistant.helpers.device_registry'].DeviceInfo = dict
sys.modules['homeassistant.helpers.translation'].async_get_translations = AsyncMock()
sys.modules['homeassistant.helpers.translation'].async_get_cached_translations = Mock(return_value={})
sys.modules['homeassistant.components.persistent_notification'].async_create = Mock()
sys.modules['homeassistant.components.persistent_notification'].async_dismiss = Mock()
