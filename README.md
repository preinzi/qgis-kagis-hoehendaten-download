# KAGIS Höhendaten Bulk-Download

A QGIS Processing script that bulk-downloads elevation raster tiles (DGM/DOM/DOL, ALS1 & ALS2) from the official KAGIS (Land Kärnten, Austria) STAC API for a chosen area, and adds ready-to-use mosaic layers straight to your project.

> **Note:** The tool's interface (parameter labels, log messages, help text) is in German, matching its target audience and the German-language KAGIS data catalog it queries. This README is in English for discoverability.

<img src="images/screenshot.png" width="400" alt="Screenshot of the tool in QGIS">

## What it does

Given an area of interest, this tool:

- Queries the official KAGIS STAC API (`https://gis.ktn.gv.at/api/stac/v1/`) for elevation raster collections covering Carinthia (Kärnten), Austria.
- Automatically discovers and merges multiple regional collections when a survey cycle is split by region (ALS2 is split into Nockberge, Hohe Tauern, Gailtaler Alpen, and Kreuzeckgruppe).
- Downloads only the tiles that intersect your chosen area — not the whole collection.
- Lets you pick which elevation model type(s) to fetch — DGM (terrain model), DOM (surface model), DOL — each producing its own mosaic layer. With both ALS cycles and both DGM/DOM selected, you get 4 separate layers.
- Builds a lightweight VRT mosaic per cycle/model-type combination (no pixel duplication on disk) and adds it directly to your QGIS project. The CRS is read directly from a real downloaded tile for each combination rather than assumed — **ALS1 and ALS2 are not in the same CRS** (see note below).
- Verifies every downloaded tile's actual position (read from its own pixel georeferencing, not from catalog metadata) against your area of interest before including it in the mosaic.
- Optional on-the-fly reprojection to a target CRS of your choice, via a standard CRS picker.
- Cancellable mid-run, including tiles already in progress; failed downloads are retried once and reported in the log.

### Note on coordinate reference systems

ALS1 is delivered in **EPSG:31258** (MGI / Austria GK M31), ALS2 in **EPSG:31255** (MGI / Austria GK Central) — two different projections. The tool detects each tile's real CRS automatically, so this only matters if you work with the raw downloaded tiles directly.

## Installation

This is a single-file **Processing script**, not a full plugin:

1. Download [`kagis_hoehendaten_bulk_download.py`](https://github.com/preinzi/qgis-kagis-hoehendaten-download/blob/main/kagis_hoehendaten_bulk_download.py).
2. In QGIS: **Processing → Toolbox → Scripts (gear icon) → Add Script to Toolbox…**, and select the file.
3. It will appear under **Skripte/Scripts → KAGIS → KAGIS Höhendaten Bulk-Download**.

No extra Python packages required beyond what ships with QGIS (uses only the Python standard library plus the bundled GDAL/PyQGIS). Tested on QGIS 3.34 LTR (Linux) and QGIS 3.44 LTR (Windows).

## Usage

| Parameter         | Description                                                                                                                                                                                              |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Gebiet (AOI)**  | Area of interest — draw a rectangle, use the current canvas extent or calculate extent from a layer                                                                                                      |
| **ALS-Zyklen**    | Which survey cycle(s) to fetch: ALS1, ALS2, or both                                                                                                                                                      |
| **Modelltyp(en)** | Which elevation model(s) to fetch: DGM, DOM, and/or DOL                                                                                                                                                  |
| **Ziel-CRS**      | Optional. Leave empty to keep each tile's own original CRS (ALS1=EPSG:31258, ALS2=EPSG:31255); pick a CRS to get an additional, virtually reprojected VRT                                                |
| **Zielordner**    | Where downloaded tiles and mosaics are stored. It is recommended to use a persistent folder, not the default temp location — results should stay usable after the QGIS session ends, and a later run over an overlapping area reuses already-downloaded tiles instead of re-fetching them |

### Output layers

For every combination of selected ALS cycle × model type, one layer is added to the project (e.g. `ALS1_DGM`, `ALS2_DOM`), named after that combination. On disk, each combination gets its own subfolder under the chosen output folder, containing the original downloaded tiles plus a `..._mosaic.vrt` (and, if a target CRS was chosen, an additional reprojected VRT).

## Data source

Quelle: Land Kärnten – KAGIS, <https://kagis.ktn.gv.at>, CC-BY-4.0

## License

GPL-2.0-or-later — see [LICENSE](https://github.com/preinzi/qgis-kagis-hoehendaten-download/blob/main/LICENSE). This follows QGIS's own licensing, since the script builds on the PyQGIS API.

## Credits

Written by Stephan Preinstorfer (LiberGIS) with help from Claude (Anthropic).

## Contributing

Issues and pull requests welcome.
