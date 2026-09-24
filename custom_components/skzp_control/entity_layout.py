"""Układ formularza i identyfikatory wyboru encji."""

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import CONF_DISABLED_ENTITIES, DOMAIN

PAGE_BOILER_BURNER = "boiler_burner"
PAGE_DHW_BUFFER = "dhw_buffer"
PAGE_CH1 = "ch1"
PAGE_CH2 = "ch2"
PAGE_CH3 = "ch3"

PAGE_ORDER = (
    PAGE_BOILER_BURNER,
    PAGE_DHW_BUFFER,
    PAGE_CH1,
    PAGE_CH2,
    PAGE_CH3,
)

SECTION_SENSORS = "sensors"
SECTION_CONFIGURATION = "configuration"
SECTION_ORDER = (SECTION_SENSORS, SECTION_CONFIGURATION)

FIELD_BOILER = "boiler"
FIELD_BURNER = "burner"
FIELD_MODULATION = "modulation"
FIELD_FUEL = "fuel"
FIELD_EXHAUST = "exhaust"
FIELD_STANDBY = "standby"
FIELD_AIR = "air"
FIELD_INTERVAL = "interval"
FIELD_FEEDER = "feeder"
FIELD_BOILER_POWER = "boiler_power"
FIELD_BUFFER = "buffer"
FIELD_DHW = "dhw"
FIELD_DHWC = "dhwc"
FIELD_CIRCUIT = "circuit"
FIELD_WORK_MODE = "work_mode"
FIELD_MIXER = "mixer"
FIELD_TEMPERATURES = "temperatures"
FIELD_ROOM = "room"
FIELD_WEATHER = "weather"

# Kolejność encji w formularzu; odpowiedniki modeli są umieszczone razem.
ENTITY_LAYOUT = (
    # Kocioł / Palnik — sensory i stany
    (
        PAGE_BOILER_BURNER,
        SECTION_SENSORS,
        FIELD_BOILER,
        (
            "BoilerTempAct",
            "CH1ReturnTempAct",
            "BuTempAct",
            "ExhaustTempAct",
            "AN01",
            "BuPhotoAct",
            "WeaTempAct",
            "K003",
            "DevStatus_Power",
            "DevStatus_Fan",
            "DevStatus_outHeater",
            "DevStatus_outFeeder",
        ),
    ),
    (
        PAGE_BOILER_BURNER,
        SECTION_SENSORS,
        FIELD_FUEL,
        (
            "BuFuelCaloric",
            "B060",
            "B062",
            "BuTotalFuel",
            "Bu24hFuel",
            "BuActualFuel",
        ),
    ),
    # Kocioł / Palnik — konfiguracja
    (
        PAGE_BOILER_BURNER,
        SECTION_CONFIGURATION,
        FIELD_BOILER,
        ("K006", "BoilerTempCmd", "BuIntHist", "K007"),
    ),
    (
        PAGE_BOILER_BURNER,
        SECTION_CONFIGURATION,
        FIELD_BURNER,
        ("BuMode", "BuOptNoFireTime", "BuOptClrPeriod", "BuOptClrTime"),
    ),
    (
        PAGE_BOILER_BURNER,
        SECTION_CONFIGURATION,
        FIELD_MODULATION,
        ("BuModulMin", "BuModulMax", "B026", "BuTimeAboveMax"),
    ),
    (
        PAGE_BOILER_BURNER,
        SECTION_CONFIGURATION,
        FIELD_FUEL,
        ("BuFuelCaloric", "BuFuelCorr"),
    ),
    (
        PAGE_BOILER_BURNER,
        SECTION_CONFIGURATION,
        FIELD_EXHAUST,
        ("ExhaustTempMax",),
    ),
    (
        PAGE_BOILER_BURNER,
        SECTION_CONFIGURATION,
        FIELD_STANDBY,
        ("BuSbyFeedTime", "BuSbyPeriod", "BuSbyAirPwr", "BuSbyAirTime", "B059"),
    ),
    (
        PAGE_BOILER_BURNER,
        SECTION_CONFIGURATION,
        FIELD_AIR,
        ("BuAirMin", "BuAirMax", "BuAditAir"),
    ),
    (
        PAGE_BOILER_BURNER,
        SECTION_CONFIGURATION,
        FIELD_INTERVAL,
        ("BuIntFeedTime", "BuIntBreakTime", "BuIntAirPwr"),
    ),
    (
        PAGE_BOILER_BURNER,
        SECTION_CONFIGURATION,
        FIELD_FEEDER,
        ("BuOptFdrEff", "BuOptFdrFTime"),
    ),
    (
        PAGE_BOILER_BURNER,
        SECTION_CONFIGURATION,
        FIELD_BOILER_POWER,
        ("BuOptPwr",),
    ),
    # CWU / Bufor — sensory i stany
    (
        PAGE_DHW_BUFFER,
        SECTION_SENSORS,
        FIELD_BUFFER,
        ("DevStatus_outBuffer", "D201", "D202", "D203", "D204"),
    ),
    (
        PAGE_DHW_BUFFER,
        SECTION_SENSORS,
        FIELD_DHW,
        ("DevStatus_outDHW", "DHWTempAct"),
    ),
    (PAGE_DHW_BUFFER, SECTION_SENSORS, FIELD_DHWC, ("DevStatus_outCirc",)),
    # CWU / Bufor — konfiguracja
    (
        PAGE_DHW_BUFFER,
        SECTION_CONFIGURATION,
        FIELD_BUFFER,
        ("D203", "D204"),
    ),
    (
        PAGE_DHW_BUFFER,
        SECTION_CONFIGURATION,
        FIELD_DHW,
        ("DHWMode", "DHWTempCmd", "DHWHist", "DHWOverH", "DHWPriority"),
    ),
    (
        PAGE_DHW_BUFFER,
        SECTION_CONFIGURATION,
        FIELD_DHWC,
        ("DHWCMode", "DHWCTempON", "DHWCWork", "DHWCBrake", "DHWCAlwaysON"),
    ),
    # Obieg CO1 — sensory i stany
    (
        PAGE_CH1,
        SECTION_SENSORS,
        FIELD_CIRCUIT,
        ("DevStatus_outCH1", "C006", "C007", "CH1MixTempAct"),
    ),
    (PAGE_CH1, SECTION_SENSORS, FIELD_MIXER, ("C001", "CH1MixValueAct")),
    (
        PAGE_CH1,
        SECTION_SENSORS,
        FIELD_ROOM,
        ("C008", "C009", "CH1RoomTempAct", "CH1RoomTempCmd"),
    ),
    # Obieg CO1 — konfiguracja
    (PAGE_CH1, SECTION_CONFIGURATION, FIELD_WORK_MODE, ("C012", "CH1Mode")),
    (
        PAGE_CH1,
        SECTION_CONFIGURATION,
        FIELD_MIXER,
        (
            "C020",
            "C022",
            "C023",
            "C024",
            "C025",
            "CH1MixActive",
            "CH1MixValueMin",
            "CH1MixValueMax",
            "CH1MixGain",
            "CH1MixPeriod",
        ),
    ),
    (
        PAGE_CH1,
        SECTION_CONFIGURATION,
        FIELD_TEMPERATURES,
        (
            "C027",
            "C028",
            "C029",
            "C046",
            "CH1MixTempBase",
            "CH1MixTempMin",
            "CH1MixTempMax",
        ),
    ),
    (
        PAGE_CH1,
        SECTION_CONFIGURATION,
        FIELD_ROOM,
        (
            "C016",
            "C013",
            "C014",
            "C015",
            "CH1RoomMode",
            "CH1RoomTempCom",
            "CH1RoomTempEco",
            "CH1RoomHist",
        ),
    ),
    (
        PAGE_CH1,
        SECTION_CONFIGURATION,
        FIELD_WEATHER,
        ("C018", "C040", "WeaCorr", "WeaTempStopCH1"),
    ),
    # Obieg CO2 — sensory i stany
    (PAGE_CH2, SECTION_SENSORS, FIELD_CIRCUIT, ("DevStatus_outCH2", "C106", "C107")),
    (PAGE_CH2, SECTION_SENSORS, FIELD_MIXER, ("C101",)),
    (PAGE_CH2, SECTION_SENSORS, FIELD_ROOM, ("C108", "C109")),
    # Obieg CO2 — konfiguracja
    (PAGE_CH2, SECTION_CONFIGURATION, FIELD_WORK_MODE, ("C112", "CH2Mode")),
    (
        PAGE_CH2,
        SECTION_CONFIGURATION,
        FIELD_MIXER,
        ("C120", "C122", "C123", "C124", "C125"),
    ),
    (
        PAGE_CH2,
        SECTION_CONFIGURATION,
        FIELD_TEMPERATURES,
        ("C127", "C128", "C129", "C146"),
    ),
    (PAGE_CH2, SECTION_CONFIGURATION, FIELD_ROOM, ("C116", "C113", "C114", "C115")),
    (
        PAGE_CH2,
        SECTION_CONFIGURATION,
        FIELD_WEATHER,
        ("C118", "C140", "WeaTempStopCH2"),
    ),
    # Obieg CO3 — sensory i stany
    (PAGE_CH3, SECTION_SENSORS, FIELD_CIRCUIT, ("DevStatus_outCH3", "C206", "C207")),
    (PAGE_CH3, SECTION_SENSORS, FIELD_MIXER, ("C201",)),
    (PAGE_CH3, SECTION_SENSORS, FIELD_ROOM, ("C208", "C209")),
    # Obieg CO3 — konfiguracja
    (PAGE_CH3, SECTION_CONFIGURATION, FIELD_WORK_MODE, ("C212",)),
    (
        PAGE_CH3,
        SECTION_CONFIGURATION,
        FIELD_MIXER,
        ("C220", "C222", "C223", "C224", "C225"),
    ),
    (
        PAGE_CH3,
        SECTION_CONFIGURATION,
        FIELD_TEMPERATURES,
        ("C227", "C228", "C229", "C246"),
    ),
    (PAGE_CH3, SECTION_CONFIGURATION, FIELD_ROOM, ("C216", "C213", "C214", "C215")),
    (
        PAGE_CH3,
        SECTION_CONFIGURATION,
        FIELD_WEATHER,
        ("C218", "C240"),
    ),
)

_KEY_LAYOUT = {
    (section_name, key): (page, section_name, field, layout_index, key_index)
    for layout_index, (page, section_name, field, keys) in enumerate(ENTITY_LAYOUT)
    for key_index, key in enumerate(keys)
}

@dataclass(frozen=True)
class EntityChoice:
    """Encja dostępna w formularzu wyboru."""

    platform: str
    key: str
    unique_suffix: str
    page: str
    section: str
    field: str
    order: tuple[int, int]
    translation_key: str | None = None

    @property
    def selection_id(self) -> str:
        """Zwraca identyfikator encji używany w opcjach."""
        return make_selection_id(self.platform, self.key)

    @property
    def option_id(self) -> str:
        """Zwraca klucz opcji formularza i jej tłumaczenia."""
        return f"{self.platform}_{self.translation_key or self.unique_suffix}"


def make_selection_id(platform: str, key: str) -> str:
    """Tworzy identyfikator encji używany w opcjach."""
    return f"{platform}:{key}"


def existing_buffer_sensor_keys(
    hass: HomeAssistant, entry_id: str
) -> frozenset[str]:
    """Zwraca istniejące sensory ustawień bufora danego wpisu."""
    registry = er.async_get(hass)
    return frozenset(
        key
        for key in ("D203", "D204")
        if registry.async_get_entity_id(
            "sensor",
            DOMAIN,
            f"{entry_id}_{key.lower()}",
        )
        is not None
    )


def is_entity_enabled(config_entry: ConfigEntry, platform: str, key: str) -> bool:
    """Sprawdza, czy encja jest włączona w opcjach integracji."""
    selection_id = make_selection_id(platform, key)
    disabled = config_entry.options.get(CONF_DISABLED_ENTITIES, [])
    return selection_id not in disabled


def entity_location(
    platform: str, key: str
) -> tuple[str, str, str, tuple[int, int]] | None:
    """Zwraca położenie encji jawnie przypisanej do formularza."""
    platform_section = (
        SECTION_SENSORS
        if platform in {"sensor", "binary_sensor"}
        else SECTION_CONFIGURATION
    )
    layout = _KEY_LAYOUT.get((platform_section, key))
    if layout is None:
        return None
    page, section_name, field, layout_index, key_index = layout
    return page, section_name, field, (layout_index, key_index)
