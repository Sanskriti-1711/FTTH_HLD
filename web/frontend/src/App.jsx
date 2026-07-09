import React, { useEffect, useRef, useState } from 'react';
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import axios from 'axios';
import API_URL from './config';

const App = () => {
  const mapContainer = useRef(null);
  const map = useRef(null);
  const [lng] = useState(13.405);
  const [lat] = useState(52.505);
  const [zoom] = useState(12);
  const [taskId, setTaskId] = useState(null);
  const [status, setStatus] = useState(null);
  const [layers, setLayers] = useState([]);
  const [file, setFile] = useState(null);
  const [project, setProject] = useState('Default Project');
  const [area, setArea] = useState('Berlin-Mitte');

  useEffect(() => {
    if (map.current) return;
    map.current = new maplibregl.Map({
      container: mapContainer.current,
      style: 'https://demotiles.maplibre.org/style.json',
      center: [lng, lat],
      zoom: zoom
    });
    map.current.addControl(new maplibregl.NavigationControl(), 'top-right');
  }, [lng, lat, zoom]);

  const handleFileChange = (e) => {
    setFile(e.target.files[0]);
  };

  const runHLD = async () => {
    if (!file) return alert('Please select an Excel file');
    const formData = new FormData();
    formData.append('excel', file);

    try {
      const response = await axios.post(`${API_URL}/run-hld`, formData);
      setTaskId(response.data.task_id);
      setStatus('processing');
    } catch (error) {
      console.error('Error running HLD:', error);
      setStatus('failed');
    }
  };

  useEffect(() => {
    let interval;
    if (taskId && status === 'processing') {
      interval = setInterval(async () => {
        try {
          const response = await axios.get(`${API_URL}/status/${taskId}`);
          if (response.data.status === 'completed') {
            setStatus('completed');
            setLayers(response.data.layers);
            clearInterval(interval);
          }
        } catch (error) {
          console.error('Error checking status:', error);
          clearInterval(interval);
        }
      }, 2000);
    }
    return () => clearInterval(interval);
  }, [taskId, status]);

  const loadLayer = async (layerName) => {
    try {
      const response = await axios.get(`${API_URL}/layers/${taskId}/${layerName}`);
      const data = response.data;

      if (map.current.getSource(layerName)) {
        map.current.getSource(layerName).setData(data);
      } else {
        map.current.addSource(layerName, {
          type: 'geojson',
          data: data
        });

        const geomType = data.features[0]?.geometry?.type;
        const isPolygon = geomType === 'Polygon';
        const isLine = geomType === 'LineString' || geomType === 'MultiLineString';

        if (isPolygon) {
          map.current.addLayer({
            id: layerName,
            type: 'fill',
            source: layerName,
            paint: {
              'fill-color': '#' + Math.floor(Math.random()*16777215).toString(16),
              'fill-opacity': 0.4
            }
          });
          map.current.addLayer({
            id: `${layerName}-outline`,
            type: 'line',
            source: layerName,
            paint: {
              'line-color': '#000',
              'line-width': 1
            }
          });
        } else if (isLine) {
          map.current.addLayer({
            id: layerName,
            type: 'line',
            source: layerName,
            paint: {
              'line-color': '#' + Math.floor(Math.random()*16777215).toString(16),
              'line-width': 2
            }
          });
        } else {
          map.current.addLayer({
            id: layerName,
            type: 'circle',
            source: layerName,
            paint: {
              'circle-radius': 5,
              'circle-color': '#' + Math.floor(Math.random()*16777215).toString(16),
              'circle-stroke-width': 1,
              'circle-stroke-color': '#fff'
            }
          });
        }
      }
    } catch (error) {
      console.error('Error loading layer:', error);
    }
  };

  return (
    <div style={{ display: 'flex', height: '100vh', width: '100vw', fontFamily: 'sans-serif' }}>
      <div style={{ width: '350px', padding: '20px', borderRight: '1px solid #ccc', zIndex: 1, backgroundColor: '#f8f9fa', overflowY: 'auto' }}>
        <h1 style={{ fontSize: '1.5rem', marginBottom: '20px', color: '#333' }}>Telecom HLD Planner</h1>

        <div style={{ marginBottom: '20px' }}>
          <label style={{ display: 'block', fontWeight: 'bold', marginBottom: '5px' }}>Project</label>
          <select value={project} onChange={(e) => setProject(e.target.value)} style={{ width: '100%', padding: '8px' }}>
            <option>Default Project</option>
            <option>Project Alpha</option>
            <option>Project Beta</option>
          </select>
        </div>

        <div style={{ marginBottom: '20px' }}>
          <label style={{ display: 'block', fontWeight: 'bold', marginBottom: '5px' }}>Area</label>
          <select value={area} onChange={(e) => setArea(e.target.value)} style={{ width: '100%', padding: '8px' }}>
            <option>Berlin-Mitte</option>
            <option>Berlin-Pankow</option>
            <option>Berlin-Charlottenburg</option>
          </select>
        </div>

        <div style={{ padding: '15px', backgroundColor: '#fff', borderRadius: '8px', boxShadow: '0 2px 4px rgba(0,0,0,0.1)' }}>
          <h2 style={{ fontSize: '1.1rem', marginTop: 0 }}>Workflow</h2>
          <label style={{ display: 'block', marginBottom: '10px' }}>
            <span style={{ display: 'block', marginBottom: '5px' }}>Upload Address List (.xlsx)</span>
            <input type="file" onChange={handleFileChange} style={{ fontSize: '0.8rem' }} />
          </label>
          <button
            onClick={runHLD}
            disabled={status === 'processing'}
            style={{
              width: '100%',
              padding: '10px',
              backgroundColor: '#007bff',
              color: 'white',
              border: 'none',
              borderRadius: '4px',
              cursor: status === 'processing' ? 'not-allowed' : 'pointer'
            }}
          >
            {status === 'processing' ? 'Generating HLD...' : 'Generate HLD'}
          </button>

          {status && (
            <div style={{ marginTop: '15px', padding: '10px', borderRadius: '4px', backgroundColor: status === 'completed' ? '#d4edda' : '#fff3cd' }}>
              <strong>Status:</strong> {status}
            </div>
          )}
        </div>

        {status === 'completed' && (
          <div style={{ marginTop: '20px' }}>
            <h3 style={{ fontSize: '1.1rem' }}>Map Layers</h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {layers.map(layer => (
                <button
                  key={layer}
                  onClick={() => loadLayer(layer)}
                  style={{
                    padding: '8px',
                    textAlign: 'left',
                    backgroundColor: '#fff',
                    border: '1px solid #ddd',
                    borderRadius: '4px',
                    cursor: 'pointer'
                  }}
                >
                  {layer}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
      <div ref={mapContainer} style={{ flex: 1 }} />
    </div>
  );
};

export default App;
