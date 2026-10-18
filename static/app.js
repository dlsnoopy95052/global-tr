'use strict';
const $ = id => document.getElementById(id);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let mapSignature, map, layer, latest, pollTimer, activeJob, token, tickTimer, timerEnd;
const colors = {healthy:'#ffffff', loss:'#ff6569', unknown:'#91a1b7'};
const stateNames = {healthy:'Healthy adjacent-hop response', loss:'High probe loss at receiving hop; physical fault location unknown', unknown:'Unconfirmed adjacent-hop connection'};
function status(message, error=false) {
  $('status').textContent = message;
  $('status').className = error ? 'error' : '';
}
function busy(value) {
  $('run').disabled = value;
  $('demo').disabled = value;
  $('targets').disabled = value;
  $('threshold').disabled = value;
  $('probe-tool').disabled = value;
  $('run').innerHTML = value ? 'Tracing… <span>◌</span>' : 'Trace my routes <span>→</span>';
}
const localTime = iso => iso ? new Date(iso).toLocaleString([], {year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'}) : 'sample data';
function tick() {
  const seconds = Math.max(0, Math.ceil((timerEnd - performance.now()) / 1000));
  $('timer').textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}
function showTimer(remaining) {
  timerEnd = performance.now() + remaining * 1000;
  $('live').hidden = false;
  tick(); clearInterval(tickTimer); tickTimer = setInterval(tick, 250);
}
function hideTimer() {
  clearInterval(tickTimer);
  $('live').hidden = true; $('stop').disabled = false;
}
function initMap() {
  if (!window.L) {
    $('map').innerHTML = '<div class="map-error">Map library could not load. Check your internet connection and reload.<br>Traceroute results are still available below.</div>';
    return;
  }
  map = L.map('map', {minZoom:2, worldCopyJump:true}).setView([26,0], 2);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    className:'key-free-basemap', maxZoom:19
  }).addTo(map);
  layer = L.featureGroup().addTo(map);
}
function coords(geo) {return [geo.lat, geo.lon];}
function hopOwner(hop) {
  if (!hop.ip) return hop.responders.length ? 'Multiple responders' : 'Unknown IP';
  if (/^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(hop.ip)) return 'Private network';
  if (!hop.geo?.org) return 'Owner unknown';
  return hop.geo.asn ? `AS${hop.geo.asn} ${hop.geo.org}` : hop.geo.org;
}
function hopLabel(hop) {
  const name=hop.unobserved ? 'Target' : `H${hop.ttl}`;
  return `<strong>${escapeHtml(name)}</strong><span class="hop-owner">${escapeHtml(hopOwner(hop))}</span>`;
}
// Choose the shorter longitude span across the dateline.
function near(point, reference) {
  let lon = point[1];
  while (lon - reference[1] > 180) lon -= 360;
  while (lon - reference[1] < -180) lon += 360;
  return [point[0],lon];
}
function hopPositions(hops, sourcePoint) {
  const positions = new Map();
  let previous = sourcePoint;
  hops.forEach((hop,index) => {
    if (!hop.geo) return;
    const point = previous ? near(coords(hop.geo), previous) : coords(hop.geo);
    positions.set(index,point);
    previous=point;
  });
  if (!positions.size && !sourcePoint) return positions;
  // Preserve unlocated hops as explicitly schematic placeholders, never IP locations.
  for (let index=0; index<hops.length; index++) {
    if (positions.has(index)) continue;
    let before=index-1, after=index+1;
    while (before>=0 && !hops[before].geo) before--;
    while (after<hops.length && !hops[after].geo) after++;
    const a=before>=0 ? positions.get(before) : sourcePoint;
    const b=after<hops.length ? positions.get(after) : null;
    if (before<0 && sourcePoint) {
      // Initial private/unknown routers have no measured geographic location.
      // Keep their schematic markers beside the source, not across the WAN.
      const offset=(index+1)*.02;
      positions.set(index,[sourcePoint[0],sourcePoint[1]+offset]);
    } else if (a && b) {
      const ratio=(index-before)/(after-before);
      positions.set(index,[a[0]+(b[0]-a[0])*ratio,a[1]+(b[1]-a[1])*ratio]);
    } else {
      const anchor=a || b;
      const offset=(a ? index-before : index-after)*.2;
      positions.set(index,[anchor[0]+offset,anchor[1]+offset]);
    }
  }
  return positions;
}
function hopDiagram(route) {
  const edges=new Map(route.edges.map(edge=>[edge.end,edge]));
  return `<div class="hop-diagram" role="group" aria-label="Adjacent hop connections">${route.hops.map((hop,index)=>{
    const edge=edges.get(index);
    const segment=edge ? `<div class="hop-segment ${edge.status}" title="${escapeHtml(stateNames[edge.status])}"><span>${edge.status==='unknown'?'Unknown':`${hop.loss}% probe loss`}</span><i></i></div>` : '';
    return `${segment}<div class="hop-node ${!hop.ip?'unknown':''}"><strong>${hop.unobserved?'Target':`H${hop.ttl}`}</strong><span>${escapeHtml(hop.ip || (hop.responders.length?'Multiple IPs':'Unknown IP'))}</span><span class="owner">${escapeHtml(hopOwner(hop))}</span></div>`;
  }).join('')}</div>`;
}
// Collapse consecutive hops owned by the same network into one ASN node.
function asnKey(hop) {
  if (!hop.ip) return hop.responders.length ? 'Multiple responders' : 'No response';
  if (hopOwner(hop) === 'Private network') return 'Private network';
  return hop.geo?.asn ? `AS${hop.geo.asn}` : 'Owner unknown';
}
function asnGroups(hops) {
  const groups = [];
  hops.forEach((hop,index) => {
    const key = asnKey(hop), last = groups[groups.length-1];
    if (last && last.key === key) {last.end = index;}
    else groups.push({key, start:index, end:index, org:hop.geo?.org || ''});
  });
  return groups;
}
function asnDiagram(route) {
  const edges = new Map(route.edges.map(edge=>[edge.end,edge]));
  const groups = asnGroups(route.hops);
  return `<div class="hop-diagram asn-diagram" role="group" aria-label="Traffic path by ASN">${groups.map((group,gi)=>{
    const edge = gi ? edges.get(group.start) : null;
    const entry = route.hops[group.start], exit = route.hops[group.end];
    const segment = edge ? `<div class="hop-segment ${edge.status}" title="${escapeHtml(stateNames[edge.status])}"><span>${edge.status==='unknown'?'Unknown':`${entry.loss}% probe loss`}</span><i></i></div>` : '';
    const label = (h=>h.unobserved?'Target':`H${h.ttl}`);
    const span = group.start===group.end ? label(entry) : `${label(entry)}–${label(exit)}`;
    const rtt = [...route.hops.slice(group.start,group.end+1)].reverse().find(h=>h.rtt!==null);
    const bare = !/^AS\d/.test(group.key);
    return `${segment}<div class="hop-node asn-node ${bare?'unknown':''}"><strong>${escapeHtml(group.key)}</strong><span class="owner">${escapeHtml(group.org)}</span><span>${span} · ${group.end-group.start+1} hop${group.end>group.start?'s':''}</span><span>${rtt?`${rtt.rtt} ms at exit`:'no RTT'}</span></div>`;
  }).join('')}</div>`;
}
// Overall conclusion across all routes; probe responses only, not proven link faults.
function summarizeRoute(route) {
  const observed = route.hops.filter(h=>!h.unobserved);
  const final = route.hops.find(h=>h.destination && !h.unobserved);
  const networks = new Set(asnGroups(route.hops).map(g=>g.key).filter(k=>/^AS\d/.test(k)));
  return {route, final, hops:observed.length, networks:networks.size,
    lossy:route.edges.filter(e=>e.status==='loss').length,
    unknown:route.edges.filter(e=>e.status==='unknown').length};
}
function renderSummary(data) {
  const box = $('summary');
  if (!data.routes.length) {box.hidden = true; box.innerHTML = ''; return;}
  const rows = data.routes.map(summarizeRoute);
  const reached = rows.filter(r=>r.route.reached);
  const lossy = rows.filter(r=>r.lossy);
  const rtts = reached.map(r=>r.final?.rtt).filter(v=>v!=null);
  const lines = [`${reached.length} of ${rows.length} destination${rows.length===1?'':'s'} reached.`];
  if (rtts.length) {
    const fast = reached.reduce((a,b)=>(a.final.rtt??1e9)<=(b.final.rtt??1e9)?a:b);
    const slow = reached.reduce((a,b)=>(a.final.rtt??-1)>=(b.final.rtt??-1)?a:b);
    lines.push(rtts.length>1 ? `Fastest ${fast.route.target} (${fast.final.rtt} ms); slowest ${slow.route.target} (${slow.final.rtt} ms).` : `Round trip to ${fast.route.target}: ${fast.final.rtt} ms.`);
  }
  lines.push(lossy.length ? `High probe loss (≥ ${data.threshold}%) at ${lossy.reduce((n,r)=>n+r.lossy,0)} hop link${lossy.reduce((n,r)=>n+r.lossy,0)===1?'':'s'} on ${lossy.map(r=>r.route.target).join(', ')}; this may be router rate limiting rather than real loss.` : `No hop crossed the ${data.threshold}% probe-loss threshold.`);
  if (rows.length>reached.length) lines.push(`Not confirmed: ${rows.filter(r=>!r.route.reached).map(r=>r.route.target).join(', ')}. Firewalls often drop probes without any fault.`);
  const verdict = !reached.length ? 'bad' : (lossy.length || reached.length<rows.length) ? 'warn' : 'good';
  const title = {good:'All routes look healthy', warn:'Reachable, with points to check', bad:'No destination confirmed'}[verdict];
  box.hidden = false;
  box.className = `summary ${verdict}`;
  box.innerHTML = `<div class="eyebrow">OVERALL CONCLUSION</div><h3>${title}${data.demo?' · sample':''}</h3><ul>${lines.map(l=>`<li>${escapeHtml(l)}</li>`).join('')}</ul><div class="table-scroll"><table><thead><tr><th>DESTINATION</th><th>STATUS</th><th>HOPS</th><th>NETWORKS</th><th>RTT</th><th>FINAL LOSS</th><th>HIGH-LOSS LINKS</th><th>UNCONFIRMED LINKS</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${escapeHtml(r.route.target)}</td><td>${r.route.reached?'Reached':'Unconfirmed'}</td><td>${r.hops}</td><td>${r.networks}</td><td>${r.final?.rtt!=null?`${r.final.rtt} ms`:'—'}</td><td>${r.final?`${r.final.loss}%`:'—'}</td><td class="${r.lossy?'loss':''}">${r.lossy}</td><td>${r.unknown}</td></tr>`).join('')}</tbody></table></div>`;
}
function drawLine(a, b, state, description) {
  if (!map) return;
  const popup = `<b>${escapeHtml(stateNames[state])}</b><br>${escapeHtml(description)}`;
  // A loop preserves distinct hubs that share an approximate city location.
  let points = [a, b];
  if (Math.abs(a[0]-b[0]) + Math.abs(a[1]-b[1]) < .02) {
    points = [a,[a[0]+.22,a[1]+.25],[a[0]-.1,a[1]+.4],b];
  }
  L.polyline(points, {color:colors[state], weight:state==='loss'?3:2,
    opacity:state==='unknown' ? 0.7 : 0.9, dashArray:state==='unknown'?'2 8':null,
    lineCap:'round'}).bindPopup(popup).addTo(layer);
}
function render(data, fit=false) {
  latest = data;
  const demo = Boolean(data.demo);
  $('mode').textContent = demo ? 'SAMPLE DATA · NOT A LIVE TRACE' : 'LIVE MEASUREMENTS';
  $('mode').className = demo ? 'demo-note' : '';
  $('source').textContent = data.source ? `Source · ${data.source.city || data.source.country} · ${data.source.ip}` : 'Source location unavailable';
  $('empty').hidden = data.routes.length > 0;
  $('export').disabled = $('export-pdf').disabled = !data.routes.length;
  const count = data.routes.reduce((sum, route) => sum + route.hops.filter(h => !h.unobserved).length, 0);
  $('map-caption').textContent = `${data.routes.length} routes · ${count} observed hop slots${demo?' · illustrative data':''}${data.routes.length&&!demo?` · updated ${localTime(new Date(Math.max(...data.routes.map(r=>Date.parse(r.timestamp)))).toISOString())}`:''}`;
  renderSummary(data);
  // Live mtr re-renders every second; redraw the map only when the path or link colours change,
  // so open popups survive. Tables and summary always refresh.
  const signature = JSON.stringify([data.source?.ip, data.routes.map(r => [r.ip, r.hops.map(h => [h.ip, h.responders, h.geo?.ip]), r.edges.map(e => e.status)])]);
  const redraw = fit || signature !== mapSignature;
  mapSignature = signature;
  if (layer && redraw) layer.clearLayers();
  const sourcePoint = data.source ? coords(data.source) : null;
  data.routes.forEach(route => {
    const positions = hopPositions(route.hops,sourcePoint);
    if (redraw && sourcePoint && positions.size) {
      const first = positions.get(0);
      drawLine(sourcePoint,first,'unknown','Public egress marker to first located hop. This marker is not a measured TTL hop.');
    }
    if (redraw) route.edges.forEach(edge => {
      if (positions.has(edge.start) && positions.has(edge.end)) {
        const hop = route.hops[edge.end];
        drawLine(positions.get(edge.start),positions.get(edge.end),edge.status,
          `${route.target} · hop ${route.hops[edge.start].ttl} → ${hop.ttl} · receiving-hop probe loss ${hop.loss ?? 'unknown'}%${!hop.geo || !route.hops[edge.start].geo?' · includes a schematic location placeholder':''}`);
      }
    });
    if (map && redraw) route.hops.forEach((hop,index) => {
      if (!positions.has(index)) return;
      const node = L.circleMarker(positions.get(index), {radius:hop.destination?6:4,
        color:hop.destination?'#b8f17b':hop.geo?'#e6edf7':'#91a1b7',weight:1.5,
        dashArray:hop.geo?null:'2 3',fillColor:'#111922',fillOpacity:1});
      const leadingUnlocated=sourcePoint && route.hops.slice(0,index+1).every(h=>!h.geo);
      node.bindTooltip(hopLabel(hop), {
        permanent:true, direction:leadingUnlocated?'right':'top',
        offset:leadingUnlocated?[12,-20*(index+1)]:[0,-6], className:'hop-label'
      });
      node.bindPopup(`<b>${escapeHtml(hop.destination?route.target:`Hop ${hop.ttl}`)}</b><br>${escapeHtml(hop.ip || hop.responders.join(', ') || 'Unknown IP')}<br>${hop.geo?`${escapeHtml(hop.geo.city)}, ${escapeHtml(hop.geo.country)}<br>${escapeHtml(hop.geo.org)}`:'Location unknown — schematic placeholder, not a geographic lookup'}<br>Probe loss: ${hop.loss ?? 'unknown'}% · RTT: ${hop.rtt ?? '—'} ms${hop.unobserved?'<br>Destination not reached':''}`).addTo(layer);
    });
  });
  if (map && sourcePoint && redraw) L.circleMarker(sourcePoint, {radius:8,color:'#b8f17b',weight:2,fillColor:'#b8f17b',fillOpacity:.25})
    .bindPopup(`<b>Your public egress${demo?' (sample)':''}</b><br>${escapeHtml(data.source.ip)}<br>${escapeHtml(data.source.city)}<br>Approximate IP location; may be a VPN exit.`).addTo(layer);
  const openRoutes = new Set([...document.querySelectorAll('#routes details.route[open]')].map(d => d.dataset.index));
  $('routes').innerHTML = data.routes.length ? data.routes.map((route,index) => {
    const final = route.hops.find(h => h.destination && !h.unobserved);
    const unknown = route.edges.filter(e=>e.status==='unknown').length;
    return `<details class="route" data-index="${index}" ${data.routes.length===1||openRoutes.has(String(index))?'open':''}><summary><div>${escapeHtml(route.target)}<small>${escapeHtml(route.ip)} · ${route.hops.filter(h=>!h.unobserved).length} hop slots · ${unknown} unconfirmed hop links</small></div><span class="badge ${!route.reached?'warn':''}">${route.reached?`${final?.rtt ?? '—'} ms · ${final?.loss ?? '—'}% probe loss`:'Destination unconfirmed'}</span></summary><div class="diagram-title">TRAFFIC PATH BY ASN</div>${asnDiagram(route)}<div class="diagram-title">HOP BY HOP</div>${hopDiagram(route)}<div class="table-scroll"><table><thead><tr><th>HOP</th><th>RESPONDER</th><th>OWNER</th><th>APPROXIMATE LOCATION</th><th>RTT</th><th>PROBE LOSS</th></tr></thead><tbody>${route.hops.map(h=>`<tr><td>${h.unobserved?'—':h.ttl}</td><td>${escapeHtml(h.ip || h.responders.join(', ') || '* * * * *')}</td><td>${escapeHtml(hopOwner(h))}</td><td>${escapeHtml(h.geo?`${h.geo.city}, ${h.geo.country}`:h.responders.length>1?'Multiple responders':h.ip?'Unlocated / private':'No response')}</td><td>${h.rtt ?? '—'}${h.rtt!==null?' ms':''}</td><td class="${h.loss>=data.threshold?'loss':''}">${h.unobserved?'Unobserved':`${h.loss}% (${h.replies}/${h.probes} replies)`}${h.unreachable?' · unreachable':''}</td></tr>`).join('')}</tbody></table></div><p class="route-note">${route.started?`Measured ${escapeHtml(localTime(route.started))} → ${escapeHtml(localTime(route.timestamp))} (last update).`:`Measured ${escapeHtml(localTime(route.timestamp))}.`} Probe response rates are end-to-hop measurements, not individual cable loss. ${escapeHtml(route.warning || '')}</p><details class="raw"><summary>Raw traceroute output</summary><pre>${escapeHtml(route.raw || 'Illustrative sample: no traceroute was executed.')}</pre></details></details>`;
  }).join('') : '<p class="placeholder">No completed routes yet.</p>';
  data.errors.forEach(error => {
    const p = document.createElement('p'); p.className='error';
    p.textContent = `${error.target}: ${error.message}`; $('routes').append(p);
  });
  if (fit) fitMap();
}
function fitMap() {
  if (map && layer.getLayers().length) map.fitBounds(layer.getBounds(),{padding:[60,70],maxZoom:7});
}
async function jsonFetch(url, options={}) {
  const response = await fetch(url,options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}
async function poll(id) {
  try {
    const data = await jsonFetch(`/api/jobs/${id}`);
    if (activeJob !== id) return;
    const fit = !latest || latest.routes.length !== data.routes.length;
    if (fit || latest.rev !== data.rev || data.state === 'done') render(data,fit);
    if (data.state === 'done') {
      busy(false); activeJob=null; hideTimer();
      status(`${data.routes.length} route${data.routes.length===1?'':'s'} complete${data.stopped?' (stopped early; showing results so far)':''}${data.errors.length?`; ${data.errors.length} failed — see details below`:''}.`, !data.routes.length);
    } else if (data.remaining != null) {
      showTimer(data.remaining);
      status(data.stopped ? 'Stopping…' : !data.routes.length ? 'Discovering the path… the 5-minute timer starts when the first results arrive.' : `Measuring ${data.total} destination${data.total===1?'':'s'} live. Hop tables update every second; press Stop to finish early.`);
      pollTimer=setTimeout(()=>poll(id),1000);
    } else {
      status(`Tracing ${data.current || 'your public egress'}… ${data.routes.length + data.errors.length}/${data.total} complete. Each destination may take up to 150 seconds.`);
      pollTimer=setTimeout(()=>poll(id),1500);
    }
  } catch (error) {
    // Retain the job ID so a transient connection failure can be retried.
    if (activeJob !== id) return;
    status(`Connection interrupted: ${error.message} Retrying…`,true);
    pollTimer=setTimeout(()=>poll(id),4000);
  }
}
$('trace-form').addEventListener('submit',async event => {
  event.preventDefault(); busy(true); clearTimeout(pollTimer); hideTimer(); status('Starting local traceroute…');
  try {
    token = token || (await jsonFetch('/api/session')).token;
    const targets=$('targets').value.split(/[\s,]+/).filter(Boolean);
    const result=await jsonFetch('/api/trace',{method:'POST',headers:{'Content-Type':'application/json','X-Session-Token':token},body:JSON.stringify({targets,threshold:Number($('threshold').value),mode:$('probe-tool').value})});
    latest=null; activeJob=result.id;
    render({routes:[],errors:[],source:null,threshold:Number($('threshold').value)});
    poll(result.id);
  } catch(error) {busy(false);status(error.message,true);}
});
$('stop').addEventListener('click',async ()=>{
  if(!activeJob) return;
  $('stop').disabled = true; status('Stopping…');
  try {
    await jsonFetch(`/api/jobs/${activeJob}/stop`,{method:'POST',headers:{'X-Session-Token':token}});
  } catch(error) {$('stop').disabled = false; status(error.message,true);}
});
$('fit').addEventListener('click',fitMap);
$('hide-hop-labels').addEventListener('change',event=>{
  $('map').classList.toggle('hide-hop-labels',event.target.checked);
});
$('present').addEventListener('click',()=>{
  document.body.classList.toggle('present');
  $('present').textContent=document.body.classList.contains('present')?'Exit presentation ↙':'Presentation view ↗';
  if(map) {map.invalidateSize();fitMap();}
});
$('export').addEventListener('click',()=>{
  if(!latest)return;
  const url=URL.createObjectURL(new Blob([JSON.stringify(latest,null,2)],{type:'application/json'}));
  const a=document.createElement('a');a.href=url;a.download=latest.demo?'route-atlas-SAMPLE.json':'route-atlas.json';a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
});
function reportRoute(route, threshold) {
  const final = route.hops.find(h => h.destination && !h.unobserved);
  const rows = route.hops.map(h => `<tr><td>${h.unobserved?'—':h.ttl}</td><td>${escapeHtml(h.ip || h.responders.join(', ') || '* * * * *')}</td><td>${escapeHtml(hopOwner(h))}</td><td>${escapeHtml(h.geo?`${h.geo.city}, ${h.geo.country}`:h.responders.length>1?'Multiple responders':h.ip?'Unlocated / private':'No response')}</td><td>${h.rtt ?? '—'}${h.rtt!==null?' ms':''}</td><td class="${h.loss>=threshold?'loss':''}">${h.unobserved?'Unobserved':`${h.loss}% (${h.replies}/${h.probes})`}</td></tr>`).join('');
  const when = route.started ? `Measured ${localTime(route.started)} → ${localTime(route.timestamp)}` : `Measured ${localTime(route.timestamp)}`;
  return `<section class="report-route"><h3>${escapeHtml(route.target)} <small>${escapeHtml(route.ip)}</small></h3><p>${route.reached?`Reached · ${final?.rtt ?? '—'} ms · ${final?.loss ?? '—'}% probe loss`:'Destination unconfirmed'} · ${escapeHtml(when)}</p><table><thead><tr><th>HOP</th><th>RESPONDER</th><th>OWNER</th><th>LOCATION</th><th>RTT</th><th>PROBE LOSS</th></tr></thead><tbody>${rows}</tbody></table>${route.warning?`<p class="note">${escapeHtml(route.warning)}</p>`:''}</section>`;
}
// PDF export uses the browser's print engine (Save as PDF): no library or build step needed.
$('export-pdf').addEventListener('click',()=>{
  if(!latest)return;
  const mode = latest.demo ? 'Sample data (not a measurement)' : latest.routes.some(r=>r.started) ? 'Live monitor (mtr / ping per hop)' : 'Traceroute';
  $('report').innerHTML = `<h1>Route Atlas report</h1><p class="meta">${escapeHtml(mode)} · generated ${escapeHtml(localTime(new Date().toISOString()))}${latest.source?` · source ${escapeHtml(latest.source.city || latest.source.country || '')} ${escapeHtml(latest.source.ip)}`:''} · loss threshold ${latest.threshold}%</p><h2>Summary</h2>${$('summary').innerHTML}<h2>Routes</h2>${latest.routes.map(r=>reportRoute(r,latest.threshold)).join('')}${latest.errors.map(e=>`<p class="note">${escapeHtml(e.target)}: ${escapeHtml(e.message)}</p>`).join('')}<p class="note">Probe response rates are end-to-hop measurements, not individual cable loss.</p>`;
  const title = document.title;
  document.title = `route-atlas-${new Date().toISOString().slice(0,16).replace(/[:T]/g,'-')}`; // default PDF file name
  window.addEventListener('afterprint',()=>{document.title = title; $('report').innerHTML = '';},{once:true});
  window.print();
});
$('demo').addEventListener('click',()=>{
  const geo=(ip,city,country,lat,lon,asn=64496)=>({ip,city,country,lat,lon,asn,org:'Illustrative network'});
  const source=geo('192.0.2.10','San Francisco','United States',37.77,-122.42);
  const hub=(ttl,ip,g,loss=0)=>({ttl,ip,responders:ip?[ip]:[],geo:g,loss,replies:5-loss/20,probes:5,rtt:ttl*18,destination:false});
  const la=geo('192.0.2.20','Los Angeles','United States',34.05,-118.24);
  const ny=geo('192.0.2.30','New York','United States',40.71,-74.01,64497);
  const london=geo('198.51.100.10','London','United Kingdom',51.51,-.13,64498);
  const tokyo=geo('203.0.113.10','Tokyo','Japan',35.68,139.69,64499);
  const singapore=geo('203.0.113.20','Singapore','Singapore',1.35,103.82,64500);
  const hopsA=[hub(1,source.ip,source),hub(2,la.ip,la),hub(3,ny.ip,ny),hub(4,london.ip,london)];
  hopsA[3].destination=true;
  const hopsB=[hub(1,source.ip,source),hub(2,null,null,100),hub(3,tokyo.ip,tokyo,40),hub(4,singapore.ip,singapore,40)];
  hopsB[3].destination=true;
  render({demo:true,source,threshold:20,errors:[],routes:[
    {target:'London · sample',ip:london.ip,reached:true,hops:hopsA,edges:[{start:0,end:1,status:'healthy',skipped:0},{start:1,end:2,status:'healthy',skipped:0},{start:2,end:3,status:'healthy',skipped:0}]},
    {target:'Singapore · sample',ip:singapore.ip,reached:true,hops:hopsB,edges:[{start:0,end:1,status:'unknown',skipped:0},{start:1,end:2,status:'unknown',skipped:0},{start:2,end:3,status:'loss',skipped:0}]}
  ]},true);
  status('Sample routes loaded. These are illustrative, not measurements from your network.');
});
initMap();
// Windows has no mtr, so its live mode is a per-hop ping monitor; label it honestly.
jsonFetch('/api/session').then(session => {
  token = session.token;
  if (session.windows) $('probe-tool').options[1].textContent = 'live monitor (ping per hop)';
}).catch(() => {});
