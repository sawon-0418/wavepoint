// 지도 분류와 검색에서 같은 행정구역 명칭을 사용한다. 원본 주소는 변경하지 않는다.
const SpotRegions = (() => {
  const groups = {
    '서울특별시': ['서울', '서울시'],
    '부산광역시': ['부산', '부산시'],
    '대구광역시': ['대구', '대구시'],
    '인천광역시': ['인천', '인천시'],
    '광주광역시': ['광주'],
    '대전광역시': ['대전', '대전시'],
    '울산광역시': ['울산', '울산시'],
    '세종특별자치시': ['세종', '세종시'],
    '경기도': ['경기'],
    '강원특별자치도': ['강원', '강원도'],
    '충청북도': ['충북'],
    '충청남도': ['충남'],
    '전북특별자치도': ['전북', '전라북도'],
    '전라남도': ['전남', '전남광주통합특별시'],
    '경상북도': ['경북'],
    '경상남도': ['경남'],
    '제주특별자치도': ['제주', '제주도'],
  };
  const aliases = new Map(Object.entries(groups).flatMap(([name, short]) => [name, ...short].map(alias => [alias, name])));
  function tokens(value) {
    return String(value || '').normalize('NFKC').trim().split(/[\s,()]+/u).filter(word => word && word !== '대한민국');
  }
  function normalize(value) {
    // 주소 중간의 경기도 광주시/제주특별자치도 제주시 등은 시·도로 바꾸지 않는다.
    return tokens(value).map((word, index) => index === 0 ? aliases.get(word) || word : word).join(' ').toLowerCase();
  }
  function location(spot) {
    for (const source of [spot.address, spot.description]) {
      const parts = tokens(source);
      const province = aliases.get(parts[0]);
      if (!province) continue;
      const district = parts[1] && /(?:시|군|구)$/.test(parts[1]) ? parts[1] : '';
      return {province, district: district || (province === '세종특별자치시' ? province : '주변 지역')};
    }
    return {province: '기타 지역', district: '주변 지역'};
  }
  function matches(spot, query) {
    const words = tokens(query).map(word => (aliases.get(word) || word).toLowerCase());
    const fields = [spot.title, spot.address, spot.description, spot.species];
    const haystack = fields.map(normalize).join(' ').replace(/\s+/g, '');
    return words.every(word => haystack.includes(word));
  }
  return {normalize, location, matches};
})();
if (typeof module !== 'undefined' && module.exports) module.exports = SpotRegions;
