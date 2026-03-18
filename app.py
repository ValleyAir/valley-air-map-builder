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
from shapely.geometry import shape, mapping
import os
import tempfile
import zipfile
import io
from datetime import datetime
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# ──────────────────────────────────────────────────────────────
# Page Config
# ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="Valley Air Map Builder 1.0", page_icon="🗺️", layout="wide")

# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────
TREATMENT_CATEGORIES = {
    "Forestry / Vegetation": [
        "Timber Harvest", "Fuels Reduction", "Reforestation", "Prescribed Burn",
        "Thinning", "Brush Clearing", "Habitat Restoration", "Other Forestry",
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
        "Agriculture": "#ddaa00",
        "Environmental Cleanup": "#4488ff",
        "Other": "#aaaaaa",
    }

    fig, ax = plt.subplots(1, 1, figsize=(14, 8))

    # Draw each polygon colored by category
    for cat, color in cat_colors.items():
        subset = gdf_3857[gdf_3857["category"] == cat]
        if len(subset) > 0:
            subset.plot(ax=ax, color=color, edgecolor="white", linewidth=1.5,
                        alpha=0.55, label=cat)

    # Anything not matching known categories
    other = gdf_3857[~gdf_3857["category"].isin(cat_colors.keys())]
    if len(other) > 0:
        other.plot(ax=ax, color="#aaaaaa", edgecolor="white", linewidth=1.5,
                   alpha=0.55, label="Other")

    # Add labels at polygon centroids
    for _, row in gdf_3857.iterrows():
        centroid = row.geometry.centroid
        name = row.get("name", "")
        acres = row.get("area_acres", 0)
        ax.annotate(
            f"{name}\n{acres:,.1f} ac",
            xy=(centroid.x, centroid.y),
            ha="center", va="center",
            fontsize=7, fontweight="bold",
            color="white",
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
    """Generate a PDF report with a map overview image and treatment area data table."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(letter),
                            topMargin=0.5*inch, bottomMargin=0.5*inch,
                            leftMargin=0.5*inch, rightMargin=0.5*inch)
    styles = getSampleStyleSheet()
    story = []

    # ── Page 1: Title + Map ──
    title_style = ParagraphStyle("Title2", parent=styles["Title"], fontSize=20,
                                  spaceAfter=4)
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
        img = Image(map_tmp_path, width=9.5*inch, height=5.2*inch)
        story.append(img)
    except Exception as e:
        story.append(Paragraph(
            f"<i>Could not render map image: {e}</i>",
            styles["Normal"],
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

    # ── Page 2: Data Table ──
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

    col_widths = [1.4*inch, 1.2*inch, 1.2*inch, 0.8*inch, 0.7*inch, 0.8*inch, 0.8*inch, 2.1*inch]
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


# ──────────────────────────────────────────────────────────────
# Sidebar — Navigation + Upload + Stats
# ──────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Valley Air Map Builder 1.0")

    st.subheader("Navigation")
    if st.button("🗺️  Draw on Map", use_container_width=True,
                 type="primary" if st.session_state.page == "draw" else "secondary"):
        st.session_state.page = "draw"
        st.rerun()
    if st.button("📋  View / Edit Data", use_container_width=True,
                 type="primary" if st.session_state.page == "list" else "secondary"):
        st.session_state.page = "list"
        st.rerun()
    if st.button("📤  Export", use_container_width=True,
                 type="primary" if st.session_state.page == "export" else "secondary"):
        st.session_state.page = "export"
        st.rerun()

    st.divider()

    # Upload
    st.subheader("📂 Import Data")
    uploaded_file = st.file_uploader(
        "Upload zipped shapefile or GeoJSON",
        type=["zip", "geojson", "json"],
        key="file_upload",
    )
    if uploaded_file is not None:
        if st.button("Import", type="primary"):
            try:
                if uploaded_file.name.endswith(".zip"):
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
            except Exception as e:
                st.error(f"Import error: {e}")

    st.divider()

    # Stats
    st.subheader("📊 Summary")
    n = len(st.session_state.features)
    st.metric("Treatment Areas", n)
    if n > 0:
        total_acres = sum(
            f.get("properties", {}).get("area_acres", 0) for f in st.session_state.features
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
        "Terrain": ("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", "OpenTopoMap"),
        "Satellite": ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", "Esri"),
        "Street": ("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", "OpenStreetMap"),
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
            "polyline": False, "circle": False, "circlemarker": False, "marker": False,
            "polygon": {
                "allowIntersection": False, "showArea": True,
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
                            "name": new_name, "category": new_cat,
                            "treatment_type": new_type, "status": new_status,
                            "priority": new_priority, "notes": new_notes,
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
                "Shapefile (.shp — zipped)", "GeoJSON (.geojson)",
                "PDF Report (.pdf)"
            ], key="export_format")

            st.caption("**Optional Filters**")
            filter_cats = st.multiselect("Filter by Category", list(TREATMENT_CATEGORIES.keys()), key="filter_cats")
            filter_status = st.multiselect("Filter by Status",
                ["Planned", "In Progress", "Completed", "On Hold", "Cancelled"], key="filter_status")

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
                        "Download Shapefile (.zip)", data=shp_buf,
                        file_name=f"{export_name}.zip", mime="application/zip", type="primary",
                    )
                elif "GeoJSON" in export_format:
                    geojson_str = gdf_export.to_json()
                    st.download_button(
                        "Download GeoJSON", data=geojson_str,
                        file_name=f"{export_name}.geojson", mime="application/json", type="primary",
                    )
                elif "PDF" in export_format:
                    pdf_buf = export_pdf(gdf_export, filename=export_name)
                    st.download_button(
                        "Download PDF Report", data=pdf_buf,
                        file_name=f"{export_name}.pdf", mime="application/pdf", type="primary",
                    )
            else:
                st.warning("No features match the current filters.")

# Footer
st.divider()
st.caption("Valley Air Map Builder 1.0 • Built with Streamlit, Folium & GeoPandas")
