# vrchat-livetranslate · 가이드

> [中文](GUIDE.md) | [English](GUIDE.en.md) | [日本語](GUIDE.ja.md) | **한국어** | [Русский](GUIDE.ru.md)

[← README로 돌아가기](README.ko.md)

---

## 0. 가장 빠른 시작: 완성된 exe 다운로드

**[Releases](https://github.com/nixi-agent/vrchat-livetranslate/releases/latest)** 에서
**`VRChatLiveTranslate.exe`** 를 받아(**단일 파일, 설치 불필요, 콘솔 창 없음**) 두 번 클릭하면 됩니다.

- **Alibaba Cloud Bailian(百炼) API key** 는 직접 준비하셔야 합니다. "⚙ 설정"에서 붙여넣고 저장하세요
- 설정과 로그는 `%APPDATA%\vrchat-livetranslate` 에 저장됩니다(읽기 전용 폴더에 exe를 둬도 동작합니다)
- 포터블로 쓰고 싶다면(설정이 exe 옆에 저장됨) exe 옆에 **빈 `portable.txt`** 를 두세요
- 예전 버전이 exe 옆에 남긴 `config.yaml` / `logs/` 는 첫 실행 때 **자동으로 새 위치로 이전**됩니다. 원본 파일은 삭제되지 않습니다

### 자동 업데이트되나요?

**자동으로 확인은 하지만 스스로 설치하지는 않습니다** — 업데이트 여부와 시점은 항상 사용자가 정합니다:

- 실행할 때마다 백그라운드에서 한 번 조용히 확인합니다. 확인에 실패해도(네트워크 없음 등) 방해하지 않고 로그만 남깁니다
- 새 버전을 찾으면 세 가지 선택지가 있는 창이 뜹니다:
  - **지금 업데이트** → 자동으로 내려받습니다(1~2분 정도). 끝나면 "지금 다시 시작하고 업데이트"를 눌러 바로 바꾸거나,
    "나중에 업데이트"를 누르면 프로그램을 닫을 때 교체되고 다음 실행부터 새 버전이 됩니다
  - **다음에** → 이번 실행에서는 더 이상 알리지 않고, 다음 실행 때 다시 확인합니다
  - **이 버전은 알리지 않기** → 해당 버전만 무시하고, 다음 버전은 다시 알려 줍니다
- 다운로드 실패 시 다시 시도할 수 있고, 창 안의 "다운로드 페이지 열기"로 Releases에서 직접 받아도 됩니다
- 팝업이 없을 때 확인하고 싶다면 → "⚙ 설정 → 소프트웨어 업데이트 → 업데이트 확인"
- 업데이트로 **설정과 API key가 사라지지 않습니다**. 업데이트 후 다음 실행 때 알려 드립니다

**소스에서 실행 중이라면**: 위 내용은 해당되지 않습니다. 저장소에서 `git pull` 하세요
(소스 실행에서 "지금 업데이트"를 눌러도 같은 안내가 나옵니다).

소스를 읽거나 직접 빌드하거나 고치고 싶다면 → 아래 "1." 부터 보세요.

---

## 1. 사전 준비

1. **Windows 10 / 11** (WASAPI와 SteamVR을 사용합니다)
2. **Python 3.11** (3.12는 미검증. 설치 시 *Add python.exe to PATH* 를 체크하세요) — *exe만 쓴다면 필요 없습니다*
   <https://www.python.org/downloads/release/python-3119/>
3. **VRChat**: 설정에서 OSC를 켜고(`OSC enabled: True`) 채팅 말풍선 표시를 **Everyone** 으로 설정
4. **SteamVR** — "상대가 말함 → 손목 오버레이" 기능에만 필요합니다
5. **Alibaba Cloud Bailian(百炼) API key** (개인 실명 인증이면 충분합니다)

## 2. 설치 (소스)

**`setup.bat`** 을 두 번 클릭하세요 (`.venv` 생성 + 의존성 설치, 1~2분).

직접 하려면:

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 3. API key 설정

> 🔑 Bailian 계정이 없다면 → **[Alibaba Cloud Bailian 가입 ▸](https://www.aliyun.com/minisite/goods?userCode=q8nma978)**

아래 순서로 찾아보고 **처음 발견된 것이 사용됩니다**:

| 우선순위 | 출처 | 설정 방법 |
|---|---|---|
| 1 | GUI에서 저장한 key | "⚙ 설정 → API key"에서 붙여넣고 저장 (`%USERPROFILE%\.vrchat-livetranslate\api_key.txt` 에 저장) |
| 2 | 환경 변수 | `setx DASHSCOPE_API_KEY "sk-your-key"` (**새로 연 터미널에서만 적용**) |
| 3 | Bailian CLI 설정 | `bl auth login --api-key "sk-your-key"` (`npm install -g bailian-cli` 필요) — `%USERPROFILE%\.bailian\config.json` 을 읽습니다 |

**key를 읽는 곳은 이 세 곳뿐이며, 프로젝트 파일이나 `config.yaml` 에는 절대 기록하지 않습니다.**

> 🔒 **커밋 안전장치**: 저장소에는 자격 증명 스캔이 포함되어 있습니다. `install_secret_guard.bat` 을
> 한 번 실행해 pre-commit 훅을 설치하면 `sk-...` / `gho_...` / `AKIA...` / `Bearer ...` 같은
> 하드코딩된 비밀이 담긴 커밋은 그 자리에서 차단됩니다(스크립트: `scripts/check_no_secrets.py`).
> 수동 전체 검사: `python scripts/check_no_secrets.py --once`
> 이유: key가 한 번 git 히스토리에 들어가면 파일을 지워도 남습니다. 히스토리를 다시 쓰기 전에 막아야 합니다.

## 4. 자가 점검

**exe 사용자**: 실행해서 세 가지를 확인하세요.

1. "⚙ 설정"에서 세 개의 장치 드롭다운이 비어 있지 않은지 (`마이크` / `VRChat 오디오` / `번역 음성 출력`)
2. API key 상태가 **설정됨** 으로 표시되는지 (아니라면 클릭 시 가입 페이지가 열립니다)
3. `chatbox` 를 체크하고 "번역 시작"을 누른 뒤 마이크에 한 문장 말하기 → 말풍선에 번역문이 나오는지

**소스 실행**: **`run_selfcheck.bat`** 을 두 번 클릭하세요. 다음 네 가지를 순서대로 실행하며, 앞의 세 가지는 마이크도 VRChat도 필요 없습니다.

1. 모듈 임포트 검사 (engine / session / overlay / chatbox / merger)
2. API key를 한 번 읽어 마스킹해서 출력 (우선순위 확인)
3. 손목 오버레이 한 프레임을 오프라인 렌더링 (SteamVR을 점유하지 않습니다)
4. 내장 테스트 오디오를 전체 파이프라인에 통과 (중국어 → 영어, chatbox로)

VRChat이 실행 중이라면 말풍선에 이렇게 표시되어야 합니다:

```
Hello, I'm Nixi. Today, we're going to test out the real-time simultaneous interpretation feature in VRChat.
```

**버그를 제보하기 전에 반드시 이 자가 점검을 먼저 돌려 주세요.**

---

## 5. 사용법

### GUI (권장)

**exe를 두 번 클릭**하거나, 소스 설치라면 **`run_gui.bat`** 을 두 번 클릭하세요 (둘 다 같은 창입니다).
(Tkinter만 사용 · 표준 라이브러리만 사용 · 추가 의존성 없음 · 즉시 실행)

| 영역 | 내용 |
|---|---|
| 첫 줄 | `번역 시작` / `번역 중지`, 방향 라디오(`나` / `상대` / `양방향`), 언어 쌍 드롭다운(원문 → 번역문), 오른쪽에 `☕ 후원` 과 `⚙ 설정` |
| 둘째 줄 | `출력:` 아래 `chatbox` / `손목 오버레이` / `번역 음성 출력` 체크박스와 `미세 조정 ▸` 버튼. 맨 오른쪽에 API key 상태(설정되면 `API key 설정됨`, **아니면 클릭 가능한 "⚠ API key 미설정 ・ Bailian 등록은 여기 ▸"**) |
| 채팅 영역 | 오른쪽 파란 말풍선 = 내가 한 말, 왼쪽 회색 = 상대가 한 말. 말풍선 하나에 두 줄 — **위에 원문(작게), 아래에 번역문(크게)**. 기록 스크롤 가능(최대 500개) |
| 상태 표시줄 | 왼쪽: 색 점 + 최신 상태 메시지 / 오른쪽: 통계(`실행 중` / `{n}건 번역` / `첫 증분 {ms}ms`). 둘은 겹치지 않습니다 |
| 하단 줄 | **텍스트 입력**: `텍스트 입력:` 칸과 `보내기`. **Enter로 전송** (`Esc` 로 지우기). 방향에 "나"가 포함될 때만 활성화되고, 아니면 회색으로 비활성화됩니다 |
| `☕ 후원` 팝업 | Ko-fi 링크 버튼 + WeChat / Alipay QR 코드(240px로 비례 축소, 바로 스캔 가능) |

- **스트리밍 표시**: 확정되지 않은 증분은 말풍선을 새로 추가하지 않고 **같은 말풍선을 그 자리에서 다시 그립니다**
- **양방향**: 한 창에서 두 방향을 번역합니다(마이크 → 오른쪽, 게임 오디오 → 왼쪽). 두 번째는 **300ms 늦게 시작**해 오디오 장치를 두고 다투지 않습니다. 한쪽이 실패해도 다른 쪽은 계속 동작합니다
- **언어 미러링**: 하나의 쌍으로 양방향을 모두 커버합니다. "중국어 → 영어"를 고르면 상대 방향은 자동으로 "영어 → 중국어"가 됩니다.
  원문 언어는 `자동 감지` / 중국어 / 영어 / 일본어 / 한국어 / 프랑스어 / 독일어 / 스페인어 / 러시아어 중에서 고를 수 있습니다(번역문 쪽은 같은 표에서 "자동 감지"만 뺀 것입니다).
  원문에서 `자동 감지` 를 고르면 상대 방향의 번역문은 중국어로 폴백되고 상태 표시줄에 그 사실이 표시됩니다. 변경은 **즉시 적용**되고 설정 파일에 기록되어 다음 실행에도 유지됩니다
- **설정 창**(`⚙ 설정`): API key 입력 / 삭제, 오디오 장치 드롭다운 3개와 `다시 검색`, 로그 영역(`로그를 ZIP으로 내보내기…`)
- **미세 조정 패널**(`미세 조정 ▸`): 손목 오버레이 앵커와 슬라이더 12개. **끌면 바로 반영되고 재시작이 필요 없습니다** ("6. 설정" 참고)
- **텍스트 입력**: 말하기 싫을 때 키보드로 대신합니다. 하단 줄에 입력하고 **Enter로 전송**. 번역문은 음성 경로와 **완전히 같은** 하류(채팅 말풍선 / 손목 오버레이 / chatbox)를 지납니다. "번역 음성 출력"이 켜져 있으면 **읽어 주기도 하며**, 번역문을 TTS로 합성해 가상 사운드카드에 넣으므로 상대방도 듣습니다("6. 설정"의 `text_input.tts` 참고). 이것은 **마이크를 대체**하므로 방향에 "나"가 포함될 때만 쓸 수 있습니다(아니면 입력칸이 회색으로 비활성화됩니다)

#### 자동 검증 (헤드리스, 창을 열지 않음)

```bat
:: 소스 설치
.venv\Scripts\python.exe -m vlt.gui --self-test
.venv\Scripts\python.exe -m vlt.gui --self-test-dual

:: exe (콘솔 창 없음; 결과는 로그로 — 빌드 스크립트의 자체 점검도 같은 방식입니다)
VRChatLiveTranslate.exe --self-test
```

성공하면 `GUI_SELFTEST_OK` / `GUI_SELFTEST_DUAL_OK mine=N theirs=N` 을 출력하고, 실패하면 `..._FAIL: ...` 을 stderr로 내보내고 0이 아닌 코드로 종료합니다. 실제 API key가 필요합니다.

> 🪵 **크래시 로그**: GUI는 실행할 때마다 `logs/` 아래에 `gui_<타임스탬프>.log` 를 남깁니다.
> 여기에는 **버전 정보**(git HEAD), Python 환경, 그리고 모든 stdout/stderr(**모든 줄에 `HH:MM:SS.mmm` 타임스탬프**)가 들어갑니다.
> 네이티브 크래시(`faulthandler`), 메인 스레드 예외, 자식 스레드 예외, Tk 콜백 예외 — 네 가지를 모두 잡습니다.
> 크래시가 나면 **이 파일을 관리자에게 보내는 것만으로 원인을 짚을 수 있습니다** — 창이 닫히면 아무것도 남지 않습니다.
> 파일 하나는 2MB에서 로테이션되고, 전체 로그가 5MB를 넘으면 오래된 것부터 삭제됩니다. "설정"에서 전체를 **개인정보 처리된** ZIP 하나로 내보낼 수 있습니다.

### 명령줄 (소스 설치 전용)

exe는 콘솔 창이 없는 단일 파일 GUI라 위의 `--self-test` 외에 명령줄 용도가 없습니다. CLI는 소스 설치에서만 의미가 있습니다.

```bat
:: 내가 말함 → chatbox (실제로 VRChat에 전송)
.venv\Scripts\python.exe -m vlt.app --direction mine --mic --sink chatbox

:: 상대가 말함 → 손목 오버레이 (SteamVR이 실행 중이어야 함)
.venv\Scripts\python.exe -m vlt.app --direction theirs --loopback --sink overlay

:: VRChat 없이 경로 검증: 내장 테스트 오디오를 통과시킴
.venv\Scripts\python.exe -m vlt.app --direction mine --pcm testdata\zh_test_16k.pcm --dry-run
```

두 방향을 동시에 번역하려면 → **명령줄 창을 두 개 엽니다**(또는 GUI의 "양방향"을 쓰면 한 창에서 두 세션이 돌아갑니다).
방향마다 별도의 WebSocket 세션을 쓰므로 서로 간섭하지 않습니다(레이트 리밋 RPM 10, 충분합니다).

명령이 길어 번거롭다면 준비된 bat을 쓰세요: **`run_chatbox.bat`**(내가 말함 → 말풍선, 먼저 장치 목록을 보여주고 시작)와
**`run_overlay.bat`**(상대가 말함 → 손목 오버레이).

#### 모든 파라미터

| 파라미터 | 효과 |
|---|---|
| `--direction mine/theirs` | `config.yaml` 의 어느 방향을 쓸지 |
| `--mic` / `--loopback` / `--pcm <file>` | 오디오 소스: 마이크 / VRChat 재생 출력 / 오프라인 PCM (하나만; 우선순위 `--pcm` > `--mic` > `--loopback`) |
| `--sink chatbox/overlay/both` | 출력 대상(기본 `chatbox`) |
| `--list-devices` | 오디오 입출력 장치를 나열하고 종료 |
| `--dry-run` | 실제로 OSC를 보내지 않고 출력과 패킷 덤프만 |
| `--overlay-dry-run` | 오버레이가 SteamVR을 점유하지 않고 각 프레임을 `out/overlay_frames/` 에 PNG로 저장 |
| `--audio-out` | 번역 음성 출력 켜기 (`config.yaml` 의 `output.audio.enabled` 를 덮어씀) |
| `--audio-device <name...>` | 가상 사운드카드 이름 폴백 순서 (`config.yaml` 의 기본값을 덮어씀) |
| `--no-realtime` | PCM을 최대한 빠르게 공급(실시간 제한 없음, `--pcm` 과 함께일 때만 의미 있음) |
| `--settle-s <seconds>` | 오디오 종료 후 응답을 기다리는 시간(기본 8) |
| `--config <path>` | 다른 설정 파일 사용 |

> 마이크 입력은 **계속 동작합니다**: `Ctrl+C` 까지 멈추지 않습니다(길이 지정 파라미터는 없습니다).
>
> 📌 **오디오 장치 지정에 플래그는 필요 없습니다**: GUI의 "⚙ 설정"에서 고르거나(드롭다운 첫 항목 "자동 검출"이
> 폴백 순서로 탐색합니다), `config.yaml` 의 `capture.mic_device` / `capture.loopback_device` 를 직접 고치세요.
> 저장되는 것은 **장치 이름 문자열**이며 인덱스가 아닙니다 — 인덱스는 핫플러그나 세션 전환으로 통째로 밀립니다.

---

## 6. 설정 (`config.yaml`)

> 📄 **첫 실행 때 `config.example.yaml` 에서 `config.yaml` 이 자동 생성됩니다.**
> `config.yaml` 은 **사용자 본인의 설정**(장치 선택, 언어 취향 등)이며 gitignore되어
> **버전 관리에 들어가지 않습니다**. 장치 이름 같은 것은 PC마다 달라 서로 오염시키기만 하기 때문입니다.
> 기본값으로 되돌리려면 파일을 지우고 다시 실행하세요.
>
> 위치:
> - **exe**: `%APPDATA%\vrchat-livetranslate\config.yaml` (`portable.txt` 가 있는 포터블 구성은 exe 옆)
> - **소스 설치**: 저장소 루트의 `config.yaml`
>
> 🛟 **설정이 깨져도 실행은 막히지 않습니다**: YAML 파싱에 실패하면 그 파일은 자동으로
> `config.yaml.broken` 으로 백업되고, 템플릿에서 새 설정이 생성되며, 이유가 출력된 뒤 실행이 계속됩니다.

GUI에서 바꾼 것(방향, 출력 체크, 언어, 장치, 손목 오버레이 미세 조정)은 이 파일에 **그 자리에서 다시 기록됩니다** —
바뀌는 것은 그 한 줄뿐이고, **주석·빈 줄·키 순서는 모두 그대로 유지됩니다**(생성된 YAML은 쓰기 전에 검증되며, 잘못된 출력은 거부됩니다).

### 주요 키

```yaml
session:
  model: qwen3.8-livetranslate-flash-realtime   # qwen3.5-* 로 바꿀 수도 있음(이벤트 이름은 세대별로 자동 분기)
  base_url: wss://dashscope.aliyuncs.com/api-ws/v1/realtime
  voice: Tina                 # ⚠️ 반드시 명시. 비우면 Voice 'Chelsie' is not supported 오류
  turn_detection: null        # 비움 = 서버 기본값
  final_silence_s: 3.0        # ⚠️ 서버 증분 간격(실측 최대 2.3s)보다 커야 합니다. 줄이면 문장 중간에 최종본이 튀어나옵니다
  max_new_sessions_per_minute: 4   # RPM 10 예산: WS 연결 1회 = 요청 1회로 계산
  reconnect_backoff: [2, 5, 10, 30]

capture:                      # 빈 문자열 = 자동 검출
  mic_device: ""
  loopback_device: ""

directions:
  mine:   { source_lang: zh,   target_lang: en, output_audio: false }   # 나 → 말풍선
  theirs: { source_lang: null, target_lang: zh, output_audio: false }   # 상대 → 손목 오버레이 (null = 자동 인식)

chatbox:
  interval_s: 2.0             # 증분 갱신 주기(리키 버킷 5건/5초 → 2초에 1건이면 여유)
  max_chars: 144              # 공식 상한(바이트가 아니라 문자 수)

text_input:                   # 텍스트 입력(하단 입력칸, Enter로 전송)
  enabled: true               # false = 입력 줄을 표시하지 않음
  model: qwen-mt-flash        # 타이핑 번역은 **텍스트** 모델. qwen-mt-plus 도 가능
                              # ⚠️ qwen3-livetranslate-flash 는 쓰지 마세요: 실측상 텍스트 입력에 원문을 그대로 돌려줍니다
  timeout_s: 20               # 번역 1회 타임아웃(초)
  tts:                        # 타이핑도 소리 내기(번역문을 TTS로 합성 → 가상 사운드카드 → 상대가 들음)
    enabled: true             # "번역 음성 출력"이 켜져 있고 방향에 "나"가 포함되어야 합니다. 아니면 이 단계는 자동으로 건너뛰고 텍스트만 나갑니다
    model: qwen3-tts-flash    # qwen3-tts-instruct-flash 도 가능
    voice: Cherry             # 다국어 음색(중/영/일 모두 읽는 것 실측)
    timeout_s: 30

merger:
  interval_s: 2.0             # 첫 증분은 즉시, 이후 2초마다 스냅샷, 문장 끝에는 반드시 최종본
  carry_over: false           # true = 직전 최종 번역을 접두사로 유지(연속 자막 느낌)

overlay:
  interval_s: 0               # 0 = 갱신이 있으면 즉시 표시(로컬 표시는 chatbox 레이트 리밋과 무관)
  anchor: right_hand          # left_hand | right_hand | tracker | hmd
  size_px: [1024, 320]
  font_size: 42               # 번역문 글자 크기
  source_font_size: 30        # 원문 글자 크기
  show_source: true           # 두 줄 표시(3.8은 원문 인식 결과도 기본 반환이라 추가 비용 없음)

output:
  audio:
    enabled: false            # 번역 음성 총 스위치: directions.<X>.output_audio 와 AND 관계
    device_name: ""           # 직접 지정한 가상 사운드카드 이름(비어 있지 않으면 폴백 순서보다 우선)
```

### 손목 오버레이 미세 조정 패널(슬라이더 12개, 끄는 즉시 반영)

| 슬라이더 | 범위 / 단위 | 슬라이더 | 범위 / 단위 |
|---|---|---|---|
| 위치 X / Y / Z | −0.30 ~ 0.30 m, 0.005 | 크기 | 0.05 ~ 0.80 m, 0.01 |
| 피치 / 요 / 롤 | −90 ~ 90°, 1 | 곡률 | 0.0 ~ 0.50, 0.01 |
| 불투명도 | 0.10 ~ 1.00, 0.05 | 번역문 글자 크기 | 20 ~ 64, 1 |
| 원문 글자 크기 | 14 ~ 48, 1 | 패널 높이 | 240 ~ 560 px, 10 |

여기에 **앵커** 드롭다운(오른손 / 왼손 / 전완 트래커 / HMD 앞 고정)과 **트래커 번호**(0~3)가 있습니다.
변경은 200ms 디바운스로 디스크에 기록되며, **번역문 글자 크기 / 원문 글자 크기 / 패널 높이**는
텍스처를 한 프레임 다시 그리고, 나머지는 변환만 다시 적용합니다.

> 실측 기준: 글자 크기 42/30 + 패널 320px이면 **대화 2라운드**, 36/24 + 420px이면 **3라운드, 6줄**.

---

## 7. 문제 해결

| 증상 | 원인 / 해결 |
|---|---|
| 말풍선에 아무것도 안 나옴 | VRChat 미실행 / OSC 꺼짐 / 말풍선 표시가 Off. `--dry-run` 에서 전송 로그가 보이면 프로그램 쪽은 정상입니다 |
| `[loopback] ❌ no loopback device found` | VRChat이 소리를 재생하지 않거나, **원격 데스크톱 세션에서 실행 중**입니다(WASAPI 엔드포인트는 세션마다 분리되므로 물리 머신의 현재 세션에서 실행해야 합니다) |
| 마이크가 아무것도 못 잡음 | 위와 같습니다. 먼저 "⚙ 설정" 드롭다운에서 선택된 장치를 확인하세요(원격 세션에서 열거가 비는 것은 정상이라고 상태 표시줄에 안내됩니다) |
| `Voice 'Chelsie' is not supported` | `session.voice` 가 비어 있습니다. `Tina` 로 두세요 |
| `Invalid translation parameter` | `session.update` 에 `translation` 필드가 없습니다(코드에서 보장됨. 수정할 때 주의) |
| `1007 Requests rate limit exceeded` | RPM 10에 도달했습니다. 1분 기다리세요. 자주 재시작하지 마세요(**WS 연결 1회마다 요청 1회**로 계산됩니다) |
| 번역이 문장 중간에서 멈춤 | 무음 폴백 임계값이 낮아졌습니다. `session.final_silence_s` 는 2.3s보다 커야 합니다(기본 3.0) |
| `[overlay] ⚠️ SteamVR not running or unavailable` | 정상적인 성능 저하입니다. 손목 오버레이만 안 나오고 chatbox에는 영향이 없습니다 |
| 손목 오버레이가 안 보임 | 먼저 SteamVR이 실행 중인지 확인하고, "미세 조정"에서 위치/크기를 조절하세요. `--overlay-dry-run` 으로 PNG가 나오면 렌더링 자체는 정상입니다 |
| 잠시 뒤 손목 오버레이가 사라짐 | 2단계 자가 치유가 들어 있습니다(3회 연속 실패 → 오버레이 재구성 → 다시 3회 → openvr 연결 하드 재시작 → 이후 50회에 1회만 재시도). 로그에 30초마다 `[overlay][diag] heartbeat: …` 줄이 있어 "마지막 성공 이후 경과 시간 / 재구성 횟수"를 볼 수 있습니다 |
| 번역 음성이 안 나옴 | ① 두 스위치가 모두 켜져 있는지(출력 체크박스 + 방향별 설정) ② "나" 방향에서만 동작합니다 ③ 가상 사운드카드가 설치되어 있는지. 이유는 상태 표시줄/로그에 그대로 나옵니다 ④ **다른 기능에는 영향이 없습니다** |
| GUI에서 "번역 시작"을 눌러도 반응이 없음 | 먼저 터미널 출력을 보세요. asyncio 콜백의 예외는 한 줄 트레이스백이 콘솔에만 나오고 상태 표시줄에는 안 나옵니다(exe라면 `logs/` 의 최신 `.log`) |
| 이해가 안 되는 오류 | **"⚙ 설정 → 로그를 ZIP으로 내보내기"** — 자동으로 개인정보가 처리됩니다. ZIP을 관리자에게 보내 주세요 |

---

## 8. 프로젝트 구조

```
vlt/
├── app.py                CLI 진입점(오디오 소스 → 세션 → 스로틀 → 출력)
├── engine.py             프로그래밍 가능한 엔진: 세션 + 워치독 재연결 + 스로틀 + chatbox/overlay/번역 음성 시작·정지
├── gui.py                Tkinter GUI(--self-test 헤드리스 검증 포함)
├── config.py             설정 로드; 자격 증명 해석 순서; 깨진 설정 자가 복구
├── textin.py             텍스트 입력: 텍스트 번역(실시간 모델이 텍스트 입력을 받지 않아 compatible-mode 경유)
├── tts.py                타이핑 소리내기: 번역문을 qwen3-tts로 합성해 가상 사운드카드 쪽으로 전달
├── credentials.py        API key 저장 / 삭제 / 마스킹
├── devices.py            오디오 장치 열거와 "이름 → 인덱스" 해석
├── paths.py              쓰기 가능 디렉터리 결정(소스 / exe / 포터블) + 예전 파일 이전
├── crashlog.py           크래시 포착 + 로그 로테이션·정리 + 개인정보 처리 내보내기
├── session/
│   ├── base.py           TextDelta / SessionConfig / create_session(모델 세대별 분기)
│   └── qwen38.py         3.8과 3.5 이벤트 분기 + 연결 예산 + 무음 폴백 + 이벤트 계측
└── output/
    ├── merger.py         첫 증분 즉시 → 2초 스냅샷 → 문장 끝 flush
    ├── chatbox.py        OSC ,sTT + 토큰 버킷 + 최종본 재전송 큐
    ├── overlay.py        SteamVR 손목 오버레이: 렌더링 + 앵커 + 핫리로드 + 2단계 자가 치유
    └── virtualmic.py     번역 음성 재투입: 24k→48k 리샘플링 + 지터 버퍼(문장 단위 폐기, 문장은 절대 쪼개지 않음)

scripts/                  탐침·디버그 도구(probe_* / osc_listen / verify_release)
tests/                    23개 파일, 148개 테스트 함수(오프라인 실행 가능, CI는 파일 단위 실행)
docs/                     P0.5 / P1 / P2 실측 결과(프로토콜, 지연, 손목 오버레이)
testdata/                 내장 테스트 오디오(중국어 8.56초, 영어 7.92초, 16kHz 모노 PCM)
assets/                   아이콘, 화면 캡처, 후원 QR 코드
```

---

## 9. 개발

### 테스트 실행

**pytest를 쓰지 않습니다**(의존성에 없습니다). 각 테스트 파일은 독립 실행 스크립트이며 CI가 돌리는 것과 동일합니다:

```bat
:: 전체(파일 단위, CI와 동일; cmd에 그대로 붙여넣기 — .bat 파일 안에서는 %t 를 %%t 로)
for %t in (tests\test_*.py) do @.venv\Scripts\python.exe %t

:: 단일 파일
.venv\Scripts\python.exe tests\test_virtualmic.py
```

테스트는 **전부 오프라인으로 동작합니다**(마이크 / VRChat / SteamVR / 네트워크 불필요). 장치 해석, 경로 결정,
재연결, 로그 로테이션과 개인정보 처리, 후원 팝업, 손목 오버레이 자가 치유,
번역 음성 버퍼 불변식, 주석을 보존하는 설정 재기록 등을 다룹니다.

유일한 예외는 `tests/test_engine.py` 로, 실제 세션을 하나 열기 때문에 **그 PC에 설정된 API key가 필요**합니다.
CI에서는 명시적으로 건너뜁니다(워크플로가 이유를 `::notice::` 로 출력합니다 — 조용히 건너뛰지 않습니다).

### 패키징

```bat
build_exe.bat                                              :: 빌드 + 이후 자체 점검
.venv\Scripts\python.exe scripts\build_exe.py --no-verify  :: 빌드만
```

출력은 `dist\VRChatLiveTranslate.exe`: PyInstaller **단일 파일, 콘솔 창 없음**,
`config.example.yaml` / `testdata` / `assets` 를 포함하고 약 37MB입니다.
기본값으로 빌드 뒤 `exe --self-test` 를 실제로 한 번 실행하고, 로그에서 `GUI_SELFTEST_OK` 를 찾아야 통과로 봅니다.

### CI / 릴리스

- **CI**(`.github/workflows/ci.yml`, main 푸시 / PR / 수동):
  구문 검사 → 자격 증명 스캔 → 오프라인 테스트 전체 파일 단위 실행 → 이어서 별도의 **패키징 파이프라인** 검사(산출물 존재 및 20MB 이상)
- **릴리스**(`.github/workflows/release.yml`, `v*` 태그 푸시로 시작):
  먼저 태그와 `vlt/__init__.py` 의 `__version__` 을 대조(불일치면 즉시 실패) → 패키징 →
  **exe** 와 `SHA256SUMS.txt` 를 첨부한 Release 생성(체크섬은 GitHub가 첨부 파일 옆에 `sha256:…` 로 표시합니다. 이 파일은 v0.2.0 이하 클라이언트를 위한 과도기 조치입니다)
- **받은 파일을 직접 검증하고 싶다면**: `scripts/verify_release.py` 가 Release 산출물을 받아 대조합니다
  (SHA256, `--self-test` 실제 실행, 버전 줄, 신규 기능 문자열을 바이트코드에서 검색, 아이콘 픽셀 비교):

  ```bat
  .venv\Scripts\python.exe scripts\verify_release.py v0.2.2 "手腕屏没启动起来"
  ```

---

## 10. 알려진 제한

- 손목 오버레이는 **SteamVR이 활성 컴포지터일 때만** 동작합니다. 벤더 순정 OpenXR 런타임에서는 서드파티 PC 측 오버레이가 표시되지 않습니다
- 입력 측은 **게임 믹스 오디오의 스테레오 채널 하나만** 가져옵니다: 화자별 채널이 존재하지 않으므로 여러 사람이 겹쳐 말하면 화자 구분은 원리적으로 부정확합니다
- WebSocket 경로에는 **에코 캔슬링도 노이즈 억제도 없습니다** → **헤드폰 착용이 필수**입니다(스피커로 하면 상대 목소리까지 잡혀 같은 문장이 두 번 인식됩니다)
- 양방향 동시 번역 = WebSocket 세션 두 개이며, 예산도 양쪽에 계상됩니다
- **타이핑 + 음성은 "번역 + TTS" 두 번의 요청**입니다. 텍스트는 거의 즉시 나오지만 음성은 TTS 합성을 기다려야 합니다(실측 엔드투엔드 약 2.6초).
  그래서 말풍선에서 보고 1~2초 뒤에 상대방에게 들립니다. 목소리는 TTS 음색(기본 `Cherry`)이며 음성 경로의 실시간 모델 음색과 **같지 않습니다**.
  조건은 세 번째 경로와 같습니다: "번역 음성 출력"이 켜져 있고 가상 사운드카드가 있어야 하며, 아니면 자동으로 건너뛰고 텍스트만 나갑니다
- **타이핑은 마이크를 대체**합니다: 그래서 방향에 "나"가 포함될 때만 쓸 수 있고, "번역 시작"을 누른 뒤에만 동작합니다
- **chatbox에는 "내가 한 말"의 번역만 실립니다**: 상대 발화의 번역은 chatbox 말풍선으로 가지 않습니다. 손목 오버레이나 GUI 채팅 영역을 보세요

---

[← README로 돌아가기](README.ko.md)
