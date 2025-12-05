# Ship Tracker - Ireland

A real-time and historical ship tracking system focused on maritime traffic around Ireland. This application tracks all ships in Irish waters using AIS (Automatic Identification System) data and maintains a historical database of ship movements.

## Features

- **Real-time Tracking**: Monitor current ship positions in Irish waters
- **Historical Tracking**: View ship movements over time (hours, days, weeks)
- **Individual Ship History**: Track specific vessels and their routes
- **Interactive Map**: Visual representation using OpenStreetMap
- **Ship Information**: View detailed ship data including:
  - MMSI (Maritime Mobile Service Identity)
  - Ship name and type
  - Speed, course, and heading
  - Destination and ETA
  - Physical dimensions

## Technology Stack

- **Backend**: Node.js, TypeScript, Express
- **Database**: SQLite3 for historical data storage
- **Frontend**: HTML, CSS, JavaScript, Leaflet.js for maps
- **Data Source**: AIS data (AISHub API with fallback to mock data)

## Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd ship-tracker
```

2. Install dependencies:
```bash
npm install
```

3. (Optional) Set up AISHub API key:
```bash
export AISHUB_API_KEY=your_api_key_here
```

> **Note**: The application works without an API key using mock data for testing. For real ship data, register for a free API key at [AISHub](https://www.aishub.net/).

## Usage

### Start the Full Application

Run both the tracker and web server:

```bash
npm run build
npm start
```

Then open your browser to `http://localhost:3000`

### Development Mode

For development with auto-reload:

```bash
npm run dev
```

### Run Tracker Only

To run only the background ship tracker (without the web interface):

```bash
npm run track
```

## API Endpoints

### Get Current Ships
```
GET /api/ships/current
```
Returns all ships detected in the last hour.

### Get Historical Ships
```
GET /api/ships/history?start=<timestamp>&end=<timestamp>
```
Returns all ship positions within the specified time range.

### Get Ship History by MMSI
```
GET /api/ships/:mmsi/history?start=<timestamp>&end=<timestamp>
```
Returns the movement history for a specific ship.

### Get All Tracked Ships
```
GET /api/ships/list
```
Returns a list of all unique ship MMSIs in the database.

### Health Check
```
GET /api/health
```
Returns server status.

## Geographic Coverage

The tracker focuses on Irish waters with the following bounding box:
- North: 55.5°
- South: 51.0°
- West: -11.0°
- East: -5.5°

This covers:
- Republic of Ireland coastline
- Northern Ireland coastline
- Irish Sea
- Celtic Sea (western approaches)
- North Atlantic approaches

## How It Works

1. **Data Collection**: The tracker fetches AIS data every 5 minutes (configurable)
2. **Storage**: Ship positions are stored in SQLite with timestamps
3. **API**: Express server exposes REST endpoints for querying data
4. **Visualization**: Web frontend displays ships on an interactive map

## Configuration

You can configure the tracker in `src/tracker.ts`:

- `updateIntervalMs`: How often to fetch new data (default: 300000ms = 5 minutes)
- Database path: Default is `./data/ships.db`

## Database Schema

```sql
CREATE TABLE ship_positions (
  id INTEGER PRIMARY KEY,
  mmsi TEXT NOT NULL,
  name TEXT,
  latitude REAL NOT NULL,
  longitude REAL NOT NULL,
  speed REAL,
  course REAL,
  heading REAL,
  timestamp INTEGER NOT NULL,
  ship_type TEXT,
  destination TEXT,
  eta TEXT,
  draught REAL,
  length REAL,
  width REAL,
  UNIQUE(mmsi, timestamp)
);
```

## Web Interface Features

### Current View
- Displays all ships currently in Irish waters
- Real-time updates every 5 minutes
- Click on ship markers for detailed information

### Historical View
- Select time ranges: 1 hour, 6 hours, 24 hours, or 7 days
- View ship movement tracks with start/end markers
- Filter by specific ship MMSI

### Ship List
- Sidebar shows all detected ships
- Click to focus on a specific ship
- Color-coded by ship type:
  - Orange: Cargo
  - Red: Tanker
  - Green: Passenger
  - Blue: Fishing
  - Gray: Unknown

## Future Enhancements

- Real-time WebSocket updates
- Ship search by name
- Port information and geofencing
- Weather overlay
- Traffic density heatmaps
- Export data to CSV/GeoJSON
- Email/SMS alerts for specific ships

## License

MIT

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
