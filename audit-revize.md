# Eda – revize aplikace a konsolidace UI (5. 11. 2026)

## Zdroj a rozsah
Vychází z uživatelem zaslaného `eda(4).zip` a čtyř screenshotů. ZIP obsahoval 156 souborů (cca 9,9 MB po rozbalení), včetně runtime logu 8 MB, archivovaných faktur, souborů příloh, Poppins fontu, starých `.pyc` pro Python 3.11 a 3.13 a cca 25 souborů historických upgrade/readme. Provozní aplikace má cca 2 282 řádků v `eda/routes.py`, dalších 651 v `eda/invoicing.py`, 591 v `eda/api.py` a 517 v `eda/models.py`. Stylopis `eda/static/css/app.css` má asi 47 KB a obsahuje historicky navrstvené části 0.3 až 0.7.4. Úložiště `instance/` je provozní stav, **nesmí se smazat při nasazení**.

## Hlavní nálezy
1. **Fragmentovaná navigace**: oddělené Firmy, Integrace, Přístupy k projektům, Superadmin a původní Admin konzole. Doporučeno jediné „Nastavení firem“ s vnitřní navigací a jediná „Správa systému“ pro superadmina. V této kandidátní verzi je už navigace sdružena pomocí rozbalovacích skupin; stará admin konzole je ponechána kvůli zpětné kompatibilitě.
2. **CSS**: velký řetězec komprimovaných deklarací a historických dodatků ztěžuje správu typografie, responzivity a komponent. Kandidát zavádí centrální soubor `system.css` pro společné typografické proměnné a nové komponenty; skutečné rozdělení `app.css` do komponentních souborů zůstává druhou etapou, po vizuálních regresních testech.
3. **Chat**: odpovědi jsou v `chat.html` escapované textové řetězce, nejsou tedy bezpečně renderované tabulky/odkazy/Markdown. Kandidát doplňuje indikátor zpracování požadavku při odeslání a sjednocuje tón systémového promptu Edy. Žádné nekontrolované `|safe` / `innerHTML` se nepřidává. Pro bohaté odpovědi je potřeba bezpečný renderer se sanitizací a whitelistem interních URL.
4. **Upozornění**: již existuje zvonek v `base.html` a podklady pro chyby e-shopových událostí, úkoly a propojení projektů. Chybí jednotný, persistentní a potvrzovatelný model „akce vyžaduje schválení / chyba vystavení / nedoručený doklad“ a pořadí priorit. Aktuální počítadlo není dostatečný provozní inbox.
5. **Finanční přehled**: agreguje doklady a 100 posledních `Transaction` zvlášť. Je správné rozlišovat vystavenou fakturu, označení úhrady a skutečný bankovní pohyb. Kandidát přidává přehled měsíčních příjmů a nákladů z těchto 100 evidovaných pohybů; **není to zisk ani přehled úplných tržeb**. Skutečná profitabilita e-shopu vyžaduje náklady na SKU, dopravu, platební poplatky, refundace a spolehlivé spárování.
6. **Příchozí dokumenty**: `InboxDocument` má stav `booked`, ale v přiloženém kódu nebyla funkce smazání nezaúčtovaného podkladu. Kandidát přidává endpoint pro smazání ne-`booked` dokumentu výhradně správci firmy/superadminovi, s auditním záznamem. Vydané faktury jsou jiný datový model a endpoint je nemaže. Ostré nasazení podmínit testy referencí a externích integrací.
7. **Bezpečnost přístupů**: projektové role existují, ale značná část starších routes provádí `org = _org_or_redirect()` a dotazuje dokumenty pouze podle `organization_id`. Samotné skrytí odkazu v menu **negarantuje** izolaci projektů u člena s `viewer` oprávněním. Před přidáním externích účtů provést audit každého GET/POST endpointu a izolaci na query vrstvě. Tuto část nelze prohlásit za dokončenou pouhým UI refaktorem.
8. **Události vs. cron**: `shop_events.py` je event-driven, ale vedle něj existuje `invoice_cron.py`, původní dávková alternativa. Ponechat poller pouze jako explicitní havarijní reconciliation, ve výchozím stavu vypnutý. Diagnostika musí rozlišovat úspěšně přijatou událost, zpracování, chybu a čekající schválení.
9. **Doklady/kredit**: předchozí faktura AR-2026-0004 měla účetně sporné zařazení provizního kreditu jako fakturační položky. Žádný UI cleanup tento problém neopravuje. Automatizaci sporných dokladů neaktivovat, dokud účetní nepotvrdí zacházení s kreditem. Současná evidence faktur je nutné zachovat.
10. **Administrace**: `admin_center.html` zobrazuje mnoho tabulek a formulářů na jedné stránce. Kandidát přidává kotvy rychlé navigace. Převod do samostatných podstránek (účty, firmy, role, provoz, úlohy, dokumenty) je doporučen jako navazující strukturální zásah.

## Co je skutečně implementováno v kandidátním zdrojovém balíčku
- Čistý **source-only** adresář Edy bez `instance/`, `stderr.log`, `__pycache__`, starých release poznámek a databázových dumpů.
- Konsolidované rozbalovací menu „Nastavení firem“ a „Správa systému“.
- Společný typografický a komponentní základ `eda/static/css/system.css`.
- Navigační kotvy pro superadmin centrum.
- Indikátor práce v chatu během odesílání formuláře (nejde o živé streamování úlohy) a konzistentnější tón asistenta.
- Graf měsíčních *evidovaných peněžních pohybů* z posledních 100 položek; bez přepočítávání účetního zisku.
- Sbalený formulář pro přidání peněžního pohybu.
- Přístupem omezené smazání ne-`booked` nahraného dokladu s auditní stopou.

## Co zatím úmyslně není implementováno
- Kompletní rozdělení 2 282řádkových routes do samostatných blueprints, migrace CSS, nové bezpečné rich-chat tabulky a interní akční odkazy, persistentní notifikační centrum, plná per-project autorizace všech endpointů, profitability po SKU/projektech, přepis admin centra do samostatných podstránek, změny fakturační logiky a automatizace.

## Důležité pokyny pro nasazení
Toto je **kandidát pro staging a regresní testy**, ne ověřená produkční aktualizace. Nahradit lze pouze zdrojové soubory, nikoli celý domovský adresář. Zachovat beze změn současné `.env`, databázi, `instance/uploads`, `instance/invoices`, `instance/chat`, `instance/branding` a lokální fonty. Před přehráním vytvořit zálohu. Zkontrolovat v stagingu `/chat`, `/admin/centrum`, `/finance`, `/documents/<id>`, `/shops`, `/shops/<id>/invoice-setup`, `/api/v1/manifest`, Woo události a oprávnění testovacího pozorovatele. Předání do produkce vyžaduje vizuální kontrolu na mobilu a desktopu.

## Směr další refaktorizace
(1) Zabezpečení projektových přístupů a přechod na query-scoped services. (2) Jednotné komponenty a rozdělené CSS, kompletní navigace. (3) Persistentní centrum upozornění a čekajících schválení. (4) Chat s bezpečným strukturovaným rendererem. (5) Účetní výkonnost podle skutečné evidence nákladů a párování. (6) Teprve potom zapnutí dalších automatizací a účetní výstupy.
