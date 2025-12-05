import fetch from 'node-fetch';
import { ShipPosition } from './database';

// Ireland bounding box (approximate)
const IRELAND_BOUNDS = {
  north: 55.5,
  south: 51.0,
  west: -11.0,
  east: -5.5
};

export class AISFetcher {
  private apiKey?: string;

  constructor(apiKey?: string) {
    this.apiKey = apiKey;
  }

  // Fetch ships using AISHub API (free tier available)
  async fetchShipsAISHub(): Promise<ShipPosition[]> {
    try {
      const url = `http://data.aishub.net/ws.php?username=${this.apiKey || 'DEMO'}&format=1&output=json&compress=0&latmin=${IRELAND_BOUNDS.south}&latmax=${IRELAND_BOUNDS.north}&lonmin=${IRELAND_BOUNDS.west}&lonmax=${IRELAND_BOUNDS.east}`;

      const response = await fetch(url);
      const data = await response.json() as any;

      if (!data || data.ERROR) {
        console.error('AISHub API error:', data?.ERROR || 'Unknown error');
        return [];
      }

      const ships: ShipPosition[] = [];
      const timestamp = Date.now();

      for (const ship of data[0] || []) {
        ships.push({
          mmsi: ship.MMSI?.toString() || '',
          name: ship.NAME || undefined,
          latitude: parseFloat(ship.LATITUDE),
          longitude: parseFloat(ship.LONGITUDE),
          speed: ship.SOG ? parseFloat(ship.SOG) : undefined,
          course: ship.COG ? parseFloat(ship.COG) : undefined,
          heading: ship.HEADING ? parseFloat(ship.HEADING) : undefined,
          timestamp,
          shipType: ship.TYPE ? this.getShipType(ship.TYPE) : undefined,
          destination: ship.DESTINATION || undefined,
          eta: ship.ETA || undefined,
          draught: ship.DRAUGHT ? parseFloat(ship.DRAUGHT) : undefined,
          length: ship.A && ship.B ? parseFloat(ship.A) + parseFloat(ship.B) : undefined,
          width: ship.C && ship.D ? parseFloat(ship.C) + parseFloat(ship.D) : undefined
        });
      }

      return ships;
    } catch (error) {
      console.error('Error fetching from AISHub:', error);
      return [];
    }
  }

  // Fetch ships using MarineTraffic API (requires API key)
  async fetchShipsMarineTraffic(): Promise<ShipPosition[]> {
    if (!this.apiKey) {
      console.warn('MarineTraffic requires an API key');
      return [];
    }

    try {
      const url = `https://services.marinetraffic.com/api/exportvessels/${this.apiKey}/v:8/protocol:json/timespan:10/minlat:${IRELAND_BOUNDS.south}/maxlat:${IRELAND_BOUNDS.north}/minlon:${IRELAND_BOUNDS.west}/maxlon:${IRELAND_BOUNDS.east}`;

      const response = await fetch(url);
      const data = await response.json() as any;

      const ships: ShipPosition[] = [];
      const timestamp = Date.now();

      for (const ship of data || []) {
        ships.push({
          mmsi: ship.MMSI?.toString() || '',
          name: ship.SHIPNAME || undefined,
          latitude: parseFloat(ship.LAT),
          longitude: parseFloat(ship.LON),
          speed: ship.SPEED ? parseFloat(ship.SPEED) : undefined,
          course: ship.COURSE ? parseFloat(ship.COURSE) : undefined,
          heading: ship.HEADING ? parseFloat(ship.HEADING) : undefined,
          timestamp,
          shipType: ship.TYPE_NAME || undefined,
          destination: ship.DESTINATION || undefined,
          eta: ship.ETA || undefined,
          length: ship.LENGTH ? parseFloat(ship.LENGTH) : undefined,
          width: ship.WIDTH ? parseFloat(ship.WIDTH) : undefined
        });
      }

      return ships;
    } catch (error) {
      console.error('Error fetching from MarineTraffic:', error);
      return [];
    }
  }

  // Generate mock data for testing
  generateMockData(): ShipPosition[] {
    const ships: ShipPosition[] = [];
    const timestamp = Date.now();
    const shipNames = ['Celtic Star', 'Irish Rover', 'Galway Bay', 'Dublin Express', 'Cork Trader'];

    for (let i = 0; i < 10; i++) {
      ships.push({
        mmsi: (210000000 + i).toString(),
        name: shipNames[i % shipNames.length],
        latitude: IRELAND_BOUNDS.south + Math.random() * (IRELAND_BOUNDS.north - IRELAND_BOUNDS.south),
        longitude: IRELAND_BOUNDS.west + Math.random() * (IRELAND_BOUNDS.east - IRELAND_BOUNDS.west),
        speed: Math.random() * 20,
        course: Math.random() * 360,
        heading: Math.random() * 360,
        timestamp,
        shipType: ['Cargo', 'Tanker', 'Passenger', 'Fishing'][Math.floor(Math.random() * 4)],
        destination: ['Dublin', 'Cork', 'Galway', 'Waterford'][Math.floor(Math.random() * 4)]
      });
    }

    return ships;
  }

  private getShipType(typeCode: number | string): string {
    const code = typeof typeCode === 'string' ? parseInt(typeCode) : typeCode;

    if (code >= 70 && code <= 79) return 'Cargo';
    if (code >= 80 && code <= 89) return 'Tanker';
    if (code >= 60 && code <= 69) return 'Passenger';
    if (code === 30) return 'Fishing';
    if (code >= 40 && code <= 49) return 'High Speed Craft';
    if (code >= 50 && code <= 59) return 'Pilot/Tug';

    return 'Unknown';
  }
}
