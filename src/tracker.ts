import { Database } from './database';
import { AISFetcher } from './ais-fetcher';

export class ShipTracker {
  private db: Database;
  private fetcher: AISFetcher;
  private interval?: NodeJS.Timeout;
  private updateIntervalMs: number;

  constructor(apiKey?: string, updateIntervalMs: number = 300000) { // Default: 5 minutes
    this.db = new Database();
    this.fetcher = new AISFetcher(apiKey);
    this.updateIntervalMs = updateIntervalMs;
  }

  async start() {
    console.log('Starting ship tracker...');
    await this.updateShips();

    this.interval = setInterval(async () => {
      await this.updateShips();
    }, this.updateIntervalMs);

    console.log(`Ship tracker started. Updating every ${this.updateIntervalMs / 1000} seconds.`);
  }

  stop() {
    if (this.interval) {
      clearInterval(this.interval);
      this.interval = undefined;
    }
    this.db.close();
    console.log('Ship tracker stopped.');
  }

  private async updateShips() {
    try {
      console.log(`[${new Date().toISOString()}] Fetching ship positions...`);

      // Try AISHub first, fall back to mock data
      let ships = await this.fetcher.fetchShipsAISHub();

      if (ships.length === 0) {
        console.log('No data from AISHub, using mock data for testing...');
        ships = this.fetcher.generateMockData();
      }

      console.log(`Found ${ships.length} ships`);

      for (const ship of ships) {
        try {
          await this.db.savePosition(ship);
        } catch (error) {
          console.error(`Error saving ship ${ship.mmsi}:`, error);
        }
      }

      console.log(`Successfully saved positions for ${ships.length} ships`);
    } catch (error) {
      console.error('Error updating ships:', error);
    }
  }

  getDatabase(): Database {
    return this.db;
  }
}

// Run tracker if this file is executed directly
if (require.main === module) {
  const apiKey = process.env.AISHUB_API_KEY;
  const tracker = new ShipTracker(apiKey);

  tracker.start();

  process.on('SIGINT', () => {
    console.log('\nShutting down...');
    tracker.stop();
    process.exit(0);
  });
}
