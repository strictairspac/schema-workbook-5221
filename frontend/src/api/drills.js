import {api} from './client.js';
export const drillsApi={list:()=>api('/drills'),create:data=>api('/drills','POST',data),detail:id=>api('/drills/'+id),
rows:(id,view='current')=>api('/drills/'+id+'/rows?view='+view),createRow:(id,values,cascade=false)=>api('/drills/'+id+'/rows','POST',{values,cascade}),
updateRow:(id,key,values,cascade=false)=>api('/drills/'+id+'/rows/'+key,'PUT',{values,cascade}),deleteRow:(id,key)=>api('/drills/'+id+'/rows/'+key,'DELETE'),
migrate:(id,maxRows=500)=>api('/drills/'+id+'/migrate?max_rows='+maxRows,'POST'),switch:(id)=>api('/drills/'+id+'/switch','POST'),tables:id=>api('/drills/'+id+'/tables'),
compare:id=>api('/drills/'+id+'/compare'),changes:id=>api('/drills/'+id+'/changes')};
