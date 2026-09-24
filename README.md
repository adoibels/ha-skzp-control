# SKZP Control — integracja dla Home Assistant

Umożliwia odczyt danych i zmianę ustawień sterowników kotłów SKZP firmy Timel
w Home Assistant przez moduł LAN/Wi-Fi.

## Obsługiwane sterowniki

- SKZP-02S
- SKZP-02T
- SKZP-04P
- SKZP-05S
- SKZP-05PW

Integracja została przetestowana na **SKZP-05S**. Obsługa pozostałych wariantów
wymaga potwierdzenia.

Model jest rozpoznawany automatycznie. Dostępność encji i funkcji zależy od modelu,
wersji oprogramowania oraz danych udostępnianych przez sterownik.

## Funkcje

- odczyt temperatur, statusów, alarmów i zużycia paliwa,
- obsługa obiegów CO, CWU, cyrkulacji, mieszaczy i bufora,
- zmiana parametrów oraz trybów pracy,
- włączanie i wyłączanie sterownika,
- rozpalanie, wygaszanie i kasowanie alarmów,
- opcjonalne powiadomienia o utracie i przywróceniu komunikacji.

## Instalacja

### Przez HACS

1. Otwórz HACS i z menu **⋮** wybierz **Repozytoria niestandardowe**.
2. Dodaj adres `https://github.com/adoibels/ha-skzp-control` i wybierz typ **Integracja**.
3. Wyszukaj **SKZP Control** w HACS i pobierz integrację.
4. Uruchom ponownie Home Assistant.

[Instrukcja dodawania repozytoriów niestandardowych w HACS](https://www.hacs.xyz/docs/faq/custom_repositories/).

### Ręcznie

1. Skopiuj folder `custom_components/skzp_control` do katalogu
   `/config/custom_components/` w Home Assistant.
2. Uruchom ponownie Home Assistant.

## Konfiguracja

1. Otwórz **Ustawienia → Urządzenia i usługi → Dodaj integrację**.
2. Wyszukaj **SKZP Control**.
3. Podaj adres IP i port TCP modułu LAN/Wi-Fi sterownika.
4. Po wykryciu sterownika wybierz sposób konfiguracji:
   - **Szybka** — zalecany wybór encji i domyślne ustawienia integracji.
   - **Zaawansowana** — własny wybór encji i ustawień integracji.

Wybór encji i ustawienia można później zmienić w opcjach integracji.

## Zgłaszanie błędów

Problemy i wyniki testów innych modeli zgłaszaj w
[Issues](https://github.com/adoibels/ha-skzp-control/issues).
Podaj model sterownika, wersję jego oprogramowania, wersję Home Assistant
i integracji oraz opis problemu i odpowiedni fragment logów.
Przed publikacją usuń dane poufne, w szczególności identyfikator urządzenia i PIN.
