# Discord 출퇴근 봇

Discord 슬래시 명령으로 출근, 퇴근, 휴식과 날짜별 실작업시간을 기록하는 Railway용 봇입니다.

## 명령어

| 명령어 | 동작 |
| --- | --- |
| `/출근` | 현재 시각으로 근무를 시작합니다. |
| `/휴식` | 근무 중이면 휴식을 시작하고, 휴식 중이면 종료합니다. |
| `/퇴근` | 근무를 종료하고 `퇴근 - 출근 - 총 휴식`으로 실작업시간을 계산합니다. 진행 중인 휴식은 자동 종료합니다. |
| `/기록 [사용자] [날짜]` | 지정 사용자의 날짜별 기록을 조회합니다. 생략하면 본인·오늘입니다. |
| `/잔디 [사용자] [월]` | 지정 사용자의 월간 작업시간을 GitHub식 잔디로 표시합니다. 월 형식은 `YYYY-MM`입니다. |
| `/셋로그 내용` 또는 `/setlog content` | 현재 한 시간 구간에 하고 있는 일을 저장합니다. 같은 시간에 다시 입력하면 수정됩니다. |
| `/today [사용자]` | 오늘 작성한 시간대별 업무 내용과 근무시간을 표시합니다. |
| `/스크럼` | 오늘의 `/today` 기록을 확인하고 상세 일일 업무보고를 작성·검토한 뒤 Discord 스크럼 채널과 Slack에 전송합니다. |

시간대 기본값은 `Asia/Seoul`이며 데이터는 서버별·사용자별로 분리됩니다. 같은 날에는 한 번의 근무 기록만 생성할 수 있습니다. 봇이나 Railway 컨테이너가 재시작되어도 PostgreSQL 기록은 유지됩니다.

`/잔디`의 색은 하루 24시간을 고정 최대치로 사용합니다. `0시간`, `0~6시간`, `6~12시간`, `12~18시간`, `18~24시간`의 5단계이며 24시간을 넘는 비정상 기록은 최고 단계로 고정됩니다.

## 시간별 업무 로그와 고정 채널

`WORK_CHANNEL_ID`에 입력한 하나의 Discord 텍스트 채널에서만 모든 명령어가 작동하며, 매시간 알림도 이 채널에만 전송됩니다. 다른 채널에서 명령어를 실행하면 고정 채널로 이동하라는 안내가 표시됩니다.

봇은 매 정각 현재 출근 상태인 사용자만 멘션합니다. 예를 들어 12:00~12:59 사이 아무 때나 아래처럼 입력하면 12:00~13:00 구간에 저장됩니다.

```text
/셋로그 내용:API 스캐너 예외 처리 구현 중
```

같은 시간에 다시 입력하면 기존 내용이 수정됩니다. `/today`는 작성한 업무 로그를 시간순으로 보여주며 `사용자` 옵션으로 다른 구성원의 기록도 조회할 수 있습니다.

## 일일 스크럼 업무보고

`/스크럼`은 `SCRUM_CHANNEL_ID`로 지정한 별도 스크럼 채널에서만 작동합니다. 실행하면 본인에게만 오늘의 `/today` 시간대별 로그와 근무시간이 표시되고, `일일 업무보고 작성` 버튼이 나타납니다.

작성 모달은 다음 5개 항목으로 구성됩니다.

1. 담당 모듈 / 작업 영역
2. 완료한 일
3. 진행 중인 일
4. 다음에 할 일
5. 어려웠던 점 / 비고사항

모달 제출은 초안 저장일 뿐이며 외부로 전송되지 않습니다. 비공개 최종 미리보기에서 `완성 및 Slack 전송` 버튼을 눌러야 스크럼 채널과 Slack에 동시에 공유됩니다. 같은 날짜의 초안은 다시 열어 수정할 수 있지만, 완성된 보고서는 중복 전송과 사후 변경을 막기 위해 잠깁니다.

외부 공유 전 다음 안전장치가 적용됩니다.

- 작성자만 작성·수정·완성 버튼을 사용할 수 있습니다.
- 전체 멘션, AWS Access Key, Slack·GitHub·Discord 토큰, Slack Webhook, 개인키, 비밀번호·토큰 형태의 값이 감지되면 저장과 전송을 차단합니다.
- Slack 내용은 `plain_text` 블록으로 보내 사용자 입력에 포함된 멘션이나 서식 명령이 실행되지 않습니다.
- 전송 상태를 PostgreSQL에 기록해 버튼 중복 클릭과 재시도 시 이중 게시를 방지합니다.
- Slack 전송에 성공한 뒤 Discord 스크럼 채널에 공개하며, 일부 전송만 성공한 경우 재시도 시 실패한 대상만 처리합니다.

Slack에서는 일일 업무보고를 받을 채널에 Incoming Webhook을 만든 뒤 URL을 Railway의 `SLACK_SCRUM_WEBHOOK_URL`에 비밀 변수로 등록하세요. Webhook URL은 채팅이나 GitHub에 입력하지 않습니다.

## Discord 봇 만들기

1. [Discord Developer Portal](https://discord.com/developers/applications)에서 **New Application**을 누릅니다.
2. **Bot** 메뉴에서 봇을 만든 뒤 토큰을 발급합니다. 토큰은 GitHub에 올리지 않습니다.
3. **OAuth2 → URL Generator**에서 `bot`, `applications.commands` scope를 선택합니다.
4. Bot Permissions는 `Send Messages`, `Embed Links`를 선택해 생성된 URL로 서버에 초대합니다.
5. 테스트 서버에서 명령어를 바로 보이게 하려면 Discord 개발자 모드를 켜고 서버 ID를 복사해 `GUILD_ID`로 사용합니다. `GUILD_ID`를 비우면 전역 명령으로 등록되며 반영에 시간이 걸릴 수 있습니다.

별도의 Message Content Intent나 관리자 권한은 필요하지 않습니다.

## Railway 배포

1. 이 폴더를 새 GitHub 저장소에 push합니다.
2. Railway에서 **New Project → Deploy from GitHub repo**로 저장소를 연결합니다.
3. 같은 프로젝트에서 **Add → Database → PostgreSQL**을 추가합니다.
4. 봇 서비스의 **Variables**에 다음 값을 추가합니다.

   ```text
   DISCORD_TOKEN=<Discord Bot 토큰>
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   TIMEZONE=Asia/Seoul
   WORK_CHANNEL_ID=<근태 및 시간대별 로그 기능을 사용할 Discord 채널 ID>
   SCRUM_CHANNEL_ID=<스크럼 업무보고를 작성·게시할 Discord 채널 ID>
   SLACK_SCRUM_WEBHOOK_URL=<Slack Incoming Webhook URL>
   GUILD_ID=<테스트할 Discord 서버 ID, 선택>
   ```

5. 재배포 후 로그에서 `로그인 완료`와 `명령어 ...개 동기화`를 확인합니다.

이 봇은 HTTP 서버가 없는 상시 실행 worker이므로 Railway에서 public domain이나 health check를 만들 필요가 없습니다. 최초 실행 시 테이블과 인덱스를 자동 생성합니다.

## 로컬 실행

Python 3.12와 PostgreSQL이 필요합니다.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DISCORD_TOKEN='...'
export DATABASE_URL='postgresql://...'
export TIMEZONE='Asia/Seoul'
export WORK_CHANNEL_ID='123456789012345678'
export SCRUM_CHANNEL_ID='234567890123456789'
export SLACK_SCRUM_WEBHOOK_URL='https://hooks.slack.com/services/...'
python app.py
```

## 테스트

```bash
python -m unittest discover -s tests -v
python -m compileall -q app.py database.py scrum_utils.py time_utils.py
```

## 운영상 주의

- 토큰은 `.env` 또는 Railway Variables에만 저장하세요.
- 날짜는 출근 시각의 현지 날짜로 고정됩니다. 자정을 넘어 퇴근해도 출근한 날짜 기록에 합산됩니다.
- 열려 있는 근무가 있으면 다음 날에도 새 출근이 차단되므로 먼저 `/퇴근`해야 합니다.
- 현재 MVP는 사용자 수정·관리자 보정·CSV 내보내기를 포함하지 않습니다.
