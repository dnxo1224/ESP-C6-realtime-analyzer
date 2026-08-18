"""P6: dual-unseen(집×인물 이중 미지) 12조합 최종 평가.

- 특징: 체인 결합 지원 ("N5|N4z" = 두 체인 특징 벡터 concat)
- 모델: RF (P2/P3 스크리닝 우승)
- 임계값 3규칙 비교:
    fixed05      p>=0.5
    train_f1     학습 데이터 내부 CV 없이 학습셋 F1 최적 임계값 (배포 가능)
    empty_q      테스트 집 부재 캘리브 윈도우 점수의 분위수 규칙 (라벨 불필요, 배포 가능)
- 운영 지표: 낙상 세션 이벤트 커버리지(pseudo-GT 버스트), 음성 스트림 FA/h
- 산출: results/06_final_eval/ (combo별 CSV, 집계표, recall-FA/h 곡선, 점수 분포 figure)
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import PLACES, SUBJECTS, HZ, RESULTS, all_sessions
from dataset import (load_amp, empty_calib_stats, add_diff_sigma, normalize,
                     iter_windows, is_fall)
from features import featurize
from eval_events import (alarm_episodes, fall_burst_times, event_recall,
                         empty_quantile_threshold)

OUT = RESULTS / "06_final_eval"


def build_all(chains, W, stride, k_cal, nodes=None):
    """체인 결합 특징. 반환 X, y, pl, sj, ac, starts. empty 캘리브 구간은 act='empty_cal'로 포함."""
    calibs = {}
    for place in PLACES:
        c = empty_calib_stats(place, k_min=k_cal)
        c = add_diff_sigma(c, place)
        if nodes is not None:
            c = {k: (v[nodes] if isinstance(v, np.ndarray) else v) for k, v in c.items()}
        calibs[place] = c
    metas_done = False
    X_parts = []
    for chain in chains:
        parts = chain.split("+")
        session_level = [p for p in parts if p != "N6"]
        win_norm = "N6" in parts
        Fs, ys, pls, sjs, acs, sts = [], [], [], [], [], []
        for place, subj, act, _ in all_sessions():
            amp, _ = load_amp(place, subj, act)
            if nodes is not None:
                amp = amp[:, nodes]
            x = amp
            for p in session_level:
                x = normalize(x, p, calibs.get(place))
            win, starts = iter_windows(x, W, stride, per_window_norm=win_norm)
            F = featurize(win)
            Fs.append(F)
            if not metas_done:
                a = np.array([act] * len(F), dtype=object)
                if act == "empty":
                    a[starts < calibs[place]["i_end"]] = "empty_cal"
                ys.append(np.full(len(F), int(is_fall(act))))
                pls += [place] * len(F); sjs += [subj] * len(F)
                acs.append(a); sts.append(starts)
        X_parts.append(np.concatenate(Fs))
        if not metas_done:
            y = np.concatenate(ys); pl = np.array(pls); sj = np.array(sjs)
            ac = np.concatenate(acs); st = np.concatenate(sts)
            metas_done = True
    return np.hstack(X_parts), y, pl, sj, ac, st


def sweep_metrics(y, p):
    from sklearn.metrics import roc_auc_score, average_precision_score, precision_recall_curve
    auc = roc_auc_score(y, p)
    ap = average_precision_score(y, p)
    pr, rc, th = precision_recall_curve(y, p)
    f1s = 2 * pr * rc / (pr + rc + 1e-12)
    return dict(auc=auc, ap=ap, f1_best=float(np.nanmax(f1s)))


def operational(place, subj_test, ac, sj, pl, st, p, thr, amps_cache):
    """테스트 집에서 임계값 thr의 운영 지표."""
    res = {}
    # 낙상 세션 이벤트 커버리지
    ers, n_alarm = [], 0
    for act in ["liedown", "sitdown"]:
        m = (pl == place) & (sj == subj_test) & (ac == act)
        eps = alarm_episodes(st[m], p[m], thr)
        bt = amps_cache[(place, subj_test, act)]
        ers.append(event_recall(bt, eps))
        n_alarm += len(eps)
    res["event_recall"] = float(np.nanmean(ers))
    # 음성 스트림 FA/h
    fa_time, fa_n = 0.0, 0
    for act, sjv in [("walk", subj_test), ("handsup", subj_test), ("empty", "none")]:
        m = (pl == place) & (sj == sjv) & (ac == act)
        if not m.any():
            continue
        dur = (st[m].max() - st[m].min()) / HZ + 200 / HZ
        eps = alarm_episodes(st[m], p[m], thr)
        fa_n += len(eps); fa_time += dur
    res["fa_per_h"] = fa_n / (fa_time / 3600.0) if fa_time else np.nan
    return res


def main():
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--chains", default="N5|N4z")
    ap_.add_argument("--W", type=int, default=200)
    ap_.add_argument("--stride", type=int, default=33)
    ap_.add_argument("--kcal", type=float, default=10.0)
    ap_.add_argument("--eq", type=float, default=0.999)
    ap_.add_argument("--nodes", default="")
    ap_.add_argument("--out", default="06_final_eval")
    args = ap_.parse_args()
    chains = args.chains.split("|")
    nodes = [int(v) - 1 for v in args.nodes.split(",")] if args.nodes else None
    global OUT
    OUT = RESULTS / args.out
    OUT.mkdir(parents=True, exist_ok=True)

    import pandas as pd
    from sklearn.ensemble import RandomForestClassifier

    t0 = time.time()
    X, y, pl, sj, ac, st = build_all(chains, args.W, args.stride, args.kcal, nodes)
    print(f"features built: X={X.shape} ({time.time()-t0:.0f}s)", flush=True)

    # pseudo-GT 버스트 시각 캐시 (낙상 세션만)
    amps_cache = {}
    for place, subj, act, _ in all_sessions():
        if act in ("liedown", "sitdown"):
            amp, _ = load_amp(place, subj, act)
            amps_cache[(place, subj, act)] = fall_burst_times(amp)

    rows = []
    curves = []  # recall-FA/h 곡선용 (combo, thr, event_recall, fa_per_h)
    for test_place in PLACES:
        for test_subj in SUBJECTS:
            tr = (pl != test_place) & (sj != test_subj) & (ac != "empty_cal")
            te_act = (pl == test_place) & ((sj == test_subj) | (ac == "empty")) \
                     & (ac != "empty_cal")
            te_cal = (pl == test_place) & (ac == "empty_cal")
            clf = RandomForestClassifier(n_estimators=400, n_jobs=24,
                                         min_samples_leaf=3, random_state=0)
            clf.fit(X[tr], y[tr])
            p_te = clf.predict_proba(X[te_act])[:, 1]
            p_cal = clf.predict_proba(X[te_cal])[:, 1]
            p_tr = clf.predict_proba(X[tr])[:, 1]
            met = sweep_metrics(y[te_act], p_te)
            np.savez(OUT / f"pred_{test_place}_{test_subj}.npz",
                     p=p_te, y=y[te_act], ac=ac[te_act].astype(str),
                     sj=sj[te_act].astype(str), st=st[te_act], p_cal=p_cal)
            # 임계값 규칙들
            from sklearn.metrics import precision_recall_curve, f1_score
            pr, rc, th = precision_recall_curve(y[tr], p_tr)
            f1s = 2 * pr * rc / (pr + rc + 1e-12)
            thr_train = float(th[np.nanargmax(f1s[:-1])])
            thr_eq = empty_quantile_threshold(p_cal, q=args.eq)
            pfull = np.zeros(len(y)); pfull[te_act] = p_te  # 인덱싱 편의
            for rule, thr in [("fixed05", 0.5), ("train_f1", thr_train),
                              ("empty_q", thr_eq)]:
                yhat = (p_te >= thr).astype(int)
                f1 = f1_score(y[te_act], yhat)
                op = operational(test_place, test_subj, ac[te_act], sj[te_act],
                                 pl[te_act], st[te_act], p_te, thr, amps_cache)
                rows.append(dict(place=test_place, subj=test_subj, rule=rule,
                                 thr=round(thr, 4), f1=round(f1, 4), **met, **op))
            # 곡선: 임계값 스윕
            for thr in np.unique(np.round(np.quantile(p_te, np.linspace(0.5, 0.999, 40)), 4)):
                op = operational(test_place, test_subj, ac[te_act], sj[te_act],
                                 pl[te_act], st[te_act], p_te, thr, amps_cache)
                curves.append(dict(place=test_place, subj=test_subj, thr=thr, **op))
            print(f"[{test_place}/{test_subj}] auc={met['auc']:.4f} "
                  f"thr_eq={thr_eq:.3f} ({time.time()-t0:.0f}s)", flush=True)
            pd.DataFrame(rows).to_csv(OUT / "results_partial.csv", index=False)

    df = pd.DataFrame(rows)
    df.to_csv(OUT / "results.csv", index=False)
    cv = pd.DataFrame(curves)
    cv.to_csv(OUT / "curves.csv", index=False)

    # 집계
    agg = df.groupby("rule").agg(
        auc=("auc", "mean"), ap=("ap", "mean"), f1_best=("f1_best", "mean"),
        f1=("f1", "mean"), f1_min=("f1", "min"),
        event_recall=("event_recall", "mean"), fa_per_h=("fa_per_h", "mean"),
    ).round(4)
    by_place = df[df.rule == "empty_q"].groupby("place").agg(
        auc=("auc", "mean"), f1=("f1", "mean"),
        event_recall=("event_recall", "mean"), fa_per_h=("fa_per_h", "mean")).round(4)
    with open(OUT / "notes.md", "w", encoding="utf-8") as f:
        f.write(f"# P6 dual-unseen 12조합 최종 평가\n\n체인: {args.chains}, W={args.W}, "
                f"K_cal={args.kcal}분, empty_q q={args.eq}\n\n## 임계값 규칙별 집계 (12조합 평균)\n\n")
        f.write(agg.to_markdown() + "\n\n## 집별 (empty_q 규칙)\n\n")
        f.write(by_place.to_markdown() + "\n\n## 조합별 상세: results.csv\n")

    # recall-FA/h 곡선 figure
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.family"] = "Malgun Gothic"
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(8, 6))
    for place in PLACES:
        sub = cv[cv.place == place].groupby("thr")[["event_recall", "fa_per_h"]].mean()
        ax.plot(sub["fa_per_h"], sub["event_recall"], "o-", ms=3, label=place)
    ax.set_xlabel("오경보 (회/시간)"); ax.set_ylabel("낙상 이벤트 커버리지")
    ax.set_xscale("symlog", linthresh=1)
    ax.set_title(f"운영 트레이드오프 곡선 (dual-unseen, {args.chains})")
    ax.grid(alpha=0.3); ax.legend()
    fig.tight_layout(); fig.savefig(OUT / "recall_vs_fah.png", dpi=130)
    print("P6 DONE", flush=True)
    print(agg.to_string(), flush=True)


if __name__ == "__main__":
    main()
