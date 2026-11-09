EDA – oprava PDF faktur a zálohových výzev (fakturační logika 0.7.9)
Zdroj: aktuálně dodaný eda(5).zip, 6. listopadu 2026.

OBSAH BALÍČKU
eda/services/eda_invoice_pdf.py – JEDINÝ nahrazovaný soubor

NASAZENÍ
1. Zálohuj eda/services/eda_invoice_pdf.py i databázi a adresář instance/invoices.
2. Při vypnuté automatické fakturaci rozbal ZIP do kořene instalace Edy, zachovej adresářovou strukturu.
3. Restartuj Passenger (běžným způsobem pro existující aplikaci).
4. Testuj na nové objednávce bez kreditu, s běžným kuponem, se zálohou a s ověřeným Fresh Affiliate kreditem.
5. Dříve vystavené PDF zůstanou nezměněné; oprava starých dokladů musí být provedena evidovaným postupem.

CO SE ZMĚNILO
- Zboží se tiskne ze skladovaných WooCommerce subtotalů před slevou (množství a částka);
  doprava a poplatky mají oddělené sekce.
- Běžná sleva je odlišena od ověřeného Fresh Affiliate kreditu. Kredit už není
  kladnou položkou zboží; jako platba snižuje zbývající úhradu.
- Souhrn ukazuje cenu před slevami, běžnou slevu, cenu po slevách,
  provizní kredit, částku k úhradě dle WooCommerce, zálohu a bankovní úhradu.
- Vyrovnávací řádek se nepřidává jako fiktivní zboží. Nevysvětlený rozdíl
  se zobrazuje samostatně se svým znaménkem a je nutno prověřit zdrojové hodnoty.
- QR SPAYD je na záloze a na případné neuhrazené konečné faktuře s použitelným IBAN.

DŮLEŽITÉ LIMITACE
- Není to účetní schválení povahy provizního kreditu; vyžaduje stanovisko účetní.
- Stávající účetní vydané záznamy a ISDOCX nejsou automaticky přepsané.
- Rozdíl zboží vs WooCommerce > 0,02 Kč je kontrolní signál: zdrojová metadata
  mohou obsahovat změny cen po úhradě; takové případy musí být před vystavením ověřeny.
- Cena na faktuře z WooCommerce je převzata, nikoli nezávisle přepočítána.
- Nahrané logo se bere ze stávajících údajů issuer['logo_path']; nevkládá se do ZIP.
