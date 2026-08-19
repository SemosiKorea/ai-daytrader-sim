# ngrok 공개 연결 설정

이 구성은 Custom GPT Action에 필요한 후보 조회·계획 등록·상태 조회 API만 ngrok을 통해 공개합니다.
관리자, 대시보드, 포트폴리오, 성과 및 시세 입력 API는 ngrok 정책에서 404로
차단합니다. 공개된 GPT Action API도 `GPT_ACTION_BEARER` 인증을 통과해야 합니다.

## 1. ngrok 계정 연결

ngrok 대시보드의 **Your Authtoken**에서 토큰을 확인한 후 맥 터미널에서 다음
명령을 한 번 실행합니다.

```bash
ngrok config add-authtoken <NGROK_대시보드에서_복사한_토큰>
ngrok config check
```

토큰은 `.env`에 넣지 않으며 GPT 대화나 Git 저장소에도 기록하지 않습니다.
ngrok CLI가 `~/Library/Application Support/ngrok/ngrok.yml`에 별도로 저장합니다.

## 2. 수동 연결 시험

첫 번째 터미널에서 API를 실행합니다.

```bash
uv run daytrader-sim
```

두 번째 터미널에서 경로 제한 정책과 함께 ngrok을 실행합니다.

```bash
ngrok http 8787 \
  --traffic-policy-file deploy/ngrok-traffic-policy.yml
```

ngrok 화면의 `Forwarding`에 표시된
`https://...ngrok-free.dev`와 같이 표시되는 주소가 Custom GPT Action에서 사용할
공개 주소입니다.
무료 계정에 할당된 개발 도메인을 사용하므로 같은 계정으로 실행하면 주소가
유지됩니다.

외부 경로 제한을 확인합니다.

```bash
curl -i https://your-assigned-domain.ngrok-free.dev/healthz
```

응답은 `404`여야 합니다. 다음 GPT Action 경로는 Bearer가 없으므로 `401`이어야
합니다.

```bash
curl -i https://your-assigned-domain.ngrok-free.dev/v1/gpt-actions/plans/example/status
```

## 3. macOS 자동 시작

계정 토큰 등록과 수동 시험이 끝나면 설치 스크립트를 실행합니다.

```bash
./scripts/install_ngrok_launch_agent.sh
```

이 스크립트는 현재 ngrok·프로젝트·계정 설정 경로를 반영한 LaunchAgent를
`~/Library/LaunchAgents/com.example.ai-daytrader-ngrok.plist`에 설치합니다.
로그인 시 자동 시작되고 비정상 종료 또는 네트워크 복구 후 재시작됩니다.

상태와 로그 확인:

```bash
launchctl print gui/$UID/com.example.ai-daytrader-ngrok
tail -f /tmp/ai-daytrader-ngrok.out.log
```

중지 및 제거:

```bash
launchctl bootout gui/$UID/com.example.ai-daytrader-ngrok
rm ~/Library/LaunchAgents/com.example.ai-daytrader-ngrok.plist
```

LaunchAgent이므로 맥 재부팅 후 사용자가 로그인해야 실행됩니다. 맥의 자동 잠자기
및 네트워크 절전도 별도로 해제해야 합니다.

## 4. Custom GPT Action 변경

`gpt_action_openapi.yaml`의 서버 주소를 ngrok 화면에 표시된 주소로 바꿉니다.

```yaml
servers:
  - url: https://your-assigned-domain.ngrok-free.dev
```

Custom GPT의 Action 인증 값에는 `.env`의 `GPT_ACTION_BEARER`와 똑같은 값을
입력합니다. ngrok Authtoken과 `GPT_ACTION_BEARER`는 서로 다른 비밀값입니다.

실시간 KIS 시세는 ngrok을 통과하지 않습니다. 로컬 `daytrader-feed`가
`127.0.0.1:8787`로 직접 전송하므로 무료 터널 요청량과 외부 공격 표면을 줄입니다.

현재 프로그램의 실주문 차단은 그대로 유지됩니다. ngrok은 승인 계획을 전달하는
통로만 제공하며 `KIS_ORDER_MODE=record_only` 또는 소스코드의 실주문 차단을
변경하지 않습니다.
