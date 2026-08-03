# Home-Assistant Energiemanagement — Systembeschreibung

Stand: 02.08.2026 · Konfiguration in `packages/`, ausgewertet aus dem vollständigen Dateisatz und dem Log vom 02.08.2026

---

## 1. Überblick

Das System regelt drei Speicher, eine PV-Anlage aus vier Wechselrichtern und eine Wallbox mit zwei Fahrzeugen. Alle Entscheidungen laufen über einen einzigen Zielwert (`sensor.target_soc`) und eine einzige Leistungsverteilung (`sensor.target_power_distribution_fast`) — es gibt keine konkurrierenden Regler.

### Hardware

| Komponente | Anbindung | Rolle |
|---|---|---|
| Fronius Wechselrichter + SmartMeter | Modbus TCP `192.168.0.49` | Dachanlage, Netzbezugsmessung (Regelgröße) |
| 3 × Balkon-/Garagen-Wechselrichter | Tasmota / MQTT | Zusatzerzeugung |
| 2 × Marstek Venus (je 5,12 kWh) | `marstek_modbus` (RS485/TCP) | AC-gekoppelte Hauptspeicher, 2500 W je Richtung |
| USV-Eigenbau (2,2 kWh, ANT-BMS) | ESPHome/MQTT + 2 Ladegeräte, 1 Inverter | Grobsteuerung per Schaltrelais |
| WARP3 Wallbox (22 A, 1-phasig) | MQTT/native Integration | Zero SR + Honda e |
| Wetterstation | weewx → MQTT `weather/loop` | Globalstrahlung für Nowcast, AWEKAS-Upload |
| BM2-Batteriewächter | BLE | 12-V-Bordbatterie-Warnung |

### Rechenketten in Kurzform

```
Fronius/SmartMeter ──► sensor.leistung_vom_netz ──┐
Marstek AC-Leistung ─────────────────────────────┼─► sensor.charge_power ─► charge_map / discharge_map
                                                  │                          └► sende_batterie_targets_v3 ─► Venus
PV-Prognose (4 Provider, Kalman) ─► pv_forecast_weighted ─┐
Verbrauchsprognose (Tagesklassen + Live-EMA) ─────────────┼─► sensor.target_soc ─► Ladeziel
Marstek usable Energy ────────────────────────────────────┘
Netz + Speicher + Wallbox ──► Überschuss ──► sensor.ev_target_charging_current ──► WARP3
```

---

## 2. Dateistruktur

| Datei | Inhalt |
|---|---|
| `globals.yaml` | Single Source of Truth: Zellspannungen, Kapazitäten, Wirkungsgrade, Sicherheitsgrenzen, Zielzeit-Parameter |
| `control.yaml` | Zielwert-Logik: `solar_soc_logic`, `dynamic_soc`, `target_soc`, Wartungsbedarf |
| `marstek_batteries.yaml` | Custom-SoC beider Venus, Leistungsverteilung, Hardware-Kommandos, Wartungsladung |
| `ups_batterie.yaml` | USV-SoC, Grobsteuerung (Laden/Entladen/Notladung), eigener Wartungszyklus |
| `consumption.yaml` | Hausverbrauch, Tagesklassen-Prognose, Restverbrauchsschätzung |
| `pv_production.yaml` | Ertragszähler und Sperrklinken gegen rückläufige Zählerstände |
| `fronius.yaml` | Modbus-Register + EMA-Glättung |
| `pv_forecast_calc / _fetch / _automations / _inputs / _nowcast / _graph` | Prognose-Ensemble |
| `ev_inputs / _sensors / _sensor_trigger / _soc / _automations` | Wallbox- und Fahrzeuglogik |
| `weewx_sensor.yaml` | Wetterstation über MQTT |
| `honda_batt_warning.yaml` | Neustart-Mail + BM2-Warnung (Dateiname historisch) |

---

## 3. Batteriemanagement

### 3.1 SoC-Modell: gerechnet statt gemeldet

Die Herstellerangabe wird nicht als Wahrheit genommen. Für alle drei Speicher gilt dasselbe Muster:

1. Ein **Riemann-Integral** über die DC-Leistung liefert einen laufenden Energiezähler (`venus_1_energy_wh`, `venus_2_energy_wh`, `ups_energyflow_wh`).
2. Ein **`reset_point`-Attribut** merkt sich den Zählerstand beim letzten sicheren Referenzpunkt.
3. Der Custom-SoC ergibt sich als `100 % − (Zähler − reset_point) / Kapazität`.

Neu gesetzt wird der `reset_point`, wenn ein verlässlicher Ankerpunkt vorliegt:

- Hardware-SoC ≥ 100 % → Zähler ist der neue Nullpunkt (Speicher voll)
- Hardware-SoC ≤ `stop_soc` (15 %) → Rückrechnung aus dem Hardware-SoC
- Zähler unterschreitet den bisherigen Reset-Punkt → nachziehen
- USV zusätzlich: höchste Zellspannung > `lfp_max_voltage` (3,565 V) oder Drift zum Hardware-SoC > 25 %

Der Sinn: LFP-Zellen haben eine flache Spannungskurve, die BMS-Schätzung driftet. Der Zähler ist zwischen zwei Ankerpunkten genauer, die Ankerpunkte verhindern das Weglaufen.

### 3.2 Kapazitätsbewertung

`sensor.marstek_usable_energy` liefert die tatsächlich entnehmbare Energie beider Venus oberhalb der Sicherheitsgrenze (`solar_soc_logic.min` = 15 %). `calculated_combined_capacity_kwh` = 9,512 kWh bewertet die Venus mit 80 % und die USV mit 60 % Verfügbarkeit — das ist die Bezugsgröße für die Mehrtagesbilanz.

### 3.3 Zellschutz

Harte Grenzen liegen in `globals.yaml` und werden an drei Stellen durchgesetzt:

- **USV Zweig 0.5** (`ups_coarse_control`): höchste Zelle > 3,565 V → Ladegeräte sofort aus, ohne jede weitere Bedingung
- **USV Zweig 2**: SoC ≤ 12 % oder tiefste Zelle < 3,00 V → Notladung, Inverter aus
- **Entladefreigabe Venus**: `discharge_map` gibt einen Speicher nur frei, wenn dessen minimale Zellspannung ≥ `lfp_min_voltage` ist
- **Sicherheitsabschaltung**: Inverter und Ladegerät gleichzeitig an → alles aus (0,5 s Entprellung)
- **BMS-Verbindungsverlust** (beide Zellspannungen = 0) → alle Schalter aus + persistente Meldung

### 3.4 Wartungsladung (Zellbalancing)

Seit dem Umbau laufen zwei getrennte Zyklen, weil die Ladezeiten um den Faktor 6 auseinanderliegen:

| | Venus | USV |
|---|---|---|
| Auslöser | `venus_x_last_full` älter als `battery_care_days` (8 Tage) | `ups_last_full` älter als 8 Tage |
| Start | Sonnenuntergang +5 min, Wiederholung alle 30 min | dito, plus Plausibilitätsprüfung der Zellspannung |
| Mechanik | `manual_charge_power` = 1000 W je Speicher | `input_boolean.ups_maintenance` → `ups_coarse_control` Zweig 1 |
| Stopp | beide Venus > 99,5 % | SoC > 99,5 % **oder** Zellspannung über Maximum |
| Notbremse | Sonnenaufgang: hart auf 0 | Sonnenaufgang: Flag aus |
| Dauer | ~30 min | 3–5 h |

Zusätzlich hebt `soc_override_weekly_control` nach 5 Tagen ohne Vollladung den Zielwert auf 100 % an (`override_soc`) und nimmt ihn zurück, sobald alle drei Speicher voll sind oder ein frischer Zeitstempel vorliegt.

---

## 4. Lademanagement

### 4.1 Regelgröße

`sensor.charge_power` = `leistung_vom_netz + marstek_ac_power_total`, also der Leistungsfluss, der ohne Speicher am Netzpunkt stünde. Negativ = Überschuss (laden), positiv = Bedarf (entladen). Eine Totzone von 40 W erzwingt eine saubere Null, Änderungen werden erst ab 20 W oder nach 60 s durchgereicht — das entkoppelt die Regelung vom Messrauschen.

Vorgelagert glättet `fronius.yaml` die Rohwerte mit einem EMA (α = 0,68 bei 5 s Takt ≈ 10 s bis 90 %) und verwirft Werte über 20 kW als Modbus-Müll.

### 4.2 Aufteilung auf die beiden Venus

`charge_map` und `discharge_map` verteilen die Gesamtleistung gewichtet nach dem Abstand zum Ziel bzw. zur Untergrenze. Drei Sonderfälle sind eingebaut, weil die Venus unterhalb ~200 W schlecht arbeiten:

- Gesamtleistung > 1000 W → beide Speicher bekommen mindestens 500 W
- Gesamtleistung > 400 W → beide mindestens 200 W
- darunter → **abwechselnder Betrieb**: der Speicher mit dem größeren Bedarf nimmt alles; bei Gleichstand entscheidet `(Wochentag + Stunde) % 2`, damit sich der Wechsel gleichmäßig verteilt

`limits_map` deckelt die Ladeleistung zusätzlich zeitabhängig: zwischen 11:15 und Sonnenuntergang −3 h wird nur so viel angefordert, wie rechnerisch nötig ist, um das Ziel bis zum Fensterende zu erreichen (Faktor 2,5 als Sicherheitszuschlag, Untergrenze 500 W). Dadurch wird die Mittagsspitze nicht sofort weggeladen, sondern über den Nachmittag gestreckt.

### 4.3 Kommandoausgabe

`sende_batterie_targets_v3` (Modus `queued`, max 3) schreibt die Sollwerte. Wesentliche Details:

- **Richtungswechsel zuerst neutralisieren**: beim Wechsel zwischen Laden und Entladen werden erst beide Leistungsregister auf 0 gesetzt, dann der Force-Mode umgeschaltet, dann der neue Sollwert geschrieben — mit 120 ms Pausen, weil die Venus sonst Kommandos verschlucken
- **Beide Speicher parallel**, mit 10 ms Versatz gegen Bus-Kollisionen
- **Nur schreiben, wenn sich der Wert ändert** — das hält den Modbus-Verkehr klein
- Schwelle 20 W: darunter gilt `standby`
- `continue_on_error` überall, damit ein hängender Speicher den anderen nicht blockiert

Nachgelagert räumen zwei Automationen auf: `marstek_set_last_full_timestamp` setzt bei > 99,5 % den Zeitstempel und die Ladeleistung auf 0, `marstek_venus_reset_charge_over_target` stoppt alle 2 Minuten das Laden über dem Zielwert.

### 4.4 USV-Grobsteuerung

Die USV kennt keine Leistungsregelung, nur drei Relais. `ups_coarse_control` entscheidet alle 12 s (Selbstdrosselung über `last_triggered`) anhand von `true_balance = netz + venus + usv`. Das Zurückrechnen der eigenen Inverterleistung ist der Trick, der das Aufschwingen verhindert: die Entscheidungsgröße bewegt sich nicht mehr, wenn die USV selbst einspeist.

Prioritätenkette: BMS-Ausfall → Zellüberspannung → Wartungsmodus → Notladung → Entladen starten (> 180 W Bedarf) → Entladen stoppen (< 50 W, mit 30 s Mindestlaufzeit) → Laden stoppen → Laden starten (> 820 W Überschuss, zweites Ladegerät ab 1200 W).

---

## 5. Zielwert-Logik

Drei Sensoren in `control.yaml`, aufeinander aufbauend:

**`solar_soc_logic`** liefert die Tageszeitmarken (Sonnenauf-/-untergang, 11:15, Sonnenuntergang −3 h) und die SoC-Stufen, jeweils gegen `stop_soc` nach unten abgesichert.

**`dynamic_soc`** ist der Fahrplan über den Tag:

| Zeitfenster | Ziel |
|---|---|
| Nacht | 15 % (min) |
| Sonnenaufgang – 11:15 | 60 % (low) |
| 11:15 – Sonnenuntergang −3 h | 80 % (mid) |
| letzte 3 Stunden | 90 % (high) |

**`target_soc`** überschreibt diesen Fahrplan nach einer festen Rangfolge:

| Wert | Bedeutung |
|---|---|
| 101 | manueller Override aktiv |
| 120 | Wartungszyklus fällig |
| 110 | manuelle Ladeleistung gesetzt |
| 130 / 131 / 133 | rollierende Energiebilanz für heute / morgen / übermorgen negativ |
| max (100,5) | Rest-PV reicht nicht bis zum dynamischen Ziel → Deckel auf, jede Wh mitnehmen |
| sonst | `dynamic_soc` |

Die Mehrtagesbilanz simuliert den Speicherstand über drei Tage: `usable_energy + PV-Prognose − Verbrauchsprognose`, jeweils auf die Speicherkapazität gedeckelt. Der dringlichste Engpass gewinnt. Der Verlauf vom 02.08. zeigt genau dieses Verhalten — nachts die Untergrenze, ab Mitternacht der Tagesfahrplan, mittags eine kurze 130er-Spitze, nachmittags der Wechsel zwischen 80 und offenem Deckel, abends 100.

`sensor.target_soc_status` übersetzt die Zahlencodes im Klartext fürs Dashboard.

---

## 6. PV-Prognose

Vier unabhängige Quellen werden nicht gemittelt, sondern nach gemessener Trefferquote gewichtet.

### 6.1 Datenquellen

| Provider | Bezug | Heute | Morgen | Übermorgen |
|---|---|---|---|---|
| Solcast | Integration | ✓ | ✓ | — |
| Forecast.Solar | REST, zwei Endpunkte (Dach 20°/−45°/7,5 kWp, Fassade 85°/45°/2,4 kWp) | ✓ | ✓ | — |
| Open-Meteo | Integration, stündliche `wh_period` | ✓ | ✓ | ✓ |
| DWD | Integration, stündliche `wh_period` | ✓ | ✓ | ✓ |

Die REST-Abfrage läuft mit `scan_interval: 31536000` faktisch nie automatisch — geholt wird gezielt bei Sonnenaufgang, mit bis zu drei Versuchen und 3 min Abstand. `*_raw_*_stable` hält die letzte gültige Antwort fest, damit ein API-Ausfall die Prognose nicht auf 0 zieht.

### 6.2 Horizontkorrektur

Die Makros `horizon_dach.jinja` und `horizon_fassade.jinja` liefern pro Datum und Stunde einen Verschattungsfaktor. Er wird auf die stündlichen Rohwerte jedes Providers angewendet, bevor summiert wird — dadurch wirkt er richtig, statt als pauschaler Tagesfaktor.

Bei Forecast.Solar werden die kumulierten Wattstunden zuerst differenziert; die aktuell laufende Stunde wird anteilig gewichtet, damit „Rest des Tages" auch untertägig stimmt.

### 6.3 Kalman-Gewichtung

Um 23:55 vergleicht `pv_daily_kalman_update` den Morgen-Snapshot jedes Providers mit dem realen Ertrag:

1. **Score je Provider** aus dem relativen Fehler (`1 / (rel_err · 10 + 0.1)`), gefiltert über einen Kalman-Schritt mit adaptivem Messrauschen (`R` wird aus der Innovationsvarianz nachgeführt, β = 0,9)
2. **Gewichte** = normierte Scores, zusätzlich in `pv_forecast_calc` durch `R + 0.1` geteilt — ein Provider, der zwar im Mittel gut liegt, aber stark streut, verliert an Einfluss
3. **Bias** (0,8–1,3) korrigiert den systematischen Gesamtfehler der Anlage, ebenfalls Kalman-gefiltert

Der statische Höhen-/Albedofaktor ist bewusst aus der Rechnung genommen und existiert nur noch zur Diagnose — er hat den gelernten Bias nach unten blockiert.

### 6.4 Nowcast

`pv_forecast_nowcast.yaml` schärft die Tagesprognose live nach:

1. Globalstrahlung über ~30 min glätten (EMA, α = 0,065 im Minutentakt)
2. Anlagenspezifischen Umrechnungsfaktor lernen (α = 0,01, nur oberhalb 100 W/m²)
3. Daraus die „gemessene" PV-Leistung ableiten und gegen die für die laufende Stunde modellierte Leistung stellen
4. Faktor (0,3–2,5) multiplikativ nur auf **heute** anwenden

Der Ansatz über die Einstrahlung statt über die PV-Leistung ist der entscheidende Punkt: Schnee, Verschattung oder ein Wechselrichterfehler drücken die Leistung, nicht die Einstrahlung — ein reiner Leistungsvergleich würde in genau diesen Fällen die Wetterprognose fälschlich nach unten ziehen.

Nachts sind beide Blöcke abgeschaltet und der Faktor friert per Sun-Guard auf 1,0 ein.

### 6.5 Verbrauchsprognose

`consumption.yaml` führt drei gleitende Mittel nach Tagestyp (Werktag / Samstag+Vortag-Feiertag / Sonntag+Feiertag), aktualisiert bei Sonnenaufgang +30 min mit unterschiedlicher Trägheit (Werktag 4:1, Wochenende 2:1). Die Feiertagslogik steckt in `holidays.jinja` und wird auch für morgen und übermorgen ausgewertet.

Für den Resttag mischt `target_soc` zwei Schätzungen zu gleichen Teilen:

- **statisch**: Tagesprognose minus bisheriger Verbrauch
- **live**: aktueller 5-min-Mittelwert hochgerechnet bis zum nächsten Sonnenaufgang

---

## 7. EV-Steuerung

### 7.1 SoC-Tracking ohne Fahrzeug-API

Beide Fahrzeuge melden ihren Ladestand nur sporadisch über BLE (ESP32-Gateway). Die Lücke füllt eine Ankerrechnung:

```
SoC = Anker-SoC + (Wallbox-Zählerstand − Anker-Zählerstand) · Wirkungsgrad / Kapazität
```

Der Anker wird **nur** bei einer BLE-Kalibrierung und beim Fahrzeugwechsel geschrieben, gehalten in `input_number`-Helfern — dadurch überlebt der Wert Neustart und YAML-Reload, und während des Ladens läuft keine Automation, die sich selbst aufschaukeln könnte. Beim Fahrzeugwechsel wird der erreichte Stand des alten Fahrzeugs eingefroren (gerechnet aus `trigger.from_state`, kein Race) und der Ankerzähler beider Fahrzeuge auf den aktuellen Zählerstand gesetzt.

Kenndaten aus `globals.yaml`: Zero 13,6 kWh bei 91,5 % (am 01.08. nachgemessen), Honda e 28,5 kWh bei 88 %.

### 7.2 Sechs Lademodi

Zentral gerechnet in `sensor.ev_target_charging_current` (alle 30 s und bei jeder relevanten Zustandsänderung). Der Überschuss wird in zwei Varianten gebildet:

- **EV first**: `Wallbox − Netz − Venus1 − Venus2` — die Ladeleistung der Hausspeicher wird für das Auto freigegeben
- **Battery first**: nur die Entladeleistung der Speicher wird abgezogen, der Ladeanteil bleibt bei der Hausbatterie

| Modus | Verhalten |
|---|---|
| `pv_ev_first` | ganzer Überschuss ans Fahrzeug |
| `pv_battery_first` | Hausspeicher zuerst, Rest ans Fahrzeug |
| `pv_shared_50_50` | dynamisch nach Füllstand; Fahrzeug voll → alles in die Batterie, Batterie ≥ 99 % → alles ins Fahrzeug |
| `min_6a_floor_battery` | mindestens 6 A, solange der SoC über dem gerechneten Reserve-Floor liegt |
| `min_6a_floor_grid` | mindestens 6 A, notfalls aus dem Netz |
| `target_time` | fertig bis zur eingestellten Uhrzeit |

Der **Reserve-Floor** (`sensor.battery_floor_soc_for_ev`) rechnet, wie viel Speicherinhalt der Haushalt bis zum nächsten Morgen noch braucht — Restverbrauch (statisch/live gemischt, 10 % Puffer) minus Rest-PV, umgerechnet in Prozent der Speicherkapazität, mindestens 15 %.

### 7.3 Zielzeitladen

Drei Phasen, ohne Helfer, ohne Latch, ohne Hysterese:

| Phase | Bedingung | Leistung |
|---|---|---|
| `pv` | vor dem Startzeitpunkt | nur Überschuss, gedeckelt auf 85 % SoC |
| `paced` | ab `Zielzeit − Puffer − Bedarf/Batterietempo` | 4500 W (Entladeleistung der Hausspeicher) oder mehr, wenn die Sonne mehr hergibt |
| `full` | ab `Zielzeit − Puffer − Bedarf/Maximalleistung` | alles, Netz inbegriffen |

Die Planungsleistungen sind mit `target_time_power_derate` = 0,9 absichtlich pessimistisch angesetzt. Dadurch schrumpft der Restbedarf schneller, als die Uhr läuft: einmal ausgelöst, rastet die Startbedingung von selbst ein, und die `full`-Stufe wird bei planmäßigem Verlauf nie erreicht. `sensor.ev_charge_start_time` macht Startzeitpunkt, Deadline, Phase und Restbedarf im Dashboard sichtbar.

### 7.4 Wallbox-Automationen

| Automation | Aufgabe |
|---|---|
| `wallbox_enforce_off` | Fahrzeugmodus „off" wird gegen jede Quelle durchgesetzt, auch gegen den Auto-Start der WARP3-Firmware |
| `wallbox_reset_session_energy` | Zählerstand-Snapshot beim Einstecken |
| `wallbox_universal_start` | Start nach 15 s stabilem Überschuss oder sofort bei Bedienung |
| `wallbox_track_target` | folgt dem Sollstrom mit 45 s Sperrzeit, damit sich die schnelle Batterieregelung gegen eine feste EV-Last einschwingen kann |
| `wallbox_stop_on_idle` | Sollstrom 2 min unter 6 A → Stopp |
| `wallbox_stop_on_limit_or_off` | Energieziel erreicht oder Fahrzeug „off"; im Zielzeitmodus bewusst ausgenommen, weil dort der Live-SoC die Wahrheit ist |
| `wallbox_manual_charge` | fester Strom, auf 22 A begrenzt |
| `wallbox_reset_vehicle` | Fahrzeugmodus beim Abstecken zurücksetzen |
| `wallbox_LED` | Statusanzeige: Zero rot, Honda weiß, frei grün; Blinkfrequenz nach SoC |

---

## 8. Infrastruktur

- **Recorder** auf MariaDB, `purge_keep_days: 366`, `commit_interval: 60`, hochfrequente Diagnosewerte per `entity_globs` ausgeschlossen (`*raw*`, `*api*`, `*fast*`, `*ant_bms*`, Venus-Spannung/-Strom u. a.)
- **Packages** über `!include_dir_named`, UI-Automationen getrennt in `automations.yaml` (Venus-Steuermodus nach Sonnenaufgang, CPU-Governor, Tasmota-Broadcast)
- **CPU-Tuning** per `shell_command` (schedutil, `rate_limit_us` 10000) plus zwei `command_line`-Sensoren für den Takt
- **AWEKAS-Upload** als `rest_command` mit Offline-Fallback-URL, wenn Temperatur oder Feuchte keine Zahl liefern
- **Benachrichtigungen**: Mail bei HA-Neustart, Mail bei BM2-Unterspannung (< 12,3 V oder < 50 %), persistente Meldung bei BMS-Verbindungsverlust
- **Custom-Integrationen**: marstek_modbus, solcast_solar, open_meteo_solar_forecast, solarprognose_de_community, grid_price_monitor, ml_weather, watchman, spook (+inverse), hwmon_temp, ha_hypertree, HACS

---

## 9. Beobachtungen aus dem Log vom 02.08.2026

| Zeit | Eintrag | Bewertung |
|---|---|---|
| 14:05:56 | `Received invalid sensor state: unavailable` für `leistung_vom_netz`, `leistung_von_pv_ac`, `energie_von_pv_ac` | Die Templates geben im `state:` den String `unavailable` zurück. Funktional gewollt, aber HA protokolliert es als ERROR — siehe TODO 3 |
| 14:10:11 | `total_increasing, but its state is not strictly increasing` für `pv_energy_yield_today` (30,06 → 30,05) | Neustart-Artefakt der Filter-Sensoren, kein Datenverlust |
| 20:46:33 | `marstek_modbus: Timeout reading … 192.168.0.208 … next poll in 180s` | Der interessante Eintrag — siehe TODO 1 |

Der Timeout um 20:46 fällt zeitlich mit dem Sprung von `sensor.target_soc` auf 130 im Verlauf zusammen. Das passt zur Rechenkette: fällt ein Venus-SoC aus, wird `sensor.marstek_usable_energy` `unavailable`, `target_soc` liest ihn mit `| float(0)` als leeren Speicher und meldet folgerichtig „Tagesbilanz negativ". Lässt sich mit einem Blick in die Historie von `sensor.marstek_usable_energy` um 20:46 bestätigen.

---

## 10. TODO — Verbesserungspotenzial

### Priorität 1 — Verhalten bei Sensorausfall

**4. Steuermodus-Switches werden nur einmal täglich gesetzt.** Startet ein Venus nach einem Stromausfall oder Firmware-Reboot neu, bleibt der RS485-Steuermodus bis zum nächsten Sonnenaufgang aus — `charge_map` und `discharge_map` schließen den Speicher dann sauber aus, aber er steht eben ungenutzt herum. Ein `time_pattern: /30` als zusätzlicher Trigger auf dieselbe Automation genügt.

### Priorität 2 — Offene Punkte aus dem Code


**8. Nowcast-Entity prüfen.** Der Code liest `sensor.wetterstation_solarstrahlung`, der Kopfkommentar nennt `sensor.solar_radiation`, aus `weewx_sensor.yaml` entstünde von Haus aus `sensor.weather_station_solar_radiation`. Passt die ID nicht, bleibt der Korrekturfaktor unbemerkt auf dem Neutralwert 1,0.

**9. Log-Rauschen abstellen.** Statt `unavailable` als Zustandstext auszugeben, gehört die Bedingung in ein `availability:`-Template — dann verschwinden die drei ERROR-Zeilen bei jedem Neustart und bei jedem Modbus-Aussetzer.

### Priorität 3 — Hygiene

**11. Recorder-Volumen.** 366 Tage Vollarchiv bei dieser Sensordichte ist die größte Einzelbelastung für die Datenbank. Für die Energiedashboards genügen die Langzeitstatistiken, die vom Purge unberührt bleiben; 30–90 Tage Detaildaten reichen in der Praxis. Zusätzlich lohnen `sensor.leistung_vom_netz`, `sensor.charge_power` und `sensor.house_consumption_power_brutto` als Kandidaten für eine gröbere Auflösung.

**12. `watchman` einmal laufen lassen.** Nach dem Entfernen von `zero.yaml`, `pv_forecast.yaml` und den beiden `pv_dynamic_charging_current`-Sensoren dürften noch verwaiste Entitäten und tote Referenzen in der Registry stehen.


### Priorität 4 — Ausbau

**15. Strompreise einbeziehen.** `grid_price_monitor` liegt ungenutzt bereit. Die Bilanzlogik in `target_soc` ist bereits mehrtägig — sie ließe sich um eine Preisstufe erweitern: bei negativen oder sehr günstigen Stunden aus dem Netz vorladen, statt nur auf die PV-Bilanz zu schauen. Das ist der größte verbliebene Hebel für die Wirtschaftlichkeit.

**16. Wallbox auf 32 A und Honda e auf 7 kW.** `wallbox_max_amps` ist bereits zentral gepflegt, die Umstellung ist ein Einzeiler. `battery_paced_power_w` (4500 W) sollte danach nachgemessen werden, sonst zieht die `paced`-Phase mehr, als die Hausspeicher tragen.

**17. Nowcast-Gain tageszeitabhängig lernen.** Der Umrechnungsfaktor Einstrahlung → Leistung ist bei fest montierten Modulen vormittags, mittags und nachmittags unterschiedlich. Getrennte Faktoren je 2-Stunden-Fenster würden den Live-Faktor spürbar schärfen — im Code als bekannte Einschränkung bereits notiert.

**18. Solarprognose.de als fünfter Provider.** Die Kalman-Struktur ist generisch, ein weiterer Score/P/R-Satz plus Gewicht genügt. Der Filter erkennt selbst, ob die Quelle etwas beiträgt.

**19. Zielzeit-Rückmeldung.** Wenn `ev_start_paced_ts` bereits in der Vergangenheit liegt, ist die Zielzeit rechnerisch nicht mehr erreichbar. Aktuell fällt das nur beim Blick aufs Dashboard auf — eine Benachrichtigung beim Einstecken („Ziel 80 % bis 07:00 nicht erreichbar, maximal 72 %") wäre die logische Ergänzung zur bereits vorhandenen Diagnose.
