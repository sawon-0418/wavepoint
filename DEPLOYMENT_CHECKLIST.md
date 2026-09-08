# 물결포인트 배포 점검표

## 배포 전 환경 변수

- `.env`는 저장소와 배포 로그에 올리지 않는다.
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, 공공데이터·네이버 지도 키를 배포 환경의 비밀 변수로 등록한다.
- 실제 도메인에서 HTTPS가 동작하는지 확인한 뒤 `FORCE_SECURE_COOKIES=true`로 설정한다.
- 배포 플랫폼이 전달하는 `PORT`를 사용하고, 로컬 기본 `HOST=127.0.0.1`은 공개 서버에 그대로 사용하지 않는다.
- Render Web Service에는 `RUN_IN_PROCESS_SYNC=false`를 설정한다. 공식 데이터 갱신은 GitHub Actions가 담당한다.

## Supabase

1. 최신 `supabase_schema.sql`을 SQL Editor에서 실행한다.
2. Authentication의 Site URL과 Redirect URL을 실제 도메인으로 변경한다.
3. 운영 환경에서는 Confirm email을 켜고, 인증 메일 발송용 SMTP와 템플릿을 설정한다.
4. Database Backups/PITR 가능 여부와 복구 절차를 확인한다.
5. Service Role Key는 서버 환경 변수에만 두고 브라우저 코드와 Supabase 공개 테이블에 넣지 않는다.

## 운영 점검

- `/api/sync`, `/api/migrate-to-supabase`, `/api/admin/*`은 운영자 로그인 계정에서만 정상 동작하는지 확인한다.
- 게시글·포인트·신고·문의 등록 제한이 HTTP 429로 동작하는지 확인한다.
- 숨김 처리한 게시글/포인트가 지도·상세·조과 랭킹에서 모두 사라지고, 숨김 해제 후 다시 보이는지 확인한다.
- GitHub Actions의 `Daily official data sync`는 `30 18 * * *`(UTC, 매일 03:30 KST)에 Render의 `/api/cron-sync`를 호출한다. 공공데이터 조회는 Render 서버에서 실행하므로 GitHub Actions 실행 지역의 접속 제한 영향을 받지 않는다.
- 32자 이상 임의의 `CRON_SYNC_TOKEN`을 Render Web Service 환경 변수와 GitHub Actions Secret에 같은 값으로 등록한다. GitHub에는 `WAVEPOINT_SYNC_URL=https://서비스주소/api/cron-sync`도 Secret으로 등록한다.
- 배포 직후 GitHub Actions → `Daily official data sync` → Run workflow로 한 번 수동 실행하고, 로그의 JSON 결과에서 `errors: []`를 확인한다.
- 500/503 오류, 동기화 실패, 스토리지 업로드 실패에 대한 알림을 배포 플랫폼 또는 모니터링 도구에서 설정한다.

## 현재 서버가 적용하는 기본 방어

- HttpOnly, SameSite 로그인·갱신 쿠키와 토큰 자동 갱신
- `X-Frame-Options`, `nosniff`, Referrer Policy, 위치·카메라·마이크 권한 차단 헤더
- 계정별 게시글·포인트·사진·신고·문의 요청 횟수 제한
- 사진 JPG/PNG/WEBP 형식 및 5MB 제한
