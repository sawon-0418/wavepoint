const $ = selector => document.querySelector(selector);
const escapeHTML = value => { const node = document.createElement('div'); node.textContent = value ?? ''; return node.innerHTML; };
const empty = text => `<p class="empty-post">${escapeHTML(text)}</p>`;
function item(title, detail, actions = '') { return `<article class="admin-item"><b>${escapeHTML(title)}</b><small>${escapeHTML(detail)}</small>${actions}</article>`; }
async function requestAction(action, id) {
  if ((action === 'delete-spot' || action === 'delete-post') && !window.confirm('숨김 처리된 대상과 연결된 데이터를 영구 삭제합니다. 계속할까요?')) return;
  const note = ['resolve-report', 'resolve-inquiry'].includes(action) ? window.prompt('처리 메모를 입력하세요. (선택)', '') : '';
  if (note === null) return;
  const response = await fetch(`/api/admin/${action}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id, note})});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) return window.alert(result.error || '처리를 완료하지 못했습니다.');
  loadAdminOverview();
}
function bindActions() { document.querySelectorAll('[data-admin-action]').forEach(button => button.addEventListener('click', () => requestAction(button.dataset.adminAction, button.dataset.adminId))); }
async function loadAdminOverview() {
  const status = $('#admin-status'); status.textContent = '운영 정보를 불러오는 중입니다.';
  try {
    const response = await fetch('/api/admin/overview'); const data = await response.json();
    if (!response.ok) throw new Error(data.error || '운영자 권한이 필요합니다.');
    const reports = data.reports || [], inquiries = data.inquiries || [], spots = data.spots || [];
    $('#admin-member-count').textContent = Number(data.memberCount || 0).toLocaleString('ko-KR');
    $('#admin-report-count').textContent = reports.filter(row => row.status === 'pending').length;
    $('#admin-inquiry-count').textContent = inquiries.filter(row => row.status === 'pending').length;
    $('#admin-spot-count').textContent = spots.length;
    $('#admin-reports').innerHTML = reports.length ? reports.map(row => item(`${row.kind || '신고'} · ${row.reason || '사유 없음'} · ${row.status === 'resolved' ? '처리 완료' : '미처리'}`, `${row.reporter_name || '회원'} · ${row.message || ''}`, `${row.target_id && row.status !== 'resolved' ? `<button data-admin-action="${row.target_hidden ? (row.kind === 'post' ? 'restore-post' : 'restore-spot') : (row.kind === 'post' ? 'hide-post' : 'hide-spot')}" data-admin-id="${escapeHTML(row.target_id)}">${row.target_hidden ? '대상 숨김 해제' : '대상 숨김'}</button>` : ''}${row.status !== 'resolved' ? `<button data-admin-action="resolve-report" data-admin-id="${escapeHTML(row.id)}">처리 완료</button>` : ''}`)).join('') : empty('신고가 없습니다.');
    $('#admin-inquiries').innerHTML = inquiries.length ? inquiries.map(row => item(`${row.kind || '문의'} · ${row.status === 'resolved' ? '처리 완료' : '미처리'}`, `${row.reporter_name || '회원'} · ${row.message || ''}`, row.status !== 'resolved' ? `<button data-admin-action="resolve-inquiry" data-admin-id="${escapeHTML(row.id)}">처리 완료</button>` : '')).join('') : empty('문의가 없습니다.');
    $('#admin-spots').innerHTML = spots.length ? spots.map(row => item(row.title, `${row.reporter_name || '회원'} · ${row.address || '주소 미제공'}`, `<button data-admin-action="${row.is_hidden ? 'restore-spot' : 'hide-spot'}" data-admin-id="${escapeHTML(row.id)}">${row.is_hidden ? '숨김 해제' : '숨김 처리'}</button>${row.is_hidden ? `<button data-admin-action="delete-spot" data-admin-id="${escapeHTML(row.id)}">영구 삭제</button>` : ''}`)).join('') : empty('사용자 공유 포인트가 없습니다.');
    $('#admin-audits').innerHTML = (data.audits || []).length ? data.audits.map(row => item(row.action || '처리', `${row.target_type || ''} · ${row.note || '메모 없음'}`)).join('') : empty('운영 기록이 없습니다.');
    status.textContent = '운영자 전용 화면입니다. 공개 사이트와 검색 결과에는 포함되지 않습니다.'; bindActions();
  } catch (error) { status.textContent = error.message || '운영 정보를 불러오지 못했습니다.'; }
}
$('#admin-refresh').addEventListener('click', loadAdminOverview);
loadAdminOverview();
