import express from 'express';
import cors from 'cors';
import { Database } from './database';
import { ShipTracker } from './tracker';
import path from 'path';

const app = express();
const PORT = process.env.PORT || 3000;

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, '../public')));

// Initialize database and tracker
const apiKey = process.env.AISHUB_API_KEY;
const tracker = new ShipTracker(apiKey);
const db = tracker.getDatabase();

// Start the tracker
tracker.start();

// API Routes

// Get current ship positions (last hour)
app.get('/api/ships/current', async (req, res) => {
  try {
    const ships = await db.getLatestPositions();
    res.json({
      count: ships.length,
      timestamp: Date.now(),
      ships
    });
  } catch (error) {
    console.error('Error fetching current ships:', error);
    res.status(500).json({ error: 'Failed to fetch current ships' });
  }
});

// Get ship history by MMSI
app.get('/api/ships/:mmsi/history', async (req, res) => {
  try {
    const { mmsi } = req.params;
    const startTime = req.query.start ? parseInt(req.query.start as string) : undefined;
    const endTime = req.query.end ? parseInt(req.query.end as string) : undefined;

    const history = await db.getShipHistory(mmsi, startTime, endTime);

    res.json({
      mmsi,
      count: history.length,
      positions: history
    });
  } catch (error) {
    console.error('Error fetching ship history:', error);
    res.status(500).json({ error: 'Failed to fetch ship history' });
  }
});

// Get all ships in a time range
app.get('/api/ships/history', async (req, res) => {
  try {
    const startTime = req.query.start ? parseInt(req.query.start as string) : Date.now() - 86400000;
    const endTime = req.query.end ? parseInt(req.query.end as string) : Date.now();

    const ships = await db.getAllShipsInTimeRange(startTime, endTime);

    res.json({
      startTime,
      endTime,
      count: ships.length,
      ships
    });
  } catch (error) {
    console.error('Error fetching historical ships:', error);
    res.status(500).json({ error: 'Failed to fetch historical ships' });
  }
});

// Get list of all tracked ships
app.get('/api/ships/list', async (req, res) => {
  try {
    const mmsiList = await db.getUniqueShips();
    res.json({
      count: mmsiList.length,
      ships: mmsiList
    });
  } catch (error) {
    console.error('Error fetching ship list:', error);
    res.status(500).json({ error: 'Failed to fetch ship list' });
  }
});

// Health check
app.get('/api/health', (req, res) => {
  res.json({ status: 'ok', timestamp: Date.now() });
});

// Serve the main HTML page
app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, '../public/index.html'));
});

app.listen(PORT, () => {
  console.log(`Ship tracker server running on http://localhost:${PORT}`);
  console.log(`API available at http://localhost:${PORT}/api`);
});

// Graceful shutdown
process.on('SIGINT', () => {
  console.log('\nShutting down server...');
  tracker.stop();
  process.exit(0);
});
