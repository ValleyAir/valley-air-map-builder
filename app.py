"""
Valley Air Map Builder 2.0
====================
Draw treatment areas on an interactive map, assign attributes,
and export as ESRI Shapefiles (.shp), GeoJSON, KML, KMZ, or Satloc .job.

Usage:
    streamlit run app.py
"""

import streamlit as st
import geopandas as gpd
import folium
from folium.plugins import Draw
from streamlit_folium import st_folium
from shapely.geometry import shape, mapping, Point, LineString, Polygon, MultiPolygon, MultiLineString, MultiPoint
import os
import re
import tempfile
import zipfile
import io
import requests
import math
from datetime import datetime
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate, SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image, PageBreak, Frame, PageTemplate, NextPageTemplate,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
import xml.etree.ElementTree as ET


# ──────────────────────────────────────────────────────────────
# Page Config
# ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="Valley Air Map Builder 2.0", page_icon="🗺️", layout="wide")


# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────
TREATMENT_CATEGORIES = {
    "Forestry / Vegetation": [
        "Timber Harvest", "Fuels Reduction", "Reforestation",
        "Prescribed Burn", "Thinning", "Brush Clearing",
        "Habitat Restoration", "Other Forestry",
    ],
    "Agriculture": [
        "Crop Treatment", "Pesticide Application", "Irrigation Zone",
        "Soil Amendment", "Cover Crop", "Fallow Area", "Other Agriculture",
    ],
    "Environmental Cleanup": [
        "Contamination Site", "Remediation Area", "Erosion Control",
        "Wetland Restoration", "Stormwater Management", "Other Environmental",
    ],
    "Other": [
        "General Treatment", "Survey Area", "Monitoring Zone", "Custom",
    ],
}

DEFAULT_CRS = "EPSG:4326"

# Flat list of all treatment types for the form (avoids dependent dropdown issue)
ALL_TREATMENT_TYPES = []
for cat, types in TREATMENT_CATEGORIES.items():
    for t in types:
        ALL_TREATMENT_TYPES.append(f"{cat} — {t}")


# ──────────────────────────────────────────────────────────────
# Session State
# ──────────────────────────────────────────────────────────────
if "features" not in st.session_state:
    st.session_state.features = []
if "pending_drawing" not in st.session_state:
    st.session_state.pending_drawing = None
# Page navigation: "draw", "list", "export"
if "page" not in st.session_state:
    st.session_state.page = "draw"
if "search_result" not in st.session_state:
    st.session_state.search_result = None


# ──────────────────────────────────────────────────────────────
# Helper Functions
# ──────────────────────────────────────────────────────────────

def extract_kml_from_kmz(kmz_bytes):
    """Extract KML content from a KMZ (zipped KML) file.

    Args:
        kmz_bytes: bytes of the KMZ file

    Returns:
        KML content as bytes, or None if extraction fails
    """
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            kmz_path = os.path.join(tmpdir, "temp.kmz")
            with open(kmz_path, "wb") as f:
                f.write(kmz_bytes)

            with zipfile.ZipFile(kmz_path, "r") as zf:
                kml_files = [f for f in zf.namelist() if f.lower().endswith(".kml")]
                if not kml_files:
                    return None
                # Prefer doc.kml, otherwise use first KML file
                kml_file = "doc.kml" if "doc.kml" in kml_files else kml_files[0]
                return zf.read(kml_file)
    except Exception:
        return None


def parse_kml_to_gdf(kml_content_bytes):
    """Parse KML content and convert to GeoDataFrame.

    Tries GDAL KML driver first (faster), then falls back to manual XML parsing.

    Args:
        kml_content_bytes: bytes of KML content

    Returns:
        GeoDataFrame with columns: geometry, name, category, treatment_type, status,
                                   priority, area_acres, notes, date_created
    """
    gdf = None

    # Try GDAL KML driver first (fastest)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            kml_path = os.path.join(tmpdir, "temp.kml")
            with open(kml_path, "wb") as f:
                f.write(kml_content_bytes)
            gdf = gpd.read_file(kml_path, driver="KML")
            if not gdf.empty:
                gdf = gdf.to_crs(DEFAULT_CRS)
                # Rename common KML columns if present
                if "Name" in gdf.columns and "name" not in gdf.columns:
                    gdf = gdf.rename(columns={"Name": "name"})
                if "Description" in gdf.columns and "description" not in gdf.columns:
                    gdf = gdf.rename(columns={"Description": "description"})
                return gdf
    except Exception:
        pass

    # Fallback: manual XML parsing
    try:
        root = ET.fromstring(kml_content_bytes)

        # Handle KML namespace
        ns = {"kml": "http://www.opengis.net/kml/2.2"}

        rows = []

        # Find all Placemarks
        placemarks = root.findall(".//kml:Placemark", ns)
        if not placemarks:
            placemarks = root.findall(".//Placemark")

        for pm in placemarks:
            name_elem = pm.find("kml:name", ns)
            if name_elem is None:
                name_elem = pm.find("name")
            name = name_elem.text if name_elem is not None and name_elem.text else "Untitled"

            description_elem = pm.find("kml:description", ns)
            if description_elem is None:
                description_elem = pm.find("description")
            description = description_elem.text if description_elem is not None and description_elem.text else ""

            geom = None

            # Try Point
            point_elem = pm.find("kml:Point/kml:coordinates", ns)
            if point_elem is None:
                point_elem = pm.find("Point/coordinates")
            if point_elem is not None and point_elem.text:
                try:
                    coords = point_elem.text.strip().split(",")
                    if len(coords) >= 2:
                        geom = Point(float(coords[0]), float(coords[1]))
                except Exception:
                    pass

            # Try LineString
            if geom is None:
                line_elem = pm.find("kml:LineString/kml:coordinates", ns)
                if line_elem is None:
                    line_elem = pm.find("LineString/coordinates")
                if line_elem is not None and line_elem.text:
                    try:
                        coords_list = []
                        for coord_str in line_elem.text.strip().split():
                            parts = coord_str.split(",")
                            if len(parts) >= 2:
                                coords_list.append((float(parts[0]), float(parts[1])))
                        if len(coords_list) >= 2:
                            geom = LineString(coords_list)
                    except Exception:
                        pass

            # Try Polygon
            if geom is None:
                poly_elem = pm.find("kml:Polygon/kml:outerBoundaryIs/kml:LinearRing/kml:coordinates", ns)
                if poly_elem is None:
                    poly_elem = pm.find("Polygon/outerBoundaryIs/LinearRing/coordinates")
                if poly_elem is not None and poly_elem.text:
                    try:
                        coords_list = []
                        for coord_str in poly_elem.text.strip().split():
                            parts = coord_str.split(",")
                            if len(parts) >= 2:
                                coords_list.append((float(parts[0]), float(parts[1])))
                        if len(coords_list) >= 3:
                            geom = Polygon(coords_list)
                    except Exception:
                        pass

            if geom is not None and not geom.is_empty:
                rows.append({
                    "geometry": geom,
                    "name": name,
                    "category": "Other",
                    "treatment_type": "General Treatment",
                    "status": "Planned",
                    "priority": "Medium",
                    "area_acres": 0,
                    "notes": description,
                    "date_created": datetime.now().strftime("%Y-%m-%d"),
                })

        if rows:
            gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=DEFAULT_CRS)
            return gdf
    except Exception:
        pass

    # Return empty GeoDataFrame if parsing failed
    return gpd.GeoDataFrame(
        columns=["geometry", "name", "category", "treatment_type",
                 "status", "priority", "area_acres", "notes", "date_created"],
        geometry="geometry", crs=DEFAULT_CRS,
    )


def features_to_gdf(features):
    if not features:
        return gpd.GeoDataFrame(
            columns=["geometry", "name", "category", "treatment_type",
                     "status", "priority", "area_acres", "notes", "date_created"],
            geometry="geometry", crs=DEFAULT_CRS,
        )
    rows = []
    for f in features:
        geom = shape(f["geometry"])
        props = f.get("properties", {})
        rows.append({
            "geometry": geom,
            "name": props.get("name", ""),
            "category": props.get("category", ""),
            "treatment_type": props.get("treatment_type", ""),
            "status": props.get("status", "Planned"),
            "priority": props.get("priority", "Medium"),
            "area_acres": props.get("area_acres", 0),
            "notes": props.get("notes", ""),
            "date_created": props.get("date_created", ""),
        })
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=DEFAULT_CRS)


def calc_area_acres(geom):
    try:
        gdf_temp = gpd.GeoDataFrame(geometry=[geom], crs=DEFAULT_CRS)
        gdf_proj = gdf_temp.to_crs("EPSG:5070")
        area_m2 = gdf_proj.geometry.area.iloc[0]
        return round(area_m2 * 0.000247105, 2)
    except Exception:
        return 0.0


def export_shapefile(gdf, filename="treatment_areas"):
    with tempfile.TemporaryDirectory() as tmpdir:
        shp_path = os.path.join(tmpdir, f"{filename}.shp")
        gdf.to_file(shp_path, driver="ESRI Shapefile")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
                fpath = os.path.join(tmpdir, f"{filename}{ext}")
                if os.path.exists(fpath):
                    zf.write(fpath, f"{filename}{ext}")
        buf.seek(0)
        return buf


def export_kml(gdf, filename="treatment_areas"):
    """Export GeoDataFrame to KML format.

    Uses GDAL/Fiona KML driver via geopandas. Falls back to manual XML
    generation if the driver is unavailable.

    Args:
        gdf: GeoDataFrame with treatment area features
        filename: base filename (without extension)

    Returns:
        io.BytesIO buffer containing the KML file content
    """
    # Ensure WGS 84 for KML (required by the KML specification)
    gdf_4326 = gdf.to_crs("EPSG:4326") if str(gdf.crs) != "EPSG:4326" else gdf.copy()

    # Try GDAL KML driver first
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            kml_path = os.path.join(tmpdir, f"{filename}.kml")
            gdf_4326.to_file(kml_path, driver="KML")
            buf = io.BytesIO()
            with open(kml_path, "rb") as f:
                buf.write(f.read())
            buf.seek(0)
            return buf
    except Exception:
        pass

    # Fallback: manual KML generation via xml.etree.ElementTree
    kml_ns = "http://www.opengis.net/kml/2.2"
    root = ET.Element("kml", xmlns=kml_ns)
    document = ET.SubElement(root, "Document")
    ET.SubElement(document, "name").text = filename

    # Define category styles
    style_map = {
        "Forestry / Vegetation": ("style_forestry", "228B22"),
        "Agriculture": ("style_agriculture", "DAA520"),
        "Environmental Cleanup": ("style_environmental", "4682B4"),
        "Other": ("style_other", "808080"),
    }
    for cat, (style_id, hex_color) in style_map.items():
        style_elem = ET.SubElement(document, "Style", id=style_id)
        poly_style = ET.SubElement(style_elem, "PolyStyle")
        # KML uses aaBBGGRR format
        r, g, b = hex_color[0:2], hex_color[2:4], hex_color[4:6]
        ET.SubElement(poly_style, "color").text = f"88{b}{g}{r}"
        ET.SubElement(poly_style, "outline").text = "1"
        line_style = ET.SubElement(style_elem, "LineStyle")
        ET.SubElement(line_style, "color").text = f"ff{b}{g}{r}"
        ET.SubElement(line_style, "width").text = "2"

    for _, row in gdf_4326.iterrows():
        pm = ET.SubElement(document, "Placemark")
        ET.SubElement(pm, "name").text = str(row.get("name", ""))
        # Build description from properties
        desc_parts = []
        for field in ["category", "treatment_type", "status", "priority", "area_acres", "notes", "date_created"]:
            val = row.get(field, "")
            if val:
                desc_parts.append(f"{field}: {val}")
        ET.SubElement(pm, "description").text = "\n".join(desc_parts)

        # Apply style
        cat = row.get("category", "Other")
        style_id = style_map.get(cat, style_map["Other"])[0]
        ET.SubElement(pm, "styleUrl").text = f"#{style_id}"

        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        if geom.geom_type == "Point":
            point_elem = ET.SubElement(pm, "Point")
            ET.SubElement(point_elem, "coordinates").text = f"{geom.x},{geom.y},0"
        elif geom.geom_type == "LineString":
            ls_elem = ET.SubElement(pm, "LineString")
            coords = " ".join(f"{x},{y},0" for x, y in geom.coords)
            ET.SubElement(ls_elem, "coordinates").text = coords
        elif geom.geom_type == "Polygon":
            poly_elem = ET.SubElement(pm, "Polygon")
            outer = ET.SubElement(poly_elem, "outerBoundaryIs")
            ring = ET.SubElement(outer, "LinearRing")
            coords = " ".join(f"{x},{y},0" for x, y in geom.exterior.coords)
            ET.SubElement(ring, "coordinates").text = coords
            for interior in geom.interiors:
                inner = ET.SubElement(poly_elem, "innerBoundaryIs")
                inner_ring = ET.SubElement(inner, "LinearRing")
                inner_coords = " ".join(f"{x},{y},0" for x, y in interior.coords)
                ET.SubElement(inner_ring, "coordinates").text = inner_coords
        elif geom.geom_type == "MultiPolygon":
            multi_geom = ET.SubElement(pm, "MultiGeometry")
            for poly in geom.geoms:
                poly_elem = ET.SubElement(multi_geom, "Polygon")
                outer = ET.SubElement(poly_elem, "outerBoundaryIs")
                ring = ET.SubElement(outer, "LinearRing")
                coords = " ".join(f"{x},{y},0" for x, y in poly.exterior.coords)
                ET.SubElement(ring, "coordinates").text = coords
                for interior in poly.interiors:
                    inner = ET.SubElement(poly_elem, "innerBoundaryIs")
                    inner_ring = ET.SubElement(inner, "LinearRing")
                    inner_coords = " ".join(f"{x},{y},0" for x, y in interior.coords)
                    ET.SubElement(inner_ring, "coordinates").text = inner_coords

    tree = ET.ElementTree(root)
    buf = io.BytesIO()
    tree.write(buf, encoding="unicode", xml_declaration=True)
    kml_bytes = buf.getvalue().encode("utf-8")
    result = io.BytesIO(kml_bytes)
    result.seek(0)
    return result


def export_kmz(gdf, filename="treatment_areas"):
    """Export GeoDataFrame to KMZ format (zipped KML).

    Args:
        gdf: GeoDataFrame with treatment area features
        filename: base filename (without extension)

    Returns:
        io.BytesIO buffer containing the KMZ file content
    """
    kml_buf = export_kml(gdf, filename=filename)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.kml", kml_buf.read())
    buf.seek(0)
    return buf


def export_job(gdf, filename="treatment_areas", job_num="900"):
    """Export GeoDataFrame polygons to a Satloc G4 .job file.

    Format reverse-engineered from a real G4 device export (900.job, 2026-06):
        .JOB <num> OIDMASTER
        .VERSION 2
        .POL 1 Poly1
        <tab>INC                 (INC = inclusion boundary; EXC = exclusion/hole)
        <tab><lat> <lon>         (6 decimals, space-separated, polygon closed)
    CRLF line endings. Coordinates MUST be WGS84 lat/lon, so we reproject here
    regardless of the export-CRS selection.

    Returns an io.BytesIO buffer containing the .job text.
    """
    gdf_wgs = gdf.to_crs("EPSG:4326")
    lines = [f".JOB {job_num} OIDMASTER", ".VERSION 2"]
    counter = {"i": 0}

    def emit_ring(coords, kind):
        pts = list(coords)
        if len(pts) < 3:
            return
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        counter["i"] += 1
        idx = counter["i"]
        lines.append(f".POL {idx} Poly{idx}")
        lines.append("\t" + kind)
        for x, y in pts:
            lines.append(f"\t{y:.6f} {x:.6f}")

    for geom in gdf_wgs.geometry:
        if geom is None or geom.is_empty:
            continue
        polys = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
        for poly in polys:
            if not isinstance(poly, Polygon):
                continue
            emit_ring(poly.exterior.coords, "INC")
            for interior in poly.interiors:
                emit_ring(interior.coords, "EXC")

    text = "\r\n".join(lines) + "\r\n"
    return io.BytesIO(text.encode("utf-8"))


def parse_job_to_gdf(job_bytes):
    """Parse a Satloc .job file into a GeoDataFrame of polygons (EPSG:4326).

    Each .POL block is one ring; INC starts a new polygon, EXC adds a hole to
    the current one. Returns a GeoDataFrame with a 'name' column compatible with
    the importer below.
    """
    text = job_bytes.decode("utf-8", "replace") if isinstance(job_bytes, (bytes, bytearray)) else job_bytes
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    polys = []          # list of {"name", "shell": [...], "holes": [[...], ...]}
    cur = None
    ring = None
    name = ""
    for raw in text.split("\n"):
        line = raw.strip("\t").strip()
        if not line or line.startswith(".JOB") or line.startswith(".VERSION"):
            continue
        if line.startswith(".POL"):
            parts = line.split(None, 2)
            name = parts[2] if len(parts) > 2 else f"Poly{len(polys)+1}"
            ring = None
            continue
        up = line.upper()
        if up in ("INC", "EXC"):
            ring = []
            if up == "INC":
                cur = {"name": name, "shell": ring, "holes": []}
                polys.append(cur)
            elif cur is not None:
                cur["holes"].append(ring)
            continue
        toks = line.split()
        if len(toks) >= 2 and ring is not None:
            try:
                lat = float(toks[0]); lon = float(toks[1])
                ring.append((lon, lat))
            except ValueError:
                pass

    geoms, names = [], []
    for p in polys:
        if len(p["shell"]) >= 3:
            geoms.append(Polygon(p["shell"], [h for h in p["holes"] if len(h) >= 3]))
            names.append(p["name"])
    if not geoms:
        return gpd.GeoDataFrame({"name": []}, geometry=[], crs=DEFAULT_CRS)
    return gpd.GeoDataFrame({"name": names}, geometry=geoms, crs=DEFAULT_CRS)


def render_map_image(gdf, focus_idx=None, figsize=(13, 8)):
    """Render treatment areas onto a satellite basemap as a PNG.

    focus_idx=None → overview of ALL areas (auto-fit to all polygons).
    focus_idx=<int> → a single field zoomed to ITS extent (other fields drawn
    faintly for context). Either way the view is computed from the geometry and
    the basemap is fetched AFTER the limits are set, so every map is framed at a
    legible zoom (fixes the too-zoomed-in / too-zoomed-out problem).
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as path_effects
    import contextily as cx

    gdf_4326 = gdf.to_crs("EPSG:4326") if str(gdf.crs) != "EPSG:4326" else gdf
    gdf_3857 = gdf_4326.to_crs("EPSG:3857")

    cat_colors = {
        "Forestry / Vegetation": "#22cc22",
        "Agriculture":           "#ddaa00",
        "Environmental Cleanup": "#4488ff",
        "Other":                 "#aaaaaa",
    }

    fig, ax = plt.subplots(1, 1, figsize=figsize)
    ax.set_aspect("equal")  # keep satellite tiles square — no horizontal stretch

    # --- 1) Decide the view BEFORE drawing the basemap ---
    if focus_idx is not None:
        xmin, ymin, xmax, ymax = gdf_3857.iloc[focus_idx].geometry.bounds
        pad_frac = 0.30          # a single field: a bit more breathing room
    else:
        xmin, ymin, xmax, ymax = gdf_3857.total_bounds
        pad_frac = 0.15

    w, h = (xmax - xmin), (ymax - ymin)
    # Floor the extent so a tiny field doesn't zoom to an absurd tile level.
    MIN_EXTENT = 250.0  # metres
    if w < MIN_EXTENT:
        cxm = (xmin + xmax) / 2; xmin, xmax = cxm - MIN_EXTENT / 2, cxm + MIN_EXTENT / 2; w = MIN_EXTENT
    if h < MIN_EXTENT:
        cym = (ymin + ymax) / 2; ymin, ymax = cym - MIN_EXTENT / 2, cym + MIN_EXTENT / 2; h = MIN_EXTENT
    xmin -= w * pad_frac; xmax += w * pad_frac
    ymin -= h * pad_frac; ymax += h * pad_frac

    # Match the bbox aspect to the figure so equal-aspect leaves no blank bands.
    fig_aspect = figsize[0] / figsize[1]
    bw, bh = (xmax - xmin), (ymax - ymin)
    if bw / bh < fig_aspect:
        nbw = bh * fig_aspect; c = (xmin + xmax) / 2; xmin, xmax = c - nbw / 2, c + nbw / 2
    else:
        nbh = bw / fig_aspect; c = (ymin + ymax) / 2; ymin, ymax = c - nbh / 2, c + nbh / 2
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)

    # --- 2) Draw polygons (highlight the focused field, dim the rest) ---
    for idx in range(len(gdf_3857)):
        row = gdf_3857.iloc[idx]
        color = cat_colors.get(row.get("category", "Other"), "#aaaaaa")
        on = (focus_idx is None) or (idx == focus_idx)
        gdf_3857.iloc[[idx]].plot(
            ax=ax, color=color, edgecolor="white",
            linewidth=2.2 if on else 1.0,
            alpha=0.55 if on else 0.18,
        )

    # Labels (focused field, or all in overview)
    for idx in range(len(gdf_3857)):
        if focus_idx is not None and idx != focus_idx:
            continue
        row = gdf_3857.iloc[idx]
        centroid = row.geometry.centroid
        ax.annotate(
            f"{row.get('name', '')}\n{row.get('area_acres', 0):,.1f} ac",
            xy=(centroid.x, centroid.y), ha="center", va="center",
            fontsize=9, fontweight="bold", color="white",
            path_effects=[path_effects.withStroke(linewidth=2.5, foreground="black")],
        )

    # --- 3) Basemap AFTER the limits are set so it covers the framed view ---
    try:
        cx.add_basemap(ax, source=cx.providers.Esri.WorldImagery, zoom="auto")
    except Exception:
        try:
            cx.add_basemap(ax, source=cx.providers.Esri.WorldImagery, zoom=16)
        except Exception:
            pass  # No basemap — polygons still render on white background

    ax.set_axis_off()

    # Legend only on the overview
    if focus_idx is None:
        from matplotlib.patches import Patch
        present = {row.get("category", "") for _, row in gdf_3857.iterrows()}
        handles = [Patch(facecolor=c, edgecolor="white", alpha=0.7, label=cat)
                   for cat, c in cat_colors.items() if cat in present]
        if handles:
            ax.legend(handles=handles, loc="lower left", fontsize=8, framealpha=0.85)

    img_buf = io.BytesIO()
    fig.savefig(img_buf, format="png", dpi=150, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    img_buf.seek(0)
    return img_buf


def export_pdf(gdf, filename="treatment_areas", customer="", crop=""):
    """Generate a PDF report with a map overview image, work order form, and treatment area data table.

    customer / crop are optional manual entries (the Map Builder has no FieldPulse
    connection); when provided they're printed on the report, otherwise blank
    fill-in lines are shown."""
    buf = io.BytesIO()
    margin = 0.5 * inch

    # All pages portrait orientation
    # Use BaseDocTemplate with landscape + portrait page templates
    doc = BaseDocTemplate(buf, pagesize=landscape(letter),
                          topMargin=margin, bottomMargin=margin,
                          leftMargin=margin, rightMargin=margin)

    lw, lh = landscape(letter)
    pw, ph = letter
    landscape_frame = Frame(margin, margin, lw - 2*margin, lh - 2*margin, id='land')
    portrait_frame = Frame(margin, margin, pw - 2*margin, ph - 2*margin, id='port')
    doc.addPageTemplates([
        PageTemplate(id='landscape', frames=[landscape_frame], pagesize=landscape(letter)),
        PageTemplate(id='portrait', frames=[portrait_frame], pagesize=letter),
    ])

    styles = getSampleStyleSheet()
    story = []

    # ── Page 1: Title + Map (landscape) ──
    title_style = ParagraphStyle("Title2", parent=styles["Title"], fontSize=20, spaceAfter=4)
    story.append(Paragraph("Valley Air Map Builder — Treatment Area Report", title_style))

    total_acres = gdf["area_acres"].sum() if "area_acres" in gdf.columns else 0
    story.append(Paragraph(
        f"Generated {datetime.now().strftime('%B %d, %Y at %I:%M %p')} &nbsp;|&nbsp; "
        f"<b>{len(gdf)}</b> treatment area(s) &nbsp;|&nbsp; "
        f"<b>{total_acres:,.2f}</b> total acres",
        styles["Normal"],
    ))
    story.append(Paragraph(
        f"<b>Customer:</b> {customer or '__________________________'} "
        f"&nbsp;&nbsp;|&nbsp;&nbsp; <b>Crop:</b> {crop or '__________________________'}",
        styles["Normal"],
    ))
    story.append(Spacer(1, 10))

    # Render map image — scale proportionally to fit portrait page
    map_tmp_path = None
    field_tmps = []
    try:
        map_img_buf = render_map_image(gdf)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(map_img_buf.getvalue())
            map_tmp_path = tmp.name

        # Read actual image dimensions to preserve aspect ratio
        from PIL import Image as PILImage
        with PILImage.open(map_tmp_path) as pil_img:
            img_w, img_h = pil_img.size

        max_w = 10.0 * inch   # landscape usable width
        max_h = 5.5 * inch   # leave room for title + summary text
        aspect = img_w / img_h

        # Scale to fit within max_w x max_h while preserving aspect ratio
        if aspect >= (max_w / max_h):
            # Image is wider than box — fit to width
            pdf_w = max_w
            pdf_h = max_w / aspect
        else:
            # Image is taller than box — fit to height
            pdf_h = max_h
            pdf_w = max_h * aspect

        img = Image(map_tmp_path, width=pdf_w, height=pdf_h)
        story.append(img)
    except Exception as e:
        story.append(Paragraph(
            f"<i>Could not render map image: {e}</i>", styles["Normal"],
        ))

    story.append(Spacer(1, 6))

    # Category legend / summary line
    cat_counts = {}
    cat_acres = {}
    for _, row in gdf.iterrows():
        c = row.get("category", "Other")
        cat_counts[c] = cat_counts.get(c, 0) + 1
        cat_acres[c] = cat_acres.get(c, 0) + row.get("area_acres", 0)
    summary_parts = []
    for cat in ["Forestry / Vegetation", "Agriculture", "Environmental Cleanup", "Other"]:
        if cat in cat_counts:
            summary_parts.append(f"<b>{cat}:</b> {cat_counts[cat]} ({cat_acres[cat]:,.1f} ac)")
    if summary_parts:
        story.append(Paragraph(" &nbsp;&nbsp;|&nbsp;&nbsp; ".join(summary_parts), styles["Normal"]))

    # ── Individual field maps (one landscape page each, auto-zoomed) ──
    from PIL import Image as PILImage
    for idx in range(len(gdf)):
        row = gdf.iloc[idx]
        story.append(NextPageTemplate('landscape'))
        story.append(PageBreak())
        story.append(Paragraph(f"Field: {row.get('name', '(unnamed)')}", styles["Heading1"]))
        detail_bits = [f"<b>{row.get('area_acres', 0):,.2f}</b> acres"]
        if str(row.get("category", "")).strip():
            detail_bits.append(str(row.get("category")))
        if str(row.get("treatment_type", "")).strip():
            detail_bits.append(str(row.get("treatment_type")))
        if str(row.get("notes", "")).strip():
            detail_bits.append(str(row.get("notes"))[:90])
        story.append(Paragraph(" &nbsp;|&nbsp; ".join(detail_bits), styles["Normal"]))
        story.append(Spacer(1, 6))
        try:
            f_buf = render_map_image(gdf, focus_idx=idx)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as ftmp:
                ftmp.write(f_buf.getvalue())
                field_tmps.append(ftmp.name)
            with PILImage.open(field_tmps[-1]) as pim:
                fiw, fih = pim.size
            fmax_w, fmax_h = 10.0 * inch, 6.0 * inch
            fasp = fiw / fih
            if fasp >= (fmax_w / fmax_h):
                fw, fh = fmax_w, fmax_w / fasp
            else:
                fh, fw = fmax_h, fmax_h * fasp
            story.append(Image(field_tmps[-1], width=fw, height=fh))
        except Exception as e:
            story.append(Paragraph(f"<i>Could not render field map: {e}</i>", styles["Normal"]))

    # ── Page 2: Work Order Form (switch to portrait) ──
    story.append(NextPageTemplate('portrait'))
    story.append(PageBreak())
    work_order_elements = build_work_order_page(gdf, styles, customer=customer, crop=crop)
    story.extend(work_order_elements)

    # ── Page 3: Data Table (switch back to landscape) ──
    story.append(NextPageTemplate('landscape'))
    story.append(PageBreak())
    story.append(Paragraph("Treatment Area Details", styles["Heading1"]))
    story.append(Spacer(1, 8))

    header = ["Name", "Category", "Treatment Type", "Status", "Priority", "Acres", "Date", "Notes"]
    table_data = [header]
    for _, row in gdf.iterrows():
        notes_text = str(row.get("notes", ""))[:80]
        if len(str(row.get("notes", ""))) > 80:
            notes_text += "..."
        table_data.append([
            str(row.get("name", "")),
            str(row.get("category", "")),
            str(row.get("treatment_type", "")),
            str(row.get("status", "")),
            str(row.get("priority", "")),
            f"{row.get('area_acres', 0):,.2f}",
            str(row.get("date_created", "")),
            notes_text,
        ])

    col_widths = [1.4*inch, 1.2*inch, 1.2*inch, 0.8*inch, 0.7*inch, 0.8*inch, 0.8*inch, 3.1*inch]
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("ALIGN", (5, 0), (5, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f0f0")]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(t)

    doc.build(story)

    # Clean up temp map images now that the PDF is built
    for _p in ([map_tmp_path] + field_tmps):
        if _p and os.path.exists(_p):
            try:
                os.unlink(_p)
            except OSError:
                pass

    buf.seek(0)
    return buf


def style_function(feature):
    cat = feature.get("properties", {}).get("category", "")
    colors = {
        "Forestry / Vegetation": "#228B22",
        "Agriculture": "#DAA520",
        "Environmental Cleanup": "#4682B4",
        "Other": "#808080",
    }
    return {
        "fillColor": colors.get(cat, "#808080"),
        "color": "#333333",
        "weight": 2,
        "fillOpacity": 0.35,
    }


def parse_treatment_selection(selection):
    """Parse 'Category — Type' string back into category and type."""
    if " — " in selection:
        parts = selection.split(" — ", 1)
        return parts[0], parts[1]
    return "Other", selection


def build_work_order_page(gdf, styles, customer="", crop=""):
    """
    Build a professional work order form page for Valley Air LLC.
    Portrait layout: 7.5" usable width (8.5" - 2x0.5" margins).
    All table column widths sum to exactly 7.5" to prevent clipping.
    """
    story = []
    W = 7.5 * inch  # total usable width

    header_style = ParagraphStyle(
        'WOHeader', parent=styles['Heading1'],
        fontSize=16, textColor=colors.HexColor('#2c3e50'),
        spaceAfter=4, alignment=1,
    )
    hdr_fill = colors.HexColor('#2c3e50')
    sec_bg = colors.HexColor('#cccccc')
    alt_bg = colors.HexColor('#f0f0f0')
    current_date = datetime.now().strftime("%m/%d/%Y")

    # Reusable section header bar
    def section(title):
        return Table([[title]], colWidths=[W], style=TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), sec_bg),
            ('FONT', (0, 0), (-1, -1), 'Helvetica-Bold', 9),
            ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ]))

    # Reusable row-of-fields table (equal-width columns that sum to W)
    def fields_row(cells, ncols=None):
        n = ncols or len(cells)
        cw = [W / n] * n
        t = Table([cells], colWidths=cw)
        t.setStyle(TableStyle([
            ('FONT', (0, 0), (-1, -1), 'Helvetica', 8),
            ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 4), ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.black),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.black),
        ]))
        return t

    total_acres = gdf["area_acres"].sum() if "area_acres" in gdf.columns else 0.0

    # ── TITLE ──
    story.append(Paragraph("Valley Air LLC — Work Order", header_style))
    story.append(Spacer(1, 6))

    # ── JOB HEADER ──  3 cols = 7.5"
    story.append(fields_row([
        f'Job #: _______________', f'Status: _______________', f'Date: {current_date}',
    ]))
    # Customer + Crop — manual entry (printed if provided, else blank fill-in)
    story.append(fields_row([
        f'Customer: {customer or "________________________"}',
        f'Crop: {crop or "________________________"}',
    ]))
    story.append(Spacer(1, 8))

    # ── SCHEDULING ──  6 cols = 7.5"
    story.append(section('SCHEDULING'))
    sc = ParagraphStyle('SchedCell', parent=styles['Normal'], fontSize=7, leading=10)
    story.append(fields_row([
        Paragraph('<b>Call Date</b><br/>______________', sc),
        Paragraph('<b>Date Proposed</b><br/>______________', sc),
        Paragraph('<b>Time Proposed</b><br/>______________', sc),
        Paragraph('<b>Schedule Date</b><br/>______________', sc),
        Paragraph('<b>Date Expires</b><br/>______________', sc),
        Paragraph('<b>Consultant</b><br/>______________', sc),
    ]))
    story.append(Spacer(1, 8))

    # ── LOCATIONS ──  9 cols = 7.5"  (0.35+1.65+0.65+0.65+0.65+0.55+0.95+0.55+0.55)
    story.append(section('LOCATIONS'))
    loc_cw = [0.35*inch, 1.65*inch, 0.65*inch, 0.65*inch, 0.65*inch, 0.55*inch, 0.95*inch, 0.55*inch, 0.55*inch]
    loc_data = [['Map#', 'Location / Customer', 'Acres', 'Planted', 'Applied', 'Wind', 'Crop', 'Strip', 'Pests']]
    for idx, row in gdf.iterrows():
        loc_data.append([
            str(idx + 1), str(row.get('name', '')), f"{row.get('area_acres', 0):.2f}",
            '', '', '', str(row.get('category', '')), '', '',
        ])
    for _ in range(3):
        loc_data.append(['', '', '', '', '', '', '', '', ''])
    loc_data.append(['', '', f'Total Acres: {total_acres:.2f}', '', '', '', '', '', ''])

    loc_t = Table(loc_data, colWidths=loc_cw)
    loc_t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), hdr_fill), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 7), ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONT', (0, 1), (-1, -1), 'Helvetica', 7),
        ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, alt_bg]),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#e8e8e8')),
        ('FONT', (0, -1), (-1, -1), 'Helvetica-Bold', 7),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    story.append(loc_t)
    story.append(Spacer(1, 8))

    # ── CHEMICALS / CHARGES ──  5 cols = 7.5"  (2.0+1.8+1.2+0.8+1.7)
    story.append(section('CHEMICALS / CHARGES'))
    chem_cw = [2.0*inch, 1.8*inch, 1.2*inch, 0.8*inch, 1.7*inch]
    chem_data = [['Chemical / Charge', 'Vendor', 'Rate/ac', 'UM', 'Total Applied']]
    for _ in range(4):
        chem_data.append(['', '', '', '', ''])

    chem_t = Table(chem_data, colWidths=chem_cw)
    chem_t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), hdr_fill), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 7), ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONT', (0, 1), (-1, -1), 'Helvetica', 7),
        ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, alt_bg]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    story.append(chem_t)
    story.append(Spacer(1, 2))

    # Diluent / Reentry / Preharvest  3 cols = 7.5"
    story.append(fields_row([
        'Diluent Rate: __________', 'Hours Reentry: __________', 'Days Preharvest: __________',
    ]))
    story.append(Spacer(1, 8))

    # ── LOADER WORKSHEET ──  5 cols = 7.5"
    story.append(section('LOADER WORKSHEET'))
    story.append(fields_row([
        'Applicator: __________', 'Vehicle: __________',
        'Vehicle Cap: __________', 'Rate: ________', 'GL: ________',
    ]))
    story.append(fields_row([
        f'Acre: ________', f'Total Job Acres: {total_acres:.2f}',
        'Loads: ________', '', '',
    ]))
    story.append(Spacer(1, 8))

    # ── APPLIED INFO ──  3 cols then 5 cols = 7.5"
    story.append(section('APPLIED INFO'))
    story.append(fields_row([
        'Applicator: _____________', 'Vehicle: _____________', 'Appl. Date: _____________',
    ]))
    story.append(fields_row([
        'Beg. Tach: ________', 'End Tach: ________', 'Net Tach: ________',
        'Flights: ________', 'Starts: ________',
    ]))
    story.append(Spacer(1, 4))

    # ── WEATHER ──  5 cols = 7.5"
    story.append(section('WEATHER (Start)'))
    story.append(fields_row([
        'Time: ________', 'Temp (°F): ______', 'Wind Dir: ______',
        'Wind mph: ______', 'Humidity: ______',
    ]))
    story.append(section('WEATHER (End)'))
    story.append(fields_row([
        'Time: ________', 'Temp (°F): ______', 'Wind Dir: ______',
        'Wind mph: ______', 'Humidity: ______',
    ]))
    story.append(Spacer(1, 2))

    # Total Time
    story.append(fields_row(['Total Time: _______________', '', '']))
    story.append(Spacer(1, 6))

    # ── COMMENTS ──
    story.append(section('COMMENTS'))
    comment_t = Table([['']], colWidths=[W], rowHeights=[0.8*inch])
    comment_t.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.5, colors.black),
        ('TOPPADDING', (0, 0), (-1, -1), 8), ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(comment_t)

    return story


# ──────────────────────────────────────────────────────────────
# Location Search Helpers
# ──────────────────────────────────────────────────────────────
def _dms_to_decimal(degrees, minutes, seconds, direction):
    """Convert DMS components to decimal degrees."""
    dd = abs(float(degrees)) + float(minutes) / 60 + float(seconds) / 3600
    if direction in ('S', 'W', 's', 'w'):
        dd *= -1
    elif float(degrees) < 0:
        dd *= -1
    return dd


def parse_coordinates(text):
    """
    Parse GPS coordinates from many input formats.

    Supported:
      - Decimal degrees:  46.0345, -118.2850  or  46.0345 -118.2850
      - DMS:  46°2'4.5"N 118°17'6.1"W   or   46° 2' 4.5" N, 118° 17' 6.1" W
      - Google Maps URL:  https://www.google.com/maps/@46.0346,-118.2855,477m/...
      - McGregor format:  46° 2 4.5093  -118° 17 6.0761  (degree min sec)

    Returns (lat, lon) tuple or None if parsing fails.
    """
    text = text.strip()
    if not text:
        return None

    # Google Maps URL: @lat,lon
    gmaps_at = re.search(r'@(-?\d+\.?\d*),(-?\d+\.?\d*)', text)
    if gmaps_at:
        return float(gmaps_at.group(1)), float(gmaps_at.group(2))
    gmaps_ll = re.search(r'll=(-?\d+\.?\d*),(-?\d+\.?\d*)', text)
    if gmaps_ll:
        return float(gmaps_ll.group(1)), float(gmaps_ll.group(2))
    gmaps_place = re.search(r'/place/[^/]+/@(-?\d+\.?\d*),(-?\d+\.?\d*)', text)
    if gmaps_place:
        return float(gmaps_place.group(1)), float(gmaps_place.group(2))

    # DMS with direction letters:  46°2'4.5"N 118°17'6.1"W
    dms_pattern = re.compile(
        r"""(-?\d+)\s*[°]\s*(\d+)\s*['′]\s*([\d.]+)\s*["″]?\s*([NSns])"""
        r"""\s*[,\s]+\s*"""
        r"""(-?\d+)\s*[°]\s*(\d+)\s*['′]\s*([\d.]+)\s*["″]?\s*([EWew])""",
        re.VERBOSE,
    )
    m = dms_pattern.search(text)
    if m:
        lat = _dms_to_decimal(m.group(1), m.group(2), m.group(3), m.group(4))
        lon = _dms_to_decimal(m.group(5), m.group(6), m.group(7), m.group(8))
        return lat, lon

    # DMS without direction letters:  46°2'4.5"  -118°17'6.1"
    dms_no_dir = re.compile(
        r"""(-?\d+)\s*[°]\s*(\d+)\s*['′]\s*([\d.]+)\s*["″]?"""
        r"""\s*[,\s]+\s*"""
        r"""(-?\d+)\s*[°]\s*(\d+)\s*['′]\s*([\d.]+)\s*["″]?""",
        re.VERBOSE,
    )
    m = dms_no_dir.search(text)
    if m:
        lat = _dms_to_decimal(m.group(1), m.group(2), m.group(3), 'N')
        lon = _dms_to_decimal(m.group(4), m.group(5), m.group(6), 'N')
        return lat, lon

    # McGregor format:  46° 2 4.5093  -118° 17 6.0761
    mcgregor_pattern = re.compile(
        r"""(-?\d+)\s*[°]\s+(\d+)\s+([\d.]+)"""
        r"""\s+"""
        r"""(-?\d+)\s*[°]\s+(\d+)\s+([\d.]+)""",
        re.VERBOSE,
    )
    m = mcgregor_pattern.search(text)
    if m:
        lat = _dms_to_decimal(m.group(1), m.group(2), m.group(3), 'N')
        lon = _dms_to_decimal(m.group(4), m.group(5), m.group(6), 'N')
        return lat, lon

    # Simple decimal degrees:  46.0345, -118.2850
    decimal_pattern = re.compile(r'(-?\d+\.?\d+)\s*[,\s]+\s*(-?\d+\.?\d+)')
    m = decimal_pattern.search(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
        if -90 <= lon <= 90 and -180 <= lat <= 180:
            return lon, lat

    return None


def geocode_trs(township, ns_dir, range_num, ew_dir, section, state="WA", meridian="WM"):
    """
    Geocode a TRS legal description to lat/lon using the BLM PLSS ArcGIS REST service.
    Returns dict with 'lat', 'lon', 'label', 'bounds', 'rings' or None on failure.
    """
    twp_str = f"T{int(township):03d}{ns_dir.upper()}"
    rng_str = f"R{int(range_num):03d}{ew_dir.upper()}"

    plss_url = (
        "https://gis.blm.gov/arcgis/rest/services/Cadastral/"
        "BLM_Natl_PLSS_CadNSDI/MapServer/1/query"
    )

    def _try_query(where_clause):
        params = {
            "where": where_clause,
            "outFields": "FRSTDIVID,FRSTDIVLAB,FRSTDIVNO",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "json",
        }
        resp = requests.get(plss_url, params=params, timeout=15)
        data = resp.json()
        if data.get("features"):
            feat = data["features"][0]
            geom = feat.get("geometry", {})
            attrs = feat.get("attributes", {})
            rings = geom.get("rings", [])
            if rings:
                ring = rings[0]
                avg_x = sum(p[0] for p in ring) / len(ring)
                avg_y = sum(p[1] for p in ring) / len(ring)
                label = attrs.get(
                    "FRSTDIVLAB",
                    f"T{township}{ns_dir} R{range_num}{ew_dir} Sec {section}",
                )
                return {
                    "lat": avg_y,
                    "lon": avg_x,
                    "label": label,
                    "bounds": [
                        [min(p[1] for p in ring), min(p[0] for p in ring)],
                        [max(p[1] for p in ring), max(p[0] for p in ring)],
                    ],
                    "rings": rings,
                }
        return None

    try:
        # First try: match by FRSTDIVID pattern and section number
        result = _try_query(
            f"FRSTDIVID LIKE '%{twp_str}{rng_str}%' "
            f"AND FRSTDIVNO='{int(section)}'"
        )
        if result:
            return result

        # Second try: broader PLSSID match
        result = _try_query(
            f"FRSTDIVNO='{int(section)}' AND PLSSID LIKE '%{twp_str}%{rng_str}%'"
        )
        return result

    except Exception:
        return None


def parse_trs_string(text):
    """
    Parse a TRS string into components.
    Handles: T6N R30E S12 | T.6N. R.30E. Sec.12 | 6N 30E 12 | Township 6 North ...
    """
    text = text.strip().upper()

    # T6N R30E S12  (with optional dots, spaces)
    p1 = re.search(
        r'T\.?\s*(\d+)\s*([NS])\.?\s*[,\s]*R\.?\s*(\d+)\s*([EW])\.?\s*[,\s]*S(?:EC)?\.?\s*(\d+)',
        text,
    )
    if p1:
        return {
            "township": int(p1.group(1)), "ns_dir": p1.group(2),
            "range": int(p1.group(3)), "ew_dir": p1.group(4),
            "section": int(p1.group(5)),
        }

    # Township 6 North Range 30 East Section 12
    p2 = re.search(
        r'TOWNSHIP\s+(\d+)\s+(NORTH|SOUTH)\s+RANGE\s+(\d+)\s+(EAST|WEST)\s+SECTION\s+(\d+)',
        text,
    )
    if p2:
        ns = "N" if p2.group(2) == "NORTH" else "S"
        ew = "E" if p2.group(4) == "EAST" else "W"
        return {
            "township": int(p2.group(1)), "ns_dir": ns,
            "range": int(p2.group(3)), "ew_dir": ew,
            "section": int(p2.group(5)),
        }

    # Compact: 6N 30E 12
    p3 = re.search(r'(\d+)\s*([NS])\s+(\d+)\s*([EW])\s+(\d+)', text)
    if p3:
        return {
            "township": int(p3.group(1)), "ns_dir": p3.group(2),
            "range": int(p3.group(3)), "ew_dir": p3.group(4),
            "section": int(p3.group(5)),
        }

    return None


# ──────────────────────────────────────────────────────────────
# Sidebar — Navigation + Upload + Stats
# ──────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Valley Air Map Builder 2.0")

    st.subheader("Navigation")
    if st.button("🗺️ Draw on Map", use_container_width=True,
                 type="primary" if st.session_state.page == "draw" else "secondary"):
        st.session_state.page = "draw"
        st.rerun()
    if st.button("📋 View / Edit Data", use_container_width=True,
                 type="primary" if st.session_state.page == "list" else "secondary"):
        st.session_state.page = "list"
        st.rerun()
    if st.button("📤 Export", use_container_width=True,
                 type="primary" if st.session_state.page == "export" else "secondary"):
        st.session_state.page = "export"
        st.rerun()

    st.divider()

    # Upload
    st.subheader("📂 Import Data")
    uploaded_file = st.file_uploader(
        "Upload zipped shapefile, GeoJSON, KML, KMZ, or Satloc .job",
        type=["zip", "geojson", "json", "kml", "kmz", "job"],
        key="file_upload",
    )

    if uploaded_file is not None:
        if st.button("Import", type="primary"):
            try:
                gdf = None

                if uploaded_file.name.endswith(".kmz"):
                    # Extract KML from KMZ and parse
                    kml_content = extract_kml_from_kmz(uploaded_file.read())
                    if kml_content:
                        gdf = parse_kml_to_gdf(kml_content)
                    else:
                        st.error("Could not extract KML from KMZ file.")
                elif uploaded_file.name.endswith(".kml"):
                    # Parse KML directly
                    gdf = parse_kml_to_gdf(uploaded_file.read())
                elif uploaded_file.name.endswith(".job"):
                    # Parse Satloc .job (boundaries) directly
                    gdf = parse_job_to_gdf(uploaded_file.read())
                elif uploaded_file.name.endswith(".zip"):
                    with tempfile.TemporaryDirectory() as tmpdir:
                        zip_path = os.path.join(tmpdir, "upload.zip")
                        with open(zip_path, "wb") as f:
                            f.write(uploaded_file.read())
                        with zipfile.ZipFile(zip_path, "r") as zf:
                            zf.extractall(tmpdir)
                        shp_files = [f for f in os.listdir(tmpdir) if f.endswith(".shp")]
                        if not shp_files:
                            st.error("No .shp file found in zip.")
                        else:
                            gdf = gpd.read_file(os.path.join(tmpdir, shp_files[0]))
                            gdf = gdf.to_crs(DEFAULT_CRS)
                else:
                    gdf = gpd.read_file(uploaded_file)
                    gdf = gdf.to_crs(DEFAULT_CRS)

                if gdf is not None and not gdf.empty:
                    count = 0
                    for _, row in gdf.iterrows():
                        geom = row.geometry
                        if geom is None or geom.is_empty:
                            continue
                        area = calc_area_acres(geom)
                        feature = {
                            "geometry": mapping(geom),
                            "properties": {
                                "name": str(row.get("name", row.get("NAME", f"Imported_{count+1}"))),
                                "category": str(row.get("category", row.get("CATEGORY", "Other"))),
                                "treatment_type": str(row.get("treatment_", row.get("treatment_type", "General Treatment"))),
                                "status": str(row.get("status", row.get("STATUS", "Planned"))),
                                "priority": str(row.get("priority", row.get("PRIORITY", "Medium"))),
                                "area_acres": area,
                                "notes": str(row.get("notes", row.get("NOTES", ""))),
                                "date_created": datetime.now().strftime("%Y-%m-%d"),
                            },
                        }
                        st.session_state.features.append(feature)
                        count += 1
                    st.success(f"Imported {count} features!")
                    st.rerun()
                else:
                    st.error("No features found in the uploaded file.")
            except Exception as e:
                st.error(f"Import error: {e}")

    st.divider()

    # Stats
    st.subheader("📊 Summary")
    n = len(st.session_state.features)
    st.metric("Treatment Areas", n)
    if n > 0:
        total_acres = sum(
            f.get("properties", {}).get("area_acres", 0)
            for f in st.session_state.features
        )
        st.metric("Total Acreage", f"{total_acres:,.2f} ac")

    st.divider()
    if st.button("🗑️ Clear All", type="secondary"):
        st.session_state.features = []
        st.session_state.pending_drawing = None
        st.rerun()


# ══════════════════════════════════════════════════════════════
# PAGE: Draw on Map
# ══════════════════════════════════════════════════════════════
if st.session_state.page == "draw":
    st.title("🗺️ Draw Treatment Areas")

    # ── Search Location ──
    with st.expander("🔍 Search Location", expanded=False):
        search_tab1, search_tab2 = st.tabs(["📍 GPS Coordinates", "📐 Legal Description (TRS)"])

        with search_tab1:
            st.caption(
                "Paste coordinates in any format: decimal degrees, DMS, "
                "or a Google Maps URL."
            )
            gps_input = st.text_input(
                "Coordinates",
                placeholder="46.0345, -118.2850  or  46°2'4.5\"N 118°17'6.1\"W  or  Google Maps URL",
                key="gps_search_input",
            )
            with st.expander("Format examples", expanded=False):
                st.code(
                    "46.0345, -118.2850\n"
                    "46°2'4.5\"N 118°17'6.1\"W\n"
                    "46° 2 4.5093  -118° 17 6.0761\n"
                    "https://www.google.com/maps/@46.0346,-118.2855,477m/...",
                    language=None,
                )
            if st.button("Search GPS", type="primary", key="btn_gps_search"):
                if gps_input:
                    result = parse_coordinates(gps_input)
                    if result:
                        lat, lon = result
                        st.session_state.search_result = {
                            "lat": lat, "lon": lon,
                            "label": f"{lat:.6f}, {lon:.6f}",
                            "zoom": 15,
                        }
                        st.success(f"Found: **{lat:.6f}, {lon:.6f}**")
                        st.rerun()
                    else:
                        st.error("Could not parse coordinates. Check the format and try again.")
                else:
                    st.warning("Enter coordinates above first.")

        with search_tab2:
            st.caption("Enter a Township/Range/Section to find the section on the map.")
            trs_input = st.text_input(
                "TRS",
                placeholder="T6N R30E S12   or   6N 30E 12",
                key="trs_search_input",
            )
            col_st, col_mer = st.columns(2)
            with col_st:
                trs_state = st.selectbox(
                    "State",
                    ["WA", "ID", "OR", "MT", "CA", "NV", "UT", "WY", "CO"],
                    key="trs_state",
                )
            with col_mer:
                meridian_map = {
                    "WA": "WM", "OR": "WM", "ID": "BM", "MT": "PM",
                    "CA": "MD", "NV": "MD", "UT": "SL", "WY": "WY", "CO": "NM",
                }
                default_mer = meridian_map.get(trs_state, "WM")
                mer_options = [
                    "WM (Willamette)", "BM (Boise)", "PM (Principal)",
                    "MD (Mt. Diablo)", "SL (Salt Lake)", "WY (Wind River)",
                    "NM (New Mexico)",
                ]
                mer_codes = ["WM", "BM", "PM", "MD", "SL", "WY", "NM"]
                trs_meridian = st.selectbox(
                    "Principal Meridian",
                    mer_options,
                    index=mer_codes.index(default_mer) if default_mer in mer_codes else 0,
                    key="trs_meridian",
                )
                meridian_code = trs_meridian.split(" ")[0]

            if st.button("Search TRS", type="primary", key="btn_trs_search"):
                if trs_input:
                    parsed = parse_trs_string(trs_input)
                    if parsed:
                        with st.spinner("Looking up section in BLM PLSS database..."):
                            result = geocode_trs(
                                parsed["township"], parsed["ns_dir"],
                                parsed["range"], parsed["ew_dir"],
                                parsed["section"],
                                state=trs_state, meridian=meridian_code,
                            )
                        if result:
                            st.session_state.search_result = {
                                "lat": result["lat"], "lon": result["lon"],
                                "label": (
                                    f"T{parsed['township']}{parsed['ns_dir']} "
                                    f"R{parsed['range']}{parsed['ew_dir']} "
                                    f"Sec {parsed['section']}"
                                ),
                                "zoom": 14,
                                "bounds": result.get("bounds"),
                                "rings": result.get("rings"),
                            }
                            st.success(
                                f"Found: **T{parsed['township']}{parsed['ns_dir']} "
                                f"R{parsed['range']}{parsed['ew_dir']} "
                                f"Sec {parsed['section']}** — "
                                f"Center: {result['lat']:.6f}, {result['lon']:.6f}"
                            )
                            st.rerun()
                        else:
                            st.error(
                                "Section not found in BLM PLSS database. "
                                "Check the township/range/section and state."
                            )
                    else:
                        st.error("Could not parse TRS. Use format: T6N R30E S12  or  6N 30E 12")
                else:
                    st.warning("Enter a TRS description above first.")

        # Show current search result
        if st.session_state.get("search_result"):
            sr = st.session_state.search_result
            st.info(f"📍 Map centered on: **{sr['label']}** ({sr['lat']:.6f}, {sr['lon']:.6f})")
            if st.button("Clear search", key="btn_clear_search"):
                st.session_state.search_result = None
                st.rerun()

    # ── STEP 1: The Map ──
    st.markdown("**Step 1:** Use the polygon or rectangle tool to draw an area on the map.")

    # Base map selector (replaces Leaflet LayerControl which crashes on rerun)
    base_map = st.radio(
        "Base Map",
        ["Terrain", "Satellite", "Street"],
        horizontal=True,
        key="base_map",
    )
    tile_options = {
        "Terrain":   ("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", "OpenTopoMap"),
        "Satellite": ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", "Esri"),
        "Street":    ("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", "OpenStreetMap"),
    }
    tile_url, tile_attr = tile_options[base_map]

    # Determine map center/zoom from search result, features, or default
    sr = st.session_state.get("search_result")
    if sr:
        map_center = [sr["lat"], sr["lon"]]
        map_zoom = sr.get("zoom", 15)
    elif st.session_state.features:
        all_coords = []
        for f in st.session_state.features:
            geom = shape(f["geometry"])
            c = geom.centroid
            all_coords.append((c.y, c.x))
        avg_lat = sum(c[0] for c in all_coords) / len(all_coords)
        avg_lon = sum(c[1] for c in all_coords) / len(all_coords)
        map_center = [avg_lat, avg_lon]
        map_zoom = 13
    else:
        map_center = [39.5, -98.35]
        map_zoom = 5

    m = folium.Map(location=map_center, zoom_start=map_zoom, tiles=tile_url, attr=tile_attr)

    # Add search result marker and section outline
    if sr:
        folium.Marker(
            [sr["lat"], sr["lon"]],
            popup=sr.get("label", "Search Result"),
            tooltip=sr.get("label", "Search Result"),
            icon=folium.Icon(color="red", icon="crosshairs", prefix="fa"),
        ).add_to(m)
        if sr.get("rings"):
            for ring in sr["rings"]:
                ring_coords = [[p[1], p[0]] for p in ring]
                folium.Polygon(
                    locations=ring_coords,
                    color="#ff6600", weight=2, fill=True,
                    fill_color="#ff6600", fill_opacity=0.1,
                    popup=sr.get("label", "Section"),
                    tooltip=f"Section: {sr.get('label', '')}",
                    dash_array="8",
                ).add_to(m)
        if sr.get("bounds"):
            m.fit_bounds(sr["bounds"], padding=[20, 20])

    # Show saved features
    if st.session_state.features:
        geojson_data = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": f["geometry"], "properties": f.get("properties", {})}
                for f in st.session_state.features
            ],
        }
        folium.GeoJson(
            geojson_data,
            style_function=style_function,
            tooltip=folium.GeoJsonTooltip(
                fields=["name", "category", "treatment_type", "area_acres"],
                aliases=["Name", "Category", "Type", "Acres"],
            ),
        ).add_to(m)

    Draw(
        export=False,
        draw_options={
            "polyline": False,
            "circle": False,
            "circlemarker": False,
            "marker": False,
            "polygon": {
                "allowIntersection": False,
                "showArea": True,
                "shapeOptions": {"color": "#ff6600", "weight": 3, "fillOpacity": 0.3},
            },
            "rectangle": {
                "shapeOptions": {"color": "#ff6600", "weight": 3, "fillOpacity": 0.3},
            },
        },
    ).add_to(m)

    map_data = st_folium(m, width=None, height=500, key="draw_map")

    # Capture drawing
    if map_data and map_data.get("last_active_drawing"):
        drawing = map_data["last_active_drawing"]
        if isinstance(drawing, dict) and "geometry" in drawing:
            coords = drawing["geometry"].get("coordinates", [])
            if coords:
                st.session_state.pending_drawing = drawing

    # ── STEP 2: Attributes ──
    st.divider()
    pending = st.session_state.pending_drawing
    if pending:
        try:
            geom_preview = shape(pending["geometry"])
            area_preview = calc_area_acres(geom_preview)
        except Exception:
            area_preview = 0.0

        st.markdown(f"**Step 2:** Shape captured! **{area_preview:,.2f} acres** — fill in details below.")

        col1, col2, col3 = st.columns(3)
        with col1:
            name = st.text_input("Name *", placeholder="e.g. Unit 42 - North Ridge", key="draw_name")
            treatment_sel = st.selectbox(
                "Category & Treatment Type *",
                ALL_TREATMENT_TYPES,
                key="draw_treatment",
            )
        with col2:
            status = st.selectbox("Status", ["Planned", "In Progress", "Completed", "On Hold", "Cancelled"], key="draw_status")
            priority = st.selectbox("Priority", ["Low", "Medium", "High", "Critical"], key="draw_priority")
        with col3:
            notes = st.text_area("Notes", placeholder="Additional details...", key="draw_notes", height=120)

        col_save, col_discard, _ = st.columns([1, 1, 3])
        with col_save:
            if st.button("Save Treatment Area", type="primary", use_container_width=True):
                if not name or not name.strip():
                    st.error("Please enter a name.")
                else:
                    category, treatment_type = parse_treatment_selection(treatment_sel)
                    geom = shape(pending["geometry"])
                    area_acres = calc_area_acres(geom)
                    feature = {
                        "geometry": mapping(geom),
                        "properties": {
                            "name": name.strip(),
                            "category": category,
                            "treatment_type": treatment_type,
                            "status": status,
                            "priority": priority,
                            "area_acres": area_acres,
                            "notes": notes,
                            "date_created": datetime.now().strftime("%Y-%m-%d"),
                        },
                    }
                    st.session_state.features.append(feature)
                    st.session_state.pending_drawing = None
                    st.success(f"Saved '{name.strip()}' — {area_acres:,.2f} acres!")
                    st.rerun()
        with col_discard:
            if st.button("Discard Drawing", use_container_width=True):
                st.session_state.pending_drawing = None
                st.rerun()
    else:
        st.info("Draw a shape on the map above. Once captured, a form will appear here to fill in treatment details.")

    # Legend
    st.divider()
    st.markdown(
        '<span style="color:#228B22">■</span> Forestry &nbsp;&nbsp;'
        '<span style="color:#DAA520">■</span> Agriculture &nbsp;&nbsp;'
        '<span style="color:#4682B4">■</span> Environmental &nbsp;&nbsp;'
        '<span style="color:#808080">■</span> Other',
        unsafe_allow_html=True,
    )


# ══════════════════════════════════════════════════════════════
# PAGE: View / Edit Data
# ══════════════════════════════════════════════════════════════
elif st.session_state.page == "list":
    st.title("📋 Treatment Areas")

    if not st.session_state.features:
        st.info("No treatment areas yet. Go to **Draw on Map** to create some, or import a shapefile.")
    else:
        for i, feat in enumerate(st.session_state.features):
            props = feat.get("properties", {})
            label = (
                f"**{props.get('name', f'Feature {i+1}')}** — "
                f"{props.get('category', '')} | {props.get('treatment_type', '')} | "
                f"{props.get('area_acres', 0):,.2f} ac"
            )
            with st.expander(label, expanded=False):
                col1, col2, col3 = st.columns(3)
                with col1:
                    new_name = st.text_input("Name", value=props.get("name", ""), key=f"name_{i}")
                    cat_list = list(TREATMENT_CATEGORIES.keys())
                    cur_cat = props.get("category", "Other")
                    cat_idx = cat_list.index(cur_cat) if cur_cat in cat_list else 3
                    new_cat = st.selectbox("Category", cat_list, index=cat_idx, key=f"cat_{i}")
                with col2:
                    types_for_cat = TREATMENT_CATEGORIES.get(new_cat, ["General Treatment"])
                    cur_type = props.get("treatment_type", "General Treatment")
                    type_idx = types_for_cat.index(cur_type) if cur_type in types_for_cat else 0
                    new_type = st.selectbox("Treatment Type", types_for_cat, index=type_idx, key=f"type_{i}")
                    statuses = ["Planned", "In Progress", "Completed", "On Hold", "Cancelled"]
                    cur_status = props.get("status", "Planned")
                    status_idx = statuses.index(cur_status) if cur_status in statuses else 0
                    new_status = st.selectbox("Status", statuses, index=status_idx, key=f"status_{i}")
                with col3:
                    priorities = ["Low", "Medium", "High", "Critical"]
                    cur_pri = props.get("priority", "Medium")
                    pri_idx = priorities.index(cur_pri) if cur_pri in priorities else 1
                    new_priority = st.selectbox("Priority", priorities, index=pri_idx, key=f"pri_{i}")
                    st.metric("Area", f"{props.get('area_acres', 0):,.2f} ac")
                new_notes = st.text_area("Notes", value=props.get("notes", ""), key=f"notes_{i}")

                col_save, col_del = st.columns(2)
                with col_save:
                    if st.button("Save Changes", key=f"save_{i}"):
                        st.session_state.features[i]["properties"].update({
                            "name": new_name,
                            "category": new_cat,
                            "treatment_type": new_type,
                            "status": new_status,
                            "priority": new_priority,
                            "notes": new_notes,
                        })
                        st.success("Saved!")
                        st.rerun()
                with col_del:
                    if st.button("Delete", key=f"del_{i}", type="secondary"):
                        st.session_state.features.pop(i)
                        st.rerun()


# ══════════════════════════════════════════════════════════════
# PAGE: Export
# ══════════════════════════════════════════════════════════════
elif st.session_state.page == "export":
    st.title("📤 Export Treatment Areas")

    if not st.session_state.features:
        st.info("No treatment areas to export. Draw on the map or import data first.")
    else:
        col_opts, col_preview = st.columns([1, 2])

        with col_opts:
            export_name = st.text_input("Filename", value="treatment_areas", key="export_name")
            export_crs = st.selectbox("Coordinate Reference System", [
                "EPSG:4326 (WGS 84 — Lat/Lon)",
                "EPSG:5070 (NAD83 Conus Albers)",
                "EPSG:3857 (Web Mercator)",
                "EPSG:26910 (UTM Zone 10N)",
                "EPSG:26911 (UTM Zone 11N)",
                "EPSG:26912 (UTM Zone 12N)",
                "EPSG:26913 (UTM Zone 13N)",
            ], key="export_crs")
            crs_code = export_crs.split(" ")[0]

            export_format = st.selectbox("Format", [
                "Shapefile (.shp — zipped)",
                "GeoJSON (.geojson)",
                "KML (.kml)",
                "KMZ (.kmz)",
                "Satloc Guidance (.job)",
                "PDF Report (.pdf)",
            ], key="export_format")

            # Satloc job number (device convention: a 3-digit number). Always
            # exported in WGS84 lat/lon regardless of the CRS selection above.
            if ".job" in export_format:
                st.text_input("Satloc job number (3-digit)", value="900",
                              max_chars=6, key="job_number")
                st.caption("Exported in WGS84 lat/lon. Drop the .job on the **root** of the device USB.")

            # Manual job info for the PDF / pilot paperwork (no FieldPulse link here).
            if "PDF" in export_format:
                st.caption("**Pilot Paperwork Info** (manual)")
                st.text_input("Customer Name", key="wo_customer",
                              placeholder="e.g. Watson Ag")
                st.text_input("Crop", key="wo_crop",
                              placeholder="e.g. Winter Wheat")

            st.caption("**Optional Filters**")
            filter_cats = st.multiselect("Filter by Category",
                                         list(TREATMENT_CATEGORIES.keys()), key="filter_cats")
            filter_status = st.multiselect("Filter by Status",
                                           ["Planned", "In Progress", "Completed", "On Hold", "Cancelled"],
                                           key="filter_status")

        with col_preview:
            gdf = features_to_gdf(st.session_state.features)
            if filter_cats:
                gdf = gdf[gdf["category"].isin(filter_cats)]
            if filter_status:
                gdf = gdf[gdf["status"].isin(filter_status)]

            if crs_code != "EPSG:4326":
                gdf_export = gdf.to_crs(crs_code)
            else:
                gdf_export = gdf.copy()

            st.caption(f"**Preview** — {len(gdf_export)} features")
            if len(gdf_export) > 0:
                preview_df = gdf_export.drop(columns=["geometry"]).copy()
                st.dataframe(preview_df, use_container_width=True, height=300)

                st.divider()
                if "Shapefile" in export_format:
                    shp_buf = export_shapefile(gdf_export, filename=export_name)
                    st.download_button(
                        "Download Shapefile (.zip)",
                        data=shp_buf,
                        file_name=f"{export_name}.zip",
                        mime="application/zip",
                        type="primary",
                    )
                elif "GeoJSON" in export_format:
                    geojson_str = gdf_export.to_json()
                    st.download_button(
                        "Download GeoJSON",
                        data=geojson_str,
                        file_name=f"{export_name}.geojson",
                        mime="application/json",
                        type="primary",
                    )
                elif "KML" in export_format and "KMZ" not in export_format:
                    kml_buf = export_kml(gdf_export, filename=export_name)
                    st.download_button(
                        "Download KML",
                        data=kml_buf,
                        file_name=f"{export_name}.kml",
                        mime="application/vnd.google-earth.kml+xml",
                        type="primary",
                    )
                elif "KMZ" in export_format:
                    kmz_buf = export_kmz(gdf_export, filename=export_name)
                    st.download_button(
                        "Download KMZ",
                        data=kmz_buf,
                        file_name=f"{export_name}.kmz",
                        mime="application/vnd.google-earth.kmz",
                        type="primary",
                    )
                elif ".job" in export_format:
                    job_num = (st.session_state.get("job_number") or "900").strip() or "900"
                    job_buf = export_job(gdf_export, filename=export_name, job_num=job_num)
                    st.download_button(
                        "Download Satloc .job",
                        data=job_buf,
                        file_name=f"{job_num}.job",
                        mime="text/plain",
                        type="primary",
                    )
                elif "PDF" in export_format:
                    pdf_buf = export_pdf(
                        gdf_export, filename=export_name,
                        customer=st.session_state.get("wo_customer", ""),
                        crop=st.session_state.get("wo_crop", ""),
                    )
                    st.download_button(
                        "Download PDF Report",
                        data=pdf_buf,
                        file_name=f"{export_name}.pdf",
                        mime="application/pdf",
                        type="primary",
                    )
            else:
                st.warning("No features match the current filters.")


# Footer
st.divider()
st.caption("Valley Air Map Builder 2.0 • Built with Streamlit, Folium & GeoPandas")
