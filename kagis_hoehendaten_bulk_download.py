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
wird, bleiben die Kacheln in ihrem jeweiligen tatsächlichen Original-CRS,
nämlich ALS1 in EPSG:31258 (MGI / Austria GK M31) und ALS2 in EPSG:31255
(MGI / Austria GK Central).
"""

import json
import os
import re
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
from osgeo import gdal, osr

# GDAL >= 3.7 warnt, wenn weder UseExceptions() noch DontUseExceptions()
# explizit aufgerufen wurde - ab GDAL 4.0 werden Exceptions Standard sein.
# Explizit aktivieren (wie in QGIS' eigenen gebuendelten GDAL-Algorithmen).
gdal.UseExceptions()
osr.UseExceptions()

# Bestaetigt funktionierend (Live-Test 2026)
# Falls KAGIS die API mal umzieht: https://gis.ktn.gv.at/osgdi/swagger/#/Suche
# zeigt die dann aktuelle Basis-URL.
API_ROOT = "https://gis.ktn.gv.at/api/stac/v1/"

# WICHTIG: KAGIS-Rasterdaten liegen NICHT einheitlich in EPSG:31258.
# ALS1-Kacheln liegen in EPSG:31258 (MGI / Austria GK M31), ALS2-Kacheln
# in EPSG:31255 (MGI / Austria GK Central). Die tatsaechliche CRS wird
# deshalb immer direkt aus einer echten heruntergeladenen Kachel gelesen
# (siehe VRT-Bau weiter unten), NICHT aus dieser Konstante. KAGIS_SOURCE_CRS
# dient nur noch als Fallback-Wert (falls eine Kachel gar keine Projektion
# hat) und fuer die AOI-Info-Ausgabe.
KAGIS_SOURCE_CRS = "EPSG:31258"

# Bekannte oesterreichische MGI-GK-Bezeichnungen (Ferro- und Greenwich-
# referenziert) als Rueckfallebene, falls osr.AutoIdentifyEPSG() eine Kachel-
# CRS nicht erkennt - das kann schon an winzigen Gleitkomma-Abweichungen im
# Ellipsoid scheitern (beobachtet bei ALS1: 299.152812800003 statt
# 299.1528128), obwohl der CRS-NAME eindeutig ist.
KNOWN_MGI_CRS_NAMES = {
    "MGI / Austria GK West": "31254",
    "MGI / Austria GK Central": "31255",
    "MGI / Austria GK East": "31256",
    "MGI / Austria GK M28": "31257",
    "MGI / Austria GK M31": "31258",
    "MGI / Austria GK M34": "31259",
}

# Letzte Rueckfallebene: dieses Tool ist ausschliesslich fuer ALS1/ALS2
# gedacht, und deren native CRS ist bekannt.
KNOWN_GROUP_CRS = {
    "ALS1": "31258",
    "ALS2": "31255",
}

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


def bbox_overlap(a, b):
    """True, wenn sich zwei (west, sued, ost, nord)-Boxen ueberschneiden."""
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def raster_extent(path):
    """Liest die tatsaechliche Ausdehnung einer heruntergeladenen Kachel
    direkt aus ihrer eigenen Georeferenzierung (nicht aus STAC-Metadaten) -
    das ist die einzige Quelle, die KAGIS nicht versehentlich falsch angeben
    kann, da sie aus den Pixeldaten selbst kommt. Rechnet dabei explizit
    nach EPSG:4326 um, unter Verwendung der EIGENEN eingebetteten CRS der
    Datei - falls einzelne Kacheln (z.B. bei ALS2 beobachtet) intern eine
    leicht abweichende CRS-Kodierung tragen, waere ein direkter
    Rohwerte-Vergleich sonst falsch. EPSG:4326 wird als Vergleichs-CRS
    gewaehlt, weil sich das im separaten Verifikations-Script bei allen
    249 getesteten Kacheln als zuverlaessig erwiesen hat."""
    try:
        ds = gdal.Open(path)
        if ds is None:
            return None
        gt = ds.GetGeoTransform()
        xsize, ysize = ds.RasterXSize, ds.RasterYSize
        src_wkt = ds.GetProjection()
        ds = None
    except Exception:
        return None

    xs = (gt[0], gt[0] + xsize * gt[1] + ysize * gt[2])
    ys = (gt[3], gt[3] + xsize * gt[4] + ysize * gt[5])
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)

    if not src_wkt:
        return None
    try:
        src_srs = osr.SpatialReference()
        src_srs.ImportFromWkt(src_wkt)
        src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        dst_srs = osr.SpatialReference()
        dst_srs.SetFromUserInput("EPSG:4326")
        dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        tr = osr.CoordinateTransformation(src_srs, dst_srs)
        lons, lats = [], []
        for x, y in [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]:
            lon, lat, _ = tr.TransformPoint(x, y)
            lons.append(lon)
            lats.append(lat)
        return (min(lons), min(lats), max(lons), max(lats))
    except Exception:
        return None


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


# Grober, aber grosszuegiger Gueltigkeitsbereich fuer Kaernten in EPSG:4326.
# Dient nur dazu, eine fehlgeschlagene/nicht durchgefuehrte CRS-Umrechnung
# zu erkennen, die sonst still falsche (unveraenderte) Koordinaten an die
# STAC-Suche durchreichen wuerde.
KAERNTEN_4326_SANITY_BOUNDS = (12.0, 46.0, 15.5, 47.5)


def looks_like_valid_4326_kaernten(bbox):
    ax0, ay0, ax1, ay1 = KAERNTEN_4326_SANITY_BOUNDS
    bx0, by0, bx1, by1 = bbox
    return not (bx1 < ax0 or bx0 > ax1 or by1 < ay0 or by0 > ay1)


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


def download_item_assets(item, out_dir, asset_key_filter, feedback=None):
    """Laedt passende Assets eines Items. Schreibt jede Datei erst unter einem
    .part-Namen und benennt sie erst nach vollstaendigem, erfolgreichem
    Download um - so kann eine abgebrochene oder fehlgeschlagene Datei nie
    faelschlich als "bereits vorhanden" durchgehen (relevant seit es
    Abbrechen mitten im Download gibt). Ein Fehlschlag wird einmal wiederholt.
    Liest in Chunks und prueft dabei laufend auf Abbruch, statt in einem
    einzigen blockierenden resp.read() - so kann ein Abbruch auch einen
    schon laufenden Download tatsaechlich stoppen, nicht nur noch nicht
    gestartete."""
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
            if feedback is not None and feedback.isCanceled():
                break
            try:
                req = Request(href, headers={"User-Agent": "kagis-qgis-tool/1.0"})
                with urlopen(req, timeout=120) as resp, open(tmp_path, "wb") as f:
                    while True:
                        if feedback is not None and feedback.isCanceled():
                            raise RuntimeError("abgebrochen")
                        chunk = resp.read(1 << 18)
                        if not chunk:
                            break
                        f.write(chunk)
                os.replace(tmp_path, out_path)  # atomar - out_path existiert erst jetzt
                ok = True
                break
            except Exception:
                if feedback is not None and feedback.isCanceled():
                    break  # kein Retry mehr, wenn der Nutzer abgebrochen hat
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
        futs = {ex.submit(download_item_assets, item, out_dir, asset_key_filter, feedback): item for item in items}
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
            "Ziel-CRS (leer lassen = jeweilige Original-CRS der Kacheln, "
            "ALS1=EPSG:31258, ALS2=EPSG:31255)",
            optional=True))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUTPUT_FOLDER, "Zielordner"))

    def processAlgorithm(self, parameters, context, feedback):
        # WICHTIG: Die QgsRectangle-Accessoren (.xMinimum() usw.), die
        # parameterAsExtent() liefert, vertauschen fuer dieses Projekt/CRS
        # nachweislich die MITTLEREN beiden Werte (ymin und xmax) beim
        # Umwandeln des Eingabe-Strings - reproduzierbar, positionsbasiert,
        # unabhaengig von den tatsaechlichen Koordinatenwerten (verifiziert
        # per Test: der rohe Eingabe-String selbst ist immer korrekt in der
        # Reihenfolge xmin,ymin,xmax,ymax, aber das daraus konstruierte
        # QgsRectangle-Objekt gibt bei .yMinimum()/.xMaximum() vertauschte
        # Werte zurueck). Deshalb wird hier der rohe Parameter-String selbst
        # geparst, statt den Objekt-Accessoren zu vertrauen. Fallback auf die
        # normalen QGIS-Methoden, falls der Rohwert (z.B. bei Aufruf aus dem
        # Modeler) kein String ist.
        raw_value = parameters.get(self.EXTENT)
        extent_crs = self.parameterAsExtentCrs(parameters, self.EXTENT, context)
        match = re.match(
            r"\s*([\-0-9.eE]+)\s*,\s*([\-0-9.eE]+)\s*,\s*([\-0-9.eE]+)\s*,\s*([\-0-9.eE]+)"
            r"\s*(?:\[\s*([^\]]+?)\s*\])?\s*$",
            raw_value) if isinstance(raw_value, str) else None
        if match:
            orig_bbox = tuple(float(match.group(i)) for i in range(1, 5))
            if match.group(5):
                extent_crs = QgsCoordinateReferenceSystem(match.group(5))
        else:
            extent_raw = self.parameterAsExtent(parameters, self.EXTENT, context)
            orig_bbox = (extent_raw.xMinimum(), extent_raw.yMinimum(),
                         extent_raw.xMaximum(), extent_raw.yMaximum())
        feedback.pushInfo(
            f"AOI in Original-CRS ({extent_crs.authid()}): west={orig_bbox[0]:.4f}, "
            f"sued={orig_bbox[1]:.4f}, ost={orig_bbox[2]:.4f}, nord={orig_bbox[3]:.4f}")

        def reproject_bbox_osr(bbox, src_authid, dst_authid):
            src_srs = osr.SpatialReference()
            src_srs.SetFromUserInput(src_authid)
            src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            dst_srs = osr.SpatialReference()
            dst_srs.SetFromUserInput(dst_authid)
            dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            tr = osr.CoordinateTransformation(src_srs, dst_srs)
            corners = [(bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[2], bbox[3]), (bbox[0], bbox[3])]
            xs, ys = [], []
            for x, y in corners:
                tx, ty, _ = tr.TransformPoint(x, y)
                xs.append(tx)
                ys.append(ty)
            return (min(xs), min(ys), max(xs), max(ys))

        # WICHTIG: Fuer EPSG:4326 die URSPRUENGLICHE QGIS-eigene Methode
        # verwenden (parameterAsExtent mit Ziel-CRS) - NICHT die eigene
        # osr-Funktion. Die hat sich nur fuer das Paar 31255->31258 als
        # notwendig erwiesen; fuer 31255->4326 hat rohes osr (ohne
        # QGIS-Kontext) beim fehlenden PROJ-Gitter offenbar eine andere,
        # diesmal falsche Rueckfalloption gewaehlt, waehrend QGIS' eigene
        # Transform-Logik hier immer ein brauchbares (wenn auch nicht
        # perfekt genaues) Ergebnis geliefert hat.
        extent_4326 = self.parameterAsExtent(
            parameters, self.EXTENT, context, QgsCoordinateReferenceSystem("EPSG:4326"))
        aoi_bbox = (extent_4326.xMinimum(), extent_4326.yMinimum(),
                    extent_4326.xMaximum(), extent_4326.yMaximum())
        feedback.pushInfo(
            f"AOI in EPSG:4326: west={aoi_bbox[0]:.6f}, sued={aoi_bbox[1]:.6f}, "
            f"ost={aoi_bbox[2]:.6f}, nord={aoi_bbox[3]:.6f}")

        if not looks_like_valid_4326_kaernten(aoi_bbox):
            feedback.reportError(
                "Die AOI wurde nach EPSG:4326 umgerechnet, liegt aber weit ausserhalb von "
                "Kaernten. Das deutet auf eine fehlgeschlagene CRS-Umrechnung hin, oft weil "
                "ein benoetigtes PROJ-Datumsgitter auf diesem System fehlt und die Umrechnung "
                "deshalb unveraendert durchgereicht wurde.")
            return {}

        # MGI-Ferro-referenzierte GK-Zonen (West/Mitte/Ost: EPSG 31254/31255/
        # 31256) und ihre Greenwich-referenzierten Gegenstuecke (M28/M31/M34:
        # EPSG 31257/31258/31259) beschreiben laut oesterreichischer
        # Vermessungskonvention dieselbe Projektion - sie unterscheiden sich
        # nur um eine FESTE Rechtswert-Konstante, der Hochwert bleibt
        # unveraendert. Falls das Original-CRS eines dieser Paare ist, wird
        # diese bekannte Konstante direkt verwendet statt osr zu bemuehen -
        # fuer jedes andere CRS wird der generische osr-Weg genutzt.
        # HINWEIS: aoi_bbox_native dient inzwischen NUR NOCH der Log-Ausgabe
        # zur Diagnose (siehe unten) - die eigentliche Kachel-Pruefung nutzt
        # ausschliesslich aoi_bbox (EPSG:4326), siehe raster_extent() und
        # dessen Verwendung weiter unten. Trotzdem hier belassen, da diese
        # Berechnung fuer die Fehlersuche wertvoll war und es bei kuenftigen
        # aehnlichen CRS-Problemen wieder sein koennte.
        MGI_FERRO_TO_GREENWICH_EASTING_OFFSET = {
            ("EPSG:31254", "EPSG:31257"): 150000,
            ("EPSG:31255", "EPSG:31258"): 450000,
            ("EPSG:31256", "EPSG:31259"): 750000,
        }
        pair = (extent_crs.authid(), KAGIS_SOURCE_CRS)
        if pair in MGI_FERRO_TO_GREENWICH_EASTING_OFFSET:
            offset = MGI_FERRO_TO_GREENWICH_EASTING_OFFSET[pair]
            aoi_bbox_native = (orig_bbox[0] + offset, orig_bbox[1],
                                orig_bbox[2] + offset, orig_bbox[3])
            feedback.pushInfo(f"Bekannte MGI-Ferro->Greenwich-Konstante ({offset} m) direkt angewendet.")
        else:
            aoi_bbox_native = reproject_bbox_osr(orig_bbox, extent_crs.authid(), KAGIS_SOURCE_CRS)

        feedback.pushInfo(
            f"AOI in {KAGIS_SOURCE_CRS} (nur zur Information): west={aoi_bbox_native[0]:.2f}, "
            f"sued={aoi_bbox_native[1]:.2f}, ost={aoi_bbox_native[2]:.2f}, nord={aoi_bbox_native[3]:.2f} "
            f"(Original-Extent-CRS: {extent_crs.authid()})")
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
                # Sicherheitsnetz: die von der API zurueckgegebenen Items
                # lokal nochmal gegen die AOI pruefen, statt der
                # serverseitigen bbox-Filterung blind zu vertrauen (die kann
                # z.B. bei Pagination Parameter verlieren).
                before = len(items)
                items = [it for it in items
                         if bbox_overlap(tuple((it.get("bbox") or list(aoi_bbox))[:4]), aoi_bbox)]
                dropped = before - len(items)
                if dropped:
                    feedback.pushWarning(
                        f"  {cid}: {dropped} von {before} Items lagen laut API-Antwort "
                        "ausserhalb der AOI - lokal herausgefiltert.")
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

                # Letzte, unabhaengige Pruefung: die tatsaechliche
                # Georeferenzierung jeder heruntergeladenen Kachel gegen die
                # AOI checken - falls KAGIS' eigene STAC-bbox-Angabe fuer eine
                # Collection fehlerhaft ist (z.B. Collection- statt
                # Item-Ausdehnung), faellt das hier auf, weil wir uns auf
                # gar keine Metadaten verlassen, sondern auf die echten
                # Pixel-Header der heruntergeladenen Datei.
                verified = []
                off_target = []
                unreadable = []
                for path in downloaded:
                    ext = raster_extent(path)
                    if ext is None:
                        unreadable.append(path)
                    elif bbox_overlap(ext, aoi_bbox):
                        verified.append(path)
                    else:
                        off_target.append(path)

                if unreadable:
                    names = ", ".join(os.path.basename(p) for p in unreadable[:5])
                    more = f" (+{len(unreadable) - 5} weitere)" if len(unreadable) > 5 else ""
                    feedback.pushWarning(
                        f"{label}: {len(unreadable)} heruntergeladene Datei(en) liessen sich "
                        f"nicht als Raster oeffnen (moeglicherweise beschaedigt) - aus dem "
                        f"Mosaik ausgeschlossen, aber NICHT geloescht, zur manuellen Pruefung: {names}{more}")

                if off_target:
                    names = ", ".join(os.path.basename(p) for p in off_target[:5])
                    more = f" (+{len(off_target) - 5} weitere)" if len(off_target) > 5 else ""
                    feedback.pushWarning(
                        f"{label}: {len(off_target)} heruntergeladene Kachel(n) liegen laut "
                        "ihrer EIGENEN Georeferenzierung tatsaechlich ausserhalb der AOI "
                        f"- geloescht: {names}{more}")
                    for p in off_target:
                        try:
                            os.remove(p)
                        except OSError as e:
                            feedback.pushWarning(f"{label}: konnte {os.path.basename(p)} nicht loeschen: {e}")

                downloaded = verified
                if not downloaded:
                    feedback.pushWarning(f"{label}: nach Extent-Pruefung keine Kacheln mehr uebrig.")
                    continue

                try:
                    vrt_path = os.path.join(out_dir, f"{label}_mosaic.vrt")

                    # WICHTIG: NICHT blind KAGIS_SOURCE_CRS (EPSG:31258)
                    # erzwingen. Stattdessen die echte CRS direkt aus einer
                    # tatsaechlich heruntergeladenen Kachel dieser Gruppe
                    # auslesen und genau die verwenden.
                    src_ds = gdal.Open(downloaded[0])
                    tile_wkt = src_ds.GetProjection() if src_ds is not None else ""
                    src_ds = None
                    if not tile_wkt:
                        feedback.pushWarning(
                            f"{label}: konnte keine Projektion aus der Original-Kachel lesen - "
                            f"verwende ersatzweise {KAGIS_SOURCE_CRS}.")
                        tile_wkt = KAGIS_SOURCE_CRS
                    else:
                        # Manche KAGIS-Kacheln (beobachtet bei ALS1) liefern
                        # eine technisch korrekte, aber nicht autoritativ
                        # referenzierte WKT (kein "ID[EPSG,...]" auf oberster
                        # Ebene) - QGIS erkennt das dann zwar als gueltige,
                        # aber "namenlose" CRS (kein authid). Drei Ebenen,
                        # JEDE EINZELN in try/except - eine Exception in
                        # Ebene 1 (z.B. weil AutoIdentifyEPSG() bei
                        # osr.UseExceptions() wirft statt nur False
                        # zurueckzugeben) darf nicht die folgenden Ebenen
                        # verhindern.
                        epsg_code = None

                        try:
                            tile_srs = osr.SpatialReference()
                            tile_srs.ImportFromWkt(tile_wkt)
                            if tile_srs.AutoIdentifyEPSG() == 0:
                                epsg_code = tile_srs.GetAuthorityCode(None)
                        except Exception:
                            tile_srs = None

                        if not epsg_code and tile_srs is not None:
                            # AutoIdentifyEPSG() vergleicht offenbar exakt
                            # gegen die EPSG-Datenbank und scheitert schon an
                            # winzigen Gleitkomma-Rundungsdifferenzen im
                            # Ellipsoid (beobachtet bei ALS1: 299.152812800003
                            # statt 299.1528128) - deshalb den CRS-NAMEN gegen
                            # die bekannten oesterreichischen MGI-Bezeichnungen
                            # abgleichen, das ist unempfindlich dagegen.
                            try:
                                crs_name = tile_srs.GetName()
                                epsg_code = KNOWN_MGI_CRS_NAMES.get(crs_name)
                                if epsg_code:
                                    feedback.pushInfo(
                                        f"{label}: CRS anhand des Namens '{crs_name}' als "
                                        f"EPSG:{epsg_code} erkannt (exakter Datenbankabgleich "
                                        "war nicht eindeutig).")
                            except Exception:
                                pass

                        if not epsg_code:
                            # Letzte Ebene: dieses Tool ist ausschliesslich
                            # fuer ALS1/ALS2 gedacht, und beide CRS sind per
                            # gdalinfo bestaetigt bekannt - direkt anhand des
                            # Zyklus zuweisen, statt bei einem unerkannten
                            # Einzelfall unbenannt zu bleiben.
                            epsg_code = KNOWN_GROUP_CRS.get(gname)
                            if epsg_code:
                                feedback.pushInfo(
                                    f"{label}: CRS anhand des bekannten Zyklus '{gname}' als "
                                    f"EPSG:{epsg_code} angenommen (weder Datenbank- noch "
                                    "Namensabgleich waren eindeutig).")

                        if epsg_code:
                            tile_wkt = f"EPSG:{epsg_code}"
                            feedback.pushInfo(f"{label}: Original-Kachel-CRS als EPSG:{epsg_code} identifiziert.")

                    vrt_options = gdal.BuildVRTOptions(outputSRS=tile_wkt)
                    gdal.BuildVRT(vrt_path, downloaded, options=vrt_options)

                    # Kontrolle: hat das VRT jetzt tatsaechlich eine Projektion?
                    check_ds = gdal.Open(vrt_path)
                    final_wkt = check_ds.GetProjection() if check_ds is not None else ""
                    check_ds = None
                    if final_wkt:
                        feedback.pushInfo(f"{label}: VRT-Projektion aus Original-Kachel uebernommen.")
                    else:
                        feedback.pushWarning(f"{label}: VRT hat auch nach outputSRS KEINE Projektion - bitte melden!")

                    feedback.pushInfo(f"{label}: VRT erstellt ({len(downloaded)} Dateien) -> {vrt_path}")

                    final_path = vrt_path
                    if target_crs.isValid():
                        safe_authid = target_crs.authid().replace(":", "_") or "custom_crs"
                        warped_path = os.path.join(out_dir, f"{label}_mosaic_{safe_authid}.vrt")
                        gdal.Warp(warped_path, vrt_path, dstSRS=target_crs.toWkt(),
                                  format="VRT", resampleAlg="cubic")
                        feedback.pushInfo(f"{label}: nach {target_crs.authid()} umprojiziert -> {warped_path}")
                        final_path = warped_path

                    results[label] = final_path
                except Exception as e:
                    feedback.pushWarning(f"{label}: VRT-Erstellung/Umprojektion fehlgeschlagen ({e}) - uebersprungen.")
                    continue

        if outer_canceled:
            feedback.pushInfo(
                f"Abgebrochen - {len(results)} bereits vollstaendig heruntergeladene Layer werden trotzdem hinzugefuegt.")

        for label, vrt_path in results.items():
            layer = QgsRasterLayer(vrt_path, label)
            if layer.isValid():
                if not layer.crs().isValid():
                    # label ist "<Zyklus>_<Modelltyp>", z.B. "ALS2_DGM" -
                    # anhand des Zyklus die bekannte richtige CRS waehlen,
                    # statt blind KAGIS_SOURCE_CRS (nur fuer ALS1 korrekt).
                    gname_for_label = label.split("_")[0]
                    fallback_epsg = KNOWN_GROUP_CRS.get(gname_for_label)
                    fallback_crs = f"EPSG:{fallback_epsg}" if fallback_epsg else KAGIS_SOURCE_CRS
                    layer.setCrs(QgsCoordinateReferenceSystem(fallback_crs))
                    feedback.pushInfo(
                        f"Layer '{label}': CRS wurde nicht automatisch erkannt - manuell auf "
                        f"{fallback_crs} gesetzt.")
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
            "<p>Die Original-CRS wird nicht einheitlich angenommen, sondern direkt "
            "aus einer echten heruntergeladenen Kachel gelesen: ALS1 liegt in "
            "EPSG:31258 (MGI / Austria GK M31), ALS2 in EPSG:31255 (MGI / Austria "
            "GK Central). Mit Ziel-CRS wird zusätzlich ein virtuell umprojiziertes "
            "VRT erzeugt.</p>"
            "<p>Quelle: Land Kärnten - KAGIS, https://kagis.ktn.gv.at, CC-BY-4.0</p>"
        )

    def createInstance(self):
        return KagisBulkDownload()
