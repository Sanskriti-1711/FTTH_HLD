import os
import shutil
import json
import uuid
import subprocess
from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict

app = FastAPI()

# Enable CORS for frontend communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory storage for task status and results
tasks: Dict[str, Dict] = {}

# Data directory for outputs
OUTPUT_DIR = "web/backend/outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Path to the existing QGIS plugin (assuming it's in the root as HLDPlanning)
PLUGIN_PATH = os.path.abspath("HLDPlanning")

@app.post("/run-hld")
async def run_hld(background_tasks: BackgroundTasks, excel: UploadFile = File(...)):
    task_id = str(uuid.uuid4())
    task_out_dir = os.path.join(OUTPUT_DIR, task_id)
    os.makedirs(task_out_dir, exist_ok=True)

    tasks[task_id] = {"status": "processing", "layers": []}

    # Save the uploaded file
    excel_path = os.path.join(task_out_dir, excel.filename)
    with open(excel_path, "wb") as buffer:
        shutil.copyfileobj(excel.file, buffer)

    def process_hld(tid: str, excel_p: str, out_p: str):
        try:
            # Check if qgis_process is available
            qgis_exec = shutil.which("qgis_process")

            if qgis_exec:
                # This is how we would call the plugin's algorithm via CLI if QGIS was installed
                # qgis_process run hldplanning:end_to_end_pipeline -- EXCEL=... OUTPUT_DIR=...
                cmd = [
                    qgis_exec, "run", "hldplanning:end_to_end_pipeline",
                    "--",
                    f"EXCEL={excel_p}",
                    f"OUTPUT_DIR={out_p}",
                    "ROADS=mock_roads.gpkg" # In a real scenario, this would be a parameter
                ]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    raise Exception(f"QGIS process failed: {result.stderr}")
            else:
                # Fallback/Mock for environment without QGIS
                import time
                time.sleep(5)

                # Create dummy GeoPackage-like JSONs for the frontend to consume
                layer_names = ["Objects", "Polygons", "PDPs", "MFG", "Final_Trenches"]
                layer_files = {}
                for name in layer_names:
                    fname = f"{name}.json"
                    fpath = os.path.join(out_p, fname)
                    with open(fpath, "w") as f:
                        # Simple GeoJSON mock
                        json.dump({
                            "type": "FeatureCollection",
                            "features": [{
                                "type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [13.405, 52.505]},
                                "properties": {"name": name}
                            }]
                        }, f)
                    layer_files[name] = fpath

                tasks[tid]["status"] = "completed"
                tasks[tid]["layers"] = layer_names
                tasks[tid]["files"] = layer_files
                return

            # If qgis_process ran successfully, we would parse its outputs from out_p
            # and update the tasks dict accordingly.
            tasks[tid]["status"] = "completed"

        except Exception as e:
            tasks[tid]["status"] = "failed"
            tasks[tid]["error"] = str(e)

    background_tasks.add_task(process_hld, task_id, excel_path, task_out_dir)

    return {"task_id": task_id}

@app.get("/status/{task_id}")
async def get_status(task_id: str):
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return tasks[task_id]

@app.get("/layers/{task_id}/{layer_name}")
async def get_layer(task_id: str, layer_name: str):
    if task_id not in tasks or layer_name not in tasks[task_id].get("layers", []):
        raise HTTPException(status_code=404, detail="Layer not found")

    file_path = tasks[task_id]["files"][layer_name]
    with open(file_path, "r") as f:
        return json.load(f)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
