# Hue Insist

Zorgt dat een lichtcommando ook echt uitgevoerd wordt.

## Het probleem

Een Hue-groep of scene gaat als Zigbee **groupcast** de lucht in. Groupcast wordt
niet per lamp bevestigd en dus ook niet herhaald: een lamp met matig bereik mist
het bericht definitief. Individuele lampcommando's gaan als unicast, met
bevestiging en met herhaling door de Zigbee-stack zelf.

Home Assistant merkt daar niets van, want een groep telt al als "aan" zodra één
van de leden brandt. Het gevolg is een kamer die half aangaat, zonder foutmelding
en zonder dat er iets is dat het corrigeert.

Gemeten in een huishouden met 34 Hue-lampen: één ledstrip stond meerdere keren
per avond tientallen minuten donker terwijl zijn groep gewoon op `on` stond. In
één geval 55 minuten.

## Wat deze integratie doet

1. **Vangt** elk lichtverzoek dat via Home Assistant loopt — automations,
   dashboards, HomeKit, Siri, spraakassistenten.
2. **Vertaalt** het naar een concrete gewenste eindstand per lamp. Voor scenes
   komt die rechtstreeks uit de definitie op de Hue-bridge, dus inclusief
   helderheid en kleur, niet alleen aan of uit.
3. **Controleert** na een instelbare pauze wat er werkelijk gebeurd is.
4. **Corrigeert** elke afwijkende lamp *afzonderlijk*. Dat is de kern: een los
   commando wordt wél bevestigd.
5. **Meldt** wat na alle pogingen niet gelukt is, en houdt bij hoe vaak er
   ingegrepen moest worden.

Werkt zonder configuratie. Alle instellingen hebben een verdedigbare
standaardwaarde.

## Installatie

### Via HACS

Voeg deze repository toe als custom repository (categorie: Integration),
installeer Hue Insist, herstart Home Assistant en voeg de integratie toe via
**Instellingen → Apparaten en diensten → Integratie toevoegen**.

### Handmatig

Kopieer `custom_components/hue_insist` naar de `custom_components`-map van je
Home Assistant-configuratie en herstart.

## Instellingen

| Instelling | Standaard | Toelichting |
|---|---|---|
| Aantal pogingen | 3 | Hoe vaak een afwijkende lamp opnieuw geprobeerd wordt |
| Wachttijd | 2 s | Pauze voor de controle, en tussen pogingen |
| Losse lampen bewaken | aan | Verzoeken aan een enkele lamp |
| Groepen en kamers bewaken | aan | Verzoeken aan een Hue room of zone |
| Scenes bewaken | aan | Verzoeken aan een scene |
| Ook helderheid controleren | aan | Niet alleen aan/uit, maar ook de dimstand |
| Ook kleur controleren | aan | Kleurtemperatuur en xy-kleur |
| Lampen overslaan | leeg | Lampen die je met rust wilt laten |
| Tolerantie helderheid | 8 | Op een schaal van 0-255, ruim 3% |
| Tolerantie kleurtemperatuur | 15 mired | Kleinere afwijkingen zijn niet zichtbaar |

## Entiteiten

| Entiteit | Betekenis |
|---|---|
| `sensor.hue_insist_correcties` | Hoe vaak een lamp bijgestuurd moest worden |
| `sensor.hue_insist_mislukt` | Hoe vaak dat na alle pogingen niet lukte |
| `sensor.hue_insist_laatste_mislukking` | Welke lamp het laatst niet reageerde |

Die eerste sensor is meer dan een teller: hij maakt zichtbaar welke lamp
structureel slecht bereik heeft. Dat is informatie die je anders niet hebt.

## Events

| Event | Wanneer |
|---|---|
| `hue_insist_corrected` | Een lamp is bijgestuurd |
| `hue_insist_failed` | Een lamp reageerde na alle pogingen niet |

Beide dragen `entities` en `source` mee, zodat je er een melding aan kunt hangen.

## Hoe het aan de Hue-bridge komt

De integratie hergebruikt de gegevens van de bestaande Hue-integratie in Home
Assistant. Er is geen tweede koppeling nodig en je hoeft niet opnieuw op de knop
te drukken.

De vertaling tussen bridge en Home Assistant is exact: de Hue-integratie gebruikt
de resource-id van de bridge rechtstreeks als `unique_id` van de entiteit, dus een
opzoeking in het entiteitenregister is genoeg. Groepen lopen via
`room.children` → device → light-service, want een room verwijst naar apparaten
en niet naar lampen.

Zonder Hue-bridge werkt de integratie ook: dan valt de groepsuitklapping terug op
het `entity_id`-attribuut dat Home Assistant zelf op groepsentiteiten zet, en
worden scenes niet uitgeklapt.

## Wat er buiten valt

**Bediening rechtstreeks in de Hue-app.** Die loopt niet langs Home Assistant en
is dus onzichtbaar. Alles wat via Home Assistant gaat — inclusief HomeKit en
Siri, mits die via de Home Assistant-bridge lopen — wordt wel gezien.

**Onbereikbare lampen.** Een lamp die `unavailable` is wordt overgeslagen en telt
niet als fout. Een lamp achter een deurschakelaar zou anders elke ronde opnieuw
geprobeerd worden en altijd mislukken.

## Licentie

MIT
