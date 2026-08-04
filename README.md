# Home-Assistant-Energiemanagement — Funktionsbeschreibung und Analyse

Stand: August 2026 · Basis: 22 Package-Dateien, `configuration.yaml`, `automations.yaml`

---

## 0. Überblick

Das System regelt vier Energieflüsse gegeneinander: PV-Erzeugung, Hausverbrauch, drei Batteriespeicher und zwei Elektrofahrzeuge. Es gibt keinen zentralen Regler — stattdessen eine Kette aus Messgrößen, abgeleiteten Zielwerten und Automationen, die diese Zielwerte an die Hardware schreiben.

**Der Kern-Datenfluss:**

```
Fronius Modbus ──► Leistung vom Netz  ──┐
Balkon/Garage-WR ─► PV Production Power ├──► House Consumption Power brutto
Marstek Modbus ──► Marstek AC Power ────┤         │
ANT-BMS ─────────► UPS AC Power ────────┤         ▼
WARP3 ───────────► EV Charging Power ───┘   Charge Power (Regelgröße)
                                                  │
PV-Prognose (4 Provider) ──► PV Forecast Weighted │
Verbrauchsprognose ────────► Expected/Estimated ──┤
                                                  ▼
                                             Target SOC
                                                  │
                          ┌───────────────────────┼────────────────────┐
                          ▼                       ▼                    ▼
                    charge_map /            ups_coarse_control    EV Target
                    discharge_map           (Relais)              Charging Current
                          │                                            │
                          ▼                                            ▼
                    Venus Modbus                                  WARP3 Wallbox
```

**Zwei getrennte Regelkreise:**

| | Venus-Speicher | USV |
|---|---|---|
| Stellgröße | stufenlose Leistung (Modbus) | Relais an/aus |
| Regeltakt | jede Änderung von `charge_power` | max. alle 12 s (Entprellung) |
| Regelgröße | `sensor.charge_power` | `true_balance` |
| Ziel | `target_soc` treffen | Netzbezug/Überschuss ausgleichen |

Die USV ist bewusst nur grob geregelt — sie hat keine stufenlose Leistungsvorgabe, sondern Ladegerät- und Wechselrichter-Relais. Deshalb die Hysteresen (±50 W, −820 W, −1200 W) und die 12-Sekunden-Sperre.

---

## 1. Messung und Basisgrößen

### 1.1 Netz- und PV-Leistung (`fronius.yaml`)

Modbus-TCP auf `192.168.0.49`, SunSpec-Register:

| Register | Slave | Größe | Takt |
|---|---|---|---|
| 40097 | 200 | SmartMeter AC-Leistung | 5 s |
| 40091 | 1 | Inverter AC-Leistung | 10 s |
| 40101 | 1 | Inverter Energie | 20 s |

Die Rohwerte gehen durch je einen EMA-Filter (`alpha 0.68` bzw. `0.78`, ≈10 s bzw. 15 s bis 90 %) zu `sensor.leistung_vom_netz` und `sensor.leistung_von_pv_ac`. Der Plausibilitätsguard (`|Wert| < 20000`) sitzt im `availability`-Template, nicht im State — das hält das Log sauber, weil ein nicht-numerischer State-Rückgabewert sonst als ERROR protokolliert würde.

**Vorzeichen:** `leistung_vom_netz` > 0 = Bezug, < 0 = Einspeisung.

### 1.2 PV-Gesamterzeugung (`pv_production.yaml`)

Vier Wechselrichter: Dach (Fronius) plus drei Balkon-/Garage-Geräte. Jeder Energiezähler läuft durch einen Monotonie-Filter (`[val, last] | max`), damit ein Nullsprung bei Kommunikationsfehlern die Utility-Meter nicht zurücksetzt. Drei Utility Meter (`daily`) liefern Tagesertrag gesamt / Balkon / Dach, jeweils nochmal mit Range-Filter gegen Ausreißer (100 / 35 / 70 kWh Obergrenze).

### 1.3 Hausverbrauch (`consumption.yaml`)

```
House Consumption Power brutto
  = Netz + Marstek AC + PV + EV-Ladeleistung + USV AC
```

Das ist der Verbrauch **ohne** Fahrzeuge und ohne Speicher — die reine Hauslast. Alle fünf Summanden müssen verfügbar sein (`availability`), sonst wird gar nicht gerechnet.

Daraus per Riemann-Integration `House Consumption Energy` → Utility Meter (täglich, Reset bei Sonnenaufgang +30 min) → `House Consumption Energy Today` mit Ausreißer-Guard (0–60 kWh).

### 1.4 Regelgröße `Charge Power`

```
ziel = Netzleistung + Marstek-AC-Leistung
```

Also: was das Haus ohne Speicherbeteiligung aus dem Netz zöge. Negativ = Überschuss (laden), positiv = Bedarf (entladen). Zwei Dämpfungen verhindern Dauerschreiben:

- Beträge unter 40 W → glatte 0
- Änderungen unter 20 W werden nur übernommen, wenn seit der letzten Änderung > 60 s vergangen sind

`availability` verlangt zusätzlich `binary_sensor.marstek_verfuegbar = on`. Bei einem Ausfall der Marstek-Integration wird der Sensor unavailable, `charge_map` rechnet `total = 0` und die Speicher gehen in Standby, statt auf falschen Daten zu regeln.

---

## 2. Batteriespeicher

### 2.1 Hardware

| Speicher | Kapazität | Steuerung | Max. Leistung |
|---|---|---|---|
| Marstek Venus 1 | 5,12 kWh | Modbus (stufenlos) | 2500 W |
| Marstek Venus 2 | 5,12 kWh | Modbus (stufenlos) | 2500 W |
| USV (Eigenbau, ANT-BMS) | 2,2 kWh | Relais (Charger/Inverter) | binär |

Rechnerisch nutzbare Gesamtkapazität (`calculated_combined_capacity_kwh` in `globals.yaml`):
`(2 × 5120 × 0,8 + 2200 × 0,6) / 1000 = 9,51 kWh`

Die Faktoren 0,8 und 0,6 sind Nutzungsgrade — die Venus werden nicht bis 0 % gefahren, die USV noch weniger.

### 2.2 SoC-Ermittlung: warum ein eigener SoC

Der Hardware-SoC der Venus springt und ist grob. Deshalb rechnet das System für jeden Speicher einen **Custom SoC** aus der integrierten Batterieleistung:

1. Riemann-Integration über `marstek_venus_X_batterieleistung` → kWh
2. `Venus X Energy Wh` = `−Riemann × 1000 + 50000` (Offset, damit der Wert nicht negativ wird; geklemmt 0…100000)
3. `Venus X Custom SOC` = `100 − (aktuelle_Wh − reset_point) / Kapazität × 100`

Der **`reset_point`** ist der Anker. Er wird als Attribut des Sensors selbst geführt und nach klaren Regeln neu gesetzt:

| Bedingung | Aktion |
|---|---|
| HW-SoC ≥ 100 % | `reset_point = aktuelle Energie` (Vollpunkt) |
| HW-SoC ≤ `stop_soc` (15 %) | Anker aus HW-SoC rückrechnen |
| Energie < letzter Anker | Anker nachziehen (Zähler ist zurückgesprungen) |
| sonst | Anker halten |

Bei der USV kommt eine **Drift-Erkennung** dazu: weicht der gerechnete SoC um mehr als 25 Prozentpunkte vom BMS-SoC ab, wird hart auf den BMS-Wert neu verankert. Außerdem setzt eine Zellspannung über `lfp_max_voltage` (3,565 V) den Vollpunkt — das ist der verlässlichere Weg, weil `ups_custom_soc` bei driftendem Anker die 100 % nie erreichen kann.

`Marstek usable Energy` fasst beides zusammen: nutzbare kWh oberhalb der aktuellen Mindest-SoC-Grenze. Bewusst **ohne** `availability`-Template — ein eingefrorener Wert ist beim Modbus-Timeout die harmlosere Lüge als eine gemeldete 0, weil `target_soc` sonst auf 130 springt und die Speicher nachts aus dem Netz laden.

### 2.3 Leistungsverteilung auf die beiden Venus

Drei Templates in `marstek_batteries.yaml` erzeugen zusammen `sensor.target_power_distribution_fast`:

**`limits_map` — dynamische Ladeleistungsgrenze**

Nur aktiv zwischen 11:15 Uhr und Sonnenuntergang −3 h und nur wenn `target_soc < 100`. Statt mit voller Leistung zu laden und dann abzuregeln, wird die Ladung über das verbleibende Zeitfenster gestreckt:

```
fak    = Kapazität / 100 / Reststunden × 2,5
Limit  = (target_soc − ist_soc) × fak,  geklemmt auf 500…2500 W
```

Das ist Lebensdauer-Optimierung: niedrigere C-Rate und weniger Zeit bei hohem SoC. Außerhalb des Fensters gelten 2500 W.

**`charge_map` — Aufteilung der Ladeleistung**

- Bei `manual_charge_power > 0`: beide Venus fest auf diesen Wert, aber nur wenn Custom-SoC < 99,5 % **und** der Steuermodus-Switch an ist. Ohne diese Prüfung entstand ein Ping-Pong mit der Vollpunkt-Automation.
- Sonst: Gewichtung nach Abstand zum Ziel (`w = target_soc − ist_soc`), Aufteilung proportional. Danach drei Korrekturstufen:
  - `total > 1000 W` → jeder Speicher bekommt mindestens 500 W
  - `total > 400 W` → mindestens 200 W
  - beide unter 200 W → **abwechselndes Laden**: der weiter entfernte bekommt alles; bei Gleichstand entscheidet `(Wochentag + Stunde) % 2`

Der letzte Punkt ist wichtig: unter ~200 W arbeiten die Venus schlecht, deshalb lieber einer voll als beide im schlechten Wirkungsgrad.

**`discharge_map` — Aufteilung der Entladeleistung**

Analog, aber mit zusätzlichem Zellspannungs-Guard: ein Speicher wird nur einbezogen, wenn seine minimale Zellspannung ≥ `lfp_min_voltage` (3,0 V) ist. Weiche Mindestlast 250 W, darunter ebenfalls abwechselnd.

### 2.4 Übertragung an die Hardware (`sende_batterie_targets_v3`)

Die heikelste Stelle, weil Modbus-Schreibvorgänge kollidieren können. Ablauf:

1. Richtung bestimmen (`charge` / `discharge` / `standby`, Schwelle 20 W)
2. **Modus-Wechsel entschärfen:** verlässt ein Speicher `charge` oder `discharge`, wird erst die alte Leistung auf 0 geschrieben, 120 ms gewartet, dann der Modus umgestellt
3. Beide Speicher parallel, aber mit 10 ms Versatz — verhindert gleichzeitige Modbus-Zugriffe
4. Jeder Schreibvorgang nur, wenn sich der Wert tatsächlich ändert (`!=`-Vergleich), mit `continue_on_error: true`
5. Abschließend 400 ms Ruhe, `mode: queued, max: 3`

### 2.5 USV-Grobsteuerung (`ups_coarse_control`)

Regelgröße ist `true_balance = Netz + Marstek + USV`. Die eigene USV-Leistung wird zurückaddiert, damit der Wechselrichter sich nicht selbst wegregelt (Schwingungsbremse).

Sieben Zweige, streng in dieser Reihenfolge:

| # | Bedingung | Aktion |
|---|---|---|
| 0 | beide Zellspannungen = 0 (BMS weg) | **alles aus** + Benachrichtigung |
| 0.5 | höchste Zelle > 3,565 V und Charger an | Charger aus (harte Grenze) |
| 1 | Wartungsmodus aktiv | Inverter aus, Charger 2 an |
| 2 | SoC ≤ 12 % oder Zelle < 3,0 V | **Notladung**: Inverter aus, Charger 2 an |
| 3 | Bedarf > 180 W, SoC > 25 %, Zellen ok | Entladen starten |
| 4 | Bedarf < 50 W (min. 30 s Laufzeit) oder SoC ≤ 20 % | Entladen stoppen |
| 5 | Netzbezug > 50 W, voll, Zellgrenze oder Trickle | Laden stoppen |
| 6 | Überschuss > 820 W | Laden starten (> 1200 W: zweiter Charger dazu) |

Zusätzlich eine harte Sicherheitsabschaltung: laufen Wechselrichter und Ladegerät gleichzeitig länger als 0,5 s, wird **alles** abgeschaltet.

**Marstek-Ausfall wirkt gestaffelt:** Zweige 3 und 6 (Einschalten) verlangen `marstek_ok`. Zweig 5 (Ausschalten) fällt ersatzweise auf den reinen Netzbezug zurück. Zweig 4 lässt den Inverter weiterlaufen und schaltet nur noch über SoC und Zellspannung ab. Die Sicherheitszweige 0, 0.5 und 2 hängen gar nicht an Marstek.

---

## 3. Ladestrategie: wie das SoC-Ziel entsteht

### 3.1 Stufe 1 — `Dynamic SOC` (Tagesprofil)

Rein zeitgesteuertes Grundprofil:

| Zeitraum | Ziel-SoC |
|---|---|
| Nacht | 15 % |
| Sonnenaufgang – 11:15 | 60 % |
| 11:15 – Sonnenuntergang −3 h | 80 % |
| Sonnenuntergang −3 h – Sonnenuntergang | 90 % |

Die Idee: morgens Platz für den PV-Ertrag freihalten, abends voll sein. Die Grenzen kommen aus `sensor.solar_soc_logic` und werden gegen `stop_soc` abgesichert.

### 3.2 Stufe 2 — `Target SOC` (Mehrtagesbilanz)

Der eigentliche Sollwert. Prioritätsreihenfolge:

| Wert | Bedeutung | Auslöser |
|---|---|---|
| 101 | Manueller Override | `input_boolean.override_soc` |
| 120 | Wartungszyklus | `battery_maintenance_required` |
| 110 | Manuelle Ladeleistung gesetzt | `manual_charge_power > 0` |
| 130 | **Heutige** Bilanz negativ | Rollierende Simulation |
| 131 | **Morgige** Bilanz negativ | " |
| 133 | **Übermorgige** Bilanz negativ | " |
| max (100,5) | Überschuss reicht nicht fürs dynamische Ziel | Deckel geöffnet |
| dynamisch | Normalbetrieb | `Dynamic SOC` |

Die rollierende Simulation:

```
balance_heute     = min(nutzbar + PV_rest − Verbrauch_rest, max_storage)
balance_morgen    = min(balance_heute + PV_morgen − Verbrauch_morgen, max_storage)
balance_übermorgen = min(balance_morgen + PV_übermorgen − Verbrauch_übermorgen, max_storage)
```

Der dringlichste Fehlbetrag gewinnt. Ist keine Lücke zu erwarten, greift eine Zusatzregel: reicht der heutige PV-Überschuss rechnerisch nicht, um die Batterie aufs dynamische Ziel zu heben, wird der Deckel auf Maximum geöffnet — jede verfügbare Wattstunde einsammeln statt einspeisen.

Die Reihenfolge Wartung **vor** Manual ist bewusst: während der Wartungsladung steht `manual_charge_power` auf 1000, wodurch früher immer 110 statt 120 angezeigt wurde.

### 3.3 Verbrauchsprognose

**Statisch** (`Expected daily consumption`): drei gleitende Mittelwerte, getrennt nach Tagestyp aus `holidays.jinja`:

| Tagestyp | Gewichtung beim Update |
|---|---|
| Werktag | `(4 × alt + neu) / 5` |
| Samstag/Vortag Feiertag | `(2 × alt + neu) / 3` |
| Sonntag/Feiertag | `(2 × alt + neu) / 3` |

Update bei Sonnenaufgang +30 min, Tagesklassifikation um 12 h zurückversetzt, damit ein früher Sonnenaufgang noch dem Vortag zugeordnet wird.

**Live** (`Estimated remaining consumption`): aktuelle Durchschnittsleistung × Zeit bis zum nächsten Sonnenaufgang.

In `Target SOC` werden beide **50:50 gemischt**. `Battery Floor SOC for EV` verwendet dieselbe Mischung.

---

## 4. Lebensverlängerung der Batterien

Das ist ein durchgehendes Thema, verteilt über mehrere Dateien.

### 4.1 Chemie-Parameter (`globals.yaml`)

```
lfp_min_voltage:  3,000 V
lfp_max_voltage:  3,565 V
lfp_hysteresis:   0,040 V
lowest_soc:      12 %   (darunter Notladung)
stop_soc:        15 %
```

3,565 V ist bewusst unter dem üblichen Ladeschluss von 3,65 V — die letzten Prozent kosten überproportional Lebensdauer.

### 4.2 Kein Dauerbetrieb bei hohem SoC

Das Tagesprofil hält die Speicher morgens bei 60 % und geht erst nachmittags auf 90 %. Nachts stehen sie bei 15 %. Die 100 % werden nur erreicht, wenn die Bilanz es verlangt (130/131/133) oder der Wartungszyklus läuft.

Die `limits_map` streckt die Ladung über das Zeitfenster bis Sonnenuntergang −3 h, statt früh mit 2500 W vollzuladen und dann stundenlang oben zu stehen.

### 4.3 Regelmäßige Vollzyklen (Zellbalancing)

LFP-Zellen balancieren nur nahe der Ladeschlussspannung. Deshalb ein erzwungener Vollzyklus:

| Parameter | Wert | Wirkung |
|---|---|---|
| `battery_care_days` | 8 | ab dann `battery_maintenance_required = on` → `target_soc = 120` |
| `override_soc_days` | 5 | ab dann `override_soc = on` → `target_soc = 101` |

**Venus-Zyklus** (`battery_maintenance_night_charge`): startet nach Sonnenuntergang, setzt `manual_charge_power = 1000 W`. Trigger sowohl bei Sonnenuntergang +5 min als auch alle 30 Minuten — der reine Flankentrigger kam nie wieder, weil der Binary Sensor „on" bleibt. Bedingung `below_horizon` statt „nach Sonnenuntergang", damit ein abgebrochener Zyklus auch nach Mitternacht noch anlaufen kann.

**USV-Zyklus** (`battery_maintenance_ups_charge`): eigener Ablauf über `input_boolean.ups_maintenance`, weil die USV 3–5 h braucht und die Venus nur ~30 min. Früher stoppte „Stop When Full" beide gemeinsam, sobald die Venus voll waren — die USV wurde nie fertig, `ups_last_full` nie gesetzt, und `battery_maintenance_required` blieb dauerhaft on.

**Drei harte Abbrüche** verhindern Dauerladung bei defekter Zelle oder hängendem Sensor:

- `battery_maintenance_hard_stop` — Venus bei Sonnenaufgang
- `battery_maintenance_ups_stop` — USV bei Sonnenaufgang
- Zweig 0.5 in `ups_coarse_control` — Zellüberspannung, unbedingt

### 4.4 Vollpunkt-Erkennung

`marstek_set_last_full_timestamp` setzt bei HW-SoC > 99,5 % (10 s stabil) den Zeitstempel **und** die Ladeleistung auf 0. Zusätzlich alle 10 Minuten als Nachzügler, weil der `numeric_state`-Trigger nur beim Überschreiten feuert.

Bei der USV wird der Zeitstempel zusätzlich beim sauberen Zyklusende gesetzt, weil die reguläre Bedingung (beide SoC-Sensoren > 99,9 % für 5 min) nach einem Stopp bei 99,5 % oft nicht mehr erreicht wird.

### 4.5 Entladeschutz

- `discharge_map` schließt einen Speicher aus, sobald seine minimale Zellspannung unter 3,0 V fällt
- `ups_coarse_control` Zweig 4 stoppt bei SoC ≤ 20 % oder Zelle < `min_v`
- Zweig 2 lädt bei SoC ≤ 12 % oder Zelle < `min_v` unbedingt nach, auch aus dem Netz
- `Battery Floor SOC for EV` verhindert, dass das Fahrzeug den Hausspeicher unter den für die Nacht benötigten Stand zieht

---

## 5. PV-Prognose

### 5.1 Vier Provider, ein gewichteter Wert

| Provider | Quelle | Horizont |
|---|---|---|
| Solcast | Custom Integration | heute, morgen |
| Forecast.Solar | REST, zwei URLs (Dach/Fassade) | heute, morgen |
| Open-Meteo | Custom Integration | heute, morgen, übermorgen |
| DWD | Custom Integration | heute, morgen, übermorgen |

Anlagendaten: **Dach** 20° Neigung, Azimut −45° (Südost), 7,5 kWp · **Fassade** 85° Neigung, Azimut +45° (Südwest), 2,4 kWp. Die vollständigen Forecast.Solar-URLs liegen in `secrets.yaml`, weil sie die Hauskoordinaten mit vier Nachkommastellen enthalten.

### 5.2 Horizontkorrektur

Zwei Jinja-Makros (`horizon_dach.jinja`, `horizon_fassade.jinja`) liefern pro Zeitstempel bzw. pro Datum+Stunde einen Faktor für die lokale Verschattung. Jeder stündliche Prognosewert wird damit multipliziert, bevor er summiert wird — die Fassade hat wegen 85° Neigung ein völlig anderes Horizontprofil als das Dach.

Für den Rest des Tages wird stundenscharf gerechnet: die laufende Stunde geht nur anteilig ein (`fraction_left`), bei Forecast.Solar sogar linear interpoliert zwischen den kumulierten Stützstellen.

### 5.3 Kalman-Gewichtung (`pv_forecast_automations.yaml`, 23:55)

Für jeden Provider werden drei Zustände geführt: **Score** (Güte), **P** (Unsicherheit), **R** (Innovationsvarianz).

```
rel_err  = |Morgenprognose − Ist| / Ist
obs      = 1 / (rel_err × 10 + 0,1)          → je genauer, desto höher
innovation = obs − score_alt
R_neu    = R_alt × 0,9 + innovation² × 0,1   adaptive Messrauschschätzung
K        = (P + Q) / (P + Q + max(R_neu − (P+Q), 0,05))
score_neu = score_alt + K × innovation
```

Ein Provider, der konstant daneben liegt, verliert Gewicht. Ein Provider, der plötzlich schlechter wird, verliert schnell (hohe Innovation → hohes R → aber auch hohes K in der nächsten Runde).

Die Gewichte in `pv_forecast_calc.yaml` entstehen als `score / (R + 0,1)`: hoher Score **und** niedrige Streuung zählen. Provider, die 0 liefern (z. B. Solcast-API abgelaufen), fallen automatisch aus der Summe.

### 5.4 Bias-Korrektur

Ein zweiter, langsamerer Kalman-Filter lernt einen globalen Multiplikator `pv_forecast_bias_korrekturfaktor` (0,8–1,3):

```
messung = Ist × bias_alt / Morgenprognose
bias_neu = bias_alt + K × (messung − bias_alt)
```

Er fängt systematische Fehler ab, die alle Provider gemeinsam haben: Modulverschmutzung, Alterung, Wechselrichter-Wirkungsgrad, Aufständerungshöhe, Albedo. Die früheren statischen Faktoren (`pv_height_correction_factor` 1,053, `pv_albedo_correction_factor` 1,0–1,1) sind deshalb aus der Rechnung genommen — zusammen mit dem Bias konnte die Gesamtkorrektur im Winter nie unter ~1,04 fallen, obwohl 1,0 richtig gewesen wäre. Sie existieren nur noch zur Diagnose.

### 5.5 Nowcast: Live-Korrektur aus Einstrahlung

`pv_forecast_nowcast.yaml` schärft die Tagesprognose mit der gemessenen Globalstrahlung der Wetterstation nach:

1. Einstrahlung über ~30 min glätten (EMA, `alpha 0,065`, 1-Min-Takt)
2. Anlagenspezifischen Umrechnungsfaktor lernen (`PV-Leistung / Einstrahlung`, sehr langsame EMA mit `alpha 0,01`, nur bei Einstrahlung > 100 W/m²)
3. „Gemessene" PV-Leistung = geglättete Einstrahlung × Faktor
4. Gegen die für die laufende Stunde modellierte Leistung (Open-Meteo) vergleichen
5. Faktor (geklemmt 0,3–2,5) nur in `PV Forecast Weighted` für **heute** multiplizieren

**Warum Einstrahlung und nicht die PV-Leistung selbst:** Schnee auf den Modulen, ein Wechselrichterfehler oder Verschattung drücken die Leistung, nicht aber die gemessene Einstrahlung. Ein reiner Leistungsvergleich würde die Wetterprognose fälschlich nach unten ziehen, obwohl das Problem an der Anlage liegt.

Nacht-Guards an drei Stellen: Zeitfenster Sonnenaufgang −30 min bis Sonnenuntergang +30 min, Schwellwert-Guard (modelliert und gemessen je > 20 W), und ein harter `below_horizon`-Guard, der den Faktor verlässlich auf 1,0 einfrieren lässt.

### 5.6 Tagesablauf der Prognose

| Zeit | Was passiert |
|---|---|
| 00:02 | Rollover-Lauf — ohne ihn bliebe `pv_forecast_weighted` auf 0 und `target_soc` sprünge auf 130 |
| Sonnenaufgang −45 min | Vorlauf, damit der Snapshot auf frischen Werten sitzt |
| Sonnenaufgang −30 min | Nowcast-Module starten |
| Sonnenaufgang | Forecast.Solar-Fetch mit bis zu 3 Retries, dann Morgen-Snapshot |
| tagsüber /5 min | Neuberechnung aller Prognosewerte |
| 11:30, 17:30 | zusätzlicher Forecast.Solar-Fetch |
| Sonnenuntergang +30 min | Nowcast friert auf 1,0 ein |
| 23:55 | Kalman-Update aller Scores und des Bias |

---

## 6. EV-Laden

### 6.1 Fahrzeuge und Wallbox

| | Zero SR | Honda e |
|---|---|---|
| Kapazität (netto) | 13,6 kWh | 28,5 kWh |
| Ladewirkungsgrad | 0,905 (gemessen 08/2026) | 0,88 (geschätzt) |
| Max. Ladeleistung | 3400 W | 4500 W |
| Phasen | 1 | 1 |

WARP3-Wallbox, aktuell auf 22 A abgesichert (`wallbox_max_amps`). Fahrzeugerkennung über zwei Binary Sensors: **Zero** wird positiv über den BLE-SoC erkannt (`delay_on 2 min`), **Honda e** negativ — Kabel gesteckt und nicht Zero (`delay_on 2:30`).

### 6.2 SoC-Tracking mit Ankerprinzip (`ev_soc.yaml`)

```
SoC = Anker-SoC + (Wallbox-Zähler − Anker-Zählerstand) × Wirkungsgrad / Kapazität × 100
```

Zustandslos gerechnet aus dem **absoluten** Wallbox-Zähler, nicht inkrementell. Der Anker liegt in `input_number`-Helpern und überlebt Neustart und YAML-Reload. Geschrieben wird er nur an zwei Stellen:

- **BLE-Kalibrierung**: neuer Wert von `sensor.zero_sr_batteriestand` bzw. `sensor.honda_e_batteriestand` → Anker-SoC und Anker-Zählerstand gleichzeitig neu setzen
- **Fahrzeugwechsel**: erreichten Stand des alten Fahrzeugs einfrieren (gerechnet aus `trigger.from_state`, kein Race), Anker-Zählerstand beider Fahrzeuge auf jetzt setzen

Während des Ladens läuft keine Automation — deshalb kann sich nichts aufschaukeln.

### 6.3 Überschussrechnung

Zwei Varianten, beide in den `variables` von `EV Target Charging Current`, damit alle Teilwerte aus derselben Momentaufnahme stammen:

```
EV FIRST:      Überschuss = Ladeleistung − Netz − Venus1 − Venus2
BATTERY FIRST: Überschuss = Ladeleistung − Netz − max(Venus1,0) − max(Venus2,0)
```

Bei EV First wird die Ladeleistung der Hausbatterie (negativ) freigegeben, bei Battery First bleibt sie bei der Batterie. Ampere = `Überschuss / Spannung + 0,2`, abgeschnitten, unter 6 A → 0.

**Marstek-Guard:** ohne gültige Speicherdaten wird der Überschuss hart auf 0 gesetzt. Vorher wurden fehlende Werte als 0 W gelesen — entladen beide Venus mit je 2500 W, erschien der Überschuss um 5 kW zu hoch und die Wallbox regelte auf ~21 A hoch, drei Minuten lang aus dem Netz.

### 6.4 Die sechs Lademodi

| Modus | Verhalten |
|---|---|
| `pv_ev_first` | Fahrzeug bekommt den vollen Überschuss inkl. der Batterie-Ladeleistung |
| `pv_battery_first` | Batterie zuerst, Fahrzeug bekommt den Rest |
| `pv_shared_50_50` | Dynamisch: Fahrzeug voll → 0; Hausbatterie voll → alles ins Auto; sonst mindestens die Hälfte |
| `min_6a_floor_battery` | Mindestens 6 A, aber nur solange SoC über `battery_floor_soc_for_ev` |
| `min_6a_floor_grid` | Mindestens 6 A, ohne Batterieschutz |
| `target_time` | Fertig bis Zielzeit (siehe unten) |

`Battery Floor SOC for EV` berechnet die Untergrenze aus dem für die Nacht erwarteten Restverbrauch:

```
Bedarf   = gemischter Restverbrauch × 1,10
netto    = max(0, Bedarf − PV-Restprognose)
Floor    = netto / Gesamtkapazität × 100,  geklemmt 15…100 %
```

### 6.5 Zielzeit-Modus

Drei Phasen ohne Helper, ohne Latch, ohne Hysterese:

| Phase | Leistung | Bedingung |
|---|---|---|
| `pv` | nur Überschuss | vor beiden Startzeitpunkten, SoC < 85 % |
| `paced` | Batterietempo (4500 W), oder mehr wenn die Sonne mehr hergibt | ab `start_paced_ts` |
| `full` | alles, Netz inbegriffen | ab `start_full_ts` |

Die Startzeitpunkte:

```
start_X_ts = Zielzeit − 30 min Puffer − (Restbedarf_kWh × 3.600.000 / Planleistung_X)
Planleistung = Sollleistung × 0,9   (target_time_power_derate)
```

Der Derate-Faktor ist der Trick: geplant wird pessimistischer als tatsächlich geladen wird. Dadurch schrumpft der Restbedarf schneller als die Uhr läuft, die Startbedingung rastet nach dem Auslösen von selbst ein, und die abflachende Ladekurve plus die Rundung auf ganze Ampere sind mit abgedeckt. Läuft alles nach Plan, wird `full` nie erreicht; fällt die Ladung zurück, greift die Stufe von selbst.

`sensor.ev_charge_start_time` macht das sichtbar (Startzeit, Deadline, Fallback-Zeitpunkt, Phase, Restbedarf, geplante Minuten).

### 6.6 Automationskette

| Automation | Aufgabe |
|---|---|
| `wallbox_reset_session_energy` | Zählerstand snapshotten beim Anstecken |
| `wallbox_enforce_off` | Fahrzeugmodus „off" durchsetzen — fängt auch die Firmware-Autostart der WARP3 ab |
| `wallbox_manual_charge` | Fester Strom, überschreibt jede Strategie |
| `wallbox_universal_start` | Start bei 15 s stabilem Überschuss oder Benutzeraktion |
| `wallbox_track_target` | Nachführen während der Ladung, 45 s Wartezeit pro Schritt |
| `wallbox_stop_on_idle` | Stopp bei < 6 A für 2 min |
| `wallbox_stop_on_limit_or_off` | Stopp bei Energieziel oder Fahrzeug „off" |
| `wallbox_calc_charge_delta` | Selbstkorrigierende Lademenge (Restbedarf + Session-Energie) |
| `wallbox_reset_vehicle` | Fahrzeugmodus auf „off" beim Abstecken |
| `wallbox_LED` | Statusanzeige: Zero rot, Honda weiß, frei grün |

Die 45 s in `wallbox_track_target` sind bewusst: sie lassen die schnelle Batterieregelung sich gegen eine feste EV-Last einschwingen, bevor das Fahrzeug den nächsten Schritt macht. Ohne das entsteht ein Grenzzyklus zwischen beiden Reglern.

Zwei Automationen haben einen **Minutentakt als Nachzügler** (`wallbox_universal_start`, `wallbox_manual_charge`): der `numeric_state`-Trigger löst nur beim Überschreiten aus. Nach einem Reload steht der Sollstrom aber meist schon oben, und es kommt nie wieder ein Übergang.

Im Zielzeit-Modus sind die energiebasierten Stopps **ausgenommen** — dort ist der Live-SoC die Wahrheit, nicht die Session-Energie. `ev_max_charge_energy` hängt über `ev_charge_by_percent` am SoC beim Anstecken und driftet nach einer BLE-Nachkalibrierung weg.

---

## 7. Ausfallsicherheit

`binary_sensor.marstek_verfuegbar` ist die zentrale Absicherung. Der Marstek-Coordinator pausiert nach einem Lesefehler 180 s; die Entitäten stehen dann je nach Fehlerbild auf `unavailable` **oder** behalten einfach ihren letzten Wert. Beide Fälle werden erkannt:

- `has_value` → deckt `unavailable`/`unknown` sofort ab
- `last_reported` gegen `max_age` 300 s → deckt eingefrorene Werte ab

`delay_on: 20 s` (nach einer Störung erst wieder vertrauen, wenn die Daten stabil sind), Ausschalten ohne Verzögerung. Seit August 2026 zusätzlich eine Benachrichtigung, wenn der Sensor 15 Minuten auf `off` steht.

Weitere Guards: Monotonie-Filter auf allen Energiezählern, Range-Filter auf allen Utility Metern, `availability`-Templates statt State-Guards (sauberes Log), `continue_on_error` auf allen Modbus-Schreibvorgängen, `float(none)`-Muster mit „letzten guten Wert halten".

---

## 8. Analyse

### 8.1 Was gut gelöst ist

**Die Guard-Kultur.** Fast jeder Sensor hat einen Fallback, und die Fallbacks sind in die *richtige* Richtung gewählt. `Marstek usable Energy` hält bewusst den letzten Wert, statt 0 zu melden, weil 0 die Bilanzstufe auf 130 kippen würde. Das ist Regelungstechnik, nicht nur Fehlerbehandlung.

**Die Kommentare erklären das Warum.** Nahezu jede nicht offensichtliche Konstante hat eine Begründung mit dem konkreten Fehlerbild, das sie behoben hat. Das ist der Grund, warum diese Config nach einem halben Jahr noch wartbar ist.

**Der Zielzeit-Modus.** Zwei Startzeitpunkte, die rechnerisch zusammenlaufen, plus ein Derate-Faktor, der die Selbsteinrastung garantiert — das ersetzt einen Zustandsautomaten mit Latches und Hysterese durch reine Arithmetik.

**Die Trennung Venus-/USV-Wartung.** Der ursprüngliche gemeinsame Zyklus war strukturell kaputt (30 min gegen 5 h), und die Lösung ist sauber getrennt statt mit Timeouts geflickt.

**Der Nowcast über Einstrahlung statt Leistung.** Die Begründung — Schnee und Wechselrichterfehler dürfen die Wetterprognose nicht verfälschen — zeigt, dass die Messgröße bewusst gewählt wurde und nicht nur, weil sie verfügbar war.

### 8.2 Befunde

**A — `estimated_remaining_consumption` läuft auf dem 5-Minuten-Mittel** · `consumption.yaml`, hoch

Der 1h-Filter ist auskommentiert, der Sensor liest `average_consumption_last_5min` und extrapoliert bis zum nächsten Sonnenaufgang. Der Wert geht mit 50 % Gewicht in `target_soc` und in `battery_floor_soc_for_ev`.

Rechenbeispiel: Herd nachmittags 5 Minuten an → 5-min-Mittel 3 kW → bis Sonnenaufgang 15 h → 45 kWh geschätzter Restverbrauch. Gemischt mit dem statischen Wert (~10 kWh) ergibt das ~27 kWh gegen 9,5 kWh Speicherkapazität → `balance_today < 0` → `target_soc = 130` → die Venus laden aus dem Netz auf 100 %, bis der Spike wieder herausgemittelt ist.

Mit dem 1h-Fenster war derselbe Herd auf 250 W gedämpft. Empfehlung: den 1h-Filter reaktivieren und `estimated_remaining_consumption` darauf laufen lassen; das 5-min-Mittel für alles behalten, was schnell reagieren soll.

**B — Der Morgen-Snapshot enthält die Nowcast-Korrektur** · `pv_forecast_automations.yaml`, mittel

Der Kopfkommentar in `pv_forecast_nowcast.yaml` argumentiert, der Snapshot liege vor Sonnenaufgang und sei unberührt. Tatsächlich triggert `pv_morning_fetch_and_snapshot` **bei** Sonnenaufgang, wartet dann 6 Minuten plus bis zu drei Retry-Runden à 40 s — der Snapshot fällt also auf Sonnenaufgang +6 bis +15 min. Der Nowcast ist ab Sonnenaufgang −30 min aktiv.

Meist rettet der `measured > 20`-Guard die Situation, weil die 30-Minuten-EMA morgens noch nahe null steht. An klaren Sommermorgen mit schnell steigender Einstrahlung kann der Faktor aber schon greifen. Dann schreibt `pv_prognose_this_morning` einen bereits korrigierten Wert, und die Bias-Lernschleife um 23:55 lernt eine Korrektur nach, die schon drin war — eine langsame Doppelkorrektur.

Fix: den Snapshot auf Sonnenaufgang −45 min ziehen (dort läuft der `predawn`-Lauf von `pv_forecast_calc` ohnehin) und den Fetch davor legen.


**C — `weight_*` sind write-only** · `pv_forecast_inputs.yaml`, kosmetisch

Die Kalman-Automation schreibt `input_number.weight_solcast` bis `weight_dwd` um 23:55. Gelesen werden sie nirgends — `pv_forecast_calc.yaml` bildet `bw_*` direkt aus Score und R. Wenn sie nicht im Dashboard hängen, kann der letzte `repeat`-Block der Automation entfallen.

**D — Recorder-Volumen** · `configuration.yaml`, offen

`purge_keep_days: 366` bei einer zweistelligen Zahl hochfrequenter Leistungssensoren (Fronius alle 5 s, EV-Target alle 30 s). Die `entity_globs` fangen die Rohwerte ab, aber `sensor.leistung_vom_netz`, `sensor.charge_power` und `sensor.house_consumption_power_brutto` laufen voll in die MariaDB. Das steht schon länger auf der Liste.

### 8.3 Strukturelle Beobachtung

Die Konfiguration hat inzwischen eine Größe erreicht, bei der **kein einzelner Wert mehr an nur einer Stelle wirkt**. `estimated_remaining_consumption` ist dafür das beste Beispiel: eine Änderung des Filterfensters von 1 h auf 5 min sieht lokal harmlos aus, kippt aber über zwei Zwischenstufen die nächtliche Netzladung.

Zwei Dinge würden das entschärfen:

1. **Die Konsumenten in den Kommentar schreiben.** Bei `binary_sensor.marstek_verfuegbar` ist das schon gemacht („Gelesen von: …"). Bei `estimated_remaining_consumption`, `charge_power` und `target_soc` fehlt es — und genau das sind die Werte mit der größten Reichweite.
2. **Einen Plausibilitätsdeckel auf die Bilanzeingänge.** `consumption_blended` in `target_soc` könnte auf das 1,5-fache des erwarteten Tagesverbrauchs geklemmt werden. Dann kann ein Messausreißer die Netzladung nicht mehr auslösen, egal welcher Sensor dahinter hängt.
