"""
KAGIS Höhendaten Bulk-Download - QGIS Processing Tool
=======================================================
Lädt für ein gewähltes Gebiet Rasterkacheln über die KAGIS-STAC-API
(https://gis.ktn.gv.at/api/stac/v1/) herunter - automatisch über mehrere
Collections hinweg, falls ein Zyklus (z.B. ALS2) auf mehrere Regionen
aufgeteilt ist.

Pro ausgewähltem ALS-Zyklus UND pro ausgewähltem Modelltyp (DGM/DOM/DOL)
entsteht ein eigenes VRT-Mosaik und ein eigener Layer im Projekt.

Sofern nicht explizit ein gewünschtes Koordinatenbezugssystem eingestellt
wird, wird EPSG 31258 (MGI / Austria GK M31) verwendet (entsprechend
der Rohdaten).
"""

import json
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsProcessingAlgorithm,
    QgsProcessingParameterCrs,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFolderDestination,
    QgsProject,
    QgsRasterLayer,
)
from osgeo import gdal

# Bestaetigt funktionierend (Live-Test 2026)
# Falls KAGIS die API mal umzieht: https://gis.ktn.gv.at/osgdi/swagger/#/Suche
# zeigt die dann aktuelle Basis-URL.
API_ROOT = "https://gis.ktn.gv.at/api/stac/v1/"

# KAGIS-Rasterdaten liegen durchgehend in EPSG:31258 (MGI / Austria GK M31) -
# bestaetigt sowohl im offiziellen KAGIS-Benutzerleitfaden als auch im
# data.gv.at-Datensatz fuer das landesweite DGM/DOM. Kaernten liegt komplett
# in dieser einen GK-Zone. Da dieses Tool ausschliesslich fuer KAGIS-Daten
# gedacht ist, wird die Projektion beim VRT-Bau direkt darauf gesetzt statt
# sie (unzuverlaessig) aus den Original-Kacheln auszulesen - GDAL kopiert
# deren eingebettete WKT zwar anstandslos, aber offenbar ohne fuer QGIS
# erkennbare EPSG-Referenz.
KAGIS_SOURCE_CRS = "EPSG:31258"

# Name -> (Begriffe, die ALLE im Collection-Titel/-id vorkommen muessen,
#          Begriffe, die KEINER vorkommen darf)
COLLECTION_GROUPS = {
    "ALS1": (["ALS 1", "Höhenraster"], ["Punktwolke"]),
    "ALS2": (["ALS 2", "Höhenraster"], ["Punktwolke"]),
}

# Name -> Substring(s), die im Asset-Schluessel vorkommen muessen (z.B. "dgm"
# passt auf "DGM", "dgm_1m" etc. - Gross-/Kleinschreibung wird ignoriert)
ASSET_TYPES = {
    "DGM": ["dgm"],
    "DOM": ["dom"],
    "DOL": ["dol"],
}


def fetch_json(url):
    req = Request(url, headers={"User-Agent": "kagis-qgis-tool/1.0", "Accept": "application/json"})
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_collections(feedback=None):
    url = urljoin(API_ROOT, "collections")
    try:
        doc = fetch_json(url)
    except Exception as e:
        if feedback is not None:
            feedback.reportError(
                f"Konnte Collection-Liste nicht laden ({url}): {e}\n"
                "Falls sich die API-URL geaendert hat: "
                "https://gis.ktn.gv.at/osgdi/swagger/#/Suche zeigt die aktuelle.")
        return []
    return doc.get("collections", doc if isinstance(doc, list) else [])


def search_items(collection_id, aoi_bbox, feedback=None, limit=200):
    """Fragt den /search-Endpunkt ab und folgt "next"-Links fuer Pagination."""
    bbox_str = ",".join(str(v) for v in aoi_bbox)
    next_url = urljoin(API_ROOT, f"search?collections={collection_id}&bbox={bbox_str}&limit={limit}")
    items = []
    seen_urls = set()
    while next_url and next_url not in seen_urls:
        seen_urls.add(next_url)
        try:
            doc = fetch_json(next_url)
        except Exception as e:
            if feedback is not None:
                feedback.reportError(f"Suche fehlgeschlagen fuer '{collection_id}': {e}")
            break
        items.extend(doc.get("features", []))
        next_url = None
        for link in doc.get("links", []):
            if link.get("rel") == "next" and link.get("href"):
                next_url = link["href"]
                break
    return items


def download_item_assets(item, out_dir, asset_key_filter):
    """Laedt passende Assets eines Items. Schreibt jede Datei erst unter einem
    .part-Namen und benennt sie erst nach vollstaendigem, erfolgreichem
    Download um - so kann eine abgebrochene oder fehlgeschlagene Datei nie
    faelschlich als "bereits vorhanden" durchgehen (relevant seit es
    Abbrechen mitten im Download gibt). Ein Fehlschlag wird einmal wiederholt."""
    out = []
    failed = []
    for key, asset in item.get("assets", {}).items():
        href = asset.get("href")
        if not href or "thumbnail" in key.lower():
            continue
        if not any(kw.lower() in key.lower() for kw in asset_key_filter):
            continue
        fname = os.path.basename(href.split("?")[0])
        out_path = os.path.join(out_dir, fname)
        if os.path.exists(out_path):
            out.append(out_path)
            continue

        tmp_path = out_path + ".part"
        ok = False
        for attempt in range(2):
            try:
                req = Request(href, headers={"User-Agent": "kagis-qgis-tool/1.0"})
                with urlopen(req, timeout=120) as resp, open(tmp_path, "wb") as f:
                    f.write(resp.read())
                os.replace(tmp_path, out_path)  # atomar - out_path existiert erst jetzt
                ok = True
                break
            except Exception:
                continue
        if not ok:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            failed.append(href)
            continue
        out.append(out_path)
    return out, failed


def run_downloads(items, out_dir, asset_key_filter, feedback, max_workers=4,
                   progress_base=0, progress_span=100):
    """Laedt Assets fuer alle Items parallel herunter, prueft dabei aber
    laufend (statt nur vor/nach dem Block) ob der Nutzer abgebrochen hat -
    darauf reagiert es innerhalb von ca. 1 Sekunde, statt erst nach dem
    naechsten fertigen Download zu reagieren (as_completed() wuerde sonst
    blockieren, bis der naechste einzelne Download durch ist).

    Der Executor wird bewusst NICHT ueber ein "with"-Statement verwaltet:
    dessen automatisches shutdown() beim Verlassen des Blocks wuerde mit
    wait=True nochmal blockieren, selbst wenn wir beim Abbruch explizit
    wait=False gesetzt haben - das shutdown() passiert hier daher genau
    einmal, kontrolliert, im finally-Block."""
    downloaded = []
    failed = []
    canceled = False
    ex = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futs = {ex.submit(download_item_assets, item, out_dir, asset_key_filter): item for item in items}
        pending = set(futs.keys())
        total = len(pending)
        done_count = 0
        while pending:
            if feedback.isCanceled():
                canceled = True
                feedback.pushInfo(
                    f"Abbruch erkannt - breche {len(pending)} verbleibende Downloads ab ...")
                for f in pending:
                    f.cancel()
                break
            done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            for f in done:
                try:
                    ok, fail = f.result()
                    downloaded.extend(ok)
                    failed.extend(fail)
                except Exception:
                    pass
                done_count += 1
            if total:
                frac = done_count / total
                feedback.setProgress(int(progress_base + frac * progress_span))
    finally:
        ex.shutdown(wait=not canceled, cancel_futures=canceled)
    return downloaded, failed, canceled


class KagisBulkDownload(QgsProcessingAlgorithm):
    EXTENT = "EXTENT"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"
    GROUPS_PARAM = "GROUPS_PARAM"
    ASSET_TYPES_PARAM = "ASSET_TYPES_PARAM"
    TARGET_CRS = "TARGET_CRS"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterExtent(self.EXTENT, "Gebiet (AOI)"))
        self.addParameter(QgsProcessingParameterEnum(
            self.GROUPS_PARAM, "ALS-Zyklen", options=list(COLLECTION_GROUPS.keys()),
            allowMultiple=True, defaultValue=list(range(len(COLLECTION_GROUPS)))))
        self.addParameter(QgsProcessingParameterEnum(
            self.ASSET_TYPES_PARAM, "Modelltyp(en)", options=list(ASSET_TYPES.keys()),
            allowMultiple=True, defaultValue=[0, 1]))  # DGM + DOM vorausgewaehlt
        self.addParameter(QgsProcessingParameterCrs(
            self.TARGET_CRS,
            f"Ziel-CRS (leer lassen = {KAGIS_SOURCE_CRS}, die KAGIS-Original-CRS)",
            optional=True))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUTPUT_FOLDER, "Zielordner"))

    def processAlgorithm(self, parameters, context, feedback):
        extent = self.parameterAsExtent(
            parameters, self.EXTENT, context, QgsCoordinateReferenceSystem("EPSG:4326"))
        aoi_bbox = (extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum())
        out_root = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        group_names = [list(COLLECTION_GROUPS.keys())[i]
                        for i in self.parameterAsEnums(parameters, self.GROUPS_PARAM, context)]
        asset_type_names = [list(ASSET_TYPES.keys())[i]
                             for i in self.parameterAsEnums(parameters, self.ASSET_TYPES_PARAM, context)]
        target_crs = self.parameterAsCrs(parameters, self.TARGET_CRS, context)

        feedback.pushInfo(f"Lade Collection-Liste von {API_ROOT}collections ...")
        all_collections = list_collections(feedback=feedback)
        if not all_collections:
            return {}

        feedback.pushInfo(f"{len(all_collections)} Collection(s) gefunden:")
        for c in all_collections:
            feedback.pushInfo(f"  - {c.get('title') or c.get('id')}  [{c.get('id')}]")

        results = {}
        total_failed = 0
        total_tasks = max(len(group_names) * len(asset_type_names), 1)
        task_index = 0
        outer_canceled = False
        for gname in group_names:
            if outer_canceled or feedback.isCanceled():
                break
            includes, excludes = COLLECTION_GROUPS[gname]
            matched = []
            for c in all_collections:
                title = (c.get("title") or c.get("id") or "").lower()
                if all(k.lower() in title for k in includes) and \
                   not any(k.lower() in title for k in excludes):
                    matched.append(c.get("id"))

            feedback.pushInfo(f"--- {gname}: {len(matched)} passende Collection(s)")
            if not matched:
                feedback.pushWarning(
                    f"{gname}: keine passenden Collections gefunden. Vergleiche die Liste oben "
                    "mit dem Filter und passe COLLECTION_GROUPS im Skript an.")
                continue
            feedback.pushInfo(f"  verwende: {', '.join(matched)}")

            all_items = []
            for cid in matched:
                items = search_items(cid, aoi_bbox, feedback=feedback)
                feedback.pushInfo(f"  {cid}: {len(items)} Item(s) in der AOI")
                all_items.extend(items)

            if not all_items:
                feedback.pushWarning(f"{gname}: keine Kacheln in der AOI gefunden.")
                continue

            sample_keys = list(all_items[0].get("assets", {}).keys())
            feedback.pushInfo(f"  Asset-Schluessel im ersten Item: {sample_keys}")

            # Fuer jeden gewaehlten Modelltyp (DGM/DOM/DOL) aus denselben Items
            # nur die passenden Assets herunterladen -> eigener Ordner + Layer.
            for atype in asset_type_names:
                if feedback.isCanceled():
                    outer_canceled = True
                    break
                asset_filter = ASSET_TYPES[atype]
                label = f"{gname}_{atype}"
                out_dir = os.path.join(out_root, label)
                os.makedirs(out_dir, exist_ok=True)

                task_index += 1
                progress_base = int((task_index - 1) / total_tasks * 100)
                progress_span = 100 / total_tasks
                downloaded, failed, canceled = run_downloads(
                    all_items, out_dir, asset_filter, feedback,
                    progress_base=progress_base, progress_span=progress_span)
                if failed:
                    total_failed += len(failed)
                    feedback.pushWarning(
                        f"{label}: {len(failed)} von {len(all_items)} Downloads endgueltig "
                        "fehlgeschlagen (nach Wiederholung) - im Mosaik fehlen entsprechend Kacheln.")
                if canceled:
                    feedback.pushWarning(f"{label}: abgebrochen, unvollstaendiger Download verworfen.")
                    outer_canceled = True
                    break

                if not downloaded:
                    feedback.pushWarning(
                        f"{label}: keine Assets mit Schluessel-Filter {asset_filter} gefunden "
                        f"(siehe Asset-Schluessel oben - Filter ggf. anpassen).")
                    continue

                vrt_path = os.path.join(out_dir, f"{label}_mosaic.vrt")

                # Projektion direkt beim Bauen erzwingen - fest auf die
                # bekannte KAGIS-CRS, nicht aus den Original-Kacheln gelesen
                # (siehe Kommentar bei KAGIS_SOURCE_CRS oben).
                vrt_options = gdal.BuildVRTOptions(outputSRS=KAGIS_SOURCE_CRS)
                gdal.BuildVRT(vrt_path, downloaded, options=vrt_options)

                # Kontrolle: hat das VRT jetzt tatsaechlich eine Projektion?
                check_ds = gdal.Open(vrt_path)
                final_wkt = check_ds.GetProjection() if check_ds is not None else ""
                check_ds = None
                if final_wkt:
                    feedback.pushInfo(f"{label}: VRT-Projektion gesetzt auf {KAGIS_SOURCE_CRS}.")
                else:
                    feedback.pushWarning(f"{label}: VRT hat auch nach outputSRS KEINE Projektion - bitte melden!")

                feedback.pushInfo(f"{label}: VRT erstellt ({len(downloaded)} Dateien, {KAGIS_SOURCE_CRS}) -> {vrt_path}")

                final_path = vrt_path
                if target_crs.isValid():
                    safe_authid = target_crs.authid().replace(":", "_") or "custom_crs"
                    warped_path = os.path.join(out_dir, f"{label}_mosaic_{safe_authid}.vrt")
                    gdal.Warp(warped_path, vrt_path, dstSRS=target_crs.toWkt(),
                              format="VRT", resampleAlg="near")
                    feedback.pushInfo(f"{label}: nach {target_crs.authid()} umprojiziert -> {warped_path}")
                    final_path = warped_path

                results[label] = final_path

        if outer_canceled:
            feedback.pushInfo(
                f"Abgebrochen - {len(results)} bereits vollstaendig heruntergeladene Layer werden trotzdem hinzugefuegt.")

        for label, vrt_path in results.items():
            layer = QgsRasterLayer(vrt_path, label)
            if layer.isValid():
                if not layer.crs().isValid():
                    layer.setCrs(QgsCoordinateReferenceSystem(KAGIS_SOURCE_CRS))
                    feedback.pushInfo(
                        f"Layer '{label}': CRS wurde nicht automatisch erkannt - manuell auf "
                        f"{KAGIS_SOURCE_CRS} gesetzt.")
                else:
                    feedback.pushInfo(f"Layer '{label}': CRS automatisch erkannt ({layer.crs().authid()}).")
                QgsProject.instance().addMapLayer(layer)
                feedback.pushInfo(f"Layer '{label}' zum Projekt hinzugefuegt.")
            else:
                feedback.pushWarning(f"Layer '{label}' konnte nicht geladen werden: {vrt_path}")

        summary = f"Fertig: {len(results)} Layer hinzugefuegt."
        if total_failed:
            summary += f" ACHTUNG: insgesamt {total_failed} Downloads endgueltig fehlgeschlagen - Mosaike ggf. lueckenhaft."
        if outer_canceled:
            summary += " Lauf wurde vom Nutzer abgebrochen."
        feedback.pushInfo(summary)

        return {}

    def name(self):
        return "kagis_bulk_download"

    def displayName(self):
        return "KAGIS Höhendaten Bulk-Download"

    def group(self):
        return "KAGIS"

    def groupId(self):
        return "kagis"

    def shortHelpString(self):
        return (
            "<p>Lädt Höhenraster-Kacheln über die KAGIS-STAC-API für das gewählte "
            "Gebiet herunter - über mehrere Collections hinweg, falls ein Zyklus "
            "(z.B. ALS2) auf mehrere Regionen aufgeteilt ist.</p>"
            "<p>Pro Zyklus UND pro gewähltem Modelltyp (DGM/DOM/DOL) entsteht ein "
            "eigener Mosaik-Layer.</p>"
            f"<p>Die Original-CRS wird fest als {KAGIS_SOURCE_CRS} angenommen "
            "(bestätigt für KAGIS-Rasterdaten); mit Ziel-CRS wird zusätzlich ein "
            "virtuell umprojiziertes VRT erzeugt.</p>"
            "<p>Quelle: Land Kärnten - KAGIS, https://kagis.ktn.gv.at, CC-BY-4.0</p>"
        )

    def createInstance(self):
        return KagisBulkDownload()
