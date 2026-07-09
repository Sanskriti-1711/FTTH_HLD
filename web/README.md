# Telecom HLD Web Portal

This directory contains a web interface for the HLD Planning QGIS plugin.

## Structure

- `backend/`: FastAPI server that triggers the HLD algorithms.
- `frontend/`: React application using MapLibre GL for visualization.

## Prerequisites

- Python 3.10+
- Node.js 18+
- npm 9+
- (Optional) QGIS 3.22+ installed and `qgis_process` in your PATH. If not found, the backend will run in mock mode.

## Setup & Running Locally

### 1. Backend

Navigate to the backend directory:
```bash
cd web/backend
```

Install dependencies:
```bash
pip install -r requirements.txt
```

Run the server:
```bash
uvicorn main:app --reload --port 8000
```
The API will be available at `http://localhost:8000`.

### 2. Frontend

Navigate to the frontend directory:
```bash
cd web/frontend
```

Install dependencies:
```bash
npm install
```

Run the development server:
```bash
npm run dev
```
The application will be available at `http://localhost:5173`.

## Usage Workflow

1. Open the portal at `http://localhost:5173`.
2. Select a **Project** and **Area** (placeholders).
3. Upload an **Excel Address List** (.xlsx) containing address information.
4. Click **Generate HLD**.
5. Wait for the status to show **completed**.
6. Click on the generated layers (Objects, Polygons, PDPs, etc.) to visualize them on the map.

## Configuration

You can change the API endpoint in `web/frontend/src/config.js` or by setting the `VITE_API_URL` environment variable during build.
