# C6 데이터·모델 계약

서버의 표준 입력은 C6 펌웨어가 만드는 46바이트 meta와 512바이트 signed int8 CSI다.
CSV는 전송 포맷이 아니라 검증/재생용 표준 표현이며 열은
`seq,(present,rssi,ts_us,data) × RX1..4`의 17개다. `data`는 gain 보상 후의 512개 정수다.

`scripts/replay_c6_sample.py`는 CSV를 실제 wire frame으로 역직렬화한다. 각 행의 범위를
표현하는 양의 gain과 int8 raw를 만들고, 서버가 실제 하드웨어와 동일하게 각 I/Q에
`int(gain * raw)`을 적용하도록 한다. 이 양자화 과정 때문에 CSV와 완전 동일한 정수로
복원되지 않을 수 있지만 형태·부호·스케일 순서·서브캐리어 선택은 동일하다.

보정 게이트:

- 4개 Rx 각각 실효 25 Hz 이상
- 512바이트 C6 리포트만 허용
- gain은 양수이면서 finite
- 최대 15 슬롯까지 진폭 영역 선형 보간, 4~15는 WARNING, 15 초과는 DEGRADED/추론 중단
- 운영 기본 19,800 슬롯(약 10분, 약 594개 창); 이전 VALID 보정은 실패 시 유지

모델은 200슬롯 창에서 N5 노드쌍 로그비 6개와 빈방 dsd로 표준화한 N4z 4개를 만들고,
각 체인에 동일한 24-band/통계 특징기를 적용해 정확히 2168차원 RF 입력을 생성한다.
`tests/test_model_contract.py`는 인수인계 구현과 동일한 특징 바이트 SHA를 고정한다.
