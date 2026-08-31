# 클라우드 배포 (네이버 클라우드 기준)

2026-08-18에 실제로 수행한 절차. 다른 서버에 다시 올릴 때도 이 순서를 따른다.

## 현재 배포 정보

| 항목 | 값 |
|---|---|
| 공인 IP | `<서버IP>` |
| OS / 사양 | Ubuntu 24.04.1 LTS, x86_64, 2 vCPU, 8GB RAM, 디스크 10GB |
| 배포 경로 | `/opt/csi` |
| SSH | `ssh -i ~/.ssh/<배포키> root@<서버IP>` |
| 관리 페이지 | <http://<서버IP>:8180> |
| 비밀값 | 서버의 `/opt/csi/.env` (권한 600, 커밋 금지) |

## 1. 서버 생성 시 주의점

- **Subnet은 반드시 Public** — 공인 IP를 붙이려면 필수다
- **메모리 4GB 이상** — 워커 피크 457MB + MySQL 1GB + admin JVM 0.5GB
- 디스크 10GB는 동작하지만 빠듯하다. 빌드 직후 88%까지 찬다
- NCP의 **pem 파일은 SSH 키가 아니다.** 콘솔의 *관리자 비밀번호 확인*에서
  이 pem으로 root 비밀번호를 복호화해 받는 용도다. pem으로 직접 SSH 로그인은 거부된다
- 접속 후 SSH 공개키를 `~/.ssh/authorized_keys`에 등록해두면 이후 자동화가 편하다

## 2. ACG(방화벽) 인바운드

| 프로토콜 | 소스 | 포트 | 용도 |
|---|---|---|---|
| TCP | 0.0.0.0/0 | 22 | SSH |
| TCP | 0.0.0.0/0 | 8180 | 관리 페이지 |
| TCP | 0.0.0.0/0 | 9600 | Relay 데이터 수신 |

**3306(MySQL)은 절대 열지 않는다.** compose가 DB를 호스트 루프백에만 노출한다.

## 3. Docker 설치

`scripts/install-docker.sh`와 동일한 내용 — 공식 저장소(서명 검증), 시간대 KST.

## 4. 코드 업로드

펌웨어·문서·git 이력은 제외하고 8MB 정도만 올린다. 서버에서 빌드하므로
이미지를 전송할 필요가 없다.

```bash
tar czf - --exclude='./.git' --exclude='./firmware' --exclude='./docs' \
  --exclude='./server/admin/target' --exclude='./server/inference/reference' \
  --exclude='*/__pycache__' --exclude='./.env' . |
  ssh -i ~/.ssh/<배포키> root@<IP> "mkdir -p /opt/csi && tar xzf - -C /opt/csi"
```

## 5. 비밀값 생성

서버에서 직접 만든다. 로컬 `.env`를 올리지 않는다.

```bash
cd /opt/csi
printf 'DB_PASSWORD=%s\nDB_ROOT_PASSWORD=%s\nCSI_TOKEN=%s\nADMIN_PASSWORD=%s\nCALIBRATION_SLOTS=19800\nTZ=Asia/Seoul\n' \
  "$(openssl rand -hex 16)" "$(openssl rand -hex 16)" \
  "$(openssl rand -hex 20)" "$(openssl rand -base64 12 | tr -d '/+=' | head -c 14)" > .env
chmod 600 .env
```

DB 비밀번호는 **첫 기동 때만** 적용된다. 나중에 바꾸려면 볼륨을 지워야 한다.

## 6. 기동과 정리

```bash
docker compose up -d --build --wait
docker builder prune -af && docker image prune -af   # 빌드 캐시 1.7GB 회수 — 생략 금지
df -h /
```

## 7. Relay 펌웨어 전환

`idf.py menuconfig` → *C6 CSI relay* 에서 두 값만 바꾼다. Wi-Fi는 설치 현장 AP 그대로.

- `SERVER_HOST` → 서버 공인 IP (호스트명도 가능 — `getaddrinfo`를 쓰므로
  DNS를 쓰면 서버 교체 시 재플래시가 필요 없다)
- `TOKEN` → 서버 `.env`의 `CSI_TOKEN`과 **정확히 동일**하게

빌드는 메모리를 많이 쓰므로 컨테이너를 내리고 `ninja -C build -j 1`로 한다.

## 8. 검증

```bash
curl -o /dev/null -w '%{http_code}\n' http://<IP>:8180/healthz    # 200
curl -o /dev/null -w '%{http_code}\n' http://<IP>:8180/api/stats  # 401 (미인증 차단)
python scripts/replay_c6_sample.py --host <IP> --port 9600 --slots 20   # 토큰 없음 → 0행
python scripts/replay_c6_sample.py --host <IP> --port 9600 --token <T> --slots 240
```

실기기 연결 후에는 Rx별 적재율이 **각 33Hz, 합계 132행/s**면 정상이다.

```sql
SELECT rx_id, COUNT(*)/20 AS hz FROM reports
WHERE recv_ts > UNIX_TIMESTAMP()-20 GROUP BY rx_id;
```

## 알려진 한계

- **평문 TCP** — CSI 데이터와 토큰이 암호화 없이 인터넷을 지난다.
  민감하다면 ACG에서 9600의 소스를 설치 현장 공인 IP로 좁힌다
- **관리 페이지도 HTTP** — 비밀번호가 평문으로 오간다. 외부 상시 노출이라면 TLS를 앞에 둘 것
- **Relay는 서버가 끊기면 데이터를 버린다**(큐가 차면 폐기). 저장 후 재전송 기능이 없으므로
  서버 다운타임 = 그 구간 데이터 소실

## 디스크 확장 (추가 블록 스토리지)

콘솔에서 스토리지를 만들어 붙이면 `/dev/vdb`로 보이지만 **포맷·마운트는 직접 해야 한다.**
붙이기만 해서는 루트 디스크 여유가 늘지 않는다.

```bash
mkfs.ext4 -F -L csi-data /dev/vdb
mkdir -p /mnt/data
echo "UUID=$(blkid -s UUID -o value /dev/vdb) /mnt/data ext4 defaults,nofail 0 2" >> /etc/fstab
mount -a
```

그다음 Docker 데이터를 옮긴다. **두 곳을 모두 옮겨야 한다** — Docker 29는 이미지를
containerd 저장소에 두기 때문에, Docker의 `data-root`만 바꾸면 `/var/lib/containerd`가
루트에 그대로 남는다(실제로 2.2GB가 남아 있었다).

```bash
cd /opt/csi && docker compose down
systemctl stop docker docker.socket containerd

mv /var/lib/docker /mnt/data/docker
printf '{
  "data-root": "/mnt/data/docker"
}
' > /etc/docker/daemon.json

mv /var/lib/containerd /mnt/data/containerd
sed -i '1i root = "/mnt/data/containerd"' /etc/containerd/config.toml

systemctl start containerd docker
cd /opt/csi && docker compose up -d --wait
```

실측(50GB 추가): 루트 78% → **52%**, 이미지·볼륨·로그가 모두 새 디스크로 이동.
