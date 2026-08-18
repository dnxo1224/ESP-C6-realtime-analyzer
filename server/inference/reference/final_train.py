"""P7: 최종 모델 학습 — 3집 전체 데이터로 RF 학습 후 배포 번들 저장.

번들(models/final_bundle.joblib) 내용:
  model      : RandomForestClassifier
  config     : chains, nodes, W, stride, k_cal, eq, 특징 파라미터
  참고: 새 집 배포 시 캘리브 통계는 그 집의 empty 데이터로 realtime_inference.py가 생성.
"""
import argparse
import sys
import time
from pathlib import Path

import joblib
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import MODELS, PLACES, HZ
from exp_final_eval import build_all


def main():
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--chains", default="N5|N4z")
    ap_.add_argument("--nodes", default="")
    ap_.add_argument("--W", type=int, default=200)
    ap_.add_argument("--stride", type=int, default=33)
    ap_.add_argument("--kcal", type=float, default=10.0)
    ap_.add_argument("--eq", type=float, default=0.999)
    ap_.add_argument("--ntree", type=int, default=600)
    ap_.add_argument("--exclude-place", default="", help="데모용: 이 집을 학습에서 제외")
    ap_.add_argument("--out", default="final_bundle.joblib")
    args = ap_.parse_args()
    chains = args.chains.split("|")
    nodes = [int(v) - 1 for v in args.nodes.split(",")] if args.nodes else None

    t0 = time.time()
    X, y, pl, sj, ac, st = build_all(chains, args.W, args.stride, args.kcal, nodes)
    tr = ac != "empty_cal"
    if args.exclude_place:
        tr = tr & (pl != args.exclude_place)
    print(f"train windows={tr.sum()} pos={int(y[tr].sum())} ({time.time()-t0:.0f}s)", flush=True)

    from sklearn.ensemble import RandomForestClassifier
    clf = RandomForestClassifier(n_estimators=args.ntree, n_jobs=32,
                                 min_samples_leaf=3, random_state=0)
    clf.fit(X[tr], y[tr])
    # 학습셋 자기 점검 (참고용 — 성능 주장 아님)
    from sklearn.metrics import roc_auc_score
    print(f"train-fit AUC (참고): {roc_auc_score(y[tr], clf.predict_proba(X[tr])[:,1]):.4f}",
          flush=True)

    MODELS.mkdir(exist_ok=True)
    bundle = dict(
        model=clf,
        config=dict(chains=chains, nodes=nodes, W=args.W, stride=args.stride,
                    k_cal_min=args.kcal, empty_quantile=args.eq, hz=HZ),
        trained_on=[p for p in PLACES if p != args.exclude_place],
        note="dual-unseen 프로토콜로 검증된 구성. 새 집 배포 시 empty 캘리브 필수.",
    )
    joblib.dump(bundle, MODELS / args.out)
    print(f"saved: {MODELS / args.out}", flush=True)


if __name__ == "__main__":
    main()
