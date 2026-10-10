# PVE-UPS

**GUI-basierte USV-Shutdown-Appliance für Proxmox VE — mit Web-Wizard und ohne
Konfigurationsdateien.**

*English version: [README.md](README.md)*

PVE-UPS überwacht eine oder mehrere USVs — **mit SNMP-Netzwerkkarte (Standard RFC 1628
oder Hersteller-MIB: APC PowerNet, CyberPower)** oder **über einen NUT-Server**, worüber USB- und
seriell angeschlossene USVs gelesen werden — und fährt bei Stromausfall einen oder mehrere
**Proxmox-VE-Hosts** (auf Wunsch auch einen **Proxmox Backup Server**) geordnet herunter.
Der moderne Ersatz für herstellergebundene Appliances wie APC PowerChute Network Shutdown.
Die komplette Einrichtung läuft über einen **Web-Wizard**; Monitoring gibt es als
**REST/JSON**.

> [!IMPORTANT]
> PVE-UPS ist für ein **internes (Management-)Netz** ausgelegt. Die Appliance hält
> API-Token, mit denen sich Server ausschalten lassen — sie gehört also wie jedes andere
> Management-Werkzeug betrieben: nur aus vertrauenswürdigen Netzen erreichbar, niemals ins
> Internet exponiert, hinter derselben Firewallierung und Zugriffskontrolle wie die
> Proxmox-Weboberflächen. Zu beachten: `/api/status` und `/api/health` sind bewusst ohne
> Anmeldung lesbar, damit ein Monitoring sie abfragen kann.

## Warum nicht NUT?

[NUT](https://networkupstools.org/) hat hervorragende Hardware-Unterstützung, bedeutet für
den üblichen Fall „fahre meine Proxmox-Hosts herunter, wenn die USV zur Neige geht" aber
`upsd`-/`upsmon`-Konfigdateien und eigene Shutdown-Skripte auf jedem Host. PVE-UPS geht
stattdessen den Appliance-Weg — und wo man NUTs Hardware-Unterstützung braucht, nutzt es
NUT als *Treiber*, statt es zu ersetzen:

- **Ein LXC, ein Installer** — ein unprivilegierter Debian-Container (~256 MB RAM),
  angelegt mit einem einzigen Befehl auf dem PVE-Host.
- **Keine Konfigdateien** — ein Web-Wizard mit Test-Buttons für jeden Schritt;
  Einstellungen greifen sofort.
- **Keine Agenten auf den Hosts** — der Shutdown läuft über die Proxmox-API mit einem
  dedizierten, widerrufbaren **API-Token**, das nur das Recht `Sys.PowerMgmt` besitzt.
  Nirgendwo Root-SSH.
- **Herstellerneutral, aber nicht blauäugig** — die Standard-RFC-1628-UPS-MIB per
  SNMP v1/v2c/v3 (reine Python-Implementierung, kein net-snmp), mit automatischem Wechsel
  auf eine Hersteller-MIB, wo der Standard nicht reicht (APC PowerNet, CyberPower) — oder
  jeder
  vorhandene NUT-Server als nur-lesender Client.
- **NUT bleibt Treiber, nie das Gehirn** — PVE-UPS liest von `upsd` ausschließlich
  Variablen. Kein `upsmon`, kein `upssched`, keine Shutdown-Skripte: Schwellen,
  Host-Logik und Entscheidung bleiben in der Appliance, wo man sie sieht.

## Screenshots

*Dashboard während eines Stromausfalls — eine USV im Akkubetrieb, Shutdown-Countdown
läuft, darunter das Live-Schaubild der Versorgung:*

![Dashboard während eines Stromausfalls](Screenshots/dashboard.png)

<details>
<summary>Weitere Screenshots (USV-Status &amp; -Einstellungen, Hosts, Cluster-Vorbereitung, Webhooks)</summary>

*USV-Statuskarten — Quelle, tatsächlich verwendete MIB, Ladung, Restlaufzeit:*

![USV-Status](Screenshots/ups-status.png)

*Ein USV-Eintrag: ausgelesen per SNMP (Netzwerkkarte) oder über einen NUT-Server, MIB-Wahl,
Zeitlimit und Wiederholungen, optionale Schwellen je USV:*

![USV-Einstellungen](Screenshots/ups-settings.png)

*Host-Einstellungen — Typ Proxmox VE oder Backup Server je Eintrag, API-Token, welche USVs
ihn versorgen, UND/ODER-Logik und das daraus entstehende Schaubild:*

![Host-Einstellungen](Screenshots/host-settings.png)

*Live-Vorschau der Shutdown-Reihenfolge: Hosts mit gleicher Nummer gehen gleichzeitig, der
eigene Host der Appliance immer zuletzt:*

![Vorschau der Shutdown-Reihenfolge](Screenshots/shutdown-sequence-preview.png)

*Cluster-Optionen je Host (Beta): den Cluster als Einheit herunterfahren, den HA-Manager
disarmen und einen hyperkonvergenten (Ceph-)Cluster vorbereiten:*

![Cluster-Optionen an einem Host](Screenshots/host-settings-cluster.png)

*Cluster-Vorbereitung — die Zeitlimits der einzelnen Schritte, die Worst-Case-Akkuzeit, die
daraus entsteht, und der Gast, in dem die Appliance selbst läuft:*

![Einstellungen der Cluster-Vorbereitung](Screenshots/cluster-preparation.png)

*Webhooks: einer je Ziel, jeder mit eigenem Format (Slack, Discord, Teams, ntfy, Status-JSON
oder eigene Vorlage), Schweregrad-Filter und Testversand:*

![Webhook-Einstellungen](Screenshots/webhook-settings.png)

</details>

## Installation

In der **Proxmox-Node-Shell** ausführen (Webinterface → Node → `>_ Shell`, als root).
Das Skript lädt das aktuelle Release nach, entpackt es und legt den LXC an:

```bash
bash -c "$(curl -fsSL https://github.com/ffind-dev/pve-ups/releases/latest/download/install.sh)"
# mit Optionen, z.B. fester IP:
curl -fsSL https://github.com/ffind-dev/pve-ups/releases/latest/download/install.sh | bash -s -- \
  --ctid 950 --ip 10.0.0.50/24 --gateway 10.0.0.1 --hostname pve-usv
```

Alle Optionen sind optional; `--help` listet sie mit ihren Standardwerten
(`curl … | bash -s -- --help`):

| Option | Standard | Bedeutung |
|---|---|---|
| `--ctid <id>` | `950` | ID des neuen Containers |
| `--hostname <name>` | `pve-usv` | Hostname des Containers |
| `--storage <name>` | automatisch | Storage für die Container-Platte (Inhalt `rootdir`). Ohne Angabe: `local-lvm`, dann `local-zfs`, dann der erste andere Nicht-Ceph-Storage |
| `--template-storage <name>` | `local` | Storage, auf den das Debian-12-Template geladen wird (Inhalt `vztmpl`). Der Standard weicht auf einen anderen geeigneten Storage aus; ein ausdrücklich genannter wird abgelehnt, wenn er keine Templates aufnehmen kann |
| `--bridge <bridge>` | `vmbr0` | Netzwerk-Bridge |
| `--ip <dhcp\|adresse/cidr>` | `dhcp` | `dhcp` oder eine feste Adresse wie `10.0.0.50/24` |
| `--gateway <ip>` | – | Standard-Gateway, nur bei fester `--ip` verwendet |
| `--memory <MB>` | `256` | Arbeitsspeicher des Containers |
| `--disk <GB>` | `4` | Größe der Container-Platte |
| `--allow-ceph-storage` | aus | Ceph-gestützten `--storage` zulassen (siehe unten) |

Auf einem **Ceph-Cluster** lehnt der Installer einen Ceph-gestützten rootfs-Storage ab
(und überspringt ihn bei der automatischen Auswahl): Dieser Container muss weiterlaufen,
während der Cluster verschwindet, den er herunterfährt — auf Ceph kann er das nicht, denn
sobald der Pool `min_size` verliert, antwortet seine eigene Platte nicht mehr. Lokalen
Storage wählen oder mit `--allow-ceph-storage` erzwingen.

PVE-UPS ist außerdem auf [community-scripts.org](https://community-scripts.org/) gelistet
(dort nach „PVE-UPS" suchen) — einer community-gepflegten Sammlung von
Proxmox-Helper-Skripten. Der Einzeiler oben bleibt der Referenzweg.

Danach das Webinterface auf **`http://<container-ip>:8080`** öffnen:
1. UI-Passwort setzen.
2. Wizard durchlaufen (USVs → Hosts → Schwellwerte → optional Webhook).
3. Solange **Dry-Run** aktiv ist, wird nichts heruntergefahren — ideal zum Testen.
4. Wenn alles passt: **Dry-Run deaktivieren** (Modus „SCHARF").

> Der LXC läuft typischerweise auf einem der zu schützenden Hosts. Diesen in der
> Host-Liste als **„Dieser Host"** markieren — er wird dann garantiert zuletzt
> heruntergefahren. Mit dem clusterweiten Gäste-Stopp (oder auf einem Ceph-Cluster)
> stattdessen diesen Container unter *Auslöser → Diese Appliance* auswählen: Die
> Markierung folgt dann dieser Auswahl, und der Gäste-Stopp weiß, welchen Gast er nie
> stoppen darf.

## Docker (alternative Bereitstellung)

Kein LXC gewünscht? Bei jedem Release wird zusätzlich ein fertiges Image nach
[`ghcr.io/ffind-dev/pve-ups`](https://github.com/ffind-dev/pve-ups/pkgs/container/pve-ups)
veröffentlicht. Es ist Multi-Arch — `linux/amd64`, `linux/arm64` und `linux/arm/v7` — und
läuft damit auch auf einem Raspberry Pi 3/4/5 mit 64- oder 32-Bit-Betriebssystem; Docker
wählt die passende Variante selbst:

```bash
curl -fsSLO https://raw.githubusercontent.com/ffind-dev/pve-ups/main/docker-compose.example.yml
mv docker-compose.example.yml docker-compose.yml
docker compose up -d
```

Das bindet zwei benannte Volumes ein (`/etc/pve-usv` für die Konfiguration,
`/var/lib/pve-usv` für Eventlog, Verlauf und Zustand), damit die Daten eine Neuerstellung des
Containers überstehen. Danach `http://<container-host>:8080` öffnen und wie gewohnt
den Wizard durchlaufen.

**Der Docker-Modus unterscheidet sich in zwei Punkten vom LXC**, weil es dort keinen
privilegierten Begleitprozess (kein systemd) im Image gibt:
- **Keine In-App-Updates.** Der Button „Update-Paket hochladen" ist ausgeblendet;
  Updates erfolgen durch ein neues Image-Tag
  (`docker compose pull && docker compose up -d`). Konfiguration und Eventlog bleiben
  in den Volumes erhalten.
- **Keine NTP-/Zeitzonen-Verwaltung über den Wizard.** Zeit und Zeitzone liegen in der
  Verantwortung des Docker-Hosts/Orchestrators — **`TZ` am Container setzen** (die
  Beispiel-Compose-Datei tut das). Der Selbsttest-Zeitplan gilt in der lokalen Zeit des
  Containers; ohne `TZ` läuft der Container in UTC.

> **Liegt das eigene Netz innerhalb von `172.17.0.0/16` – `172.31.0.0/16`**, den
> Standard-Adresspool von Docker *vor* dem Start des Containers verschieben — Docker
> beansprucht diesen Bereich für seine Bridges, und der Container würde eine USV oder
> einen Proxmox-Host darin nicht mehr erreichen. In `/etc/docker/daemon.json`:
> `{"bip":"10.210.0.1/24","default-address-pools":[{"base":"10.211.0.0/16","size":24}]}`,
> danach `systemctl restart docker`.

Alles Weitere (SNMP-Polling, Proxmox-Shutdown, Schwellwerte, Webhook, Selbsttest)
funktioniert identisch zur LXC-Bereitstellung. Die LXC-Installation (oben) bleibt der
primäre, vollständig selbst-aktualisierende Weg.

## Hosts anbinden (API-Token)

Die Appliance fährt Hosts über die Proxmox-API herunter — kein Root-SSH, kein Agent auf
dem Host. Jeder Host-Eintrag hat einen **Typ**, denn Proxmox VE und Proxmox Backup Server
nutzen unterschiedliche Token-Schemata und Rechtenamen:

| | Proxmox VE | Proxmox Backup Server |
|---|---|---|
| API-Port | 8006 | 8007 |
| Recht | `Sys.PowerMgmt` | `Sys.PowerManagement` |
| Vergeben auf | `/nodes` | `/system/status` |
| Feld „Node" | Bezeichnung; muss zu einem Node-Namen passen | freie Bezeichnung (PBS ignoriert es) |
| API-URL | eine pro Knoten — sie entscheidet, welche Maschine heruntergefahren wird | eine pro Server |

Der falsch gewählte Typ ist die häufigste Ursache für einen fehlschlagenden Test: ein als
„Proxmox VE" eingetragener Backup Server weist die Anfrage rundweg ab und meldet
*„Authentication failed (token invalid?)"* — egal wie gültig das Token ist.

**Jedem Host-Eintrag seine eigene API-URL geben.** Der Shutdown geht an den Knoten hinter
der eingetragenen URL — sie entscheidet also, welche Maschine heruntergefahren wird, und
ein bereits ausgeschalteter Knoten kann den Shutdown für die übrigen nicht weiterreichen.
Der Name unter „Node" wird gegen die API geprüft und beschriftet den Host überall im UI,
steuert den Shutdown aber nicht mehr. Zwei Einträge mit derselben URL sind die Ausnahme —
davor warnt PVE-UPS.

### Proxmox VE

Es braucht einen dedizierten Benutzer mit **einem einzigen Recht** (`Sys.PowerMgmt`) und
einen API-Token. **Einmalig** in einer Node-Shell (als root):

```bash
# 1) Dedizierten Benutzer anlegen (PVE-Realm)
pveum user add ups@pve

# 2) Rolle mit nur dem Power-Management-Recht
pveum role add UpsShutdown -privs "Sys.PowerMgmt"

# 3) Rolle auf /nodes vergeben (oder enger: /nodes/<name>)
pveum acl modify /nodes -user ups@pve -role UpsShutdown

# 4) API-Token erzeugen — Privilege Separation AUS, damit der Token das Recht erbt
pveum user token add ups@pve shutdown --privsep 0
```

Der letzte Befehl gibt die **Token-ID** (`ups@pve!shutdown`) und das **Secret** aus (eine
UUID, wird nur dieses eine Mal angezeigt — jetzt kopieren). Beides im Wizard unter
**Proxmox-Hosts** eintragen (API-URL ist `https://<host-ip>:8006`) und die Verbindung mit
**Test** prüfen.

- **Im Cluster die Befehle nur einmal ausführen, auf einem beliebigen Knoten.** Benutzer,
  API-Token und ACLs liegen auf Datacenter-Ebene (`/etc/pve`) und gelten damit auf jedem
  Knoten — bei jedem Host-Eintrag *dieselbe* Token-ID und dasselbe Secret eintragen. Bei
  Standalone-Hosts ohne gemeinsamen Cluster die Befehle je Host wiederholen.
- Jedem Host-Eintrag **seine eigene API-URL** geben (`https://<ip-dieses-knotens>:8006`).
  Die API würde eine Anfrage zwar an einen anderen Knoten weiterreichen, aber ein bereits
  heruntergefahrener Knoten kann das für die nachfolgenden nicht mehr tun.
- **TLS prüfen** aus lassen, solange der Host das selbstsignierte Proxmox-Zertifikat nutzt.
- Der Token ist jederzeit widerrufbar: `pveum user token remove ups@pve shutdown`.

### Proxmox Backup Server

Diese Befehle in der Shell des Backup Servers ausführen. Die ACL wird **zweimal** vergeben
— einmal an den Benutzer, einmal an das Token:

```bash
proxmox-backup-manager user create ups@pbs
proxmox-backup-manager user generate-token ups@pbs shutdown
proxmox-backup-manager acl update /system/status Admin --auth-id 'ups@pbs'
proxmox-backup-manager acl update /system/status Admin --auth-id 'ups@pbs!shutdown'
```

Als API-URL `https://<pbs-ip>:8007` eintragen und mit **Test** prüfen. Drei Dinge
unterscheiden sich von Proxmox VE und erklären die Befehle oben:

- **Beide ACL-Einträge sind nötig.** Die Rechte eines Tokens werden aus dessen eigenen
  ACL-Einträgen berechnet und anschließend mit denen seines Benutzers geschnitten — ein
  Token kann seinen Benutzer also nie übersteigen. Fehlt einer der beiden, hat das Token
  das Recht nicht.
- **Die Rolle ist `Admin`.** PBS kennt keine feingranulare Power-Rolle —
  `Sys.PowerManagement` trägt nur `Admin`. Die Vergabe auf `/system/status` statt auf `/`
  hält den Umfang so eng, wie PBS es zulässt.
- **Das Feld „Node" ist nur eine Bezeichnung.** PBS ignoriert den Node im API-Pfad, der
  Shutdown geht immer an `/nodes/localhost/status`; nichts muss zum Hostnamen passen.

> [!WARNING]
> **Gut abwägen, ob ein Backup Server hier überhaupt eingebunden werden soll.** Das obige
> Token ist ein `Admin`-Token, und PVE-UPS muss die PBS-API aus genau der Umgebung heraus
> erreichen, die es schützt. Das verbreitert den Weg von einem kompromittierten
> Virtualisierungshost zu den Backups — also zu dem, was eine solche Kompromittierung
> gerade überleben soll. Für Produktivumgebungen **empfehlen wir daher, PBS nicht** an
> dieselbe PVE-UPS-Instanz zu hängen, die die PVE-Hosts verwaltet. Alternativen: dem Backup
> Server einen eigenen Shutdown-Weg geben (eigene PVE-UPS-Instanz, nur aus einem getrennten
> Management-Segment erreichbar) oder ihn kurze Ausfälle auf seiner eigenen USV-Laufzeit
> aussitzen lassen.

## Funktionen

- **Mehrere USVs** pro Instanz mit Host↔USV-Zuordnung und Logik pro Host
  (**UND** = redundante Netzteile, **ODER** = aufgeteilte Last), inkl. Live-Schaubild.
- **Zwei Ziel-Typen**, in einer Instanz mischbar: **Proxmox-VE**-Knoten und ein **Proxmox
  Backup Server** — je Host-Eintrag wählbar, jeweils mit eigenem Token-Schema und eigener
  Rechteprüfung (vor dem Anbinden eines PBS in Produktion aber bitte die Warnung oben
  lesen).
- **Zwei USV-Quellen**, in einer Instanz frei mischbar:
  - **SNMP v1/v2c und v3** (authPriv), nur lesend. Liest die Standard-RFC-1628-UPS-MIB
    oder eine **Hersteller-MIB** — derzeit **APC PowerNet**, womit APC-Karten
    funktionieren, die RFC 1628 nur teilweise (NMC2 unter Firmware sumx/sy v5.1.7) oder gar
    nicht (NMC1: AP9617/AP9618/AP9619) implementieren, und **CyberPower** (CPS-MIB,
    RMCARD-Familie), Wert für Wert gelesen für Karten, die keine Anfrage nach mehreren Werten
    auf einmal beantworten. Wird je USV automatisch erkannt und lässt sich von Hand
    festlegen.
  - **NUT-Server** (TCP 3493) als nur-lesender Client — für USVs ohne Netzwerkkarte.
    Funktioniert mit dem eingebauten USV-Server einer Synology/QNAP/TrueNAS, einem
    Raspberry Pi, OPNsense oder einem NUT auf einem Proxmox-Host. QNAP und Synology geben
    ihre Werte fest vor (QNAP: USV-Name `qnapups`, Benutzer `admin`, Kennwort `123456`;
    Synology: USV-Name und Benutzer `ups`, kein Kennwort) und antworten nur Hosts, die in
    ihrer Freigabeliste stehen — siehe Handbuch, Abschnitt 5.
- **Web-Wizard** für USVs, Hosts, Schwellwerte und Benachrichtigungen — mit Test-Buttons;
  der USV-Test schlüsselt sein Ergebnis je Objekt auf, sodass fehlende OID bzw.
  NUT-Variable, falsche Zugangsdaten und blockierter Port auf einen Blick unterscheidbar
  sind. Er benennt außerdem die Auslöser, die das Gerät gar nicht bedienen kann — so
  bleibt keine Schwelle stillschweigend wirkungslos.
- **Zweisprachige Oberfläche**: Englisch (Standard) und Deutsch, automatisch passend
  zur Browsersprache; eingebautes Benutzerhandbuch (beide Sprachen).
- **Schwellen-Overrides je USV** zusätzlich zu den globalen Standardwerten.
- **Reiter „Verlauf“**: Restlaufzeit, Last und Ladung jeder USV im Zeitverlauf (1 Stunde
  bis 90 Tage oder frei gewählter Beginn und frei gewähltes Ende), mit Ausfällen, Auslösern und Nicht-Erreichbarkeit als farbigen Bändern,
  Markierungen aus dem Ereignisprotokoll, Zoom per Ziehen, Ausfall-Tabelle und CSV-Export.
  Wird eine einstellbare Anzahl Tage in einer eigenen kleinen Datenbank aufbewahrt und im
  Hintergrund geschrieben, berührt also nie den Shutdown-Pfad; abschaltbar. Bei
  Neuinstallationen an, nach einem Update aus, bis es eingeschaltet wird.
- **Cluster-Vorbereitung für Proxmox VE** (**Beta**, setzt Proxmox VE **9.2+** voraus):
  einmal je Cluster, bevor dessen erster Knoten heruntergefahren wird, wird der HA-Manager
  disarmt, damit keine Dienste auf Knoten verschoben werden, die selbst gerade
  herunterfahren. Überprüft statt angenommen, unter einem harten Zeitlimit, und die
  Schaltfläche **„Cluster wiederherstellen"** macht es danach rückgängig. Weil diese
  Vorbereitung clusterweit wirkt, der Shutdown aber hostweise, fährt
  **„Gesamten Cluster als Einheit herunterfahren"** (standardmäßig an) alle Knoten des
  Clusters herunter, sobald einer davon fällig ist — sonst bleibt bei einer einzelnen
  ausfallenden USV ein halbierter Cluster zurück. Auf Wunsch werden vor dem ersten Knoten
  **alle Gäste des Clusters gleichzeitig zum Herunterfahren aufgefordert** („Alle Gäste
  clusterweit zuerst stoppen", standardmäßig aus): schneller, als wenn jeder Knoten seine
  Gäste in umgekehrter Startreihenfolge abarbeitet, wenn der Akku knapp ist — um den Preis,
  dass diese Reihenfolge ignoriert wird. Als Beta gekennzeichnet, solange
  Praxiserfahrung gesammelt wird; durchgehend opt-in, Rückmeldungen gern über
  [Issues](https://github.com/ffind-dev/pve-ups/issues).
- **Hyperkonvergente Cluster (Ceph)** (**Beta**): Mit eingeschalteter Ceph-Option folgt
  PVE-UPS der offiziellen Proxmox-Reihenfolge — HA disarmen, dann **alle Gäste des Clusters
  stoppen**, dann die Wartungsflags (`noout,nobackfill,norecover,norebalance`) setzen. Dass
  die Gäste zuerst gestoppt werden, macht einen solchen Cluster überhaupt erst
  überlebensfähig: Knoten für Knoten fällt der Pool unter `min_size`, während noch Gäste
  laufen, deren IO blockiert und der letzte Knoten nie ausgeht. **Standardmäßig aus** und auf
  Clustern ohne Ceph vollständig übersprungen. Es braucht drei weitere Rechte, der eigene
  Gast der Appliance wird aus einer Liste ausgewählt, damit er nie gestoppt wird, und dieser
  Gast darf nicht auf Ceph-Storage liegen — der Installer lehnt das ab. „Als Einheit" hier
  an lassen: Ein halb heruntergefahrener hyperkonvergenter Cluster verliert sein
  Monitor-Quorum und hat dann auch keinen funktionierenden Storage mehr.
- **Webhook-Benachrichtigungen** bei wichtigen Ereignissen — beliebig viele Ziele, jedes
  mit eigenem Format (vollständiges Status-JSON, **Microsoft Teams**, **Slack**,
  **Discord**, **ntfy**, Klartext oder eine **eigene Vorlage** mit Platzhalter-Ersetzung),
  Stufenfilter, optionalem Authentifizierungs-Header samt beliebigen weiteren Headern, die
  das Ziel braucht (etwa für Nextcloud Talk), und Testversand. Der Versand läuft
  parallel und unter einer gemeinsamen Obergrenze; ein nicht erreichbares Ziel kostet den
  anderen also weder ihre Meldung noch dem Shutdown Akkulaufzeit. Ein Ziel, das nicht mehr
  funktioniert, wird wie ein kaputtes Shutdown-Credential gemeldet: als Ereignis, als
  Hinweis auf seiner Karte und als Zeile im Dashboard — ein Alarm, den niemand bekommt,
  ist einer, auf den niemand reagiert.
- **REST-Status** (`/api/status`, `/api/health`) — lesend, ohne Auth, ohne Secrets;
  Ereignisprotokoll der letzten 48 h inklusive. `/api/health` meldet zusätzlich, wie viele
  Shutdown-Ziele ihren letzten Selbsttest bestanden haben, wie viele
  Benachrichtigungsziele zustellen und ob der Dry-Run noch an ist — alle drei sind reine
  Monitoring-Angaben und verändern weder den gemeldeten Status noch den HTTP-Code.
  Ereignis-/Webhook-Texte sind einheitlich englisch.
- **Konfigurations-Export/-Import**, NTP/Zeitzone, regelmäßiger Selbsttest je Ziel
  (Startzeit plus Intervall von 15 min bis 24 h; das Ergebnis wird je Host gemerkt und im
  Dashboard angezeigt), In-Place-**Updates per Paket-Upload** im Webinterface.

## Sicherheitsmodell

- **Fail-safe als Standard:** ein Kontaktverlust zur USV ist *kein* bestätigter
  Stromausfall — er löst Alarm aus und fährt nie etwas herunter. Das gilt genauso für
  einen NUT-Server, der wegen eines abgestürzten Treibers veraltete Daten liefert: das
  zählt als nicht erreichbar, nie als „Netzbetrieb". Zwei explizite Opt-ins
  verfeinern das: einen bestätigten Akkubetrieb-Countdown über den Verbindungsverlust
  hinweg fortsetzen (Standard an) und einen anhaltenden reinen Kommunikationsverlust doch
  als Ausfall behandeln (Standard aus) — Letzteres greift nur bei Stille, ein Gerät mit
  unbrauchbarer Antwort ist also ein Alarm, aber nie ein Kommunikationsverlust.
- **Dry-Run als Standard:** nach der Installation protokolliert die Engine nur, was sie
  tun würde. Ein **Test-Shutdown** simuliert die Abschaltreihenfolge ohne Wirkung. Weil
  das der leiseste Fehler überhaupt ist — vollständig konfiguriert, Selbsttest grün, und
  es wird trotzdem nie etwas heruntergefahren — sagen Dashboard, Ereignisprotokoll und
  `/api/health` es, solange er an ist.
- **Die Ausfalldauer wird auf der eigenen Uhr dieser Appliance gemessen**, nie über den
  Zähler, den die USV meldet: RFC 1628 lässt diesen Wert im Netzbetrieb undefiniert, und
  Karten, die den Wert des letzten Umschaltens behalten, lösten den Shutdown in der ersten
  Sekunde eines Ausfalls aus.
- **Ein fehlgeschlagener Shutdown wird wiederholt** (drei Versuche), statt die Maschine zu
  kosten: ein 503 von einem ausgelasteten `pveproxy` oder eine abgelaufene Frist ist kein
  Beweis dafür, dass der Knoten heruntergefahren wird.
- Ein ausgelöster Trigger und der Akkubetrieb-Countdown werden **auf Platte persistiert**
  und überstehen einen Dienst-Neustart. Was aus dieser Datei zurückgelesen wird, darf
  allerdings nichts von sich aus auslösen — es wartet darauf, dass ein Poll des neuen
  Prozesses den laufenden Ausfall bestätigt.
- **Von selbst bereit für den nächsten Ausfall:** Ein tatsächlich gesendeter Shutdown bleibt
  gesperrt, damit eine herunterfahrende Maschine den Befehl nie zweimal bekommt. Die Sperre
  löst sich, sobald jede USV fünf Minuten lang erreichbar und am Netz war (einstellbar,
  oder aus zugunsten des Knopfes „Zustand zurücksetzen") — ein Netz, das binnen einer Minute
  zweimal einbricht, darf dazwischen nicht wieder scharf werden, und eine nicht erreichbare
  USV gilt nie als zurückgekehrtes Netz.
- **„Eigener Host zuletzt":** der Host, der die Appliance trägt, fährt immer zuletzt
  herunter.
- **Ziele blockieren sich nicht gegenseitig:** Hosts mit gleicher Abschaltreihenfolge
  werden gleichzeitig angewiesen, jeder Aufruf hat eine harte Obergrenze, und der eigene
  Host bildet weiterhin die letzte Stufe — eine Maschine, die nicht mehr antwortet, kann
  die übrigen und den Akku-Countdown damit nicht aufhalten.
- Die App läuft **unprivilegiert**; ein schmaler privilegierter Begleiter wendet Updates
  und NTP-/Zeitzonen-Änderungen an. Secrets verlassen die Appliance nie über die API.

## Standard-Auslöser

Es genügt **eine** zutreffende Bedingung (im Wizard änderbar; Feld leeren = aus):

| Bedingung | Standard |
|---|---|
| Akkubetrieb länger als | 600 s |
| Restlaufzeit unter | 10 min |
| Ladestand unter | 30 % |
| USV meldet `battery low/depleted` | an |

Poll-Intervall: 30 s im Netzbetrieb, 8 s im Akkubetrieb.

## Updates

Das Release-Paket (`pve-usv-<version>.tar.gz`) von der
[Release-Seite](https://github.com/ffind-dev/pve-ups/releases) herunterladen und im
Webinterface unter **Update** hochladen. Die Konfiguration bleibt erhalten; der Dienst
startet automatisch neu. Das Update von 2.x funktioniert genauso (die zwei
Verhaltensänderungen stehen im Handbuch).
Läuft die Appliance in Docker? Siehe [Docker](#docker-alternative-bereitstellung) oben —
dort erfolgen Updates über ein neues Image-Tag.

> **Hinweis:** Der Produktname ist PVE-UPS, technisch heißen Dienst und Pfade aber
> `pve-usv` (`systemctl status pve-usv`, `/etc/pve-usv/config.yaml`,
> `/var/lib/pve-usv/`). Das ist beabsichtigt und hält bestehende Installationen kompatibel.

## Entwickeln / testen ohne Hardware

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                       # Unit-Tests, keine Hardware nötig

# SNMP-USV simulieren (separates Terminal); Snapshots unter ./snmpdata/:
snmpsim-command-responder --data-dir=./snmpdata --agent-udpv4-endpoint=127.0.0.1:1161

# ... oder einen NUT-Server simulieren:
python -m tests.nutsim --port 3493 --scenario battery   # mains | battery | low | sparse

PVE_USV_CONFIG=./dev-config.yaml PVE_USV_DB=./dev-events.db python -m app.main
# UI: http://127.0.0.1:8080
#   SNMP: Host 127.0.0.1, Port 1161, Community "public"  -> Netzbetrieb (100 %)
#                                    Community "battery" -> Stromausfall -> Auslöser greifen
#         Die APC-Snapshots enthalten nur PowerNet-OIDs, also eine Karte ohne RFC 1628:
#                                    Community "apc"         -> Netzbetrieb, MIB wird APC
#                                    Community "apc-battery" -> Stromausfall auf der APC-MIB
#         Ebenso für CyberPower (nur CPS-MIB, Wert für Wert abgefragt):
#                                    Community "cyberpower"         -> Netzbetrieb, MIB wird CyberPower
#                                    Community "cyberpower-battery" -> Stromausfall auf der CyberPower-MIB
#   NUT:  Host 127.0.0.1, Port 3493, USV-Name "ups"
```

## Grenzen / Annahmen

- Hosts werden **einzeln** über ihre jeweils eigene API heruntergefahren. Für einen
  **Proxmox-VE-Cluster** lässt sich der Shutdown vorab einmal je Cluster vorbereiten
  (HA-Disarm, clusterweites Herunterfahren der Gäste und die Ceph-Wartungsflags, siehe
  Funktionsliste); darüber hinaus löst PVE-UPS keine Abhängigkeiten zwischen Knoten auf und
  verwaltet kein Quorum. Die Knoten-Reihenfolge bleibt manuell — ein MON-Knoten, der zuletzt
  gehen sollte, wird gemeldet, nicht umsortiert —, und die Gäste werden parallel gestoppt,
  nicht in ihrer `startup`-Reihenfolge. Die Vorbereitung beginnt, sobald der *erste* Knoten
  eines Clusters fällig ist — genau dafür gibt es „Gesamten Cluster als Einheit
  herunterfahren": Es hält Shutdown und Vorbereitung bei derselben Menge Maschinen. Diese
  Vorbereitung ist in 4.0.0 **Beta**: Der Mechanismus überprüft jeden Schritt und wählt im
  Zweifel die sichere Variante, hat bisher aber wenige echte Cluster gesehen.
- Shutdown-Ziele sind **Proxmox VE und Proxmox Backup Server**. Proxmox Mail Gateway und
  Datacenter Manager sind nicht umgesetzt: beide sprechen ihr eigenes Token-Schema, und
  ungetesteter Support wäre schlechter als keiner.
- Liest die Standard-RFC-1628-UPS-MIB, die MIBs von APC PowerNet und CyberPower oder die
  Variablen eines NUT-Servers. Weitere Hersteller-MIBs sind noch nicht umgesetzt — ein Gerät außerhalb
  davon braucht entweder RFC 1628 oder einen NUT-Treiber. Die Appliance selbst spricht kein
  USB/seriell; eine lokal angeschlossene USV wird über einen NUT-Server erreicht.
- Das NUT-Protokoll ist unverschlüsselt. Es gehört in ein vertrauenswürdiges Netz — oder
  auf einen `upsd`, der nur auf dem Loopback-Interface derselben Maschine lauscht.
- In der optionalen Docker-Bereitstellung sind In-App-Updates und NTP-/Zeitzonen-Verwaltung
  nicht verfügbar (siehe [Docker](#docker-alternative-bereitstellung)) — alles Weitere ist
  identisch.

## Lizenz

MIT — Copyright © 2026 Florian Finder. Siehe [LICENSE](LICENSE).

<sub>Entwickelt mit KI-Unterstützung.</sub>
