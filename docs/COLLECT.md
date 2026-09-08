# 시나리오 수집 (낙상 데이터)

`COLLECTOR_HANDOFF.md`(모델 개발 세션 인수인계)의 구현. 도구 두 개가 **나란히** 돈다.

| 도구 | 역할 | 출력 |
|---|---|---|
| `tools/csi_session.py` | Tx의 USB에서 CSI 프레임을 받아 저장 | `rx1~4.csv`, `synced.csv`, `meta.json` |
| `tools/collect_session.py` | 60초 격자로 음성·화면 지시, 큐 시각 기록 | `cues.csv`, `session_meta.json` |

둘 다 **같은 PC 시계**(`time.time`)로 `pc_time`을 남긴다. 라벨은 나중에 두 파일의
pc_time을 맞춰 자동 계산하므로, 수집 중에 라벨을 입력할 필요가 없다.

## 배선과 전제

- Tx의 USB를 노트북에 연결한다(현재 **COM4**). Rx 4대는 **전원만** 있으면 된다.
- Rx는 **최종 위치에서** 전원을 넣는다 — 게인 baseline이 부팅 후 첫 100프레임에서 잡힌다.
- 노드 배치: 위에서 볼 때 TX 6시, RX1→RX4 반시계, TX 양옆이 RX1·RX4.
- 클라우드 릴레이(S3)는 켜둔 채로 둬도 된다. 두 경로는 독립이라 서로 방해하지 않는다.

## 실행

수집기를 먼저 켜고 큐 스크립트를 켠다. `--collector-cmd`를 주면 큐 스크립트가 대신 띄운다.

```powershell
# 이벤트 세션 (10분 10초, 낙상 6회 + 휴식 2회)
python tools\collect_session.py --mode event --session-no 1 --subject A --config o1_f01 `
  --collector-cmd "python tools\csi_session.py --port COM4 --duration 630 --prep 0 --session-dir <세션폴더>"

# 일상 세션 (10분 10초, 낙상 없음 — 시간당 오경보 측정용)
python tools\collect_session.py --mode life --session-no 1 --subject A --config o1_f01

# 부재 기준선 (기본 15분)
python tools\collect_session.py --mode empty --minutes 15 --config o1_f01

# 리허설 (수집기 없이 타이머·음성·화면만)
python tools\collect_session.py --dry-run --mode event --session-no 3
```

`--mute`(무음), `--windowed`(창 모드)는 리허설·개발용이다.

## 세션 구성

`--session-no`가 순서를 정한다(무작위 없음). 내용은 모든 세션이 같고 순서만 회전한다.

- 0:00 부재 2분 → 2:00 입장 → 2:10부터 60초 사이클 8개 → 10:10 종료
- 낙상 사이클(6개): 행동 20초 → **낙상** → 누워 있기 30초 → 일어서기 10초
- 휴식 사이클(2개, c4·c8): 50초 가만히 → 일어서기 10초
- "조금씩 움직이세요"(낙상 3초 뒤)는 **항상 c1**

## 키

| 키 | 동작 |
|---|---|
| `X` | 현재 사이클 무효 — `cues.csv`의 `valid=0`, 메타의 `invalid_cycles`에 기록 |
| `N` | 메모 한 줄 |
| `P` | 일시정지 / 재개 (재개 시 현재 사이클을 처음부터 다시) |
| `Q` | 중단 후 저장 |

넘어지지 않았거나 매트를 벗어난 사이클은 **즉시 `X`**.

`Q`는 한 번 더 묻는다. 한글 입력 상태에서 `ㅂ`이 `q`, `ㅔ`가 `p`라 오타로 눌리기 쉬운데,
10분짜리 세션이 그렇게 날아가면 다시 찍어야 한다. 리허설을 `--windowed`로 돌릴 때
창이 포커스를 가져가지 않도록 뒤로 내려두는 것도 같은 이유다.

## 음성

문구는 시작할 때 한 번 WAV로 합성해 `tools/assets/voice/`에 캐시하고, 재생은 `winsound`로 한다.
1순위는 Windows 내장 한국어 음성(Heami), 없으면 `edge-tts`, 둘 다 안 되면 삐 소리와 화면 문구만
쓰고 시작 화면에 경고를 띄운다.

> 합성 목록을 PowerShell에 **stdin으로 넘기면 조용히 0개를 만들고 성공으로 끝난다**(`$input`이 빈다).
> 임시 JSON 파일로 넘기고, 만들어진 파일 수까지 확인해야 한다.

## 산출물

```
<out>/<config_id>/<session_id>/
    rx1.csv ~ rx4.csv, synced.csv, meta.json    ← 수집기
    cues.csv, session_meta.json                 ← 큐 스크립트
    photo.jpg                                   ← 배치 사진 (사람이 넣음)
```

`cues.csv`: `t_pc_unix, t_iso, t_session_s(계획), t_actual_s(실제), cycle, label, phase, text, valid`

## 세션 종료 시 자동 점검

`session_meta.json`의 `checks`에 들어가고 콘솔에도 뜬다.

- 노드별 수신률 — **30Hz 미만이면 경고**(계약 33Hz, 가드 25Hz)
- 4노드 완전행 비율 — 95% 미만이면 경고
- seq 단조성, 큐 t0 앞뒤 10초 프레임 여유
- 계획 대비 실제 큐 시각 오차 — **50ms 이상이면 경고**

완전행이 95%에 못 미치면 대개 **배치 문제**다. 보드가 서로 가까우면 RSSI가 −10dBm 근처로
포화되어 수신이 불안정해진다. −30~−50dBm이 정상 범위다.
