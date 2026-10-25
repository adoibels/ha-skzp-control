"""Potwierdzanie i ponawianie komend niezależne od HA i jednostek sterownika."""
import asyncio
from collections.abc import Awaitable, Callable


class CommandManager:
    """Obsługuje komendy bez blokowania oczekiwania na pozostałe potwierdzenia.

    Encje sprawdzają potwierdzenia, przeliczenia wartości i zastąpienie komendy
    nowszą zmianą oraz zarządzają stanem pokazywanym przed potwierdzeniem.
    Tylko zapisy do połączenia TCP są wykonywane pojedynczo.
    """

    def __init__(self) -> None:
        self.active_count = 0

    async def execute(self, **kwargs) -> bool:
        """Śledzi trwające operacje, aby import nie rozpoczął się w ich środku."""
        self.active_count += 1
        try:
            return await self._execute(**kwargs)
        finally:
            self.active_count -= 1

    async def wait_for_confirmation(
        self, event: asyncio.Event, timeout: float, matches: Callable[[], bool],
        *, is_current: Callable[[], bool] | None = None,
    ) -> bool:
        """Czeka na aktualne potwierdzenie, pomijając nieaktualne sygnały."""
        try:
            async with asyncio.timeout(timeout):
                while True:
                    await event.wait()
                    if is_current is not None and not is_current():
                        return False
                    if matches():
                        return True
                    event.clear()
        except TimeoutError:
            return False

    async def _execute(
        self, *, send: Callable[[int], Awaitable[None]],
        wait: Callable[[], Awaitable[bool]],
        is_current: Callable[[], bool], retry_count: int, retry_delay: float,
        is_confirmed: Callable[[], bool] | None = None,
    ) -> bool:
        """Zwraca True po potwierdzeniu lub zastąpieniu komendy, False po wyczerpaniu prób.

        Błędy połączenia i anulowanie są przekazywane dalej bez ponowienia.
        Funkcja send otrzymuje numer próby: 0 dla pierwszego wysłania.
        Przed ponowieniem sprawdza potwierdzenie odebrane podczas przerwy.
        """
        for attempt in range(retry_count + 1):
            if not is_current():
                return True
            if attempt > 0 and is_confirmed is not None and is_confirmed():
                return True
            await send(attempt)
            if await wait():
                return True
            if not is_current():
                return True
            if attempt < retry_count:
                if retry_delay > 0:
                    await asyncio.sleep(retry_delay)
                if not is_current():
                    return True
        return False
