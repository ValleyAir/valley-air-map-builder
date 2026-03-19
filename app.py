"""
Valley Air Map Builder 1.0
====================
Draw treatment areas on an interactive map, assign attributes,
and export as ESRI Shapefiles (.shp) or GeoJSON.

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
import tempfile
import zipfile
import io
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
st.set_page_config(page_title="Valley Air Map Builder 1.0", page_icon="🗺️", layout="wide")


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


def render_map_image(gdf):
    """Render treatment areas onto a satellite basemap as a PNG image using matplotlib."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import contextily as cx

    # Project to Web Mercator for basemap tiles
    gdf_4326 = gdf.to_crs("EPSG:4326") if str(gdf.crs) != "EPSG:4326" else gdf
    gdf_3857 = gdf_4326.to_crs("EPSG:3857")

    cat_colors = {
        "Forestry / Vegetation": "#22cc22",
        "Agriculture":           "#ddaa00",
        "Environmental Cleanup": "#4488ff",
        "Other":                 "#aaaaaa",
    }

    fig, ax = plt.subplots(1, 1, figsize=(14, 8))

    # Draw each polygon colored by category
    for cat, color in cat_colors.items():
        subset = gdf_3857[gdf_3857["category"] == cat]
        if len(subset) > 0:
            subset.plot(ax=ax, color=color, edgecolor="white",
                        linewidth=1.5, alpha=0.55, label=cat)

    # Anything not matching known categories
    other = gdf_3857[~gdf_3857["category"].isin(cat_colors.keys())]
    if len(other) > 0:
        other.plot(ax=ax, color="#aaaaaa", edgecolor="white",
                   linewidth=1.5, alpha=0.55, label="Other")

    # Add labels at polygon centroids
    for _, row in gdf_3857.iterrows():
        centroid = row.geometry.centroid
        name = row.get("name", "")
        acres = row.get("area_acres", 0)
        ax.annotate(
            f"{name}\n{acres:,.1f} ac",
            xy=(centroid.x, centroid.y),
            ha="center", va="center",
            fontsize=7, fontweight="bold", color="white",
            path_effects=[
                matplotlib.patheffects.withStroke(linewidth=2, foreground="black")
            ],
        )

    # Add satellite basemap tiles
    try:
        cx.add_basemap(ax, source=cx.providers.Esri.WorldImagery, zoom="auto")
    except Exception:
        try:
            cx.add_basemap(ax, source=cx.providers.OpenTopoMap, zoom="auto")
        except Exception:
            pass  # No basemap — polygons still render on white background

    # Pad bounds so polygons aren't edge-to-edge
    xmin, ymin, xmax, ymax = gdf_3857.total_bounds
    dx = (xmax - xmin) * 0.15 or 5000
    dy = (ymax - ymin) * 0.15 or 5000
    ax.set_xlim(xmin - dx, xmax + dx)
    ax.set_ylim(ymin - dy, ymax + dy)
    ax.set_axis_off()

    # Build manual legend (GeoDataFrame.plot doesn't auto-create legend handles)
    from matplotlib.patches import Patch
    legend_handles = []
    for cat, color in cat_colors.items():
        if cat in [row.get("category", "") for _, row in gdf_3857.iterrows()]:
            legend_handles.append(Patch(facecolor=color, edgecolor="white", alpha=0.7, label=cat))
    if legend_handles:
        ax.legend(handles=legend_handles, loc="lower left", fontsize=8, framealpha=0.85)

    ax.set_title("Treatment Area Overview", fontsize=12, fontweight="bold", pad=10)
    plt.tight_layout()

    # Save to bytes
    img_buf = io.BytesIO()
    fig.savefig(img_buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    img_buf.seek(0)
    return img_buf


def export_pdf(gdf, filename="treatment_areas"):
    """Generate a PDF report with a map overview image, work order form, and treatment area data table."""
    buf = io.BytesIO()
    margin = 0.5 * inch

    # All pages portrait orientation
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            topMargin=margin, bottomMargin=margin,
                            leftMargin=margin, rightMargin=margin)

    styles = getSampleStyleSheet()
    story = []

    # ── Page 1: Title + Map (portrait) ──
    title_style = ParagraphStyle("Title2", parent=styles["Title"], fontSize=20, spaceAfter=4)
    story.append(Paragraph("Valley Air Map Builder — Treatment Area Report", title_style))

    total_acres = gdf["area_acres"].sum() if "area_acres" in gdf.columns else 0
    story.append(Paragraph(
        f"Generated {datetime.now().strftime('%B %d, %Y at %I:%M %p')} &nbsp;|&nbsp; "
        f"<b>{len(gdf)}</b> treatment area(s) &nbsp;|&nbsp; "
        f"<b>{total_acres:,.2f}</b> total acres",
        styles["Normal"],
    ))
    story.append(Spacer(1, 10))

    # Render map image — keep temp file alive until after doc.build()
    map_tmp_path = None
    try:
        map_img_buf = render_map_image(gdf)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(map_img_buf.getvalue())
            map_tmp_path = tmp.name
        img = Image(map_tmp_path, width=7.5*inch, height=5.5*inch)
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

    # ── Page 2: Work Order Form ──
    story.append(PageBreak())
    work_order_elements = build_work_order_page(gdf, styles)
    story.extend(work_order_elements)

    # ── Page 3: Data Table ──
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

    col_widths = [1.1*inch, 0.9*inch, 0.9*inch, 0.7*inch, 0.6*inch, 0.7*inch, 0.7*inch, 1.9*inch]
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

    # Clean up temp map image now that the PDF is built
    if map_tmp_path and os.path.exists(map_tmp_path):
        os.unlink(map_tmp_path)

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


def build_work_order_page(gdf, styles):
    """
    Build a professional work order form page for Valley Air LLC.
    Uses portrait page width (7.5" usable) with generous spacing for handwriting.

    Args:
        gdf: GeoDataFrame with location data containing 'name', 'area_acres', 'category' columns
        styles: reportlab style sheet from getSampleStyleSheet()

    Returns:
        list of reportlab story elements (Paragraphs, Tables, Spacers, etc.)
    """
    story = []

    # Custom styles
    header_style = ParagraphStyle(
        'WorkOrderHeader',
        parent=styles['Heading1'],
        fontSize=18,
        textColor=colors.HexColor('#2c3e50'),
        spaceAfter=6,
        alignment=1  # center
    )

    section_header_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Normal'],
        fontSize=10,
        textColor=colors.white,
        fontName='Helvetica-Bold',
        spaceAfter=0,
        leftIndent=4
    )

    small_bold = ParagraphStyle(
        'SmallBold',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.black,
        fontName='Helvetica-Bold',
        spaceAfter=2
    )

    current_date = datetime.now().strftime("%m/%d/%Y")
    full_width = 7.5 * inch  # Portrait letter: 8.5" - 2*0.5" margins

    # ===== HEADER SECTION =====
    story.append(Paragraph("Valley Air LLC — Work Order", header_style))
    story.append(Spacer(1, 0.15*inch))

    # Header row: Job #, Status, Date (full width)
    header_data = [
        ['Job #: _________________________', 'Status: _________________________', f'Date: {current_date}']
    ]
    header_table = Table(header_data, colWidths=[3.2*inch, 3.2*inch, 3.6*inch])
    header_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 9),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('LINEBELOW', (0, 0), (0, 0), 0.5, colors.black),
        ('LINEBELOW', (1, 0), (1, 0), 0.5, colors.black),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 0.20*inch))

    # ===== SCHEDULING SECTION =====
    section_bg = colors.HexColor('#cccccc')
    story.append(Table(
        [['SCHEDULING']],
        colWidths=[full_width],
        style=TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), section_bg),
            ('FONT', (0, 0), (-1, -1), 'Helvetica-Bold', 10),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ])
    ))

    scheduling_data = [[
        Paragraph('<b>Call Date</b><br/>____________________', styles['Normal']),
        Paragraph('<b>Date Proposed</b><br/>____________________', styles['Normal']),
        Paragraph('<b>Time Proposed</b><br/>____________________', styles['Normal']),
        Paragraph('<b>Schedule Date</b><br/>____________________', styles['Normal']),
        Paragraph('<b>Date Expires</b><br/>____________________', styles['Normal']),
        Paragraph('<b>Consultant</b><br/>____________________', styles['Normal'])
    ]]
    scheduling_table = Table(scheduling_data, colWidths=[1.65*inch, 1.65*inch, 1.65*inch, 1.65*inch, 1.65*inch, 1.65*inch])
    scheduling_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 8),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    story.append(scheduling_table)
    story.append(Spacer(1, 0.20*inch))

    # ===== LOCATIONS TABLE =====
    story.append(Table(
        [['LOCATIONS']],
        colWidths=[full_width],
        style=TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), section_bg),
            ('FONT', (0, 0), (-1, -1), 'Helvetica-Bold', 10),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ])
    ))

    # Build locations table
    locations_data = [['Map #', 'Location/Customer', 'Acres', 'Planted', 'Applied', 'Wind', 'Crop', 'Strip', 'Pests']]
    total_acres = 0.0

    for idx, row in gdf.iterrows():
        map_num = idx + 1
        location_name = row.get('name', '')
        acres = row.get('area_acres', 0.0)
        crop = row.get('category', '')

        total_acres += acres

        locations_data.append([
            str(map_num),
            location_name,  # Full name, no truncation
            f"{acres:.2f}",
            '___________',
            '___________',
            '___________',
            crop,  # Full category, no truncation
            '___________',
            '___________'
        ])

    # Add 3-4 extra blank rows for additional locations
    for _ in range(4):
        locations_data.append([
            '____',
            '___________________',
            '________',
            '___________',
            '___________',
            '___________',
            '___________________',
            '___________',
            '___________'
        ])

    # Add total row
    locations_data.append([
        '',
        '',
        f'Total Applied Acres: {total_acres:.2f}',
        '',
        '',
        '',
        '',
        '',
        ''
    ])

    locations_table = Table(locations_data, colWidths=[0.45*inch, 2.5*inch, 0.6*inch, 0.75*inch, 0.75*inch, 0.65*inch, 1.2*inch, 0.75*inch, 0.75*inch])

    # Style locations table
    header_fill = colors.HexColor('#2c3e50')
    locations_table.setStyle(TableStyle([
        # Header row
        ('BACKGROUND', (0, 0), (-1, 0), header_fill),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 8),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, 0), 6),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),

        # Data rows
        ('FONT', (0, 1), (-1, -2), 'Helvetica', 8),
        ('ALIGN', (0, 1), (-1, -2), 'CENTER'),
        ('VALIGN', (0, 1), (-1, -2), 'TOP'),
        ('TOPPADDING', (0, 1), (-1, -2), 8),
        ('BOTTOMPADDING', (0, 1), (-1, -2), 8),

        # Alternating row backgrounds
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, colors.HexColor('#f0f0f0')]),

        # Total row
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#e8e8e8')),
        ('FONT', (0, -1), (-1, -1), 'Helvetica-Bold', 8),
        ('ALIGN', (0, -1), (-1, -1), 'CENTER'),
        ('TOPPADDING', (0, -1), (-1, -1), 6),
        ('BOTTOMPADDING', (0, -1), (-1, -1), 6),

        # Grid lines
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    story.append(locations_table)
    story.append(Spacer(1, 0.20*inch))

    # ===== CHEMICALS / CHARGES TABLE =====
    story.append(Table(
        [['CHEMICALS / CHARGES']],
        colWidths=[full_width],
        style=TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), section_bg),
            ('FONT', (0, 0), (-1, -1), 'Helvetica-Bold', 10),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ])
    ))

    chemicals_data = [['Chemical/Charge', 'Vendor', 'Rate/ac', 'UM', 'Total Applied']]
    for _ in range(6):
        chemicals_data.append(['_____________________', '___________________', '__________', '__________', '__________'])

    chemicals_table = Table(chemicals_data, colWidths=[2.5*inch, 2.5*inch, 1.5*inch, 1.5*inch, 2.0*inch])
    chemicals_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), header_fill),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 8),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, 0), 6),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),

        ('FONT', (0, 1), (-1, -1), 'Helvetica', 8),
        ('ALIGN', (0, 1), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 1), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 1), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 8),

        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f0f0f0')]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    story.append(chemicals_table)
    story.append(Spacer(1, 0.08*inch))

    # Chemicals footer info in full-width table
    chem_footer_data = [['Diluent Rate: ____________________', 'Hours Reentry: ____________________', 'Days Preharvest: ____________________']]
    chem_footer_table = Table(chem_footer_data, colWidths=[3.3*inch, 3.3*inch, 3.4*inch])
    chem_footer_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 8),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(chem_footer_table)
    story.append(Spacer(1, 0.20*inch))

    # ===== LOADER WORKSHEET SECTION =====
    story.append(Table(
        [['LOADER WORKSHEET']],
        colWidths=[full_width],
        style=TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), section_bg),
            ('FONT', (0, 0), (-1, -1), 'Helvetica-Bold', 10),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ])
    ))

    loader_row1_data = [['Select Applicator: ____________________', 'Vehicle: ____________________', 'Vehicle Capacity: ____________________', 'Rate: __________', 'GL: __________']]
    loader_row1_table = Table(loader_row1_data, colWidths=[2.0*inch, 2.0*inch, 2.4*inch, 1.6*inch, 1.8*inch])
    loader_row1_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 8),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    story.append(loader_row1_table)

    loader_row2_data = [['Acre: __________', f'Total Job Acres: {total_acres:.2f}', 'Loads: __________', '', '']]
    loader_row2_table = Table(loader_row2_data, colWidths=[2.0*inch, 2.0*inch, 2.4*inch, 1.6*inch, 1.8*inch])
    loader_row2_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 8),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    story.append(loader_row2_table)
    story.append(Spacer(1, 0.20*inch))

    # ===== APPLIED INFO SECTION =====
    story.append(Table(
        [['APPLIED INFO']],
        colWidths=[full_width],
        style=TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), section_bg),
            ('FONT', (0, 0), (-1, -1), 'Helvetica-Bold', 10),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ])
    ))

    applied_row1_data = [['Applicator: ____________________', 'Vehicle: ____________________', 'Application Date: ____________________']]
    applied_row1_table = Table(applied_row1_data, colWidths=[3.2*inch, 3.2*inch, 3.6*inch])
    applied_row1_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 8),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    story.append(applied_row1_table)

    applied_row2_data = [['Beg. Tach: __________', 'End Tach: __________', 'Net Tach: __________', 'Flights: __________', 'Starts: __________']]
    applied_row2_table = Table(applied_row2_data, colWidths=[2.0*inch, 2.0*inch, 2.0*inch, 2.0*inch, 2.0*inch])
    applied_row2_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 8),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    story.append(applied_row2_table)
    story.append(Spacer(1, 0.12*inch))

    # Weather sub-section
    story.append(Table(
        [['WEATHER (Start)']],
        colWidths=[full_width],
        style=TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), section_bg),
            ('FONT', (0, 0), (-1, -1), 'Helvetica-Bold', 9),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ])
    ))

    weather_row1_data = [['Start Time: __________', 'Start Temp (°F): __________', 'Start Wind Dir: __________', 'Start Wind mph: __________', 'Start Humidity: __________']]
    weather_row1_table = Table(weather_row1_data, colWidths=[2.0*inch, 2.0*inch, 2.0*inch, 2.0*inch, 2.0*inch])
    weather_row1_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 8),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    story.append(weather_row1_table)

    story.append(Table(
        [['WEATHER (End)']],
        colWidths=[full_width],
        style=TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), section_bg),
            ('FONT', (0, 0), (-1, -1), 'Helvetica-Bold', 9),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ])
    ))

    weather_row2_data = [['End Time: __________', 'End Temp (°F): __________', 'End Wind Dir: __________', 'End Wind mph: __________', 'End Humidity: __________']]
    weather_row2_table = Table(weather_row2_data, colWidths=[2.0*inch, 2.0*inch, 2.0*inch, 2.0*inch, 2.0*inch])
    weather_row2_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 8),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    story.append(weather_row2_table)

    story.append(Spacer(1, 0.10*inch))

    applied_row3_data = [['Total Time: ____________________']]
    applied_row3_table = Table(applied_row3_data, colWidths=[full_width])
    applied_row3_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 8),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    story.append(applied_row3_table)
    story.append(Spacer(1, 0.15*inch))

    # Comments section
    story.append(Table(
        [['COMMENTS']],
        colWidths=[full_width],
        style=TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), section_bg),
            ('FONT', (0, 0), (-1, -1), 'Helvetica-Bold', 10),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ])
    ))

    comment_data = [['_' * 200]]
    comment_table = Table(comment_data, colWidths=[full_width])
    comment_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 7),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
        ('HEIGHT', (0, 0), (-1, -1), 1.0*inch),
    ]))
    story.append(comment_table)

    return story


# ──────────────────────────────────────────────────────────────
# Sidebar — Navigation + Upload + Stats
# ──────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Valley Air Map Builder 1.0")

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
        "Upload zipped shapefile, GeoJSON, KML, or KMZ",
        type=["zip", "geojson", "json", "kml", "kmz"],
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

    m = folium.Map(location=[39.5, -98.35], zoom_start=5, tiles=tile_url, attr=tile_attr)

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
                "PDF Report (.pdf)"
            ], key="export_format")

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
                elif "PDF" in export_format:
                    pdf_buf = export_pdf(gdf_export, filename=export_name)
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
st.caption("Valley Air Map Builder 1.0 • Built with Streamlit, Folium & GeoPandas")
