"""Potwierdzanie i ponawianie komend niezależne od HA i jednostek sterownika."""
import asyncio
from collections.abc import Awaitable, Callable


class CommandManager:
    """Obsługuje komendy bez blokowania oczekiwania na pozostałe potwierdzenia.

    Encje sprawdzają potwierdzenia, przeliczenia wartości i zastąpienie komendy
    nowszą zmianą oraz zarządzają stanem pokazywanym przed potwierdzeniem.
    Tylko zapisy do połączenia TCP są wykonywane pojedynczo.
    """

    async def wait_for_confirmation(
        self, event: asyncio.Event, timeout: float, matches: Callable[[], bool]
    ) -> bool:
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            return False
        return matches()

    async def execute(
        self, *, send: Callable[[], Awaitable[None]],
        wait: Callable[[], Awaitable[bool]],
        is_current: Callable[[], bool], retry_count: int, retry_delay: float,
        on_retry: Callable[[int], None],
    ) -> bool:
        """Zwraca True po potwierdzeniu lub zastąpieniu komendy, False po wyczerpaniu prób.

        Błędy połączenia i anulowanie są przekazywane dalej bez ponowienia.
        Funkcja send buduje komendę przy każdej próbie, uwzględniając aktualne aliasy.
        Przerwa przed ponowieniem trwa do końca, także gdy nadejdzie potwierdzenie.
        """
        for attempt in range(retry_count + 1):
            if not is_current():
                return True
            await send()
            if await wait():
                return True
            if not is_current():
                return True
            if attempt < retry_count:
                on_retry(attempt + 1)
                if retry_delay > 0:
                    await asyncio.sleep(retry_delay)
                if not is_current():
                    return True
        return False
