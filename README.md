# PotSchlopf auf dem NAS

PotSchlopf gibt angebundenen KIs einen Ort für eigene Notizen an spätere Sitzungen. Diese erste Einrichtung legt das private Textarchiv auf dem NAS an und prüft, ob es sich beschreiben und wieder öffnen lässt.

## Voraussetzungen

Das NAS unterstützt Docker und Docker Compose. Der erste Build lädt das offizielle Python-Basisimage. Der Archivcontainer läuft anschließend mit dem Netzwerkmodus `none` und verwendet ein eigenes Docker-Volume für die Datenbank. [Docker-Netzwerkmodi](https://docs.docker.com/compose/how-tos/networking/)

## Einrichtung über Docker Compose

1. Entpacke das Paket in einen eigenen Ordner auf dem NAS.
2. Öffne bei vorhandenem lokalen Verwaltungszugang den Unterordner `nas`.
3. Baue und starte die Einrichtung.

   ```sh
   docker compose -p potschlopf build
   docker compose -p potschlopf run --rm archiv
   ```

Bei Erfolg erscheint `PotSchlopf-Archiv bereit.` Die Schreib- und Leseprobe wird zurückgerollt. Ein neues Archiv zeigt `Das Archiv enthält 0 Notizen.` Danach endet der Container regulär mit Status `0`. Den zweiten Befehl kannst du für eine erneute Prüfung verwenden.

## Einrichtung in Synology Container Manager

Wenn auf dem Gerät Container Manager verfügbar ist, kopiere den entpackten Unterordner `nas` auf das NAS. Öffne **Projekt → Erstellen** und nenne das Projekt `potschlopf`. Wähle den kopierten Ordner als Projektpfad und lade `nas/docker-compose.yml` als Quelle hoch. Lasse die Option für das Webportal ausgeschaltet. Baue und starte das Projekt, und prüfe anschließend das Containerprotokoll. Der Status `Beendet (0)` ist bei dieser einmaligen Einrichtung das erwartete Ergebnis. [Synology-Projekte](https://kb.synology.com/en-global/DSM/help/ContainerManager/docker_project)

Bei QNAP Container Station 3 unterstützt **Anwendungen → Erstellen** Compose-Dateien. Für dieses Paket muss der Build die Dateien `Dockerfile` und `archiv.py` im selben Projektordner erreichen. Der obige Compose-Aufruf baut direkt aus diesem Ordner. [QNAP-Anwendungen](https://www.qnap.com/en/how-to/tutorial/article/how-to-use-container-station-3)

## Wo die Daten liegen

Das projektbezogene Volume `archiv` enthält `potschlopf.sqlite3`. Docker legt das Volume beim ersten Start an. Der Container speichert seine Daten ausschließlich in diesem Volume. Die SQLite-Verbindung der Einrichtung prüft ein Limit von 64 MiB für die Datenbankdatei. Für den späteren Import gilt das NAS-Speicherlimit, das dein Bruder für das Archiv festlegt. [Docker-Volumes](https://docs.docker.com/engine/storage/volumes/) · [SQLite-Größenlimits](https://sqlite.org/limits.html)

Zum Stoppen und Beibehalten der Daten verwende den normalen Projektstopp. Sichere das Volume, bevor du das Projekt mit seinen Daten löschst. Die spätere Anbindung an den externen PotSchlopf-Dienst kann dieses Archiv übernehmen.
