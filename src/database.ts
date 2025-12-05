import sqlite3 from 'sqlite3';
import { promisify } from 'util';

export interface ShipPosition {
  mmsi: string;
  name?: string;
  latitude: number;
  longitude: number;
  speed?: number;
  course?: number;
  heading?: number;
  timestamp: number;
  shipType?: string;
  destination?: string;
  eta?: string;
  draught?: number;
  length?: number;
  width?: number;
}

export class Database {
  private db: sqlite3.Database;

  constructor(dbPath: string = './data/ships.db') {
    this.db = new sqlite3.Database(dbPath);
    this.initDatabase();
  }

  private initDatabase() {
    this.db.run(`
      CREATE TABLE IF NOT EXISTS ship_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
      )
    `);

    this.db.run(`
      CREATE INDEX IF NOT EXISTS idx_mmsi ON ship_positions(mmsi)
    `);

    this.db.run(`
      CREATE INDEX IF NOT EXISTS idx_timestamp ON ship_positions(timestamp)
    `);
  }

  async savePosition(position: ShipPosition): Promise<void> {
    return new Promise((resolve, reject) => {
      this.db.run(
        `INSERT OR IGNORE INTO ship_positions
        (mmsi, name, latitude, longitude, speed, course, heading, timestamp, ship_type, destination, eta, draught, length, width)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        [
          position.mmsi,
          position.name,
          position.latitude,
          position.longitude,
          position.speed,
          position.course,
          position.heading,
          position.timestamp,
          position.shipType,
          position.destination,
          position.eta,
          position.draught,
          position.length,
          position.width
        ],
        (err) => {
          if (err) reject(err);
          else resolve();
        }
      );
    });
  }

  async getLatestPositions(): Promise<ShipPosition[]> {
    return new Promise((resolve, reject) => {
      this.db.all(
        `SELECT * FROM ship_positions
         WHERE timestamp > ?
         GROUP BY mmsi
         HAVING timestamp = MAX(timestamp)
         ORDER BY timestamp DESC`,
        [Date.now() - 3600000], // Last hour
        (err, rows: any[]) => {
          if (err) reject(err);
          else resolve(rows.map(this.rowToPosition));
        }
      );
    });
  }

  async getShipHistory(mmsi: string, startTime?: number, endTime?: number): Promise<ShipPosition[]> {
    const start = startTime || Date.now() - 86400000 * 7; // Default: last 7 days
    const end = endTime || Date.now();

    return new Promise((resolve, reject) => {
      this.db.all(
        `SELECT * FROM ship_positions
         WHERE mmsi = ? AND timestamp >= ? AND timestamp <= ?
         ORDER BY timestamp ASC`,
        [mmsi, start, end],
        (err, rows: any[]) => {
          if (err) reject(err);
          else resolve(rows.map(this.rowToPosition));
        }
      );
    });
  }

  async getAllShipsInTimeRange(startTime: number, endTime: number): Promise<ShipPosition[]> {
    return new Promise((resolve, reject) => {
      this.db.all(
        `SELECT * FROM ship_positions
         WHERE timestamp >= ? AND timestamp <= ?
         ORDER BY timestamp DESC`,
        [startTime, endTime],
        (err, rows: any[]) => {
          if (err) reject(err);
          else resolve(rows.map(this.rowToPosition));
        }
      );
    });
  }

  async getUniqueShips(): Promise<string[]> {
    return new Promise((resolve, reject) => {
      this.db.all(
        `SELECT DISTINCT mmsi FROM ship_positions ORDER BY mmsi`,
        [],
        (err, rows: any[]) => {
          if (err) reject(err);
          else resolve(rows.map(r => r.mmsi));
        }
      );
    });
  }

  private rowToPosition(row: any): ShipPosition {
    return {
      mmsi: row.mmsi,
      name: row.name,
      latitude: row.latitude,
      longitude: row.longitude,
      speed: row.speed,
      course: row.course,
      heading: row.heading,
      timestamp: row.timestamp,
      shipType: row.ship_type,
      destination: row.destination,
      eta: row.eta,
      draught: row.draught,
      length: row.length,
      width: row.width
    };
  }

  close() {
    this.db.close();
  }
}
