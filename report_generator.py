"""
report_generator.py  - PISTA 競輪版
バックテスト結果・推奨車券のレポート生成
"""

from __future__ import annotations
from datetime import date
from feature_engine import group_by_race
from strategy_engine import StrategyConfig, apply_strategy


def generate_backtest_report(optim_results) -> str:
    lines = [
        "=" * 60,
        "  PISTA 競輪AI  バックテスト・最適化レポート",
        f"  生成日時: {date.today()}",
        "=" * 60,
        "",
    ]

    live_results  = [r for r in optim_results if r.live_ready]
    other_results = [r for r in optim_results if not r.live_ready]

    lines.append(f"## 実運用OK（テスト回収率≥100%） — {len(live_results)}戦略")
    lines.append("")
    if live_results:
        for r in live_results:
            lines.append(f"  ✅ {r.backtest.summary()}")
            if r.test_backtest:
                lines.append(f"     テスト: {r.test_backtest.summary()}")
            lines.append(f"     パラメータ: {r.strategy.params}")
            lines.append("")
    else:
        lines.append("  なし（データ不足または戦略が合っていません）")
        lines.append("")

    lines.append(f"## 未達 — {len(other_results)}戦略")
    lines.append("")
    for r in other_results:
        lines.append(f"  ❌ {r.backtest.summary()}")
        if r.test_backtest:
            lines.append(f"     テスト: {r.test_backtest.summary()}")
    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


BET_AMOUNT = 100  # 1点あたりの賭け金


def _ev_label(odds: float, hit_rate: float | None) -> tuple[str, str]:
    """(ラベル, 説明文) を返す。odds=0 はオッズ未確定。"""
    if hit_rate is None or odds <= 0:
        return "△", "オッズ未確定"
    ev = hit_rate * odds * BET_AMOUNT
    min_odds = BET_AMOUNT / hit_rate if hit_rate > 0 else 9999
    if ev >= BET_AMOUNT:
        return "◎", f"EV={ev:.0f}円 ✅ 賭け推奨（最低必要オッズ{min_odds:.0f}円）"
    else:
        return "△", f"EV={ev:.0f}円 ❌ 見送り推奨（最低必要オッズ{min_odds:.0f}円）"


def generate_picks_report(
    entry_features: list[dict],
    live_strategies: list[StrategyConfig],
    odds_available: bool = False,
) -> str:
    if not entry_features:
        return "出走表データがありません。"

    lines = [
        "=" * 60,
        "  PISTA 競輪AI  推奨車券",
        f"  生成日時: {date.today()}",
        "=" * 60,
        "",
    ]

    if not odds_available:
        lines.append("  ⚠️  オッズ未確定（暫定ピックス）\n")

    hit_rate_map: dict[str, float | None] = {s.name: s.hit_rate for s in live_strategies}

    races = group_by_race(entry_features)
    picks_found = False

    for race_id in sorted(races.keys()):
        horses = races[race_id]
        if not horses:
            continue

        race_signals = []
        for strategy in live_strategies:
            signals = apply_strategy(horses, strategy)
            race_signals.extend(signals)

        if not race_signals:
            continue

        # レースヘッダ
        h = horses[0]
        lines.append(
            f"【{h.get('venue', '')} R{h.get('race_no', '')} "
            f"{h.get('race_name', '')} ({h.get('grade', '')})】"
        )
        lines.append(f"  日付: {h.get('date', '')} | バンク: {h.get('bank_length', '')}m")
        lines.append("")

        # 出走表サマリ
        lines.append("  出走者:")
        for hr in sorted(horses, key=lambda x: x.get("car_no", 0)):
            car   = hr.get("car_no", "?")
            name  = hr.get("racer_name", "")
            cls   = hr.get("class_rank", "")
            style = hr.get("racing_style", "")
            lpos  = hr.get("line_position", -1)
            lsize = hr.get("line_size", 1)
            lno   = hr.get("line_no", 0)
            pos_label = {0: "先頭", 1: "番手", 2: "三番手"}.get(lpos, "?") if lpos >= 0 else "-"
            lines.append(
                f"    {car}車 {name} [{cls}] {style} | "
                f"L{lno}-{pos_label}({lsize}車ライン)"
            )
        lines.append("")

        # 推奨シグナル + EV判定 + 買い方
        all_cars = sorted(h.get("car_no", 0) for h in horses)
        lines.append("  推奨車券:")
        for sig in race_signals:
            hit_rate = hit_rate_map.get(sig.strategy)
            ev_mark, ev_desc = _ev_label(sig.odds, hit_rate)
            odds_str = f"{sig.odds:.0f}円" if sig.odds > 0 else "オッズ未確定"
            lines.append(
                f"    {ev_mark} {sig.bet_type.upper()} {sig.car_no}車 "
                f"{sig.racer_name} [{sig.class_rank}] "
                f"({odds_str} / 人気{sig.popularity}) "
                f"← {sig.strategy}"
            )
            lines.append(f"       {ev_desc}")
            # 具体的な買い方
            axis = sig.car_no
            others = [c for c in all_cars if c != axis]
            others_str = "・".join(str(c) for c in others)
            n_combos = len(others)
            total = n_combos * 100
            bet_name = "2車複" if sig.bet_type.lower() == "nishafuku" else "ワイド"
            lines.append(f"    ┌─【買い方】{bet_name} 軸1頭流し")
            lines.append(f"    │  軸  : {axis}車")
            lines.append(f"    │  相手: {others_str}車（全{n_combos}点）")
            lines.append(f"    └─ 合計: {total}円（1点100円）")
        lines.append("")
        picks_found = True

    if not picks_found:
        lines.append("  本日の推奨車券はありません。")
        lines.append("  （実運用基準クリアの戦略に合う出走者がいません）")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)
