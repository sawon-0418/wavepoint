const {test} = require('node:test');
const assert = require('node:assert/strict');
const regions = require('./spot-regions.js');

test('국가명과 제주 축약어, 주소만 가진 포인트를 검색한다', () => {
  const spot = {title:'하예동 포인트', address:'제주특별자치도 서귀포시 예래해안로 100', description:'바다 포인트'};
  for (const query of ['대한민국 제주특별자치도 서귀포시 예래해안로', '제주도 서귀포시 예래해안로', '제주 예래해안로', '예래해안로 제주', '서귀포시   예래해안로']) {
    assert.equal(regions.matches(spot, query), true, query);
  }
  assert.equal(regions.matches(spot, '제주특별자치도 제주시 예래해안로'), false);
  assert.equal(regions.matches(spot, '부산 예래해안로'), false);
});
test('약칭·옛 명칭과 정식 명칭을 같은 지역으로 분류한다', () => {
  for (const [alias, province, district] of [
    ['충남','충청남도','아산시'], ['경남','경상남도','창녕군'],
    ['대전','대전광역시','중구'], ['강원도','강원특별자치도','강릉시'],
    ['전라북도','전북특별자치도','전주시'], ['제주도','제주특별자치도','서귀포시']
  ]) {
    const spot = {address:`대한민국 ${alias} ${district} 도로 1`};
    assert.deepEqual(regions.location(spot), {province, district});
    assert.ok(regions.matches(spot, `${province} ${district}`));
    assert.ok(regions.matches({address:`${province} ${district}`}, alias));
  }
  assert.equal(regions.location({address:'세종시 금남면 도로 1'}).province, '세종특별자치시');
});
test('경기도 광주시를 광주광역시로 바꾸지 않는다', () => {
  const spot = {address:'경기도 광주시 도척면'};
  assert.deepEqual(regions.location(spot), {province:'경기도', district:'광주시'});
  assert.ok(regions.matches(spot, '경기 광주시'));
  assert.ok(regions.matches(spot, '광주시'));
  assert.equal(regions.matches(spot, '광주광역시'), false);
});
test('주소가 없으면 설명을 확인하며 제목만으로 지역을 추측하지 않는다', () => {
  assert.equal(regions.location({description:'충남 아산시 도로 1'}).province, '충청남도');
  assert.equal(regions.location({title:'청평 낚시터'}).province, '기타 지역');
  assert.equal(regions.matches({title:'청평 낚시터',species:'배스'}, '청평 배스'), true);
  assert.equal(regions.matches({}, ''), true);
});
