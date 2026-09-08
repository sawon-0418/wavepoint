const defaultCatches = [
  { name: '김바다', species: '참돔', length: 78.4, color: 'amber', ai: false },
  { name: '루어소녀', species: '배스', length: 58.2, color: 'violet', ai: false },
  { name: '민물왕', species: '쏘가리', length: 46.8, color: 'green', ai: true },
  { name: '파도타는낚시', species: '광어', length: 44.1, color: 'blue', ai: false },
];
const deploymentResetTitles = new Set(['청평호 수상좌대', '을왕리 방파제', '팔당대교 하류', '대부도 선감항', '독도 #테스트용 포인트', '테스트']);
const savedSpots = JSON.parse(localStorage.getItem('wave-spots') || 'null');
const storedSpots = Array.isArray(savedSpots) ? savedSpots.filter(spot => !deploymentResetTitles.has(spot.title)) : null;
if (Array.isArray(savedSpots) && storedSpots.length !== savedSpots.length) localStorage.setItem('wave-spots', JSON.stringify(storedSpots));
let spots = storedSpots || [];
let catches = JSON.parse(localStorage.getItem('wave-catches') || 'null') || defaultCatches;
let map, markers = [], protectedLayers = [], selectedLatLng = null, pickingSpotLocation = false, spotPickerMap, spotPickerMarker, filter = 'all', mapRegionFilter = null, mapHistory = [], infoWindow;
const $ = (s) => document.querySelector(s);
function save(){ localStorage.setItem('wave-spots', JSON.stringify(spots)); localStorage.setItem('wave-catches', JSON.stringify(catches)); }
function escapeHTML(v){ const d=document.createElement('div'); d.textContent=v; return d.innerHTML; }
function toast(message){ const el=$('#toast'); el.textContent=message; el.classList.add('show'); setTimeout(()=>el.classList.remove('show'),2800); }
function markerContent(kind){ return `<div style="width:29px;height:29px;border-radius:50% 50% 50% 0;transform:rotate(-45deg);background:${kind==='sea'?'#ed8d49':'#008b87'};border:3px solid #fff;box-shadow:0 2px 6px #1238"><span style="display:block;transform:rotate(45deg);color:#fff;text-align:center;line-height:23px;font-size:13px">${kind==='sea'?'⚓':'◉'}</span></div>`; }
function setMapView(lat,lng,zoom){ if(mapRegionFilter && zoom===12) zoom=9; map.setCenter(new naver.maps.LatLng(Number(lat),Number(lng))); if(Number.isFinite(zoom)) map.setZoom(zoom); }
async function setSpotAddress(lat, lng) { const addressNote=$('#selected-address'), addressInput=$('#spot-address'); addressInput.value=''; addressNote.classList.remove('address-unavailable'); addressNote.textContent='주소를 확인하고 있어요…'; try { const response=await fetch(`/api/reverse-geocode?lat=${encodeURIComponent(lat)}&lng=${encodeURIComponent(lng)}`); const result=await response.json(); if(!response.ok || !result.address) throw new Error(result.error || '주소를 찾지 못했습니다.'); addressInput.value=result.address; addressNote.textContent=`⌖ ${result.address}`; } catch { addressNote.classList.add('address-unavailable'); addressNote.textContent='주소를 찾지 못했습니다 · 지도 중심 위치로 등록됩니다.'; } }
function setSpotPickerPosition(lat, lng, zoom=15) { selectedLatLng={lat:Number(lat),lng:Number(lng)}; const position=new naver.maps.LatLng(selectedLatLng.lat,selectedLatLng.lng); if(spotPickerMap){spotPickerMap.setCenter(position);spotPickerMap.setZoom(zoom);if(spotPickerMarker)spotPickerMarker.setPosition(position);else spotPickerMarker=new naver.maps.Marker({map:spotPickerMap,position,zIndex:100,icon:{content:markerContent('river'),anchor:new naver.maps.Point(14,28)}});} $('#selected-location').textContent='등록용 지도에서 선택한 위치에 포인트가 등록됩니다.';setSpotAddress(selectedLatLng.lat,selectedLatLng.lng);}
function openSpotPickerMap() { const seoul={lat:37.5665,lng:126.9780}, defaultZoom=12; const lat=selectedLatLng?.lat ?? seoul.lat, lng=selectedLatLng?.lng ?? seoul.lng; if(!spotPickerMap){spotPickerMap=new naver.maps.Map('spot-picker-map',{center:new naver.maps.LatLng(lat,lng),zoom:defaultZoom,zoomControl:true,zoomControlOptions:{position:naver.maps.Position.TOP_RIGHT}});naver.maps.Event.addListener(spotPickerMap,'click',event=>setSpotPickerPosition(event.coord.lat(),event.coord.lng(),spotPickerMap.getZoom()));} setTimeout(()=>{spotPickerMap.refresh();setSpotPickerPosition(lat,lng,defaultZoom);},0);}
function fitSpots(spotsToFit,maxZoom=12){ const bounds=new naver.maps.LatLngBounds(); spotsToFit.forEach(spot=>bounds.extend(new naver.maps.LatLng(Number(spot.lat),Number(spot.lng)))); map.fitBounds(bounds,{top:42,right:42,bottom:42,left:42}); if(map.getZoom()>maxZoom) map.setZoom(maxZoom); }
function openSpotInfo(spot, marker){ infoWindow.setContent(`<div class="spot-popup"><span class="spot-meta">${spot.kind==='sea'?'바다':'민물'} · ${escapeHTML(spot.species)}</span><h3>${escapeHTML(spot.title)}</h3><p>${escapeHTML(spot.address||spot.description)}</p></div>`); infoWindow.open(map,marker); }
function isOfficialSpot(spot){ return spot.source==='official'||spot.source==='낚시터 정보 조회서비스'||String(spot.id||'').startsWith('official-'); }
function hasUnavailableAddress(spot){ return spot.addressStatus==='unavailable'||spot.address==='해상 좌표 · 주소 미제공'; }
function isUserSpot(spot){ return !isOfficialSpot(spot)&&(spot.source==='user'||Number(spot.id)>1000000000000); }
function visibleSpots(){ const q=$('#search-input').value.trim().toLowerCase(); return spots.filter(s=>(filter==='all'||(filter==='community'?isUserSpot(s):s.kind===filter))&&(!mapRegionFilter||provinceOf(s)===mapRegionFilter)&&(`${s.title} ${s.description} ${s.species}`).toLowerCase().includes(q)); }
function regionOf(spot){ const source=`${spot.address||''} ${spot.description||''}`; const match=source.match(/(?:특별자치도|특별시|광역시|도)\s+([^\s]+?(?:시|군|구))(?:\s|$)/); if(match)return match[1]; if(spot.title.includes('청평'))return '가평군'; if(spot.title.includes('을왕리'))return '중구'; if(spot.title.includes('대부도'))return '안산시'; return '주변 지역'; }
function provinceOf(spot){ const source=`${spot.address||''} ${spot.description||''}`; const match=source.match(/^([^\s]+(?:특별자치도|특별시|광역시|도))/); if(match)return match[1].replace('전남광주통합특별시','전라남도'); if(spot.title.includes('청평'))return '경기도'; if(spot.title.includes('을왕리'))return '인천광역시'; if(spot.title.includes('대부도'))return '경기도'; return '기타 지역'; }
function clusterContent(region,count){ return `<div class="region-cluster" aria-label="${escapeHTML(region)} 낚시터 ${count}곳"><b>${escapeHTML(region)}</b><span>${count}곳</span></div>`; }
function resolveClusterPosition(lat, lng, placed) { const offsets=[[0,0],[0,.42],[.32,.24],[-.32,.24],[.32,-.24],[-.32,-.24],[0,-.42]]; for(const [latOffset,lngOffset] of offsets){const candidate={lat:lat+latOffset,lng:lng+lngOffset};if(placed.every(point=>Math.hypot((candidate.lat-point.lat)*1.15,candidate.lng-point.lng)>.34)){placed.push(candidate);return candidate;}} const candidate={lat,lng};placed.push(candidate);return candidate; }
function clearMarkers(){ markers.forEach(marker=>marker.setMap(null)); markers=[]; infoWindow?.close(); }
function showRegionDetail(region, group){ $('#region-detail-title').textContent=region; $('#region-detail-count').textContent=`등록된 낚시터 ${group.length}곳`; $('#region-spot-list').innerHTML=group.map(spot=>`<button class="region-spot-item" type="button"><span>${escapeHTML(spot.title)}</span><small class="${hasUnavailableAddress(spot)?'address-unavailable':''}">${escapeHTML(spot.address||spot.description||'위치 정보 확인')}</small></button>`).join(''); $('#region-focus').onclick=()=>{ $('#region-detail-modal').close(); const center=group.reduce((sum,spot)=>({lat:sum.lat+Number(spot.lat),lng:sum.lng+Number(spot.lng)}),{lat:0,lng:0}); center.lat/=group.length; center.lng/=group.length; const isProvince=/(특별자치도|특별시|광역시|도)$/.test(region); const previous=map.getCenter(); mapHistory.push({lat:previous.lat(),lng:previous.lng(),zoom:map.getZoom(),regionFilter:mapRegionFilter}); mapRegionFilter=isProvince?region:null; setMapView(center.lat,center.lng,isProvince?12:11); renderSpots(); toast(isProvince?`${region} 낚시터만 지도에 표시합니다.`:`${region} 중심으로 이동했습니다.`); }; document.querySelectorAll('.region-spot-item').forEach((button,index)=>button.addEventListener('click',()=>{ $('#region-detail-modal').close(); showSpotDetail(group[index]); })); $('#region-detail-modal').showModal(); }
function refreshMapMarkers(){ if(!map)return; clearMarkers(); const visible=visibleSpots(); if(map.getZoom()<=9){ const grouped=new Map(), clusterPositions=[]; const wideView=map.getZoom()<=8; visible.forEach(spot=>{ const region=wideView ? provinceOf(spot) : regionOf(spot); if(!grouped.has(region))grouped.set(region,[]); grouped.get(region).push(spot); }); grouped.forEach((group,region)=>{ const lat=group.reduce((sum,spot)=>sum+Number(spot.lat),0)/group.length; const lng=group.reduce((sum,spot)=>sum+Number(spot.lng),0)/group.length; const position=wideView?resolveClusterPosition(lat,lng,clusterPositions):{lat,lng}; const marker=new naver.maps.Marker({position:new naver.maps.LatLng(position.lat,position.lng),map,zIndex:200,clickable:true,icon:{content:clusterContent(region,group.length),anchor:new naver.maps.Point(38,26)}}); naver.maps.Event.addListener(marker,'click',()=>showRegionDetail(region,group)); markers.push(marker); }); return; } visible.forEach(spot=>{ const marker=new naver.maps.Marker({position:new naver.maps.LatLng(Number(spot.lat),Number(spot.lng)),map,zIndex:100,clickable:true,icon:{content:markerContent(spot.kind),anchor:new naver.maps.Point(14,28)}}); naver.maps.Event.addListener(marker,'click',()=>{openSpotInfo(spot,marker);showSpotDetail(spot);}); markers.push(marker); }); }
function renderSpots(){ const list=$('#spot-list'); const visible=visibleSpots(); list.innerHTML=visible.length?visible.map(s=>`<button class="spot-item" data-id="${s.id}"><span class="spot-meta">${isUserSpot(s)?'사용자 공유 · ':''}${s.kind==='sea'?'바다':'민물'} · ${escapeHTML(s.species)}</span><h3>${escapeHTML(s.title)}</h3><p class="${hasUnavailableAddress(s)?'address-unavailable':''}">${escapeHTML(s.description)}</p></button>`).join(''):'<p style="font-size:12px;color:#637479;padding:16px 2px">검색 결과가 없습니다.</p>'; refreshMapMarkers(); const pointCount=$('#point-count'); if(pointCount) pointCount.textContent=spots.length; }
function renderRanking(){ /* 기존 독립 랭킹은 낚시터 상세 게시글 기반으로 전환됨 */ }
function preview(input, target){ input.addEventListener('change',()=>{ const [file]=input.files; if(!file)return; const reader=new FileReader(); reader.onload=()=>{target.src=reader.result;target.hidden=false}; reader.readAsDataURL(file); }); }
async function loadNaverMaps(){ const config=await fetch('/api/map-config').then(response=>response.json()); if(!config.ncpKeyId) throw new Error('네이버 지도 Client ID가 없습니다.'); if(window.naver?.maps) return window.naver.maps; await new Promise((resolve,reject)=>{const script=document.createElement('script');script.src=`https://oapi.map.naver.com/openapi/v3/maps.js?ncpKeyId=${encodeURIComponent(config.ncpKeyId)}&submodules=geocoder`;script.onload=resolve;script.onerror=reject;document.head.append(script);}); if(!window.naver?.maps) throw new Error('네이버 지도를 초기화하지 못했습니다.'); return window.naver.maps; }
async function init(){ try { await loadNaverMaps(); } catch(error) { $('#map').innerHTML='<p class="map-load-error">네이버 지도를 불러오지 못했습니다. API 서비스와 Web 서비스 URL 설정을 확인해주세요.</p>'; return; } map=new naver.maps.Map('map',{center:new naver.maps.LatLng(37.47,127.0),zoom:8,zoomControl:true,zoomControlOptions:{position:naver.maps.Position.BOTTOM_RIGHT}}); infoWindow=new naver.maps.InfoWindow({borderWidth:0,backgroundColor:'transparent',disableAnchor:true,pixelOffset:new naver.maps.Point(0,-30)}); renderSpots();renderRanking(); naver.maps.Event.addListener(map,'zoom_changed',refreshMapMarkers); $('#search-input').addEventListener('input',renderSpots); document.querySelectorAll('.chip').forEach(b=>b.addEventListener('click',()=>{filter=b.dataset.filter;document.querySelector('.chip.selected').classList.remove('selected');b.classList.add('selected');renderSpots();})); $('#locate-btn').addEventListener('click',()=>navigator.geolocation?navigator.geolocation.getCurrentPosition(p=>{setMapView(p.coords.latitude,p.coords.longitude,14);toast('현재 위치로 이동했어요.');},()=>toast('위치를 가져올 수 없어 서울 중심 지도를 표시합니다.')):toast('이 브라우저에서는 위치 정보를 지원하지 않습니다.')); $('#open-spot-modal').addEventListener('click',()=>{selectedLatLng=null; const center=map.getCenter(); $('#selected-location').textContent='현재 지도 중심 위치에 포인트가 등록됩니다.'; $('#spot-modal').showModal(); setSpotAddress(center.lat(),center.lng());}); $('#open-catch-modal').addEventListener('click',()=>$('#catch-modal').showModal()); preview($('#catch-image'),$('#catch-preview')); $('#spot-form').addEventListener('submit',e=>{e.preventDefault();const d=new FormData(e.target);const center=selectedLatLng||map.getCenter();spots.unshift({id:Date.now(),title:d.get('title'),kind:d.get('kind'),description:d.get('description'),species:'새 포인트',lat:center.lat(),lng:center.lng()});save();renderSpots();e.target.reset();$('#spot-modal').close();toast('새 낚시 포인트를 공유했어요!');}); $('#catch-form').addEventListener('submit',e=>{e.preventDefault();const d=new FormData(e.target);const input=d.get('length');const ai=!input;const length=ai?Number((28+(d.get('species').length*3.7)+Math.random()*18).toFixed(1)):Number(input);catches.push({name:'박낚시꾼',species:d.get('species'),length,color:'blue',ai});save();renderRanking();e.target.reset();$('#catch-preview').hidden=true;$('#catch-modal').close();toast(ai?`AI가 ${length}cm로 추정해 랭킹에 등록했어요.`:`${length}cm 조과를 랭킹에 등록했어요!`);}); await loadOfficialData(); }

function switchTab(id){
  document.querySelectorAll('.tab-panel').forEach(panel => { panel.hidden = panel.id !== id; });
  document.querySelectorAll('nav [data-tab]').forEach(button => button.classList.toggle('active', button.dataset.tab === id));
  if(id === 'map-section') setTimeout(() => map?.refresh(), 0);
}
document.querySelectorAll('nav [data-tab]').forEach(button => button.addEventListener('click', () => switchTab(button.dataset.tab)));
document.querySelectorAll('[data-close-modal]').forEach(button => button.addEventListener('click', () => document.getElementById(button.dataset.closeModal).close()));

const seasonalRules = {
  sea: [
    { label: '꽃게', size: '금지체장 6.4cm', closed: ['06-21', '08-20'] },
    { label: '고등어', size: '금지체장 21cm', closed: ['04-01', '06-30'] },
    { label: '광어', size: '금지체장 35cm', closed: null },
    { label: '우럭', size: '금지체장 23cm', closed: null }
  ],
  river: [
    { label: '쏘가리', size: '금지체장 18cm', closed: ['05-01', '06-30'] },
    { label: '배스', size: '지역별 방류 제한 확인', closed: null },
    { label: '붕어', size: '지역별 기준 확인', closed: null }
  ]
};
const restrictedZones = [
  { name: '청평호 수질보호 구역', points: [[37.735,127.405],[37.735,127.440],[37.705,127.440],[37.705,127.405]] },
  { name: '을왕리 항만 안전 구역', points: [[37.460,126.350],[37.460,126.385],[37.435,126.385],[37.435,126.350]] },
  { name: '대부도 양식장 보호 구역', points: [[37.205,126.620],[37.205,126.665],[37.170,126.665],[37.170,126.620]] }
];
function dateInRange(range) { if(!range) return false; const now = new Date(); const current = (now.getMonth()+1)*100 + now.getDate(); const [start,end] = range.map(value => { const [m,d]=value.split('-').map(Number); return m*100+d; }); return start <= end ? current >= start && current <= end : current >= start || current <= end; }
function showSpotDetail(spot) {
  currentSpot = spot;
  const rules = seasonalRules[spot.kind] || [];
  $('#detail-title').textContent = spot.title;
  $('#detail-description').textContent = spot.description;
  const ownerCard = $('#detail-owner-card');
  const ownerId = spot.owner_id || spot.user_id;
  if (isUserSpot(spot) && ownerId) {
    const ownerName = spot.owner_name || '낚시꾼';
    $('#detail-owner-name').textContent = ownerName;
    $('#detail-owner-profile').dataset.ownerId = ownerId;
    ownerCard.hidden = false;
  } else {
    $('#detail-owner-profile').dataset.ownerId = '';
    ownerCard.hidden = true;
  }
  const addressButton = $('#detail-address');
  addressButton.textContent = `⌖ ${spot.address || spot.description} · 확대하기`;
  addressButton.classList.toggle('address-unavailable', hasUnavailableAddress(spot));
  addressButton.dataset.lat = spot.lat;
  addressButton.dataset.lng = spot.lng;
  $('#detail-region').textContent = `${spot.kind === 'sea' ? '해양' : '내수면'} · 등록 어종: ${spot.species}`;
  $('#detail-regulations').innerHTML = rules.map(rule => `<div class="regulation-row"><b>${rule.label}</b><span>${rule.size}${rule.closed ? ` · ${dateInRange(rule.closed) ? '<strong style="color:#c9473e">현재 금어기</strong>' : `금어기 ${rule.closed[0]}~${rule.closed[1]}`}` : ''}</span></div>`).join('');
  loadSpotPosts(spot.id);
  $('#spot-detail-modal').showModal();
}
function focusSpotOnMap(spot, zoom = 16, closeDetail = true) { switchTab('map-section'); if(closeDetail) $('#spot-detail-modal').close(); setMapView(spot.lat,spot.lng,zoom); const marker = markers.find(item => Math.abs(item.getPosition().lat() - Number(spot.lat)) < .00001 && Math.abs(item.getPosition().lng() - Number(spot.lng)) < .00001); if(marker) openSpotInfo(spot,marker); }
$('#detail-address').addEventListener('click', event => { const button = event.currentTarget; focusSpotOnMap({lat: button.dataset.lat, lng: button.dataset.lng}); });
$('#detail-owner-profile').addEventListener('click', event => {
  const ownerId = event.currentTarget.dataset.ownerId;
  if (ownerId) openAnglerProfile(ownerId);
});
function displayProtectedAreas(geojson) { if(!map)return; const legend=$('#protected-legend'); protectedLayers.forEach(layer=>layer.setMap(null)); protectedLayers=[]; const addPolygon=(paths,name)=>{const layer=new naver.maps.Polygon({map,paths,zIndex:1,strokeColor:'#c94239',strokeWeight:1.5,strokeStyle:'shortdash',fillColor:'#e04f45',fillOpacity:.26}); naver.maps.Event.addListener(layer,'click',()=>toast(`⚠ ${name||'수산자원보호구역'}`)); protectedLayers.push(layer);}; if(!geojson.features?.length) { if(legend)legend.innerHTML='<span></span> 포획·출입 제한 구역 (예시)'; restrictedZones.forEach(zone=>addPolygon(zone.points.map(([lat,lng])=>new naver.maps.LatLng(lat,lng)),`예시 · ${zone.name}`)); return; } if(legend)legend.innerHTML='<span></span> 수산자원보호구역 (공식 WFS)'; geojson.features.forEach(feature=>{const geometry=feature.geometry||{};const name=feature.properties?.name||feature.properties?.NAME||'수산자원보호구역';const polygons=geometry.type==='Polygon'?[geometry.coordinates]:geometry.type==='MultiPolygon'?geometry.coordinates:[];polygons.forEach(polygon=>addPolygon(polygon[0].map(([lng,lat])=>new naver.maps.LatLng(lat,lng)),name));}); }
async function enrichOfficialAddresses(){ const targets=spots.filter(spot=>isOfficialSpot(spot)&&spot.address==='공식 API 등록 포인트'); if(!targets.length)return; try { toast(`주소가 비어 있는 ${targets.length}개 포인트를 보완하고 있어요.`); const result=await fetch('/api/address-enrichment',{method:'POST'}).then(response=>response.json()); const refreshed=await fetch('/api/spots').then(response=>response.json()); const officialById=new Map((refreshed.items||[]).map(spot=>[String(spot.id),spot])); spots.forEach((spot,index)=>{const newer=officialById.get(String(spot.id));if(newer)spots[index]={...spot,...newer,source:'official'};}); renderSpots(); const reason=Array.isArray(result.errors)&&result.errors[0]; toast(result.updated?`${result.updated}개 포인트의 주소를 네이버 지도로 보완했어요.`:reason||'주소 보완 결과가 없습니다. 네이버 API 설정을 확인해주세요.'); } catch { toast('서버의 네이버 주소 보완을 시작하지 못했습니다. 서버를 다시 실행한 뒤 재시도해주세요.'); } }
async function loadOfficialData() { try { const [spotsResponse, areasResponse] = await Promise.all([fetch('/api/spots'), fetch('/api/protected-areas')]); const official = await spotsResponse.json(); const areas = await areasResponse.json(); const known = new Set(spots.map(spot => `${Number(spot.lat).toFixed(5)}:${Number(spot.lng).toFixed(5)}`)); (official.items || []).forEach(spot => { const index = spots.findIndex(existing => String(existing.id) === String(spot.id)); const key = `${Number(spot.lat).toFixed(5)}:${Number(spot.lng).toFixed(5)}`; if(index >= 0) spots[index] = {...spots[index],...spot,source:'official'}; else if(!known.has(key)) spots.push({...spot,source:'official'}); }); renderSpots(); displayProtectedAreas(areas); await enrichOfficialAddresses(); } catch { displayProtectedAreas({features:[]}); } }
// 등록용 지도 안에서 장소를 찾고, 검색 결과는 이 지도만 이동시킨다.
async function searchSpotLocation() {
  const query=$('#spot-location-query').value.trim(), results=$('#spot-location-results');
  if(query.length<2){toast('두 글자 이상 입력해 주세요.');return;}
  results.hidden=false;results.innerHTML='<p>장소를 찾고 있어요…</p>';
  try {
    let data;
    try { const response=await fetch(`/api/geocode?query=${encodeURIComponent(query)}`);data=await response.json();if(!response.ok||!data.items?.length)throw new Error(data.error||'검색 결과가 없습니다.'); }
    catch { data=await new Promise((resolve,reject)=>naver.maps.Service.geocode({query},(status,response)=>{const addresses=response?.v2?.addresses||[];if(status!==naver.maps.Service.Status.OK||!addresses.length)return reject(new Error('검색 결과가 없습니다.'));resolve({items:addresses.slice(0,8).map(item=>{const address=item.roadAddress||item.jibunAddress||query;return {name:address,address,lat:Number(item.y),lng:Number(item.x)};})});})); }
    results.innerHTML=data.items.map((item,index)=>`<button type="button" class="spot-location-result" data-index="${index}"><b>${escapeHTML(item.name)}</b><small>${escapeHTML(item.address)}</small></button>`).join('');
    results.querySelectorAll('.spot-location-result').forEach(button=>button.addEventListener('click',()=>{const item=data.items[Number(button.dataset.index)];results.hidden=true;setSpotPickerPosition(item.lat,item.lng,15);toast('등록용 지도에서 위치를 세밀하게 조정하세요.');}));
  } catch(error) {results.innerHTML=`<p>${escapeHTML(error.message||'장소를 찾지 못했습니다.')}</p>`;}
}
// 위치 권한 없이, 사용자가 직접 주소를 검색하고 지도의 정확한 지점을 선택한다.
let spotPickerMapListenerReady = false;
const spotPickerMapReady = setInterval(() => {
  if (!map || spotPickerMapListenerReady) return;
  spotPickerMapListenerReady = true;
  clearInterval(spotPickerMapReady);
  naver.maps.Event.addListener(map, 'click', event => {
    if (!pickingSpotLocation) return;
    pickingSpotLocation = false;
    selectedLatLng = {lat:event.coord.lat(), lng:event.coord.lng()};
    $('#selected-location').textContent = '지도에서 선택한 위치에 포인트가 등록됩니다.';
    $('#spot-modal').showModal();
    setSpotAddress(selectedLatLng.lat, selectedLatLng.lng);
  });
}, 100);
$('#open-spot-modal').addEventListener('click', () => {
  pickingSpotLocation = false;
  $('#spot-location-query').value = '';
  $('#spot-location-results').hidden = true;
  setTimeout(openSpotPickerMap, 0);
});
$('#spot-location-search-button').addEventListener('click', searchSpotLocation);
$('#spot-location-query').addEventListener('keydown', event => {
  if (event.key === 'Enter') { event.preventDefault(); searchSpotLocation(); }
});
$('#pick-location-on-map')?.addEventListener('click', () => {
  $('#spot-modal').close();
  pickingSpotLocation = true;
  toast('지도를 클릭해 정확한 포인트 위치를 선택하세요.');
});
init();
$('#spot-list').addEventListener('click', event => { const item = event.target.closest('.spot-item'); if(!item) return; event.stopImmediatePropagation(); const spot = spots.find(value => String(value.id) === String(item.dataset.id)); if(spot) showSpotDetail(spot); }, true);

let currentSpot = null, reportTarget = null;
function safeImageUrl(value) { try { const url = new URL(value); return /^https?:$/.test(url.protocol) ? url.href : ''; } catch { return ''; } }
async function loadSpotPosts(spotId) {
  const container = $('#spot-posts'); container.innerHTML = '<p class="empty-post">게시글을 불러오는 중…</p>';
  try {
    const result = await fetch(`/api/community?spotId=${encodeURIComponent(spotId)}`).then(response => response.json());
    if (result.error) throw new Error(result.error);
    const posts = result.posts || [];
    const viewerId = result.viewerId || '';
    container.innerHTML = posts.length ? posts.map(post => { const imageUrl=safeImageUrl(post.image_url); const ownPost=viewerId && post.author_id === viewerId; return `<article class="post-card${imageUrl?' has-image':''}"><div class="post-copy"><header><b>${escapeHTML(post.author || '익명 낚시꾼')}</b><span>${post.length ? `${Number(post.length).toFixed(1)}cm${post.length_is_ai ? ' · AI 추정' : ''}` : '일반 후기'}</span></header><p>${escapeHTML(post.content)}</p><footer><span>${post.species ? escapeHTML(post.species) : '조황 정보'} · 추천 ${post.likes || 0}</span><span><button data-like="${post.id}">추천</button> <button class="report-post" data-report-post="${post.id}">신고</button>${ownPost ? ` <button class="delete-post" data-delete-post="${post.id}">삭제</button>` : ''}</span></footer></div>${imageUrl?`<a class="post-thumbnail" href="${escapeHTML(imageUrl)}" target="_blank" rel="noopener"><img src="${escapeHTML(imageUrl)}" alt="게시글 사진" /></a>`:''}</article>`; }).join('') : '<p class="empty-post">아직 게시글이 없습니다. 첫 조황을 남겨보세요.</p>';
    container.querySelectorAll('.post-card header b').forEach((author, index) => {
      const authorId = posts[index]?.author_id;
      if (!authorId) return;
      author.classList.add('author-profile-link'); author.tabIndex = 0; author.setAttribute('role', 'button'); author.setAttribute('aria-label', `${posts[index].author || '작성자'} 프로필 보기`);
      const openProfile = () => openAnglerProfile(authorId);
      author.addEventListener('click', openProfile);
      author.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openProfile(); } });
    });
    container.querySelectorAll('[data-like]').forEach(button => button.addEventListener('click', async () => { await fetch('/api/community/like', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({postId:button.dataset.like})}); loadSpotPosts(spotId); }));
    container.querySelectorAll('[data-report-post]').forEach(button => button.addEventListener('click', () => openReport('post', button.dataset.reportPost)));
    container.querySelectorAll('[data-delete-post]').forEach(button => button.addEventListener('click', async () => {
      if (!window.confirm('내 게시글을 삭제할까요? 삭제한 게시글은 복구할 수 없습니다.')) return;
      const response = await fetch('/api/community/delete-post', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({postId:button.dataset.deletePost})});
      if (!response.ok) return toast(await apiError(response, '게시글을 삭제하지 못했습니다.'));
      toast('게시글을 삭제했습니다.'); loadSpotPosts(spotId); loadHeroRanking();
    }));
  } catch { container.innerHTML = '<p class="empty-post">게시글 서비스를 준비 중입니다. Supabase 테이블 설정을 확인해주세요.</p>'; }
}
function openReport(kind, targetId) { if (!signedInUser) { showAuth(); toast('로그인 후 신고할 수 있습니다.'); return; } reportTarget={kind,targetId}; $('#report-modal').showModal(); }
$('#new-post-button').addEventListener('click', () => { if(!currentSpot) return; fillLoggedInAuthor(); $('#post-modal').showModal(); });
$('#report-spot-button').addEventListener('click', () => { if(currentSpot) openReport('spot', currentSpot.id); });
$('#open-inquiry-modal').addEventListener('click', () => $('#inquiry-modal').showModal());
async function apiError(response, fallback) { try { const result = await response.json(); return result.error || fallback; } catch { return fallback; } }
async function imageDataUrl(file) { if(!file || !file.size) throw new Error('사진을 선택해 주세요.'); if(file.size > 5 * 1024 * 1024) throw new Error('사진은 5MB 이하만 업로드할 수 있습니다.'); return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(file);}); }
async function uploadPostImage(file) { if(!file || !file.size) return ''; const dataUrl=await imageDataUrl(file); const response=await fetch('/api/community/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataUrl})}); if(!response.ok) throw new Error(await apiError(response,'사진 업로드에 실패했습니다.')); return (await response.json()).imageUrl; }
function setupImagePreview(inputId, previewId) {
  const input = $(`#${inputId}`), preview = $(`#${previewId}`), image = preview.querySelector('img');
  input.addEventListener('change', () => {
    const file = input.files?.[0];
    if (!file) { preview.hidden = true; image.removeAttribute('src'); return; }
    image.src = URL.createObjectURL(file);
    preview.hidden = false;
  });
  preview.querySelector('[data-reselect-image]').addEventListener('click', () => input.click());
}
setupImagePreview('post-image', 'post-image-selection');
setupImagePreview('rank-image', 'rank-image-selection');
function showFormStatus(form, message = '', tone = 'error') {
  const status = form.querySelector('.form-status');
  if (!status) return;
  status.hidden = !message;
  status.textContent = message;
  status.dataset.tone = tone;
}
function fieldLabel(field) {
  return field.closest('label')?.childNodes?.[0]?.textContent?.trim() || '입력 항목';
}
function validateSubmission(form) {
  const invalid = [...form.elements].find(field => typeof field.checkValidity === 'function' && !field.checkValidity());
  if (!invalid) return true;
  const message = `${fieldLabel(invalid)}을(를) 확인해 주세요.`;
  showFormStatus(form, message);
  invalid.focus();
  return false;
}
async function submitOnce(form, dialog, request, successMessage, fallback) { if(form.dataset.submitting === 'true') return false; form.dataset.submitting='true'; const button=form.querySelector('button[type="submit"]'), originalLabel=button?.textContent; showFormStatus(form, '처리 중입니다…', 'pending'); if(button){button.disabled=true;button.textContent='처리 중…';} try { const response=await request(); if(!response.ok){const message=await apiError(response, fallback); showFormStatus(form, message); toast(message);return false;} form.reset(); if(dialog.open) dialog.close(); toast(successMessage); return true; } catch (error) { const message=error.message || fallback; showFormStatus(form, message); toast(message); return false; } finally { form.dataset.submitting='false'; if(button){button.disabled=false;button.textContent=originalLabel;} } }
function addFormValidationFeedback(form) {
  // 브라우저 기본 검증만으로는 모바일에서 제출이 막힌 이유가 잘 보이지 않을 수 있다.
  form.addEventListener('invalid', event => {
    if (form.dataset.validationNotified === 'true') return;
    form.dataset.validationNotified = 'true';
    setTimeout(() => { delete form.dataset.validationNotified; }, 0);
    const field = event.target;
    const message = `${fieldLabel(field)}을(를) 확인해 주세요.`;
    showFormStatus(form, message);
    toast(message);
  }, true);
}

addFormValidationFeedback($('#post-form'));
addFormValidationFeedback($('#rank-catch-form'));

$('#post-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!signedInUser) { showAuth(); return toast('로그인 후 게시글을 등록할 수 있습니다.'); }
  if (!currentSpot) return toast('낚시터를 다시 선택한 뒤 게시글을 등록해 주세요.');
  const form = event.currentTarget;
  if (!validateSubmission(form)) return;
  const data = new FormData(form), length = data.get('length');
  const saved = await submitOnce(form, $('#post-modal'), async () => {
    const imageUrl = await uploadPostImage(data.get('image'));
    return fetch('/api/community/post', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({spotId:currentSpot.id, author:data.get('author') || '익명 낚시꾼', content:data.get('content'), imageUrl, species:data.get('species'), length:length ? Number(length) : null, lengthIsAi:false})
    });
  }, '게시글과 조과가 등록되었습니다.', '게시글 저장에 실패했습니다.');
  if (saved) loadSpotPosts(currentSpot.id);
});
$('#report-form').addEventListener('submit', event => { event.preventDefault(); const form=event.currentTarget,data=new FormData(form); submitOnce(form,$('#report-modal'),()=>fetch('/api/community/report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...reportTarget,reason:data.get('reason'),message:data.get('message')})}),'신고가 접수되었습니다.','신고 접수에 실패했습니다.'); });
$('#inquiry-form').addEventListener('submit', event => { event.preventDefault(); const form=event.currentTarget,data=new FormData(form); submitOnce(form,$('#inquiry-modal'),()=>fetch('/api/community/inquiry',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:data.get('kind'),contact:data.get('contact'),message:data.get('message')})}),'문의가 접수되었습니다.','문의 전송에 실패했습니다.'); });
$('#clear-region-filter').addEventListener('click', () => { mapRegionFilter=null; renderSpots(); setMapView(37.47,127.0,8); toast('전국 낚시터를 표시합니다.'); });
$('#map-back-button').addEventListener('click', () => { const previous=mapHistory.pop(); if(!previous) return toast('이전 지도 위치가 없습니다.'); mapRegionFilter=previous.regionFilter; setMapView(previous.lat,previous.lng,previous.zoom); renderSpots(); toast('이전 지도 위치로 돌아왔습니다.'); });
async function loadCommunitySpots() {
  try {
    const result = await fetch('/api/community-spots').then(response => response.json());
    if (result.error) return;
    // 사용자 공유 포인트는 서버 목록을 기준으로 교체한다. 삭제된 항목이 로컬 저장소에
    // 남아 지도에 재등장하는 일을 막는다.
    spots = spots.filter(spot => !isUserSpot(spot));
    (result.items || []).forEach(spot => spots.unshift({...spot, source:'user'}));
    save();
    renderSpots();
  } catch {}
}
$('#spot-form').addEventListener('submit', async event => {
  // 이전 데모용 로컬 저장 처리보다 먼저 실행해 입력값 초기화와 이중 저장을 막는다.
  event.preventDefault();
  event.stopImmediatePropagation();
  const form = event.currentTarget;
  const data = new FormData(form);
  const center = selectedLatLng || map.getCenter();
  const latitude = Number(typeof center?.lat === 'function' ? center.lat() : center?.lat);
  const longitude = Number(typeof center?.lng === 'function' ? center.lng() : center?.lng);
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
    toast('등록용 지도에서 위치를 한 번 선택해 주세요.');
    return;
  }
  let createdSpot;
  const success = await submitOnce(form, $('#spot-modal'), async () => {
    const response = await fetch('/api/community/spot', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({title:data.get('title'),kind:data.get('kind'),description:data.get('description'),address:data.get('address'),lat:latitude,lng:longitude})
    });
    if (response.ok) createdSpot = (await response.clone().json()).spot;
    return response;
  }, '포인트가 저장되었습니다.', '포인트 저장에 실패했습니다.');
  if (!success || !createdSpot) return;
  const index = spots.findIndex(spot => spot.title === createdSpot.title && Math.abs(Number(spot.lat) - Number(createdSpot.lat)) < .00001 && Math.abs(Number(spot.lng) - Number(createdSpot.lng)) < .00001);
  if (index >= 0) spots[index] = {...spots[index], ...createdSpot, source:'user'};
  else spots.unshift({...createdSpot, source:'user'});
  save();
  renderSpots();
}, true);
setTimeout(loadCommunitySpots, 1200);
function openRankedCatch(post) {
  const spot = spots.find(item => String(item.id) === String(post.spot_id));
  if (!spot) return toast('연결된 낚시 포인트 정보를 불러오는 중입니다. 잠시 후 다시 눌러 주세요.');
  showSpotDetail(spot);
  toast(`${spot.title}의 조과 게시글을 확인하세요.`);
}
async function openAnglerProfile(userId) {
  const dialog = $('#angler-profile-modal');
  $('#angler-profile-name').textContent = '낚시꾼 프로필';
  $('#angler-catch-total').textContent = '조과 정보를 불러오는 중…';
  $('#angler-top-catches').innerHTML = '';
  if (!dialog.open) dialog.showModal();
  try {
    const response = await fetch(`/api/angler-profile?id=${encodeURIComponent(userId)}`);
    const profile = await response.json();
    if (!response.ok) throw new Error(profile.error || '프로필을 불러오지 못했습니다.');
    $('#angler-profile-name').textContent = profile.displayName || '낚시꾼 프로필';
    $('#angler-catch-total').textContent = `총 조과 ${Number(profile.totalCatches || 0)}건`;
    const catches = profile.topCatches || [];
    $('#angler-top-catches').innerHTML = catches.length ? catches.map((post, index) => `<button type="button" class="angler-catch-row" data-profile-post="${escapeHTML(post.id)}"><b>${index + 1}</b><span>${escapeHTML(post.species || '조과')} · ${Number(post.length).toFixed(1)}cm</span><small>포인트·게시글 보기</small></button>`).join('') : '<p class="empty-post">등록된 조과가 없습니다.</p>';
    $('#angler-top-catches').querySelectorAll('[data-profile-post]').forEach(button => button.addEventListener('click', () => {
      const post = catches.find(item => String(item.id) === button.dataset.profilePost);
      if (post) { dialog.close(); openRankedCatch(post); }
    }));
  } catch (error) {
    $('#angler-catch-total').textContent = error.message || '프로필을 불러오지 못했습니다.';
  }
}
function userCatchRanking(posts) {
  const bestByUser = new Map();
  posts.filter(post => Number(post.length) > 0).forEach(post => {
    const key = post.author_id || `author:${post.author || '익명'}:${post.id}`;
    const current = bestByUser.get(key);
    if (!current || Number(post.length) > Number(current.length)) bestByUser.set(key, post);
  });
  return [...bestByUser.values()].sort((left, right) => Number(right.length) - Number(left.length));
}
async function loadHeroRanking() {
  const list=$('#hero-ranking-list');
  try {
    const result=await fetch('/api/community').then(response=>response.json());
    const ranked=userCatchRanking(result.posts||[]), topFive=ranked.slice(0,5);
    const topMarkup=topFive.map((post,index)=>`<button class="hero-rank" type="button" data-rank-post="${escapeHTML(post.id)}"><b>${String(index+1).padStart(2,'0')}</b><span>${escapeHTML(post.author||'익명 낚시꾼')} · ${escapeHTML(post.species||'조과')}</span><strong>${Number(post.length).toFixed(1)} <small>cm</small></strong></button>`).join('');
    const myIndex=ranked.findIndex(post => result.viewerId && post.author_id === result.viewerId);
    const myMarkup=result.viewerId ? (myIndex >= 0 ? `<button class="hero-rank hero-my-rank" type="button" data-rank-post="${escapeHTML(ranked[myIndex].id)}"><b>내 ${myIndex+1}위</b><span>${escapeHTML(ranked[myIndex].species||'조과')} · 내 최고 조과</span><strong>${Number(ranked[myIndex].length).toFixed(1)} <small>cm</small></strong></button>` : '<p class="my-rank-empty">내 조과를 등록하면 현재 순위를 확인할 수 있어요.</p>') : '';
    list.innerHTML=ranked.length ? `${topMarkup}${myMarkup}` : '<p>등록된 조과가 아직 없습니다.</p>';
    list.querySelectorAll('[data-rank-post]').forEach(button => button.addEventListener('click', () => {
      const post=(result.posts||[]).find(item => String(item.id) === button.dataset.rankPost);
      if (post) openRankedCatch(post);
    }));
  } catch { list.innerHTML='<p>랭킹을 불러오지 못했습니다.</p>'; }
}
setTimeout(loadHeroRanking, 1400);

function rankSpotLabel(spot) { return `${spot.title}${spot.address ? ` · ${spot.address}` : ''}`; }
function renderRankSpotOptions() {
  $('#rank-spot-options').innerHTML = spots.map(spot => `<option value="${escapeHTML(rankSpotLabel(spot))}">${escapeHTML(spot.species || '')}</option>`).join('');
}
function updateRankSpotAddress() {
  const picker = $('#rank-spot-picker');
  const query = picker.value.trim();
  // 목록을 고르면 전체 라벨을 쓰지만, 사용자가 포인트명만 입력한 경우도 인식한다.
  // 동명 포인트는 전체 라벨을 선택해야 주소까지 정확히 구분된다.
  const sameTitle = spots.filter(item => item.title === query);
  const spot = spots.find(item => rankSpotLabel(item) === query)
    || (sameTitle.length === 1 ? sameTitle[0] : null);
  const note = $('#rank-address-note');
  $('#rank-spot-id').value = spot ? spot.id : '';
  if (!spot) {
    note.classList.remove('address-unavailable');
    note.textContent = picker.value ? '목록에서 제안된 낚시 포인트를 선택해 주세요.' : '포인트명이나 지역을 검색한 뒤 목록에서 선택해 주세요.';
    return;
  }
  const unavailable = hasUnavailableAddress(spot) || !spot.address;
  note.classList.toggle('address-unavailable', unavailable);
  note.textContent = unavailable ? '주소 미제공 · 지도에서 위치를 확인한 뒤 포인트 정보를 보완해 주세요.' : `주소: ${spot.address}`;
}
function openRankCatchModal() {
  fillLoggedInAuthor();
  $('#rank-spot-picker').value = '';
  $('#rank-spot-id').value = '';
  renderRankSpotOptions();
  const note = $('#rank-address-note');
  note.classList.remove('address-unavailable');
  note.textContent = '포인트명이나 지역을 검색한 뒤 목록에서 선택해 주세요.';
  $('#rank-catch-modal').showModal();
}
$('#open-rank-catch-modal').addEventListener('click', openRankCatchModal);
$('#rank-spot-picker').addEventListener('input', updateRankSpotAddress);
$('#rank-spot-picker').addEventListener('change', updateRankSpotAddress);
$('#rank-open-spot').addEventListener('click', () => {
  $('#rank-catch-modal').close();
  $('#open-spot-modal').click();
});
$('#rank-catch-form').addEventListener('submit', event => {
  event.preventDefault();
  if (!signedInUser) { showAuth(); return toast('로그인 후 조과를 등록할 수 있습니다.'); }
  const form = event.currentTarget;
  if (!validateSubmission(form)) return;
  updateRankSpotAddress();
  if (!$('#rank-spot-id').value) return toast('목록에서 낚시 포인트를 선택해 주세요.');
  const data = new FormData(form);
  submitOnce(form, $('#rank-catch-modal'), async () => {
    const imageUrl = await uploadPostImage(data.get('image'));
    return fetch('/api/community/post', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({spotId:data.get('spotId'),author:data.get('author') || '익명 낚시꾼',species:data.get('species'),length:Number(data.get('length')),content:data.get('content') || '실시간 조과 랭킹 등록',imageUrl,lengthIsAi:false})
    });
  }, '실시간 조과 랭킹에 등록되었습니다.', '랭킹 등록에 실패했습니다.').then(success => {
    if (success) loadHeroRanking();
  });
});

let signedInUser = null;
let verifiedSignupEmail = '';
function fillLoggedInAuthor() {
  document.querySelectorAll('#post-form [name="author"], #rank-catch-form [name="author"]').forEach(input => {
    input.value = signedInUser?.displayName || '';
    input.readOnly = true;
  });
}
function startEmailCooldown(button, seconds = 60) {
  let remaining = seconds;
  const original = button.dataset.label || button.textContent;
  button.dataset.label = original;
  button.dataset.cooldown = 'true';
  button.disabled = true;
  button.textContent = `${remaining}초 후 다시 시도`;
  const timer = setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) { clearInterval(timer); delete button.dataset.cooldown; button.disabled = false; button.textContent = original; return; }
    button.textContent = `${remaining}초 후 다시 시도`;
  }, 1000);
}
function applySession(user) {
  signedInUser = user || null;
  const profile = $('#profile-button'), adminTab = $('#admin-tab');
  profile.classList.toggle('is-admin', signedInUser?.role === 'admin');
  profile.innerHTML = signedInUser ? `${escapeHTML(signedInUser.displayName)}<span>${signedInUser.role === 'admin' ? '운영자' : '내 프로필'}</span>` : '로그인';
  profile.setAttribute('aria-label', signedInUser ? '내 프로필 및 로그아웃' : '로그인');
  adminTab.hidden = signedInUser?.role !== 'admin';
  fillLoggedInAuthor();
  if (signedInUser?.role === 'admin') loadAdminOverview();
}
async function refreshSession() {
  try { const result = await fetch('/api/auth/me').then(response => response.json()); applySession(result.user); }
  catch { applySession(null); }
}
function showAuth() {
  if ($('#signup-modal').open) $('#signup-modal').close();
  if (!$('#auth-modal').open) $('#auth-modal').showModal();
}
function showSignup() {
  if ($('#auth-modal').open) $('#auth-modal').close();
  $('#email-confirmation-panel').hidden = true; $('#signup-form').hidden = false;
  if (!verifiedSignupEmail) { $('#signup-details').hidden = true; $('#signup-email').readOnly = false; $('#signup-email-state').textContent = '인증 필요'; }
  if (!$('#signup-modal').open) $('#signup-modal').showModal();
}
function showEmailConfirmation(email) {
  $('#signup-form').hidden = true;
  $('#confirmation-email').textContent = email || '입력한 이메일';
  $('#email-confirmation-panel').hidden = false;
}
$('#profile-button').addEventListener('click', () => {
  if (!signedInUser) return showAuth();
  $('#profile-email').textContent = signedInUser.email || '이메일 정보 없음';
  $('#profile-role').textContent = signedInUser.role === 'admin' ? '운영자 계정' : '일반 회원';
  $('#profile-modal').showModal();
});
$('#profile-catches').addEventListener('click', () => {
  if (!signedInUser) return;
  $('#profile-modal').close();
  openAnglerProfile(signedInUser.id);
});
$('#profile-logout').addEventListener('click', async () => {
  const button = $('#profile-logout');
  if (button.disabled) return;
  button.disabled = true;
  try {
    await fetch('/api/auth/signout', {method:'POST'});
    $('#profile-modal').close();
    applySession(null);
    toast('로그아웃했습니다.');
  } finally { button.disabled = false; }
});
$('#show-signup').addEventListener('click', showSignup);
$('#show-signin').addEventListener('click', () => showAuth(false));
async function submitAuth(event, action) {
  event.preventDefault();
  const form = event.currentTarget, data = new FormData(form);
  const button = form.querySelector('button[type="submit"]');
  if (button?.disabled) return;
  const label = button?.textContent;
  if (button) { button.disabled = true; button.textContent = '처리 중…'; }
  try {
    const response = await fetch(`/api/auth/${action}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(Object.fromEntries(data))});
    const result = await response.json();
    if (!response.ok) return toast(result.error || '인증을 완료하지 못했습니다.');
    if (result.requiresEmailConfirmation) {
      showEmailConfirmation(result.email || String(data.get('email') || ''));
      return toast('인증 메일을 확인해 주세요.');
    }
    if (!result.user) return toast('가입 확인 이메일을 확인한 뒤 로그인해 주세요.');
    // 응답만 믿지 않고 HttpOnly 세션 쿠키가 실제로 저장됐는지 다시 확인한다.
    const sessionResponse = await fetch('/api/auth/me', {credentials: 'same-origin'});
    const session = await sessionResponse.json();
    if (!session.user) return toast('로그인 세션을 저장하지 못했습니다. server.py를 재시작한 뒤 다시 시도해 주세요.');
    applySession(session.user); $('#auth-modal').close(); form.reset();
    toast(action === 'signup' ? '회원가입과 로그인이 완료되었습니다.' : '로그인했습니다.');
  } catch {
    toast('인증 서버에 연결하지 못했습니다. server.py를 재시작하고 Supabase 설정을 확인해 주세요.');
  } finally {
    if (button) { button.disabled = false; button.textContent = label; }
  }
}
$('#signin-form').addEventListener('submit', event => submitAuth(event, 'signin'));
$('#start-email-verification').addEventListener('click', async event => {
  const email = $('#signup-email').value.trim();
  const button = event.currentTarget;
  if (!email) return toast('이메일을 입력해 주세요.');
  if (button.disabled) return;
  button.disabled = true; button.textContent = '발송 중…';
  try {
    const response = await fetch('/api/auth/start-signup-verification', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({email})});
    const result = await response.json();
    if (!response.ok && response.status !== 202) return toast(result.error || '인증 메일을 보내지 못했습니다.');
    if (result.emailVerified) {
      verifiedSignupEmail = result.email || email;
      $('#signup-email').value = verifiedSignupEmail; $('#signup-email').readOnly = true;
      $('#signup-email-state').textContent = '로컬 인증 완료'; $('#signup-details').hidden = false;
      return toast('로컬 개발 환경: 이메일 인증을 생략했습니다. 가입 정보를 입력해 주세요.');
    }
    showEmailConfirmation(result.email || email); toast('인증 메일을 보냈습니다.'); startEmailCooldown(button);
  } catch { toast('인증 서버에 연결하지 못했습니다. server.py를 재시작해 주세요.'); }
  finally { if (!button.dataset.cooldown) { button.disabled = false; button.textContent = '인증하기'; } }
});
$('#signup-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget, data = new FormData(form), button = form.querySelector('button[type="submit"]');
  if (!verifiedSignupEmail) return toast('이메일 인증을 먼저 완료해 주세요.');
  if (button.disabled) return;
  button.disabled = true; button.textContent = '가입 중…';
  try {
    const response = await fetch('/api/auth/complete-signup', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(Object.fromEntries(data))});
    const result = await response.json();
    if (!response.ok || !result.ok) return toast(result.error || '회원가입을 완료하지 못했습니다.');
    const email = String(data.get('email') || '');
    form.reset(); verifiedSignupEmail = ''; $('#signup-details').hidden = true; $('#signup-email').readOnly = false;
    $('#signin-form [name="email"]').value = email; showAuth();
    toast('회원가입이 완료되었습니다. 비밀번호를 입력해 로그인해 주세요.');
  } catch { toast('인증 서버에 연결하지 못했습니다.'); }
  finally { button.disabled = false; button.textContent = '회원가입'; }
});
$('#confirmation-to-signin').addEventListener('click', () => showAuth(false));
$('#verify-signup-email').addEventListener('click', async event => {
  const email = $('#confirmation-email').textContent.trim();
  const token = $('#signup-email-token').value.trim();
  const button = event.currentTarget;
  if (!token) return toast('메일로 받은 인증번호를 입력해 주세요.');
  if (button.disabled) return;
  button.disabled = true;
  try {
    const response = await fetch('/api/auth/verify-signup-email', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({email, token})});
    const result = await response.json();
    if (!response.ok || !result.emailVerified) return toast(result.error || '인증번호를 확인하지 못했습니다.');
    verifiedSignupEmail = result.email || email;
    showSignup(); $('#signup-email').value = verifiedSignupEmail; $('#signup-email').readOnly = true;
    $('#signup-email-state').textContent = '인증 완료'; $('#signup-details').hidden = false; $('#signup-email-token').value = '';
    toast('이메일 인증이 완료되었습니다. 가입 정보를 입력해 주세요.');
  } catch { toast('인증 서버에 연결하지 못했습니다.'); }
  finally { button.disabled = false; }
});
$('#resend-confirmation').addEventListener('click', async event => {
  const email = $('#confirmation-email').textContent.trim();
  const button = event.currentTarget;
  if (!email || button.disabled) return;
  button.disabled = true;
  try {
    const response = await fetch('/api/auth/resend-confirmation', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({email})});
    const result = await response.json();
    if (response.ok) { toast('인증 메일을 다시 보냈습니다.'); startEmailCooldown(button); }
    else toast(result.error || '인증 메일을 보내지 못했습니다.');
  } catch { toast('인증 서버에 연결하지 못했습니다.'); }
  finally { if (!button.dataset.cooldown) button.disabled = false; }
});
document.querySelectorAll('[data-open-policy]').forEach(button => button.addEventListener('click', () => $(`#${button.dataset.openPolicy}`).showModal()));
document.addEventListener('click', event => {
  const protectedAction = event.target.closest('#open-spot-modal,#open-rank-catch-modal,#new-post-button,#open-inquiry-modal,#report-spot-button,[data-report-post],[data-like]');
  if (protectedAction && !signedInUser) {
    event.preventDefault(); event.stopImmediatePropagation(); showAuth();
    const actionLabel = protectedAction.matches('[data-like]') ? '추천' : protectedAction.matches('#open-inquiry-modal') ? '문의' : protectedAction.matches('#report-spot-button,[data-report-post]') ? '신고' : '등록';
    toast(`로그인 후 ${actionLabel}할 수 있습니다.`);
  }
}, true);
function adminItem(title, detail, buttonText, action, id) {
  return `<article class="admin-item"><b>${escapeHTML(title)}</b><small>${escapeHTML(detail || '')}</small>${action ? `<button type="button" data-admin-action="${action}" data-admin-id="${escapeHTML(id)}">${buttonText}</button>` : ''}</article>`;
}
function adminSpotItem(item) {
  const action = item.is_hidden ? 'restore-spot' : 'hide-spot';
  const label = item.is_hidden ? '숨김 해제' : '숨김 처리';
  const deleteButton = item.is_hidden ? `<button type="button" class="admin-delete-button" data-admin-action="delete-spot" data-admin-id="${escapeHTML(item.id)}">영구 삭제</button>` : '';
  return `<article class="admin-item"><b>${escapeHTML(item.title)}</b><small>${escapeHTML(`${memberLabel(item)} · ${item.address || '주소 미제공'} · ${item.description}`)}</small><button type="button" data-admin-action="${action}" data-admin-id="${escapeHTML(item.id)}">${label}</button>${deleteButton}</article>`;
}
function memberLabel(item, role = '작성자') {
  const name = item.reporter_name || '기존 익명 항목';
  const key = item.reporter_key ? ` · 회원 ${item.reporter_key}` : '';
  return `${role}: ${name}${key}`;
}
function adminReportItem(item) {
  const targetType = item.kind === 'post' ? 'post' : item.kind === 'spot' ? 'spot' : '';
  const targetAction = targetType ? `${item.target_hidden ? 'restore' : 'hide'}-${targetType}` : '';
  const targetLabel = item.target_hidden ? '숨김 해제' : '대상 숨김';
  const status = item.status === 'resolved' ? '처리 완료' : '미처리';
  const deleteButton = item.kind === 'post' && item.target_hidden ? `<button type="button" class="admin-delete-button" data-admin-action="delete-post" data-admin-id="${escapeHTML(item.target_id)}">영구 삭제</button>` : '';
  return `<article class="admin-item"><b>${escapeHTML(item.kind || '신고')} · ${escapeHTML(item.reason || '사유 없음')} · ${status}</b><small>${escapeHTML(memberLabel(item, '신고자'))} · ${escapeHTML(item.message || '')}${item.admin_note ? ` · 메모: ${escapeHTML(item.admin_note)}` : ''}</small>${targetAction && item.target_id ? `<button type="button" data-admin-action="${targetAction}" data-admin-id="${escapeHTML(item.target_id)}">${targetLabel}</button>` : ''}${deleteButton}${item.status !== 'resolved' ? `<button type="button" data-admin-action="resolve-report" data-admin-id="${escapeHTML(item.id)}">처리 완료</button>` : ''}</article>`;
}
async function loadAdminOverview() {
  if (signedInUser?.role !== 'admin') return;
  const response = await fetch('/api/admin/overview');
  const data = await response.json();
  if (!response.ok) return toast(data.error || '운영자 데이터를 불러오지 못했습니다.');
  const pendingReports = (data.reports || []).filter(item => item.status !== 'resolved');
  const pendingInquiries = (data.inquiries || []).filter(item => item.status !== 'resolved');
  const resolvedReports = (data.reports || []).filter(item => item.status === 'resolved');
  const resolvedInquiries = (data.inquiries || []).filter(item => item.status === 'resolved');
  $('#admin-report-count').textContent = pendingReports.length;
  $('#admin-inquiry-count').textContent = pendingInquiries.length;
  $('#admin-spot-count').textContent = (data.spots || []).length;
  $('#admin-reports').innerHTML = `${pendingReports.length ? pendingReports.map(adminReportItem).join('') : '<p class="empty-post">미처리 신고가 없습니다.</p>'}${resolvedReports.length ? `<details class="admin-history"><summary>처리 이력 ${resolvedReports.length}건</summary>${resolvedReports.map(adminReportItem).join('')}</details>` : ''}`;
  $('#admin-inquiries').innerHTML = `${pendingInquiries.length ? pendingInquiries.map(item => adminItem(item.kind || '문의', `${memberLabel(item)} · ${item.message}${item.contact ? ` · ${item.contact}` : ''}`, '처리 완료', 'resolve-inquiry', item.id)).join('') : '<p class="empty-post">미처리 문의가 없습니다.</p>'}${resolvedInquiries.length ? `<details class="admin-history"><summary>처리 이력 ${resolvedInquiries.length}건</summary>${resolvedInquiries.map(item => adminItem(item.kind || '문의', `${memberLabel(item)} · ${item.message}${item.admin_note ? ` · 메모: ${item.admin_note}` : ''}`, '', '', item.id)).join('')}</details>` : ''}`;
  $('#admin-spots').innerHTML = (data.spots || []).length ? data.spots.map(adminSpotItem).join('') : '<p class="empty-post">사용자 공유 포인트가 없습니다.</p>';
  $('#admin-audits').innerHTML = (data.audits || []).length ? data.audits.map(item => `<article class="admin-item"><b>${escapeHTML(item.action || '처리')}</b><small>${escapeHTML(item.target_type || '')} · ${escapeHTML(item.note || '메모 없음')}</small></article>`).join('') : '<p class="empty-post">운영 기록이 없습니다.</p>';
  document.querySelectorAll('[data-admin-action]').forEach(button => button.addEventListener('click', async () => {
    const action = button.dataset.adminAction;
    if (action === 'delete-spot' && !window.confirm('숨김 처리된 포인트와 연결된 게시글을 영구 삭제합니다. 계속할까요?')) return;
    if (action === 'delete-post' && !window.confirm('숨김 처리된 게시글을 영구 삭제합니다. 계속할까요?')) return;
    const note = action === 'resolve-report' || action === 'resolve-inquiry' ? window.prompt('처리 메모를 입력하세요. (선택)', '') : '';
    if (note === null) return;
    const response = await fetch(`/api/admin/${action}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id:button.dataset.adminId, note})});
    if (!response.ok) return toast(await apiError(response, '처리를 완료하지 못했습니다.'));
    toast(action === 'delete-spot' ? '포인트를 영구 삭제했습니다.' : '운영자 처리가 완료되었습니다.');
    if (['hide-post','hide-spot','restore-post','restore-spot','delete-post','delete-spot'].includes(action)) loadHeroRanking();
    if (['hide-spot','restore-spot','delete-spot'].includes(action)) loadCommunitySpots();
    loadAdminOverview();
  }));
}
$('#admin-refresh').addEventListener('click', loadAdminOverview);
refreshSession();
