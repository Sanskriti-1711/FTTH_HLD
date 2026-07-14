FROM qgis/qgis:3.44

ENV PYTHONUNBUFFERED=1 \
    QT_QPA_PLATFORM=offscreen \
    QGIS_PLUGINPATH=/app \
    QGIS_EXECUTABLE=/usr/bin/qgis_process

WORKDIR /app

COPY HLDPlanning /app/HLDPlanning
COPY web/backend /app/web/backend

WORKDIR /app/web/backend

# qgis/qgis:3.44 ships Ubuntu 25.10 with PEP 668 (externally-managed Python).
# --break-system-packages bypasses this guard without affecting the runtime.
#
# Step 1: Web stack only (no numpy, no geospatial). Use --ignore-installed here
# to work around debian's typing_extensions RECORD-file issue. None of these
# packages conflict with QGIS's system libs.
RUN python3 -m pip install --no-cache-dir --break-system-packages --ignore-installed \
    fastapi uvicorn python-multipart psycopg2-binary

# Step 2: Geospatial and data packages.
# These MUST be installed WITHOUT --ignore-installed to avoid overwriting
# QGIS's system-level C extension libraries (numpy, pyproj).
#
# CRITICAL: pip's shapely wheel BUNDLES its own GEOS library. When qgis_process
# loads shapely, two copies of GEOS exist in the same process (QGIS's system GEOS
# + shapely's bundled GEOS), causing a SIGSEGV at import time.
#
# Fix: install libgeos-dev headers, then force shapely to compile from source
# against the SYSTEM GEOS using --no-binary shapely. All packages must be in a
# SINGLE pip command so geopandas doesn't pull in a pre-compiled shapely wheel first.
RUN apt-get update -qq && \
    apt-get install -y -qq libgeos-dev && \
    rm -rf /var/lib/apt/lists/* && \
    python3 -m pip install --no-cache-dir --break-system-packages \
        --no-binary shapely \
        pandas geopandas openpyxl shapely networkx

# Register HLDPlanning plugin with qgis_process so it appears in the algorithm list.
RUN mkdir -p /root/.local/share/QGIS/QGIS3/profiles/default/python/plugins && \
    cp -r /app/HLDPlanning /root/.local/share/QGIS/QGIS3/profiles/default/python/plugins/HLDPlanning && \
    qgis_process plugins enable HLDPlanning 2>&1

EXPOSE 8000

CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
