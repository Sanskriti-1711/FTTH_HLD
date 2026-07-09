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

  useEffect(() => {
    if (map.current) return;
    map.current = new maplibregl.Map({
      container: mapContainer.current,
      style: 'https://demotiles.maplibre.org/style.json',
      center: [lng, lat],
      zoom: zoom
    });
  });

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

        const isPolygon = data.features[0]?.geometry?.type === 'Polygon';

        map.current.addLayer({
          id: layerName,
          type: isPolygon ? 'fill' : 'circle',
          source: layerName,
          paint: isPolygon ? {
            'fill-color': '#' + Math.floor(Math.random()*16777215).toString(16),
            'fill-opacity': 0.5
          } : {
            'circle-radius': 6,
            'circle-color': '#' + Math.floor(Math.random()*16777215).toString(16)
          }
        });
      }
    } catch (error) {
      console.error('Error loading layer:', error);
    }
  };

  return (
    <div style={{ display: 'flex', height: '100vh', width: '100vw' }}>
      <div style={{ width: '300px', padding: '20px', borderRight: '1px solid #ccc', zIndex: 1, backgroundColor: 'white' }}>
        <h2>HLD Planner</h2>
        <input type="file" onChange={handleFileChange} />
        <button onClick={runHLD} style={{ marginTop: '10px' }}>Generate HLD</button>
        <p>Status: {status}</p>
        {status === 'completed' && (
          <div>
            <h3>Layers</h3>
            {layers.map(layer => (
              <div key={layer}>
                <button onClick={() => loadLayer(layer)} style={{ marginTop: '5px' }}>{layer}</button>
              </div>
            ))}
          </div>
        )}
      </div>
      <div ref={mapContainer} style={{ flex: 1 }} />
    </div>
  );
};

export default App;
