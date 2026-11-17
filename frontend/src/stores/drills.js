import {drillsApi} from '../api/drills.js';
export const drillsStore={items:[],async refresh(){this.items=await drillsApi.list();return this.items;}};
