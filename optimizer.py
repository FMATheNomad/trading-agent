import json
import time
import numpy as np
from openai import OpenAI
import config

SYSTEM_PROMPT = """Kamu adalah AI Performance Analyst untuk FMA Alpha Quant Labs, systematic trading bot untuk Indodax.

Arsitektur bot:
- HMM Regime Gate (4 state: BULL/BEAR/SIDEWAYS/HIGH_VOL)
- Per-regime strategy: momentum (BULL/BEAR), mean-reversion (SIDEWAYS), survival (HIGH_VOL)
- State machine dengan limit TP/SL order di exchange
- Circuit breaker 3 lapis (daily loss, consecutive loss, equity floor)
- Kelly-based position sizing per regime
- Rothschild mode: 6 slot, pyramid ON, trailing SL (aktif setelah 5 BULL streak)

Parameter yang bisa kamu optimasi (dengan batas aman):
1. kelly_fraction: float 0.05-0.35 (default 0.10 di ALPHA)
2. atr_sl_multiplier: float 1.0-2.5 (default 1.2 di ALPHA)
3. atr_tp_multiplier: float 1.0-3.0 (default 2.0)
4. max_daily_trades: int 5-50 (default 999999)
5. session_weight_asia: float 0.5-1.0 (default 0.7)
6. session_weight_us: float 1.0-2.0 (default 1.3)

Aturan:
- Jangan rekomendasikan perubahan yang mendekati batas aman tanpa alasan kuat
- Prioritaskan konsistensi (Sharpe ratio, win rate) dibanding profit mentah
- Jika data trade < 20, jangan rekomendasikan perubahan apapun
- Berikan alasan spesifik berdasarkan data, bukan spekulasi
- RESPON HANYA JSON, tanpa teks lain

Format response:
{
    "observation": "1-2 kalimat observasi market & performa terkini",
    "recommendations": [
        {
            "param": "kelly_fraction",
            "value": 0.15,
            "reason": "Alasan spesifik berdasarkan data"
        }
    ],
    "auto_apply": true
}
"""

class AIOptimizer:
    def __init__(self):
        self.last_cycle = 0
        self.last_recommendations = []

    def _build_metrics(self, trades: list[dict], equity_curve: list[float]) -> dict:
        sells = [t for t in trades if t.get("side") == "sell" and t.get("pnl") is not None]
        if len(sells) < 5:
            return {"trade_count": len(sells), "skip": True}

        pnls = [t["pnl"] for t in sells]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        win_rate = len(wins) / len(pnls) * 100 if pnls else 0
        avg_win = np.mean(wins) if wins else 0
        avg_loss = abs(np.mean(losses)) if losses else 1
        profit_factor = (sum(wins) / abs(sum(losses))) if sum(losses) != 0 else float("inf")

        returns = []
        for i in range(1, len(equity_curve)):
            prev = equity_curve[i-1]
            curr = equity_curve[i]
            if prev > 0:
                returns.append((curr - prev) / prev)
        sharpe = 0
        if len(returns) > 1 and np.std(returns) > 0:
            sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(365))

        peak = max(equity_curve) if equity_curve else 0
        trough = min(equity_curve) if equity_curve else 0
        dd = (peak - trough) / peak * 100 if peak > 0 else 0

        return {
            "trade_count": len(sells),
            "win_rate": round(win_rate, 1),
            "avg_win_idr": round(float(avg_win)),
            "avg_loss_idr": round(float(avg_loss)),
            "profit_factor": round(profit_factor, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown_pct": round(dd, 1),
            "total_pnl_idr": round(sum(pnls)),
            "skip": False,
        }

    def _build_context(self, metrics: dict) -> str:
        return json.dumps({
            "performance_metrics": metrics,
            "current_config": {
                "kelly_fraction": config.KELLY_FRACTION,
                "atr_sl_multiplier": config.ATR_SL_MULTIPLIER,
                "atr_tp_multiplier": config.ATR_TP_MULTIPLIER,
                "max_daily_trades": config.MAX_DAILY_TRADES,
                "daily_loss_floor_idr": config.DAILY_LOSS_FLOOR_IDR,
                "position_size_pct": config.POSITION_SIZE_PCT,
            },
            "mode": "ALPHA" if config.ALPHA_MODE else ("INSANE" if config.INSANE_MODE else "STANDARD"),
            "safe_ranges": {
                "kelly_fraction": [config.AI_OPTIMIZER_KELLY_MIN, config.AI_OPTIMIZER_KELLY_MAX],
                "atr_sl_multiplier": [config.AI_OPTIMIZER_ATR_SL_MIN, config.AI_OPTIMIZER_ATR_SL_MAX],
                "atr_tp_multiplier": [config.AI_OPTIMIZER_ATR_TP_MIN, config.AI_OPTIMIZER_ATR_TP_MAX],
                "max_daily_trades": [config.AI_OPTIMIZER_MAX_DAILY_TRADES_MIN, config.AI_OPTIMIZER_MAX_DAILY_TRADES_MAX],
            },
        }, indent=2)

    def _apply_recommendation(self, rec: dict) -> str | None:
        param = rec.get("param")
        value = rec.get("value")
        if param is None or value is None:
            return None

        if param == "kelly_fraction":
            old = config.KELLY_FRACTION
            clamped = max(config.AI_OPTIMIZER_KELLY_MIN, min(config.AI_OPTIMIZER_KELLY_MAX, float(value)))
            config.KELLY_FRACTION = clamped
            return f"kelly_fraction: {old:.2f} → {clamped:.2f}"

        elif param == "atr_sl_multiplier":
            old = config.ATR_SL_MULTIPLIER
            clamped = max(config.AI_OPTIMIZER_ATR_SL_MIN, min(config.AI_OPTIMIZER_ATR_SL_MAX, float(value)))
            config.ATR_SL_MULTIPLIER = clamped
            return f"atr_sl_multiplier: {old:.1f} → {clamped:.1f}"

        elif param == "atr_tp_multiplier":
            old = config.ATR_TP_MULTIPLIER
            clamped = max(config.AI_OPTIMIZER_ATR_TP_MIN, min(config.AI_OPTIMIZER_ATR_TP_MAX, float(value)))
            config.ATR_TP_MULTIPLIER = clamped
            return f"atr_tp_multiplier: {old:.1f} → {clamped:.1f}"

        elif param == "max_daily_trades":
            old = config.MAX_DAILY_TRADES
            clamped = max(config.AI_OPTIMIZER_MAX_DAILY_TRADES_MIN, min(config.AI_OPTIMIZER_MAX_DAILY_TRADES_MAX, int(value)))
            config.MAX_DAILY_TRADES = clamped
            return f"max_daily_trades: {old} → {clamped}"

        return None

    async def run(self, trades: list[dict], equity_curve: list[float], regime_history: list[str]) -> str | None:
        if not config.DEEPSEEK_API_KEY:
            return None

        metrics = self._build_metrics(trades, equity_curve)
        if metrics.get("skip"):
            return None

        context = self._build_context(metrics)
        context += f"\n\nregime_history (last 25): {regime_history[-25:]}"

        try:
            client = OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL)
            resp = client.chat.completions.create(
                model=config.DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Data performa dan konfigurasi saat ini:\n\n{context}"},
                ],
                temperature=0.3,
                max_tokens=1000,
            )

            raw = resp.choices[0].message.content
            if not raw:
                return None

            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            elif cleaned.startswith("```json"):
                cleaned = cleaned[7:].rsplit("```", 1)[0].strip()

            result = json.loads(cleaned)
            self.last_recommendations = result.get("recommendations", [])
            observation = result.get("observation", "")

            lines = [f"🤖 AI OPTIMIZER ANALYSIS"]
            if observation:
                lines.append(f"  Observasi: {observation}")
            lines.append(f"  Metrics: {metrics['trade_count']} trade | WR {metrics['win_rate']}% | PF {metrics['profit_factor']} | Sharpe {metrics['sharpe']}")

            changes = []
            for rec in self.last_recommendations:
                if config.AI_OPTIMIZER_AUTO_APPLY:
                    change = self._apply_recommendation(rec)
                    if change:
                        changes.append(change)

            if changes:
                lines.append(f"  Auto-apply: {'ON' if config.AI_OPTIMIZER_AUTO_APPLY else 'OFF'}")
                for c in changes:
                    lines.append(f"    • {c}")
            else:
                lines.append(f"  Auto-apply: ON — tidak ada perubahan")

            return "\n".join(lines)

        except json.JSONDecodeError:
            return f"🤖 AI OPTIMIZER: failed to parse AI response"
        except Exception as e:
            return f"🤖 AI OPTIMIZER error: {str(e)[:100]}"
